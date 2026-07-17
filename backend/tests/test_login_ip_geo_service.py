"""login-ip §3.5 geo step — IP -> country for the "1.2.3.4 (CN)" CRM value.

No network: the MaxMind web-service client is faked. The cache runs against a
tmp_path login_ip.db via the same ``_DB_PATH`` monkeypatch as the push tests.

What these pin, in order of how much it would cost to get wrong:
  - a cache hit bills nothing (credit is finite; a regression is invisible
    until the balance runs out)
  - a definitive no-answer is cached and pushed; a transient failure is neither
  - account-level failures abort rather than mark every IP individually failed
"""

from __future__ import annotations

from types import SimpleNamespace

import geoip2.errors
import pytest

from app.services import login_ip_geo_service as geo


# ── Fakes ─────────────────────────────────────────────────────

class FakeMaxMind:
    """Scripted MaxMind. `answers` maps ip -> ISO code, an Exception to raise,
    or None to mean "200 but no country" (real: some anonymized ranges)."""

    def __init__(self, answers: dict):
        self.answers = answers
        self.calls = []

    def country(self, ip):
        self.calls.append(ip)
        answer = self.answers.get(ip, "CN")
        if isinstance(answer, Exception):
            raise answer
        return SimpleNamespace(country=SimpleNamespace(iso_code=answer))


@pytest.fixture()
def db(tmp_path, monkeypatch):
    from app.core import login_ip_db

    monkeypatch.setattr(login_ip_db, "_DB_PATH", tmp_path / "login_ip.db")
    login_ip_db.init_login_ip_db()
    return login_ip_db


@pytest.fixture()
def settings(monkeypatch):
    s = SimpleNamespace(
        MAXMIND_ACCOUNT_ID="123456",
        MAXMIND_LICENSE_KEY="k" * 40,
        MAXMIND_HOST="geoip.maxmind.com",
        MAXMIND_TIMEOUT=10.0,
        LAST_CLOSE_IP_GEO_WORKERS=2,
        LAST_CLOSE_IP_GEO_CACHE_TTL_DAYS=30,
    )
    monkeypatch.setattr(geo, "get_settings", lambda: s)
    return s


def _fake_client(monkeypatch, answers):
    fake = FakeMaxMind(answers)
    monkeypatch.setattr(geo, "_get_client", lambda: fake)
    return fake


# ── Happy path ────────────────────────────────────────────────

def test_resolves_countries_and_caches_them(db, settings, monkeypatch):
    fake = _fake_client(monkeypatch, {"1.1.1.1": "CN", "2.2.2.2": "HK"})

    res = geo.resolve_countries(["1.1.1.1", "2.2.2.2"])

    assert res.countries == {"1.1.1.1": "CN", "2.2.2.2": "HK"}
    assert res.api_calls == 2 and res.cache_hits == 0
    assert db.get_cached_countries(["1.1.1.1", "2.2.2.2"]) == {
        "1.1.1.1": "CN", "2.2.2.2": "HK",
    }


def test_cache_hit_bills_nothing(db, settings, monkeypatch):
    # The credit-burn guard. ~600 new IPs/day is affordable; 1,190/day is not,
    # and the only symptom of a broken cache is the balance draining.
    _fake_client(monkeypatch, {"1.1.1.1": "CN"})
    geo.resolve_countries(["1.1.1.1"])

    fake2 = _fake_client(monkeypatch, {"1.1.1.1": "CN"})
    res = geo.resolve_countries(["1.1.1.1"])

    assert fake2.calls == []          # not one request
    assert res.api_calls == 0 and res.cache_hits == 1
    assert res.countries == {"1.1.1.1": "CN"}


def test_duplicate_ips_are_billed_once(db, settings, monkeypatch):
    fake = _fake_client(monkeypatch, {"1.1.1.1": "CN"})

    res = geo.resolve_countries(["1.1.1.1", "1.1.1.1", "1.1.1.1"])

    assert fake.calls == ["1.1.1.1"]
    assert res.api_calls == 1


def test_expired_cache_entries_are_refetched(db, settings, monkeypatch):
    _fake_client(monkeypatch, {"1.1.1.1": "CN"})
    geo.resolve_countries(["1.1.1.1"])

    # Age the row past the TTL.
    with db.get_connection() as conn:
        conn.execute(
            "UPDATE ip_geo_cache SET resolved_at = ? WHERE ip = ?",
            ("2020-01-01T00:00:00Z", "1.1.1.1"),
        )
        conn.commit()

    fake2 = _fake_client(monkeypatch, {"1.1.1.1": "JP"})
    res = geo.resolve_countries(["1.1.1.1"])

    assert fake2.calls == ["1.1.1.1"]
    assert res.countries == {"1.1.1.1": "JP"}   # picks up the new answer


