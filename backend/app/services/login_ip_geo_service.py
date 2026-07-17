"""IP -> country lookup for the last-close-IP CRM push.

Answers one question: what ISO country code goes in the parentheses of the
``"1.2.3.4 (CN)"`` value written to the CRM's ``custom_last_close_position_ip``.

Backend is MaxMind's GeoIP2 Precision **Country** web service — the same paid
account Manager_Log_Monitor uses. Only the IP ever leaves this process; no
customer PII, and never a free/public IP API.

Two things shape the whole design:

1. **Credit is finite.** ~1,190 distinct IPs close positions each day but only
   ~600 are new, so every answer is cached (``ip_geo_cache``). Without the cache
   the balance lasts ~7 months instead of ~14. The cache is a cost control, not
   a latency optimization.

2. **The answer feeds a diff, so instability is expensive.** The pushed value is
   compared string-wise against what CRM holds; an IP that reads "CN" today and
   "Unknown" tomorrow re-pushes forever. Hence the error taxonomy below: a
   *definitive* no-answer is a cacheable fact, a *transient* failure is not an
   answer at all.

Error taxonomy (mirrors Manager_Log_Monitor/app/geo.py, deliberately re-stated
rather than imported — that module has an offline mmdb tier to fall back to and
this one does not, so the same exception must produce a different outcome):

  AddressNotFoundError
      Definitive: MaxMind knows this IP isn't geolocatable (reserved/private).
      -> "Unknown", cached, pushed. It will still be "Unknown" tomorrow.

  AuthenticationError / PermissionRequiredError / OutOfQueriesError
      Persistent account-level problems (bad creds, no Precision entitlement,
      out of credit). Every IP will hit these until someone fixes the account,
      so failing per-IP would just mean ~1,190 identical errors.
      -> raise GeoUnusableError; the caller aborts the run and emails.

  network / timeout / HTTP / parse
      Transient and per-IP. -> None, NOT cached, client skipped this run and
      retried tomorrow. Never degrade to "Unknown" here: that would write a
      wrong value AND guarantee a re-push once the network recovers.
"""

from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from app.core import login_ip_db
from app.core.config import get_settings

logger = logging.getLogger(__name__)

try:
    import geoip2.errors
    import geoip2.webservice

    _HAVE_GEOIP2 = True
except ImportError:  # pragma: no cover - exercised only on a host without the dep
    _HAVE_GEOIP2 = False


# The country string used when MaxMind gives a definitive "not geolocatable".
# Cached and pushed like any other answer, because it is one.
UNKNOWN_COUNTRY = "Unknown"


class GeoUnusableError(RuntimeError):
    """Geo lookup cannot work at all right now (creds / entitlement / credit).

    Signals the caller to abort the whole push rather than mark ~1,190 clients
    individually failed.
    """


@dataclass(frozen=True)
class GeoResolution:
    """Outcome of one batch resolve.

    countries maps every requested IP to an ISO code, to UNKNOWN_COUNTRY, or to
    None meaning "no answer this run — skip this client, retry tomorrow".
    cache_hits / api_calls are reported in the digest: they are the canary for a
    cache regression, which would otherwise only surface when credit runs out.
    """

    countries: dict[str, str | None]
    cache_hits: int
    api_calls: int

    @property
    def failed_ips(self) -> list[str]:
        return [ip for ip, c in self.countries.items() if c is None]


# One client per worker thread. geoip2's Client wraps a requests.Session, whose
# thread-safety is a documented gray area; a Session per thread costs almost
# nothing and takes the question off the table.
_thread_local = threading.local()


def _get_client():
    client = getattr(_thread_local, "client", None)
    if client is None:
        s = get_settings()
        client = geoip2.webservice.Client(
            int(s.MAXMIND_ACCOUNT_ID),
            s.MAXMIND_LICENSE_KEY,
            host=s.MAXMIND_HOST,
            timeout=s.MAXMIND_TIMEOUT,
        )
        _thread_local.client = client
    return client


def _check_usable() -> None:
    """Fail fast, before spawning threads, when geo can't possibly work."""
    if not _HAVE_GEOIP2:
        raise GeoUnusableError(
            "geoip2 is not installed — add it to requirements.txt and rebuild the image"
        )
    s = get_settings()
    if not (s.MAXMIND_ACCOUNT_ID and s.MAXMIND_LICENSE_KEY):
        raise GeoUnusableError(
            "MAXMIND_ACCOUNT_ID / MAXMIND_LICENSE_KEY are not configured"
        )
    try:
        int(s.MAXMIND_ACCOUNT_ID)
    except ValueError:
        raise GeoUnusableError(
            f"MAXMIND_ACCOUNT_ID is not numeric: {s.MAXMIND_ACCOUNT_ID!r}"
        ) from None


