from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import List

from dotenv import load_dotenv


# Ensure .env is loaded for local development
load_dotenv()


def _env_flag(name: str, default: bool = False) -> bool:
    """Read a boolean env var tolerantly.

    `.strip()` is load-bearing: backend/.env has CRLF line endings, so a value
    read through python-dotenv can arrive as "true\\r" and compare unequal to
    "true" — the same trap the MAIL_TO / MAXMIND settings below already guard.
    """
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


class Settings:
    # Database
    DB_HOST: str | None
    DB_USER: str | None
    DB_PASSWORD: str | None
    DB_NAME: str | None
    DB_PORT: int
    DB_CHARSET: str
    FXBACK_DB_NAME: str | None

    # MySQL for ETL (source)
    MYSQL_HOST: str | None
    MYSQL_USER: str | None
    MYSQL_PASSWORD: str | None
    MYSQL_DATABASE: str | None
    MYSQL_PORT: int
    MYSQL_SSL_CA: str | None

    # PostgreSQL for reporting (target)
    POSTGRES_HOST: str | None
    POSTGRES_USER: str | None
    POSTGRES_PASSWORD: str | None
    POSTGRES_DBNAME: str | None
    POSTGRES_PORT: int

    # Risk-V2 case layer (OPT-0047): dedicated database + least-privilege
    # account on the SAME Azure PG flexible server as the reporting DB
    # (host/port reuse POSTGRES_HOST/POSTGRES_PORT).
    RISK_CASES_PG_DBNAME: str | None
    RISK_CASES_PG_USER: str | None
    RISK_CASES_PG_PASSWORD: str | None

    # Paths (resolved relative to repo root by default)
    PARQUET_DIR: str | None
    PUBLIC_EXPORT_DIR: str | None

    # CORS
    CORS_ORIGINS: List[str]

    # SMTP (email sending)
    SMTP_SERVER: str | None
    SMTP_PORT: int
    SMTP_USERNAME: str | None
    SMTP_PASSWORD: str | None

    # Logging
    LOG_LEVEL: str

    # Client Return Rate async export
    CLIENT_RETURN_EXPORT_DIR: str | None
    CLIENT_RETURN_EXPORT_EXPIRE_HOURS: int
    CLIENT_RETURN_EXPORT_MAX_ROWS: int
    CLIENT_RETURN_EXPORT_MAX_WORKERS: int
    CLIENT_RETURN_EXPORT_CLEANUP_DAYS: int

    # ClickHouse-backed query endpoints (IB Report / Client PnL Analysis)
    CLICKHOUSE_ROUTES_ENABLED: bool

    # ROACE precompute scheduler (M2 / OPT-0006)
    CLIENT_ROACE_SCHEDULER_ENABLED: bool
    CLIENT_ROACE_REFRESH_HOUR: int
    CLIENT_ROACE_REFRESH_MINUTE: int

    # Alert mail center (OPT-0042/0043): recipient domain allowlist
    ALERT_MAIL_ALLOWED_DOMAINS: set[str]
    AUTH_ALLOWED_EMAIL_DOMAINS: set[str]
    AUTH_ALLOWED_EMAIL_DOMAINS_EXPLICIT: bool

    # Auth session layer (auth design P1)
    AUTH_ENABLED: bool
    AUTH_DEV_LOGIN_EMAIL: str
    AUTH_COOKIE_NAME: str
    AUTH_COOKIE_ENABLED: bool
    AUTH_COOKIE_SECURE: bool
    AUTH_COOKIE_SAMESITE: str
    AUTH_COOKIE_SAMESITE_RAW: str
    AUTH_COOKIE_PATH: str
    AUTH_COOKIE_DOMAIN: str | None
    AUTH_SESSION_IDLE_HOURS: int
    AUTH_SESSION_ABSOLUTE_HOURS: int
    AUTH_SESSION_RENEW_BELOW_HOURS: int
    AUTH_MANAGER_EMAILS: set[str]
    AUTH_FAILURE_EVENTS_PER_MINUTE: int
    AUTH_EVENTS_RETENTION_DAYS: int
    AUDIT_LOG_RETENTION_DAYS: int
    AUDIT_MISSING_ALERT_ENABLED: bool

    # Interactive API docs surface (Swagger /docs, ReDoc /redoc, /openapi.json)
    API_DOCS_ENABLED: bool

    # Entra ID OIDC provider (auth design P3)
    ENTRA_TENANT_ID: str
    ENTRA_CLIENT_ID: str
    ENTRA_CLIENT_SECRET: str
    ENTRA_REDIRECT_URI: str
    ENTRA_ENABLED: bool
    ENTRA_TRANSACTION_TTL_MINUTES: int

    def __init__(self) -> None:
        self.DB_HOST = os.environ.get("DB_HOST")
        self.DB_USER = os.environ.get("DB_USER")
        self.DB_PASSWORD = os.environ.get("DB_PASSWORD")
        self.DB_NAME = os.environ.get("DB_NAME")
        self.DB_PORT = int(os.environ.get("DB_PORT", "3306"))
        self.DB_CHARSET = os.environ.get("DB_CHARSET", "utf8mb4")
        
        self.FXBACK_DB_NAME = os.environ.get("FXBACK_DB_NAME")

        # MySQL (ETL 源库)
        self.MYSQL_HOST = os.environ.get("MYSQL_HOST")
        self.MYSQL_USER = os.environ.get("MYSQL_USER")
        self.MYSQL_PASSWORD = os.environ.get("MYSQL_PASSWORD")
        self.MYSQL_DATABASE = os.environ.get("MYSQL_DATABASE")
        self.MYSQL_PORT = int(os.environ.get("MYSQL_PORT", "3306"))
        self.MYSQL_SSL_CA = os.environ.get("MYSQL_SSL_CA")
        self.MYSQL_DATABASE_FXBACKOFFICE = os.environ.get("MYSQL_DATABASE_FXBACKOFFICE", "fxbackoffice")
        # Dedicated host for client-return-rate page; falls back to MYSQL_HOST
        self.MYSQL_HOST_PRIMARY = os.environ.get("MYSQL_HOST_PRIMARY") or self.MYSQL_HOST

        # PostgreSQL (报表库)
        self.POSTGRES_HOST = os.environ.get("POSTGRES_HOST")
        self.POSTGRES_USER = os.environ.get("POSTGRES_USER")
        self.POSTGRES_PASSWORD = os.environ.get("POSTGRES_PASSWORD")
        self.POSTGRES_DBNAME = os.environ.get("POSTGRES_DBNAME")
        self.POSTGRES_PORT = int(os.environ.get("POSTGRES_PORT", "5432"))

        # Risk-V2 case layer (OPT-0047)
        self.RISK_CASES_PG_DBNAME = os.environ.get("RISK_CASES_PG_DBNAME")
        self.RISK_CASES_PG_USER = os.environ.get("RISK_CASES_PG_USER")
        self.RISK_CASES_PG_PASSWORD = os.environ.get("RISK_CASES_PG_PASSWORD")

        self.PARQUET_DIR = os.environ.get("PARQUET_DIR")
        self.PUBLIC_EXPORT_DIR = os.environ.get("PUBLIC_EXPORT_DIR")

        self.CORS_ORIGINS = [o.strip() for o in os.environ.get("CORS_ORIGINS", "*").split(",") if o.strip()]

        # SMTP (email sending)
        self.SMTP_SERVER = os.environ.get("SMTP_SERVER", "smtp.office365.com")
        self.SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
        self.SMTP_USERNAME = os.environ.get("SMTP_USERNAME")
        self.SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD")

        # ── Honeypot decoy endpoints (security honeytoken) ───────────────────
        # Two inert decoy routes (routes/honeypot.py) that no legitimate flow
        # ever calls; any hit is an intrusion signal. On a trip the handler
        # forwards the caller's context to the central collector if
        # HONEYPOT_COLLECTOR_URL is set (authenticated with HONEYPOT_SHARED_SECRET),
        # and otherwise falls back to emailing HONEYPOT_ALERT_TO directly, so the
        # trap is never silent even before the Azure collector is up.
        #
        # HONEYPOT_SAFE_IDS is the set of "safe" ids: a request carrying one of
        # them is our own camouflage traffic — it does not alert and gets a
        # plausible 200 back, so the endpoint looks like a live, used API.
        self.HONEYPOT_COLLECTOR_URL = os.environ.get("HONEYPOT_COLLECTOR_URL", "").strip()
        self.HONEYPOT_SHARED_SECRET = os.environ.get("HONEYPOT_SHARED_SECRET", "").strip()
        # Recipient lives in the environment (backend/.env), not in code.
        self.HONEYPOT_ALERT_TO = os.environ.get("HONEYPOT_ALERT_TO", "").strip()
        self.HONEYPOT_SAFE_IDS = {
            i.strip()
            for i in os.environ.get("HONEYPOT_SAFE_IDS", "136017").split(",")
            if i.strip()
        }

        # Logging configuration
        # Options: DEBUG, INFO, WARNING, ERROR, CRITICAL
        self.LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")

        # Client Return Rate async CSV export
        self.CLIENT_RETURN_EXPORT_DIR = os.environ.get("CLIENT_RETURN_EXPORT_DIR")
        self.CLIENT_RETURN_EXPORT_EXPIRE_HOURS = int(
            os.environ.get("CLIENT_RETURN_EXPORT_EXPIRE_HOURS", "24")
        )
        self.CLIENT_RETURN_EXPORT_MAX_ROWS = int(
            os.environ.get("CLIENT_RETURN_EXPORT_MAX_ROWS", "200000")
        )
        self.CLIENT_RETURN_EXPORT_MAX_WORKERS = int(
            os.environ.get("CLIENT_RETURN_EXPORT_MAX_WORKERS", "1")
        )
        self.CLIENT_RETURN_EXPORT_CLEANUP_DAYS = int(
            os.environ.get("CLIENT_RETURN_EXPORT_CLEANUP_DAYS", "7")
        )

        # Master switch for the two endpoints that query ClickHouse Cloud
        # directly (IB Report, Client PnL Analysis). Parked 2026-07-27: neither
        # page is in active use, and the Cloud instance idle-suspends, so a cold
        # call pays a 36-159s wake-up (measured) that only serves to keep waking
        # a cluster nobody reads. Defaults to false so no .env edit is needed to
        # stay parked; set CLICKHOUSE_ROUTES_ENABLED=true to restore.
        self.CLICKHOUSE_ROUTES_ENABLED = (
            os.environ.get("CLICKHOUSE_ROUTES_ENABLED", "false").strip().lower()
            == "true"
        )

        # ROACE precompute (OPT-0006). Nightly cron writes avg_daily_equity into
        # backend/data/client_roace.db so the web request doesn't have to join
        # stats_balances (19M rows) on every hit.
        self.CLIENT_ROACE_SCHEDULER_ENABLED = (
            os.environ.get("CLIENT_ROACE_SCHEDULER_ENABLED", "false").lower() == "true"
        )
        self.CLIENT_ROACE_REFRESH_HOUR = int(
            os.environ.get("CLIENT_ROACE_REFRESH_HOUR", "6")
        )
        self.CLIENT_ROACE_REFRESH_MINUTE = int(
            os.environ.get("CLIENT_ROACE_REFRESH_MINUTE", "0")
        )

        # API Key for protecting /api/* endpoints (None = skip validation, for dev)
        self.API_KEY = os.environ.get("API_KEY")

        # Gap Trade → CRM risk tag (OPT-0032). Dedicated credentials, isolated
        # from other CRM integrations. .strip() defends against stray
        # whitespace in .env values (a leading space once shipped here and
        # only worked because both dotenv and compose happen to trim it).
        self.CRM_RISK_API_URL = (os.environ.get("CRM_RISK_API_URL") or "").strip()
        self.CRM_RISK_API_TOKEN = (os.environ.get("CRM_RISK_API_TOKEN") or "").strip()
        # Per-round digest recipients (comma-separated). Every tag change
        # (tagged / failed / skipped_cid) is emailed to this list.
        self.CRM_RISK_MAIL_TO = ",".join(
            a.strip()
            for a in os.environ.get("CRM_RISK_MAIL_TO", "").split(",")
            if a.strip()
        )

        # Login IP → CRM last close position IP. Dedicated credentials,
        # isolated from CRM_RISK_API_* so either integration can be revoked
        # without breaking the other.
        self.CRM_LAST_CLOSE_IP_API_URL = (
            os.environ.get("CRM_LAST_CLOSE_IP_API_URL") or ""
        ).strip()
        self.CRM_LAST_CLOSE_IP_API_TOKEN = (
            os.environ.get("CRM_LAST_CLOSE_IP_API_TOKEN") or ""
        ).strip()
        # Master gate for live writes. false = resolve + diff + log only.
        self.LAST_CLOSE_IP_CRM_WRITE_ENABLED = (
            os.environ.get("LAST_CLOSE_IP_CRM_WRITE_ENABLED", "false").strip().lower()
            == "true"
        )
        # Absolute volume ceiling: a run wanting more writes than this aborts
        # having written nothing.
        #
        # Be clear about what this does and does NOT catch (measured 2026-07-15).
        # Daily volume is ~800 writes / ~1,200 clients, and the snapshot itself
        # caps the plausible maximum near 1,300 — so this does NOT fire for any
        # normal-shaped failure, including a totally broken diff re-pushing every
        # client (1,206 < 5,000), nor for the genuinely dangerous case of pushing
        # WRONG values to the right clients. Those are covered elsewhere:
        #   - re-pushing correct values is harmless: write_field reads first and
        #     no-ops when CRM already agrees, so a broken diff costs reads, not writes
        #   - wrong values are caught by nothing automatic — that is the price of
        #     skipping the canary; value_before is the rollback and the first
        #     round's digest is the human check
        # What it IS: a ceiling on an upstream explosion (e.g. someone reverts the
        # analyzer's demo filter or close-only narrowing and the snapshot jumps from
        # 1,488 to five figures). Set well above any plausible day so it never
        # false-trips; it only ever fires on something absurd.
        self.LAST_CLOSE_IP_CRM_MAX_WRITES_PER_RUN = int(
            os.environ.get("LAST_CLOSE_IP_CRM_MAX_WRITES_PER_RUN", "5000")
        )
        # Digest recipient. Every run emails here — including aborts, since a
        # push that silently stops looks identical to one with nothing to do.
        # .strip() is load-bearing: backend/.env is CRLF, so a value read through
        # compose's env_file can arrive with a trailing \r that would otherwise
        # ride into the SMTP envelope.
        self.LAST_CLOSE_IP_CRM_MAIL_TO = (
            os.environ.get("LAST_CLOSE_IP_CRM_MAIL_TO")
            or "it.support@kohleservices.com"
        ).strip()

        # --- Geo enrichment for the last-close-IP push (2026-07-17) -----------
        # The pushed value is "1.2.3.4 (CN)"; the country comes from MaxMind's
        # GeoIP2 Precision Country web service. Same vendor account as
        # Manager_Log_Monitor, which is why these names match its config.
        #
        # .strip() is load-bearing here for the same CRLF reason as MAIL_TO above,
        # and it bites harder: a trailing \r on the license key is invisible in
        # logs and fails auth as a flat 401, which reads like "wrong key".
        self.MAXMIND_ACCOUNT_ID = (os.environ.get("MAXMIND_ACCOUNT_ID") or "").strip()
        self.MAXMIND_LICENSE_KEY = (os.environ.get("MAXMIND_LICENSE_KEY") or "").strip()
        self.MAXMIND_HOST = (
            os.environ.get("MAXMIND_HOST") or "geoip.maxmind.com"
        ).strip()
        self.MAXMIND_TIMEOUT = float(os.environ.get("MAXMIND_TIMEOUT", "10"))
        # Parallelism for the per-IP lookups. ~1,190 distinct IPs/day at ~150ms
        # each would add ~3min serially; 8 workers brings it under 30s. MaxMind
        # permits concurrent queries — this is polite, not a documented ceiling.
        self.LAST_CLOSE_IP_GEO_WORKERS = int(
            os.environ.get("LAST_CLOSE_IP_GEO_WORKERS", "8")
        )
        # Cache TTL. Exists to bound credit burn, not for speed — see the
        # ip_geo_cache schema comment in login_ip_db.py.
        self.LAST_CLOSE_IP_GEO_CACHE_TTL_DAYS = int(
            os.environ.get("LAST_CLOSE_IP_GEO_CACHE_TTL_DAYS", "30")
        )
        # Systemic-geo-failure gate. If more than this fraction of the day's
        # distinct IPs fail to resolve, the run aborts writing nothing rather
        # than pushing a partial set: that many failures means MaxMind is
        # degraded, not that those IPs are odd.
        self.LAST_CLOSE_IP_GEO_FAIL_ABORT_RATIO = float(
            os.environ.get("LAST_CLOSE_IP_GEO_FAIL_ABORT_RATIO", "0.2")
        )

        # Alert mail center (OPT-0042/0043): server-side recipient domain
        # allowlist. Subscription mail_to/mail_cc and test-send recipients may
        # only target mailboxes in these domains — anyone holding the frontend
        # API key could otherwise create a subscription (or test-send) routing
        # client financial data to an arbitrary external address. Comma-
        # separated env override; a blank/unset env falls back to the default.
        self.ALERT_MAIL_ALLOWED_DOMAINS = {
            d.strip().lower().lstrip("@")
            for d in (
                os.environ.get("ALERT_MAIL_ALLOWED_DOMAINS")
                or "kohleservices.com,kcmtrade.com,th.kohlecapital.com"
            ).split(",")
            if d.strip()
        }

        # Which email domains may LOG IN. Separate knob from the alert-mail
        # recipient allowlist above even though it defaults to the same value:
        # those answer different questions, and sharing one variable means
        # adding an external auditor as a report recipient silently grants that
        # domain login rights to a risk-control system (auth P3.5).
        self.AUTH_ALLOWED_EMAIL_DOMAINS = {
            d.strip().lower().lstrip("@")
            for d in os.environ.get("AUTH_ALLOWED_EMAIL_DOMAINS", "").split(",")
            if d.strip()
        } or set(self.ALERT_MAIL_ALLOWED_DOMAINS)
        # Whether the split above is real or only nominal. The fallback keeps a
        # box that never set the variable working, but it also means the P3.5
        # separation silently does not exist there: adding an external auditor
        # to ALERT_MAIL_ALLOWED_DOMAINS would hand that domain login rights,
        # which is exactly what splitting the two knobs was meant to prevent.
        # Surfaced at boot (main.py) rather than enforced, so a missing line
        # cannot lock everyone out.
        self.AUTH_ALLOWED_EMAIL_DOMAINS_EXPLICIT = bool(
            os.environ.get("AUTH_ALLOWED_EMAIL_DOMAINS", "").strip()
        )

        # ── Auth session layer (auth design P1) ──────────────────────────────
        # Master kill switch. OFF means AuthMiddleware short-circuits before it
        # touches the DB, /auth/me answers "anonymous", and the product behaves
        # exactly as it did before P1 — that is the rollback plan: flip to false
        # in backend/.env, then `docker compose -f docker-compose.prod.yml up -d api`.
        #
        # NOT `docker compose restart`. That restarts the process inside the
        # existing container; env is resolved once when the container is CREATED,
        # so the flip is silently ignored and nothing errors. Measured 2026-08-17:
        # after `restart` the container id and the value are both unchanged;
        # `up -d` prints "Recreated" and the new value takes effect.
        #
        # ⚠ The DEFAULT is True (changed 2026-08-19; it was False from P1 until
        # then, when auth was opt-in and its absence meant "nothing has shipped
        # yet"). Auth has been the only lock on this API since P3, so a missing
        # env line must not be able to remove it: with the old default, dropping
        # one line from backend/.env put the whole system into an unauthenticated
        # state with no error, no warning and no failed request — the app simply
        # let everybody in. Disabling auth is now something you have to write
        # down, which is the right cost for an action whose blast radius is
        # "every endpoint, for the entire internet" once CF Access is retired
        # (design doc §4.2.2, prerequisite 1).
        self.AUTH_ENABLED = _env_flag("AUTH_ENABLED", True)

        # Interactive API docs (Swagger /docs, ReDoc /redoc, /openapi.json).
        # These sit at the app root, OUTSIDE the /api/ scope both credential
        # middlewares guard, so when on they expose the whole route+schema
        # surface with no auth. Prod nginx does not route them (they fall
        # through to the SPA), but the backend still serves them on loopback
        # and inside the docker network. Default OFF so a missing env line
        # fails safe — dev opts in via backend/.env, same shape as the
        # AUTH_DEV_LOGIN_EMAIL back door above.
        self.API_DOCS_ENABLED = _env_flag("API_DOCS_ENABLED", False)

        # Dev back door: POST /api/v1/auth/dev-login mints a session for this
        # address with no IdP. Refuses to work unless the address is set AND
        # its domain is in ALERT_MAIL_ALLOWED_DOMAINS. Leave empty in prod.
        self.AUTH_DEV_LOGIN_EMAIL = (
            os.environ.get("AUTH_DEV_LOGIN_EMAIL") or ""
        ).strip().lower()

        # Cookie transport. Deliberately DISABLED by default in P1: on bare
        # `http://10.6.20.138` the `Secure` attribute is inert and `__Host-` is
        # unusable, and cookies ignore ports (RFC 6265) so a session cookie set
        # for that IP is also sent to :80/:7001/:7003/:8088/:19999 — five other
        # projects on this host — and shared between dev(:5173) and prod(:3000).
        # The mechanism is built and configurable; P2 (internal domain + TLS)
        # is what makes turning it on correct. Until then sessions travel as
        # `Authorization: Bearer <sid>`, per the design doc §7.1 fallback.
        self.AUTH_COOKIE_ENABLED = _env_flag("AUTH_COOKIE_ENABLED", False)
        self.AUTH_COOKIE_NAME = (
            os.environ.get("AUTH_COOKIE_NAME") or "kcm_sid"
        ).strip()
        self.AUTH_COOKIE_SECURE = _env_flag("AUTH_COOKIE_SECURE", False)
        # Lax, not Strict: the OIDC callback (P3) is a cross-site navigation
        # back from login.microsoftonline.com, and Strict withholds the cookie
        # on exactly that request.
        #
        # Whitelisted, because `none` is a valid cookie attribute that browsers
        # accept happily and that removes the ONLY CSRF defence this app has
        # (there is no synchronizer token anywhere) — one env typo would make
        # every state-changing endpoint reachable from any origin, with no error
        # and no visible symptom. Anything outside the set falls back to `lax`;
        # main.py prints the effective value at boot so a rejected typo is
        # visible rather than merely harmless.
        _samesite = (
            os.environ.get("AUTH_COOKIE_SAMESITE") or "lax"
        ).strip().lower()
        self.AUTH_COOKIE_SAMESITE_RAW = _samesite
        self.AUTH_COOKIE_SAMESITE = (
            _samesite if _samesite in ("lax", "strict") else "lax"
        )
        self.AUTH_COOKIE_PATH = (os.environ.get("AUTH_COOKIE_PATH") or "/").strip()
        # Empty -> host-only cookie (no Domain attribute), which is the tighter
        # of the two and what we want unless a subdomain ever needs to share.
        self.AUTH_COOKIE_DOMAIN = (
            os.environ.get("AUTH_COOKIE_DOMAIN") or ""
        ).strip() or None

        # Sliding 12h idle window inside a hard 7d ceiling (design doc §2.2).
        self.AUTH_SESSION_IDLE_HOURS = int(
            (os.environ.get("AUTH_SESSION_IDLE_HOURS") or "12").strip()
        )
        self.AUTH_SESSION_ABSOLUTE_HOURS = int(
            (os.environ.get("AUTH_SESSION_ABSOLUTE_HOURS") or "168").strip()
        )
        # Only rewrite expires_at when less than this much idle time is left,
        # so a busy tab does not turn every request into a SQLite write.
        self.AUTH_SESSION_RENEW_BELOW_HOURS = int(
            (os.environ.get("AUTH_SESSION_RENEW_BELOW_HOURS") or "6").strip()
        )

        # Seed managers. Defaults to the three addresses already in
        # backend/data/ib_financial.db's admin_whitelist (design doc §5.2), so
        # an unset env still produces a usable manager set.
        self.AUTH_MANAGER_EMAILS = {
            e.strip().lower()
            for e in (
                os.environ.get("AUTH_MANAGER_EMAILS")
                or "kieran.xiang@kohleservices.com,"
                   "lawrence.li@kohleservices.com,"
                   "teresa.wong@kohleservices.com"
            ).split(",")
            if e.strip()
        }

        # Bound what an unauthenticated caller can write into users.db.
        # /api/v1/auth/callback is exempt from both the API key and the session
        # layer (a browser arriving from Microsoft can present neither), so its
        # failure paths are reachable by anyone on the internet. nginx allows
        # 60 r/s per IP; without a cap that is ~5.2M auth_events rows a day,
        # every one of them contending for the same SQLite write lock as every
        # real request's resolve_session(). Successful and session-derived
        # events are NOT throttled — they all require a real session to exist.
        #
        # ⚠ Despite the name, this budget now covers BOTH refusal events:
        # login_failure (keyed by source IP) and, since auth P4b's module gate,
        # permission_denied (keyed by the refused subject). The name was left
        # alone deliberately — renaming it would mean an env change on a
        # deployment where nobody has ever set it, to buy a word. What the two
        # share is the property the cap exists for: they are written once per
        # REQUEST, at a rate the caller picks, unlike every other event kind
        # which fires once per thing that happened to an account.
        self.AUTH_FAILURE_EVENTS_PER_MINUTE = int(
            (os.environ.get("AUTH_FAILURE_EVENTS_PER_MINUTE") or "10").strip()
        )
        # Nothing has ever deleted from these two append-only tables. Retention
        # runs from lifespan on the scheduler-owning worker, next to the
        # expired-session purge. audit_log is business audit and keeps the 365d
        # the remarks history tables already use; auth_events was 90d as an
        # operational log, raised to the same 365d on 2026-08-19 (user call) —
        # "who logged in when" turns out to be asked with the same lag as
        # "who changed this", and the measured volume (~25 rows/day, ~2k rows
        # at 90d) never made the shorter window worth the lost year. The cap
        # that actually bounds this table is AUTH_FAILURE_EVENTS_PER_MINUTE
        # above, not the retention window.
        self.AUTH_EVENTS_RETENTION_DAYS = int(
            (os.environ.get("AUTH_EVENTS_RETENTION_DAYS") or "365").strip()
        )
        self.AUDIT_LOG_RETENTION_DAYS = int(
            (os.environ.get("AUDIT_LOG_RETENTION_DAYS") or "365").strip()
        )
        # AuditMissingMiddleware: warn (AUDIT_MISSING) when a successful write
        # produced no audit row. Default ON — the point of a fallback alarm is
        # that nobody has to remember to switch it on; the escape hatch exists
        # only for the case where one noisy known-unaudited endpoint would
        # otherwise drown the token the health check greps for.
        self.AUDIT_MISSING_ALERT_ENABLED = _env_flag("AUDIT_MISSING_ALERT_ENABLED", True)

        # ── Entra ID (Azure AD) OIDC provider (auth design P3) ───────────────
        # App registration lives in tenant 11cf6a7b-… (design doc §8.1). The
        # secret is in backend/.env and nowhere else; it expires ≈2028-08 and
        # an expired secret means NOBODY can log in, with a 500 rather than a
        # 401 as the symptom (§8.4).
        self.ENTRA_TENANT_ID = (os.environ.get("ENTRA_TENANT_ID") or "").strip()
        self.ENTRA_CLIENT_ID = (os.environ.get("ENTRA_CLIENT_ID") or "").strip()
        self.ENTRA_CLIENT_SECRET = (os.environ.get("ENTRA_CLIENT_SECRET") or "").strip()

        # Must byte-match a redirect URI registered on the app registration, so
        # it is configured rather than derived from the request Host header —
        # deriving it would both break on a mismatch and hand an attacker a
        # Host-header injection lever into the OIDC flow.
        #   prod: https://analysis.kohleservices.com/api/v1/auth/callback
        #   dev : http://localhost:5173/api/v1/auth/callback  (Entra exempts
        #         localhost from its https-only rule; reach the dev server via
        #         `ssh -L 5173:127.0.0.1:5173`, since P2 bound it to loopback)
        self.ENTRA_REDIRECT_URI = (os.environ.get("ENTRA_REDIRECT_URI") or "").strip()

        # Derived, not configured: a half-configured provider should look absent
        # rather than fail at the token exchange with the user already bounced
        # through Microsoft.
        self.ENTRA_ENABLED = bool(
            self.ENTRA_TENANT_ID
            and self.ENTRA_CLIENT_ID
            and self.ENTRA_CLIENT_SECRET
            and self.ENTRA_REDIRECT_URI
        )

        # How long a browser may sit on the Microsoft login page before the
        # /auth/login transaction we stored for it goes stale.
        self.ENTRA_TRANSACTION_TTL_MINUTES = int(
            (os.environ.get("ENTRA_TRANSACTION_TTL_MINUTES") or "10").strip()
        )

    @property
    def repo_root(self) -> Path:
        # This file: backend/app/core/config.py -> repo root is parents[3]
        return Path(__file__).resolve().parents[3]

    @property
    def parquet_dir(self) -> Path:
        if self.PARQUET_DIR:
            return Path(self.PARQUET_DIR)
        return self.repo_root / "backend" / "data"

    @property
    def public_export_dir(self) -> Path:
        if self.PUBLIC_EXPORT_DIR:
            return Path(self.PUBLIC_EXPORT_DIR)
        return self.repo_root / "frontend" / "public"

    # --- Helpers for services ---
    def postgres_dsn(self) -> str:
        """构建 PostgreSQL DSN。供服务层直接使用。

        fresh grad note: 使用 simple DSN 便于 psycopg2 连接；避免在代码各处手拼接。
        """
        host = self.POSTGRES_HOST or "localhost"
        port = self.POSTGRES_PORT
        db = self.POSTGRES_DBNAME or "reporting_db"
        user = self.POSTGRES_USER or "postgres"
        password = self.POSTGRES_PASSWORD or ""
        return f"host={host} port={port} dbname={db} user={user} password={password}"

    def risk_cases_pg_configured(self) -> bool:
        """True when the OPT-0047 case-layer PG credentials are present."""
        return bool(
            self.POSTGRES_HOST
            and self.RISK_CASES_PG_DBNAME
            and self.RISK_CASES_PG_USER
            and self.RISK_CASES_PG_PASSWORD
        )

    def risk_cases_pg_dsn(self) -> str:
        """DSN for the risk-V2 case database (OPT-0047).

        Host/port reuse the reporting-PG server (same Azure flexible server);
        dbname/user/password are the dedicated least-privilege `risk_cases` /
        `risk_app` pair created 2026-07-12.
        """
        host = self.POSTGRES_HOST or "localhost"
        port = self.POSTGRES_PORT
        db = self.RISK_CASES_PG_DBNAME or "risk_cases"
        user = self.RISK_CASES_PG_USER or "risk_app"
        password = self.RISK_CASES_PG_PASSWORD or ""
        # Hardening (OPT-0055):
        # - TCP keepalives detect Azure silently killing idle WAN
        #   connections (pooled connections would otherwise hang on a dead
        #   socket instead of failing fast).
        # - sslmode=require: Azure PG already enforces TLS, this just makes
        #   the requirement explicit client-side (no-op hardening).
        # - statement_timeout=30s: deliberately NOT lower — the case-engine
        #   write pipeline shares this DSN and its DELETE/upserts can wait
        #   on row locks; 30s converts a rare indefinite hang into a
        #   fail-open retry without cancelling legitimately slow writes.
        return (
            f"host={host} port={port} dbname={db} user={user} password={password}"
            " keepalives=1 keepalives_idle=30 keepalives_interval=10"
            " keepalives_count=3 sslmode=require application_name=risk_cases"
            " options='-c statement_timeout=30000'"
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Build (once) and return the process-wide settings.

    Cached because this sits on the per-request hot path: an authenticated
    /api/* call reaches it four times (both middlewares, ``extract_sid``,
    ``resolve_session``), and an uncached ``Settings()`` re-reads ~70 env vars
    and rebuilds several sets each time — measured at 32.8 us a call, i.e. ~131
    us per request, nearly triple the 45.6 us session lookup that
    ``auth_middleware`` goes to such lengths to keep cheap. Worse, it grew with
    every env var anyone added.

    Env is read at process start and never changes at runtime, so a single
    instance is also more honest than pretending otherwise. Tests that
    monkeypatch env must call ``get_settings.cache_clear()`` — the autouse
    fixture in ``tests/conftest.py`` does it for every test.
    """
    return Settings()