# ── Definitive no-answer vs transient failure ─────────────────

def test_address_not_found_is_a_cacheable_answer_not_a_failure(db, settings, monkeypatch):
    # A private/reserved IP is definitively not geolocatable. That is a fact:
    # cache it, push it, and don't re-bill for it tomorrow.
    _fake_client(monkeypatch, {
        "10.0.0.1": geoip2.errors.AddressNotFoundError("reserved"),
    })

    res = geo.resolve_countries(["10.0.0.1"])

    assert res.countries == {"10.0.0.1": geo.UNKNOWN_COUNTRY}
    assert res.failed_ips == []
    assert db.get_cached_countries(["10.0.0.1"]) == {"10.0.0.1": "Unknown"}


def test_200_with_no_country_is_treated_as_unknown(db, settings, monkeypatch):
    _fake_client(monkeypatch, {"1.1.1.1": None})

    res = geo.resolve_countries(["1.1.1.1"])

    assert res.countries == {"1.1.1.1": "Unknown"}


def test_transient_failure_yields_no_answer_and_is_not_cached(db, settings, monkeypatch):
    # Caching a network blip would pin a wrong "Unknown" for the whole TTL and
    # guarantee a re-push once it recovers.
    _fake_client(monkeypatch, {
        "1.1.1.1": TimeoutError("read timeout"),
        "2.2.2.2": "HK",
    })

    res = geo.resolve_countries(["1.1.1.1", "2.2.2.2"])

    assert res.countries == {"1.1.1.1": None, "2.2.2.2": "HK"}
    assert res.failed_ips == ["1.1.1.1"]
    assert db.get_cached_countries(["1.1.1.1"]) == {}   # not cached
    assert db.get_cached_countries(["2.2.2.2"]) == {"2.2.2.2": "HK"}


# ── Account-level failures ────────────────────────────────────

@pytest.mark.parametrize("exc", [
    geoip2.errors.AuthenticationError("bad key"),
    geoip2.errors.PermissionRequiredError("no entitlement"),
    geoip2.errors.OutOfQueriesError("out of credit"),
])
def test_account_level_failures_raise_rather_than_fail_every_ip(
    db, settings, monkeypatch, exc
):
    # These fail identically for every IP until someone fixes the account, so
    # ~1,190 individual "failures" would be noise around one real problem.
    _fake_client(monkeypatch, {"1.1.1.1": exc})

    with pytest.raises(geo.GeoUnusableError, match="account unusable"):
        geo.resolve_countries(["1.1.1.1"])


def test_missing_credentials_raise_before_any_lookup(db, settings, monkeypatch):
    settings.MAXMIND_LICENSE_KEY = ""
    fake = _fake_client(monkeypatch, {"1.1.1.1": "CN"})

    with pytest.raises(geo.GeoUnusableError, match="not configured"):
        geo.resolve_countries(["1.1.1.1"])
    assert fake.calls == []


def test_non_numeric_account_id_raises_a_clear_error(db, settings, monkeypatch):
    settings.MAXMIND_ACCOUNT_ID = "not-a-number"
    _fake_client(monkeypatch, {"1.1.1.1": "CN"})

    with pytest.raises(geo.GeoUnusableError, match="not numeric"):
        geo.resolve_countries(["1.1.1.1"])


def test_only_uncached_ips_are_billed(db, settings, monkeypatch):
    _fake_client(monkeypatch, {"1.1.1.1": "CN"})
    geo.resolve_countries(["1.1.1.1"])   # seed the cache

    fake2 = _fake_client(monkeypatch, {"1.1.1.1": "CN", "2.2.2.2": "HK"})
    res = geo.resolve_countries(["1.1.1.1", "2.2.2.2"])

    assert fake2.calls == ["2.2.2.2"]           # only the miss
    assert res.api_calls == 1 and res.cache_hits == 1
    assert res.countries == {"1.1.1.1": "CN", "2.2.2.2": "HK"}


def test_empty_input_is_not_an_error(db, settings, monkeypatch):
    fake = _fake_client(monkeypatch, {})

    res = geo.resolve_countries([])

    assert res.countries == {} and res.api_calls == 0
    assert fake.calls == []


# ── Format ────────────────────────────────────────────────────

def test_format_push_value_is_the_single_source_of_the_format():
    # The diff, the push log and CRM must agree byte for byte.
    assert geo.format_push_value("1.2.3.4", "CN") == "1.2.3.4 (CN)"
    assert geo.format_push_value("1.2.3.4", None) == "1.2.3.4 (Unknown)"
    assert geo.format_push_value("1.2.3.4", "Unknown") == "1.2.3.4 (Unknown)"