def _lookup_one(ip: str) -> tuple[str, str | None, Exception | None]:
    """Resolve one IP. Returns (ip, country_or_None, fatal_exc_or_None).

    A fatal exception is returned rather than raised so the pool can drain and
    the caller can raise once, instead of surfacing whichever thread lost a race.
    """
    try:
        resp = _get_client().country(ip)
    except geoip2.errors.AddressNotFoundError:
        return ip, UNKNOWN_COUNTRY, None
    except (
        geoip2.errors.AuthenticationError,
        geoip2.errors.PermissionRequiredError,
        geoip2.errors.OutOfQueriesError,
    ) as exc:
        return ip, None, exc
    except Exception as exc:  # network / timeout / HTTP / parse
        logger.warning("MaxMind lookup failed for %s (%s) — skipping this run", ip, exc)
        return ip, None, None

    # A 200 with no country is the same fact as AddressNotFoundError, and
    # MaxMind does return it for some anonymized ranges.
    return ip, resp.country.iso_code or UNKNOWN_COUNTRY, None


def resolve_countries(ips) -> GeoResolution:
    """Resolve every IP in `ips` to an ISO country code.

    Dry runs resolve too, and deliberately so. It looks tempting to serve them
    from cache only — dev shares backend/.env, so it shares the MaxMind account
    — but that reasoning doesn't survive contact with the facts:

      - dev's scheduler is off (LOGIN_IP_SCHEDULER_ENABLED=false), so dev cannot
        bill anything nightly; only a manual trigger reaches this code.
      - dev and prod share ONE login_ip.db (same file on the host), so the cache
        is shared: an IP dev resolves is banked for prod's next run, not wasted.
      - a cache-only dry run is worthless as a rehearsal. On a cold cache every
        IP comes back unresolved, which trips the systemic-failure gate and
        aborts — so the one mode meant to answer "what would this run do?"
        answers "nothing, I gave up".

    Raises GeoUnusableError when the account itself is unusable.
    """
    unique = list(dict.fromkeys(ip for ip in ips if ip))
    if not unique:
        return GeoResolution({}, 0, 0)

    s = get_settings()
    cached = login_ip_db.get_cached_countries(
        unique, ttl_days=s.LAST_CLOSE_IP_GEO_CACHE_TTL_DAYS
    )
    countries: dict[str, str | None] = dict(cached)
    missing = [ip for ip in unique if ip not in cached]

    if not missing:
        logger.info("geo: %d/%d IPs served from cache, 0 API calls", len(cached), len(unique))
        return GeoResolution(countries, cache_hits=len(cached), api_calls=0)

    _check_usable()

    fatal: Exception | None = None
    to_cache: list[dict] = []
    workers = max(1, min(s.LAST_CLOSE_IP_GEO_WORKERS, len(missing)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for ip, country, exc in pool.map(_lookup_one, missing):
            countries[ip] = country
            if exc is not None:
                fatal = fatal or exc
            elif country is not None:
                # Only definitive answers are cached. A transient failure has
                # country=None and must stay uncached so tomorrow retries it.
                to_cache.append({"ip": ip, "country": country, "source": "maxmind_api"})

    if fatal is not None:
        raise GeoUnusableError(
            f"MaxMind account unusable ({type(fatal).__name__}: {fatal}). Check "
            f"MAXMIND_ACCOUNT_ID / MAXMIND_LICENSE_KEY and that GeoIP2 Precision "
            f"is purchased and in credit at account.maxmind.com"
        ) from fatal

    if to_cache:
        login_ip_db.upsert_ip_geo_cache(to_cache)

    failed = sum(1 for ip in missing if countries[ip] is None)
    logger.info(
        "geo: %d cache hits, %d API calls, %d cached, %d transient failures",
        len(cached),
        len(missing),
        len(to_cache),
        failed,
    )
    return GeoResolution(countries, cache_hits=len(cached), api_calls=len(missing))


def format_push_value(ip: str, country: str | None) -> str:
    """Build the exact string written to CRM: ``"1.2.3.4 (CN)"``.

    Single source of truth for the format — the diff, the log and the CRM must
    agree byte for byte, so nobody should be f-stringing this inline.
    """
    return f"{ip} ({country or UNKNOWN_COUNTRY})"
