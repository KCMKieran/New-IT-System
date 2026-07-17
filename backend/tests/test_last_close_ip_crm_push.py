"""
login-ip §3.5 — last close IP -> CRM push: winner selection, SID mapping,
diff, blast-radius cap, and the CRM's silent-no-op failure mode.

No network, no MySQL: the CRM client takes an injectable session, the push
service takes an injected client, and the SQLite tables run against a tmp_path
via the same ``_DB_PATH`` monkeypatch the other login-ip tests use.

The AC these cover (doc §3.5.8 item 7):
  - pick_winners takes the latest close, including across servers
  - SID mapping does not drop "MT4"
  - diff logic
  - HTTP 200 + silent no-op is judged verify_failed, not success
"""

from __future__ import annotations

import csv
import io
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services import crm_last_close_ip_client as crm_client
from app.services import last_close_ip_crm_push_service as svc
from app.services import login_ip_geo_service as geo_svc
from app.services.crm_last_close_ip_client import (
    FIELD_KEY,
    CrmLastCloseIpClient,
    extract_field,
)
from app.services.login_ip_geo_service import GeoResolution, GeoUnusableError


# ── Fakes ─────────────────────────────────────────────────────

class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text if text else json.dumps(payload or {})

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class FakeCrmSession:
    """Scripted CRM. Reads serve `users`; writes mutate it (nested shape).

    `swallow_writes=True` reproduces the CRM's real and nastiest failure mode:
    a mistyped field key returns 200 and writes nothing.
    """

    def __init__(self, users: dict, swallow_writes: bool = False,
                 write_status: int = 200, read_status: int = 200):
        self.users = users          # client_id -> current field value (or None)
        self.posts = []
        self.swallow_writes = swallow_writes
        self.write_status = write_status
        self.read_status = read_status

    @property
    def writes(self):
        return [p for p in self.posts if "customFields" in p]

    def post(self, url, json=None, headers=None, timeout=None):
        payload = json or {}
        self.posts.append(payload)
        uid = int(payload["user"])
        if "customFields" not in payload:  # read
            if self.read_status != 200:
                return FakeResponse(self.read_status, {"error": "nope"})
            value = self.users.get(uid)
            cf = {FIELD_KEY: value} if value is not None else {}
            return FakeResponse(200, {"id": uid, "customFields": cf})
        # write
        if self.write_status != 200:
            return FakeResponse(self.write_status, {"error": "boom"})
        if not self.swallow_writes:
            self.users[uid] = payload["customFields"][FIELD_KEY]
        return FakeResponse(200, {"ok": True})


def _row(server, account_id, ip, time_mt, date="20260714"):
    return {
        "trade_date": date, "server_name": server, "account_id": account_id,
        "ip_address": ip, "event_time_mt": time_mt, "event_kind": "close",
        "order_ref": "#1",
    }


@pytest.fixture()
def db(tmp_path, monkeypatch):
    """login_ip.db in tmp_path, schema created."""
    from app.core import login_ip_db

    monkeypatch.setattr(login_ip_db, "_DB_PATH", tmp_path / "login_ip.db")
    login_ip_db.init_login_ip_db()
    return login_ip_db


def _settings(**over):
    base = dict(
        LAST_CLOSE_IP_CRM_WRITE_ENABLED=True,
        LAST_CLOSE_IP_CRM_MAX_WRITES_PER_RUN=2000,
        LAST_CLOSE_IP_CRM_MAIL_TO="test@example.com",
        CRM_LAST_CLOSE_IP_API_URL="https://crm.test/rest/users/update",
        CRM_LAST_CLOSE_IP_API_TOKEN="tok",
        LAST_CLOSE_IP_GEO_FAIL_ABORT_RATIO=0.2,
        LAST_CLOSE_IP_GEO_CACHE_TTL_DAYS=30,
        LAST_CLOSE_IP_GEO_WORKERS=4,
        MAXMIND_ACCOUNT_ID="123456",
        MAXMIND_LICENSE_KEY="k" * 40,
        MAXMIND_HOST="geoip.maxmind.com",
        MAXMIND_TIMEOUT=10.0,
    )
    base.update(over)
    return SimpleNamespace(**base)


def _stub_geo(monkeypatch, mapping=None, *, default="XX", calls=None):
    """Patch the geo resolver so no test touches MaxMind.

    `mapping` overrides specific IPs (use None to simulate a transient failure
    that skips the client); everything else resolves to `default`. `calls` is an
    optional list that records each batch, so tests can assert zero lookups.
    """
    mapping = mapping or {}

    def fake(ips):
        ips = list(ips)
        if calls is not None:
            calls.append({"ips": sorted(ips)})
        countries = {ip: mapping.get(ip, default) for ip in ips}
        return GeoResolution(countries, cache_hits=0, api_calls=len(ips))

    monkeypatch.setattr(geo_svc, "resolve_countries", fake)


# ── pick_winners: latest close wins ───────────────────────────

def test_pick_winners_takes_latest_close_within_one_server():
    rows = [
        _row("MT5", 111, "1.1.1.1", "09:00:00.000"),
        _row("MT5", 222, "2.2.2.2", "17:30:00.000"),
    ]
    id_map = {"5-111": 900, "5-222": 900}  # same client, two accounts
    winners, unresolved = svc.pick_winners(rows, id_map)

    assert unresolved == []
    assert winners[900]["ip_address"] == "2.2.2.2"
    assert winners[900]["contenders"] == 2
    assert winners[900]["distinct_ips"] == 2


def test_pick_winners_compares_across_servers_on_one_mt_clock():
    # All three servers stamp MT local (UTC+3), so the raw string compares
    # directly. If someone ever "fixes" this by converting per server, this
    # test fails.
    rows = [
        _row("MT4", 111, "1.1.1.1", "23:59:00.000"),
        _row("MT5", 222, "2.2.2.2", "23:59:00.500"),
        _row("MT4_Live2", 333, "3.3.3.3", "08:00:00.000"),
    ]
    id_map = {"1-111": 900, "5-222": 900, "6-333": 900}
    winners, _ = svc.pick_winners(rows, id_map)

    assert winners[900]["ip_address"] == "2.2.2.2"
    assert winners[900]["server_name"] == "MT5"
    assert winners[900]["contenders"] == 3
    assert winners[900]["distinct_ips"] == 3
    assert winners[900]["all_ips"] == ["1.1.1.1", "2.2.2.2", "3.3.3.3"]


def test_pick_winners_single_account_client_has_no_conflict():
    rows = [_row("MT4", 111, "1.1.1.1", "10:00:00.000")]
    winners, _ = svc.pick_winners(rows, {"1-111": 900})
    assert winners[900]["contenders"] == 1
    assert winners[900]["distinct_ips"] == 1


def test_pick_winners_same_ip_from_two_accounts_is_not_a_conflict():
    rows = [
        _row("MT4", 111, "1.1.1.1", "10:00:00.000"),
        _row("MT5", 222, "1.1.1.1", "11:00:00.000"),
    ]
    winners, _ = svc.pick_winners(rows, {"1-111": 900, "5-222": 900})
    assert winners[900]["contenders"] == 2
    assert winners[900]["distinct_ips"] == 1  # would not be listed in digest


def test_pick_winners_is_deterministic_on_identical_timestamps():
    rows = [
        _row("MT5", 222, "2.2.2.2", "10:00:00.000"),
        _row("MT4", 111, "1.1.1.1", "10:00:00.000"),
    ]
    id_map = {"1-111": 900, "5-222": 900}
    first, _ = svc.pick_winners(rows, id_map)
    second, _ = svc.pick_winners(list(reversed(rows)), id_map)
    assert first[900]["ip_address"] == second[900]["ip_address"]


# ── SID mapping: the "MT4" trap ───────────────────────────────

def test_sid_map_covers_mt4_which_shared_sid_map_would_drop():
    # app/core/sql_helpers.py SID_MAP keys this server "MT4_Live"; the analyzer
    # writes "MT4". Importing that map here would silently drop every MT4 row
    # (over half the daily snapshot). Pin our own map's keys.
    from app.core.sql_helpers import SID_MAP

    assert svc.SERVER_SID == {"MT4": 1, "MT5": 5, "MT4_Live2": 6}
    assert "MT4" not in SID_MAP, (
        "sql_helpers.SID_MAP grew an 'MT4' key — re-check whether this module "
        "can now share it instead of keeping a private copy"
    )


def test_mt4_rows_resolve_and_are_not_dropped():
    rows = [_row("MT4", 8522845, "1.1.1.1", "10:00:00.000")]
    winners, unresolved = svc.pick_winners(rows, {"1-8522845": 900})
    assert unresolved == []
    assert winners[900]["ip_address"] == "1.1.1.1"


def test_unknown_server_and_unmapped_loginsid_are_unresolved_not_crashes():
    rows = [
        _row("MT4", 111, "1.1.1.1", "10:00:00.000"),      # not in mt4_users
        _row("MT9_Nope", 222, "2.2.2.2", "11:00:00.000"),  # unknown server
    ]
    winners, unresolved = svc.pick_winners(rows, {})
    assert winners == {}
    assert len(unresolved) == 2
    reasons = " ".join(u["reason"] for u in unresolved)
    assert "not in mt4_users" in reasons and "unknown server_name" in reasons


# ── schema migration ──────────────────────────────────────────

# The 14-column table exactly as it stood before pushed_value was added
# (prod, MT day 20260716). Frozen here on purpose: this is the shape the
# migration has to cope with on a real install.
_LEGACY_PUSH_LOG_SQL = """
CREATE TABLE crm_last_close_ip_push_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT, trade_date TEXT NOT NULL,
    client_id INTEGER NOT NULL, ip_address TEXT NOT NULL, server_name TEXT NOT NULL,
    account_id INTEGER NOT NULL, event_time_mt TEXT NOT NULL,
    contenders INTEGER NOT NULL DEFAULT 1, distinct_ips INTEGER NOT NULL DEFAULT 1,
    value_before TEXT, result TEXT NOT NULL, http_status INTEGER, detail TEXT,
    pushed_at TEXT NOT NULL, UNIQUE (trade_date, client_id));
"""


def test_migration_adds_pushed_value_to_an_existing_install(tmp_path, monkeypatch):
    # Without this ALTER, _SCHEMA_SQL's CREATE TABLE IF NOT EXISTS silently
    # leaves prod at 14 columns and upsert_crm_push_log — which runs AFTER the
    # write loop — raises. The run would push ~1,200 clients to the real CRM,
    # fail to log any of it, and re-push all of them every night after, without
    # the blast-radius cap ever firing (1,200 < 5,000).
    from app.core import login_ip_db

    db_path = tmp_path / "login_ip.db"
    monkeypatch.setattr(login_ip_db, "_DB_PATH", db_path)

    conn = sqlite3.connect(str(db_path))
    conn.executescript(_LEGACY_PUSH_LOG_SQL)
    conn.execute(
        "INSERT INTO crm_last_close_ip_push_log (trade_date, client_id, ip_address,"
        " server_name, account_id, event_time_mt, result, pushed_at) VALUES"
        " ('20260716', 100335, '106.87.121.169', 'MT4', 8001450, '18:00:10.265',"
        " 'pushed', '2026-07-16T21:10:00Z')"
    )
    conn.commit()
    cols = {r[1] for r in conn.execute("PRAGMA table_info(crm_last_close_ip_push_log)")}
    assert "pushed_value" not in cols  # guard the guard: legacy shape confirmed
    conn.close()

    login_ip_db.init_login_ip_db()

    with login_ip_db.get_connection() as c:
        cols = {r[1] for r in c.execute("PRAGMA table_info(crm_last_close_ip_push_log)")}
        assert "pushed_value" in cols
        row = c.execute(
            "SELECT ip_address, pushed_value FROM crm_last_close_ip_push_log"
        ).fetchone()
    # Legacy row keeps its bare IP and gets a NULL pushed_value — the honest
    # answer, and what makes the diff re-push it once in the new format.
    assert row["ip_address"] == "106.87.121.169"
    assert row["pushed_value"] is None
    assert login_ip_db.get_known_crm_ips() == {100335: None}

    login_ip_db.init_login_ip_db()  # idempotent: several uvicorn workers re-run it
    with login_ip_db.get_connection() as c:
        cols = [r[1] for r in c.execute("PRAGMA table_info(crm_last_close_ip_push_log)")]
    assert cols.count("pushed_value") == 1


def test_duplicate_column_race_does_not_kill_a_losing_worker(tmp_path, monkeypatch):
    # Workers boot concurrently and all run check-then-ALTER; the losers' ALTERs
    # hit "duplicate column". An unguarded raise would kill them on startup.
    from app.core import login_ip_db

    monkeypatch.setattr(login_ip_db, "_DB_PATH", tmp_path / "login_ip.db")
    login_ip_db.init_login_ip_db()

    with login_ip_db.get_connection() as conn:
        login_ip_db._alter_ignore_duplicate_column(
            conn, "ALTER TABLE crm_last_close_ip_push_log ADD COLUMN pushed_value TEXT"
        )
        # Any other OperationalError must still surface.
        with pytest.raises(sqlite3.OperationalError):
            login_ip_db._alter_ignore_duplicate_column(
                conn, "ALTER TABLE does_not_exist ADD COLUMN x TEXT"
            )


# ── diff ──────────────────────────────────────────────────────

def test_diff_splits_changed_from_current():
    # The diff compares the full push value, not the bare IP — `known` holds
    # what CRM was last given.
    winners = {
        1: {"ip_address": "1.1.1.1", "push_value": "1.1.1.1 (CN)"},  # same -> skip
        2: {"ip_address": "2.2.2.2", "push_value": "2.2.2.2 (CN)"},  # differs -> push
        3: {"ip_address": "3.3.3.3", "push_value": "3.3.3.3 (CN)"},  # never pushed -> push
    }
    known = {1: "1.1.1.1 (CN)", 2: "9.9.9.9 (HK)"}
    to_push, current = svc.diff_winners(winners, known)

    assert set(to_push) == {2, 3}
    assert set(current) == {1}


def test_diff_repushes_when_country_changed_but_ip_did_not():
    # Same IP, MaxMind now says HK. The value CRM holds is stale and must be
    # corrected — a diff on the bare IP would call this "unchanged" forever.
    winners = {1: {"ip_address": "1.1.1.1", "push_value": "1.1.1.1 (HK)"}}
    to_push, current = svc.diff_winners(winners, {1: "1.1.1.1 (CN)"})

    assert set(to_push) == {1}
    assert current == {}


def test_diff_repushes_legacy_rows_written_before_the_geo_change():
    # THE cutover path: pre-2026-07-17 rows have pushed_value NULL, so CRM holds
    # a bare IP. Every such client must be re-pushed exactly once, in the new
    # format. If this regresses, the whole fleet either never converts or
    # re-pushes nightly forever.
    winners = {
        1: {"ip_address": "1.1.1.1", "push_value": "1.1.1.1 (CN)"},
        2: {"ip_address": "2.2.2.2", "push_value": "2.2.2.2 (JP)"},
    }
    known = {1: None, 2: None}  # what get_known_crm_ips returns for legacy rows
    to_push, current = svc.diff_winners(winners, known)

    assert set(to_push) == {1, 2}
    assert current == {}


def test_known_crm_ips_uses_latest_mt_day_and_ignores_unsettled(db):
    # Only pushed/unchanged mean "CRM holds this". failed/dry_run must not
    # poison the baseline, or a failed write would never be retried.
    db.upsert_crm_push_log([
        {"trade_date": "20260712", "client_id": 900, "ip_address": "1.1.1.1",
         "pushed_value": "1.1.1.1 (CN)",
         "server_name": "MT4", "account_id": 1, "event_time_mt": "10:00:00.000",
         "result": "pushed", "pushed_at": "2026-07-12T00:00:00Z"},
        {"trade_date": "20260714", "client_id": 900, "ip_address": "2.2.2.2",
         "pushed_value": "2.2.2.2 (CN)",
         "server_name": "MT4", "account_id": 1, "event_time_mt": "10:00:00.000",
         "result": "pushed", "pushed_at": "2026-07-14T00:00:00Z"},
        {"trade_date": "20260714", "client_id": 901, "ip_address": "3.3.3.3",
         "pushed_value": "3.3.3.3 (HK)",
         "server_name": "MT5", "account_id": 2, "event_time_mt": "10:00:00.000",
         "result": "failed", "pushed_at": "2026-07-14T00:00:00Z"},
        {"trade_date": "20260714", "client_id": 902, "ip_address": "4.4.4.4",
         "pushed_value": "4.4.4.4 (JP)",
         "server_name": "MT5", "account_id": 3, "event_time_mt": "10:00:00.000",
         "result": "dry_run", "pushed_at": "2026-07-14T00:00:00Z"},
    ])
    known = db.get_known_crm_ips()

    assert known == {900: "2.2.2.2 (CN)"}  # latest day wins; failed/dry_run absent


def test_known_crm_ips_returns_none_for_legacy_rows(db):
    # A row from before the geo change: pushed_value was never set. It must come
    # back as None (not the bare ip_address) so the diff re-pushes it once.
    db.upsert_crm_push_log([
        {"trade_date": "20260716", "client_id": 900, "ip_address": "1.1.1.1",
         "server_name": "MT4", "account_id": 1, "event_time_mt": "10:00:00.000",
         "result": "pushed", "pushed_at": "2026-07-16T00:00:00Z"},
    ])
    assert db.get_known_crm_ips() == {900: None}


# ── CRM client: 200 is not proof ──────────────────────────────

def test_write_field_verifies_read_back():
    session = FakeCrmSession(users={900: None})
    client = CrmLastCloseIpClient("https://crm.test", "tok", session=session)

    res = client.write_field(900, "1.2.3.4")

    assert res.ok and not res.verify_failed
    assert res.value_before is None
    assert res.value_after == "1.2.3.4"
    assert session.users[900] == "1.2.3.4"


def test_write_field_flags_200_but_silent_noop_as_verify_failed():
    # The CRM's documented behaviour for a mistyped custom-field key: HTTP 200,
    # nothing written. Judging that a success is the single worst bug this
    # module could ship — it would report 1,200 pushes and change nothing.
    session = FakeCrmSession(users={900: None}, swallow_writes=True)
    client = CrmLastCloseIpClient("https://crm.test", "tok", session=session)

    res = client.write_field(900, "1.2.3.4")

    assert res.ok is False
    assert res.verify_failed is True
    assert res.http_status == 200
    assert "verify failed" in res.detail


def test_write_field_skips_post_when_crm_already_holds_value():
    session = FakeCrmSession(users={900: "1.2.3.4"})
    client = CrmLastCloseIpClient("https://crm.test", "tok", session=session)

    res = client.write_field(900, "1.2.3.4")

    assert res.ok and res.no_op
    assert session.writes == []


def test_write_field_reports_http_error_without_retrying_4xx():
    session = FakeCrmSession(users={900: None}, write_status=403)
    client = CrmLastCloseIpClient("https://crm.test", "tok", session=session)

    res = client.write_field(900, "1.2.3.4")

    assert res.ok is False and res.verify_failed is False
    assert res.http_status == 403
    assert len(session.writes) == 1  # 403 is deterministic; no retry storm


def test_extract_field_handles_both_read_shapes_and_empties():
    assert extract_field({"customFields": {FIELD_KEY: "1.1.1.1"}}) == "1.1.1.1"
    assert extract_field({FIELD_KEY: "2.2.2.2"}) == "2.2.2.2"   # flat fallback
    assert extract_field({"customFields": {}}) is None          # unset client
    assert extract_field({"customFields": {FIELD_KEY: ""}}) is None
    assert extract_field({}) is None


# ── End-to-end push rounds ────────────────────────────────────

@pytest.fixture()
def snapshot(db):
    """Two clients: 900 has two accounts disagreeing on IP, 901 has one."""
    db.upsert_last_trade_ips([
        ("20260714", "MT4", 111, "1.1.1.1", "09:00:00.000", "close", "#1"),
        ("20260714", "MT5", 222, "2.2.2.2", "17:00:00.000", "close", "#2"),
        ("20260714", "MT5", 333, "3.3.3.3", "12:00:00.000", "close", "#3"),
    ])
    return {"1-111": 900, "5-222": 900, "5-333": 901}


def _run(db, monkeypatch, snapshot, session, settings, *, geo=None, **kw):
    monkeypatch.setattr(svc, "resolve_client_ids", lambda rows, s=None: snapshot)
    if geo is None:
        _stub_geo(monkeypatch)
    client = CrmLastCloseIpClient("https://crm.test", "tok", session=session)
    return svc.push_last_close_ips_to_crm(
        "20260714", settings=settings, client=client, send_email=False, **kw
    )


def test_live_run_pushes_value_with_country_and_logs(db, monkeypatch, snapshot):
    session = FakeCrmSession(users={900: None, 901: None})
    _stub_geo(monkeypatch, {"2.2.2.2": "CN", "3.3.3.3": "HK"})
    summary = _run(db, monkeypatch, snapshot, session, _settings(), geo="stubbed")

    assert summary["clients"] == 2
    assert summary["pushed"] == 2
    assert summary["ip_conflict_clients"] == 1        # client 900
    assert summary["multi_account_clients"] == 1
    assert summary["pushed_by_server"] == {"MT5": 2}  # both winners are MT5
    # What CRM ends up holding: IP plus country, latest close won.
    assert session.users == {900: "2.2.2.2 (CN)", 901: "3.3.3.3 (HK)"}
    assert summary["countries"] == {"CN": 1, "HK": 1}

    rows = {r["client_id"]: r for r in db.get_crm_push_log("20260714")}
    assert rows[900]["result"] == "pushed"
    # ip_address stays the bare snapshot value; pushed_value carries the country.
    assert rows[900]["ip_address"] == "2.2.2.2"
    assert rows[900]["pushed_value"] == "2.2.2.2 (CN)"
    assert rows[900]["value_before"] is None
    assert rows[900]["distinct_ips"] == 2
    assert rows[901]["contenders"] == 1


def test_unresolvable_country_pushes_unknown_suffix(db, monkeypatch, snapshot):
    # MaxMind's definitive "not geolocatable" (private/reserved range). Unknown
    # is a real answer: it gets pushed and is stable, so it won't churn.
    session = FakeCrmSession(users={900: None, 901: None})
    _stub_geo(monkeypatch, {"2.2.2.2": "Unknown", "3.3.3.3": "HK"})
    summary = _run(db, monkeypatch, snapshot, session, _settings(), geo="stubbed")

    assert session.users[900] == "2.2.2.2 (Unknown)"
    assert summary["geo_failed"] == 0  # a definitive answer is not a failure


def test_rerunning_the_same_day_is_idempotent_and_sends_no_writes(
    db, monkeypatch, snapshot
):
    settings = _settings()
    session = FakeCrmSession(users={900: None, 901: None})
    _stub_geo(monkeypatch, {"2.2.2.2": "CN", "3.3.3.3": "HK"})
    _run(db, monkeypatch, snapshot, session, settings, geo="stubbed")

    second = FakeCrmSession(users=dict(session.users))
    summary = _run(db, monkeypatch, snapshot, second, settings, geo="stubbed")

    assert summary["pushed"] == 0
    assert summary["unchanged"] == 2
    assert second.posts == []  # local diff — not one request, read or write


def test_value_before_records_the_old_value_for_rollback(db, monkeypatch, snapshot):
    session = FakeCrmSession(users={900: "9.9.9.9", 901: None})
    _stub_geo(monkeypatch, {"2.2.2.2": "CN", "3.3.3.3": "HK"})
    _run(db, monkeypatch, snapshot, session, _settings(), geo="stubbed")

    rows = {r["client_id"]: r for r in db.get_crm_push_log("20260714")}
    assert rows[900]["value_before"] == "9.9.9.9"
    assert rows[900]["ip_address"] == "2.2.2.2"
    assert rows[900]["pushed_value"] == "2.2.2.2 (CN)"


def test_dry_run_logs_everything_and_sends_zero_requests(db, monkeypatch, snapshot):
    session = FakeCrmSession(users={900: None, 901: None})
    summary = _run(db, monkeypatch, snapshot, session,
                   _settings(LAST_CLOSE_IP_CRM_WRITE_ENABLED=False))

    assert summary["dry_run"] == 2
    assert summary["pushed"] == 0
    assert session.posts == []
    assert session.users == {900: None, 901: None}

    rows = db.get_crm_push_log("20260714")
    assert len(rows) == 2
    assert {r["result"] for r in rows} == {"dry_run"}
    # dry_run must not settle the diff — the real run still has to push.
    assert db.get_known_crm_ips() == {}


def test_cap_exceeded_aborts_writing_nothing(db, monkeypatch, snapshot):
    session = FakeCrmSession(users={900: None, 901: None})
    summary = _run(db, monkeypatch, snapshot, session,
                   _settings(LAST_CLOSE_IP_CRM_MAX_WRITES_PER_RUN=1))

    assert summary["aborted"] and "exceeds" in summary["aborted"]
    assert session.posts == []
    assert db.get_crm_push_log("20260714") == []  # not even the unchanged rows


# ── geo: the country in "1.2.3.4 (CN)" ────────────────────────

def test_transient_geo_failure_skips_the_client_without_logging_a_row(
    db, monkeypatch, snapshot
):
    # A network blip on one IP. That client keeps whatever CRM already holds and
    # is retried tomorrow — no row, because a row would settle the diff.
    session = FakeCrmSession(users={900: None, 901: None})
    _stub_geo(monkeypatch, {"2.2.2.2": None, "3.3.3.3": "HK"})
    summary = _run(db, monkeypatch, snapshot, session, _settings(), geo="stubbed")

    assert summary["geo_failed"] == 1
    assert summary["pushed"] == 1
    assert session.users[900] is None          # untouched
    assert session.users[901] == "3.3.3.3 (HK)"

    rows = {r["client_id"]: r for r in db.get_crm_push_log("20260714")}
    assert 900 not in rows                     # no row at all for the skipped client
    assert db.get_known_crm_ips() == {901: "3.3.3.3 (HK)"}


def test_geo_failure_on_never_pushed_client_does_not_settle_as_unchanged(
    db, monkeypatch, snapshot
):
    # The sharpest trap in this design. Client 900 has never been pushed
    # (known.get -> None) and geo fails (push_value -> None). If the filter ran
    # inside the write loop instead of before the diff, `None == None` would read
    # as "unchanged" and write a settled row asserting CRM holds a value nobody
    # ever sent — excluding that client from every future push, silently, while
    # the digest counted them as fine.
    session = FakeCrmSession(users={900: None, 901: None})
    _stub_geo(monkeypatch, {"2.2.2.2": None, "3.3.3.3": "HK"})
    summary = _run(db, monkeypatch, snapshot, session, _settings(), geo="stubbed")

    assert summary["unchanged"] == 0
    assert 900 not in {r["client_id"] for r in db.get_crm_push_log("20260714")}
    assert 900 not in db.get_known_crm_ips()

    # Proof it isn't stuck: once geo recovers, the client pushes normally.
    _stub_geo(monkeypatch, {"2.2.2.2": "CN", "3.3.3.3": "HK"})
    second = FakeCrmSession(users=dict(session.users))
    summary2 = _run(db, monkeypatch, snapshot, second, _settings(), geo="stubbed")

    assert summary2["pushed"] == 1
    assert second.users[900] == "2.2.2.2 (CN)"


def test_log_row_refuses_to_persist_a_row_without_a_push_value():
    # Defence in depth for the trap above: a settled row with no pushed_value
    # poisons the next diff, so it must be impossible to write, not merely
    # avoided by the caller.
    with pytest.raises(ValueError, match="no push_value"):
        svc._log_row(900, {"ip_address": "1.1.1.1", "server_name": "MT4",
                           "account_id": 1, "event_time_mt": "10:00:00.000"},
                     "20260714", result="unchanged")


def test_systemic_geo_failure_aborts_the_run_writing_nothing(db, monkeypatch):
    # Most IPs failing is MaxMind being degraded, not a data quirk. Pushing the
    # remainder would look like an ordinary night in the digest.
    db.upsert_last_trade_ips([
        ("20260714", "MT5", 400 + i, f"10.0.0.{i}", "12:00:00.000", "close", "#1")
        for i in range(40)
    ])
    monkeypatch.setattr(svc, "resolve_client_ids",
                        lambda rows, s=None: {f"5-{400 + i}": 500 + i for i in range(40)})
    # 30 of 40 distinct IPs fail = 75%, over both the floor and the ratio.
    _stub_geo(monkeypatch, {f"10.0.0.{i}": None for i in range(30)})
    session = FakeCrmSession(users={})
    client = CrmLastCloseIpClient("https://crm.test", "tok", session=session)
    summary = svc.push_last_close_ips_to_crm(
        "20260714", settings=_settings(), client=client, send_email=False
    )

    assert summary["aborted"] and "geo failed" in summary["aborted"]
    assert session.posts == []
    assert db.get_crm_push_log("20260714") == []


def test_geo_gate_ignores_the_ratio_on_a_tiny_day(db, monkeypatch, snapshot):
    # 1 of 3 distinct IPs failing is 33% — over the ratio, but the sample is too
    # small for the ratio to mean anything. A near-empty day (or a weekend) must
    # not abort over one blip; the client is just skipped and retried.
    session = FakeCrmSession(users={900: None, 901: None})
    _stub_geo(monkeypatch, {"2.2.2.2": None})
    summary = _run(db, monkeypatch, snapshot, session, _settings(), geo="stubbed")

    assert summary["aborted"] is None
    assert summary["geo_failed"] == 1
    assert summary["pushed"] == 1  # the healthy client still goes through


def test_geo_account_unusable_aborts_the_run_writing_nothing(
    db, monkeypatch, snapshot
):
    # Out of credit / no entitlement / bad key: every IP would fail the same way.
    def boom(ips):
        raise GeoUnusableError("out of queries")

    monkeypatch.setattr(geo_svc, "resolve_countries", boom)
    session = FakeCrmSession(users={900: None, 901: None})
    summary = _run(db, monkeypatch, snapshot, session, _settings(), geo="stubbed")

    assert summary["aborted"] and "geo lookup unusable" in summary["aborted"]
    assert session.posts == []
    assert db.get_crm_push_log("20260714") == []


def test_dry_run_still_resolves_countries_so_the_rehearsal_is_faithful(
    db, monkeypatch, snapshot
):
    # A dry run must answer "what would the live run do?". Serving it from cache
    # only looks like a saving but isn't: dev's scheduler is off (it can't bill
    # nightly) and dev/prod share one login_ip.db, so a resolved IP is banked for
    # prod rather than wasted. Meanwhile a cold-cache dry run would resolve
    # nothing, trip the systemic-failure gate, and abort — telling you nothing.
    calls = []
    _stub_geo(monkeypatch, {"2.2.2.2": "CN", "3.3.3.3": "HK"}, calls=calls)
    session = FakeCrmSession(users={900: None, 901: None})
    summary = _run(db, monkeypatch, snapshot, session,
                   _settings(LAST_CLOSE_IP_CRM_WRITE_ENABLED=False), geo="stubbed")

    assert len(calls) == 1                 # geo ran
    assert summary["aborted"] is None      # and did NOT abort
    assert summary["dry_run"] == 2         # the number the rehearsal exists to give
    assert summary["geo_failed"] == 0
    assert session.posts == []             # still zero CRM traffic

    rows = {r["client_id"]: r for r in db.get_crm_push_log("20260714")}
    # The rehearsal shows the exact value a live run would send.
    assert rows[900]["pushed_value"] == "2.2.2.2 (CN)"
    assert {r["result"] for r in rows.values()} == {"dry_run"}
    assert db.get_known_crm_ips() == {}     # dry_run must not settle the diff


def test_crm_outage_aborts_after_consecutive_failures_without_raising(
    db, monkeypatch, snapshot
):
    # A 401/403/outage fails every client identically. Bail loudly instead of
    # grinding through 1,200 identical errors.
    monkeypatch.setattr(svc, "_CONSECUTIVE_FAILURE_ABORT", 2)
    # 503 is retryable, so the client would really sleep out its backoff here.
    monkeypatch.setattr(crm_client, "_BACKOFF_BASE_SEC", 0)
    session = FakeCrmSession(users={900: None, 901: None}, read_status=503)
    summary = _run(db, monkeypatch, snapshot, session, _settings())

    assert summary["failed"] == 2
    assert summary["pushed"] == 0
    assert summary["aborted"] and "consecutive failures" in summary["aborted"]
    assert db.get_known_crm_ips() == {}  # failures never settle the diff


def test_verify_failure_is_counted_separately_from_failure(db, monkeypatch, snapshot):
    session = FakeCrmSession(users={900: None, 901: None}, swallow_writes=True)
    summary = _run(db, monkeypatch, snapshot, session, _settings())

    assert summary["verify_failed"] >= 1
    assert summary["pushed"] == 0
    rows = {r["client_id"]: r for r in db.get_crm_push_log("20260714")}
    assert rows[900]["result"] == "verify_failed"


def test_snapshot_read_is_not_capped_at_the_ui_search_limit(db, monkeypatch):
    # search_last_trade_ips caps at 5,000 rows for the UI. The push must read
    # the whole day instead: borrowing that cap would silently stop pushing
    # clients past row 5,000, with no error and no gap in the digest counts.
    db.upsert_last_trade_ips([
        ("20260714", "MT5", 400000 + i, f"10.0.{i // 256}.{i % 256}",
         "10:00:00.000", "close", f"#{i}")
        for i in range(6000)
    ])
    rows = db.get_last_trade_ips_for_date("20260714")
    assert len(rows) == 6000

    monkeypatch.setattr(
        svc, "resolve_client_ids",
        lambda rows, s=None: {f"5-{400000 + i}": 500000 + i for i in range(6000)},
    )
    _stub_geo(monkeypatch)
    session = FakeCrmSession(users={})
    client = CrmLastCloseIpClient("https://crm.test", "tok", session=session)
    summary = svc.push_last_close_ips_to_crm(
        "20260714", settings=_settings(LAST_CLOSE_IP_CRM_WRITE_ENABLED=False),
        client=client, send_email=False,
    )

    assert summary["accounts"] == 6000
    assert summary["clients"] == 6000


def test_empty_snapshot_is_not_an_error(db, monkeypatch):
    monkeypatch.setattr(svc, "resolve_client_ids", lambda rows, s=None: {})
    _stub_geo(monkeypatch)
    session = FakeCrmSession(users={})
    client = CrmLastCloseIpClient("https://crm.test", "tok", session=session)
    summary = svc.push_last_close_ips_to_crm(
        "20260101", settings=_settings(), client=client, send_email=False
    )

    assert summary["accounts"] == 0
    assert summary["aborted"] is None
    assert session.posts == []


# ── Digest email ──────────────────────────────────────────────

def _capture_send(monkeypatch):
    """Capture the digest, reading attachments before the temp dir is wiped."""
    sent = {}

    def fake_send(subject, body, to, cc=None, attachments=None):
        sent.update(subject=subject, body=body, to=to, attachments=[])
        for path in attachments or []:
            sent["attachments"].append(
                (Path(path).name, Path(path).read_text(encoding="utf-8-sig"))
            )

    monkeypatch.setattr("app.services.email_service.send_email", fake_send)
    return sent


def test_digest_attaches_full_conflict_csv_and_has_no_emoji(db, monkeypatch, snapshot):
    monkeypatch.setattr(svc, "resolve_client_ids", lambda rows, s=None: snapshot)
    _stub_geo(monkeypatch, {"2.2.2.2": "CN", "3.3.3.3": "HK"})
    sent = _capture_send(monkeypatch)
    session = FakeCrmSession(users={900: None, 901: None})
    client = CrmLastCloseIpClient("https://crm.test", "tok", session=session)
    svc.push_last_close_ips_to_crm(
        "20260714", settings=_settings(), client=client, send_email=True
    )

    body = sent["body"]
    assert sent["to"] == "test@example.com"
    assert "各服务器更新客户数" in body  # per-server counts stay in the body

    # The body carries counts only — no per-client records at all (user's call
    # 2026-07-15: the CSV is the record, a preview in the body is noise).
    assert "Client 900" not in body
    assert "2.2.2.2" not in body
    assert "last_close_ip_conflicts_20260714.csv" in body  # points at the CSV

    # The conflict list ships as CSV, not as a grid in the body.
    (name, csv_text), = sent["attachments"]
    assert name == "last_close_ip_conflicts_20260714.csv"
    rows = list(csv.DictReader(io.StringIO(csv_text)))
    assert [r["client_id"] for r in rows] == ["900"]   # 901 has one IP, not a conflict
    assert rows[0]["pushed_ip"] == "2.2.2.2"           # latest close won
    assert rows[0]["pushed_value"] == "2.2.2.2 (CN)"   # what CRM was actually given
    # The country must never leak into pushed_ip/other_ips: other_ips filters on
    # the bare IP, so a contaminated winner would list itself as an "other".
    assert rows[0]["other_ips"] == "1.1.1.1"
    assert rows[0]["winning_server"] == "MT5"
    assert rows[0]["distinct_ips"] == "2"

    # alert-email-style: no emoji anywhere, subject included.
    for text in (body, sent["subject"]):
        assert all(ord(c) < 0x1F000 for c in text)


def test_digest_body_stays_small_when_conflicts_are_many(db, monkeypatch):
    # Gmail clips a body at ~102 KB. The body must not scale with the conflict
    # count — that is the whole reason the list moved to an attachment. The
    # country table is bounded for the same reason: ~1,190 IPs can span ~100
    # countries, and a row each would add ~18 KB.
    many = [
        (cid, {"ip_address": "1.1.1.1", "all_ips": ["1.1.1.1", "2.2.2.2"],
               "server_name": "MT5", "account_id": cid, "distinct_ips": 2,
               "event_time_mt": "10:00:00.000", "contenders": 2})
        for cid in range(1000, 3000)
    ]
    body = svc._build_digest_html(
        {"write_enabled": True, "ip_conflict_clients": len(many),
         "countries": {f"C{i}": i for i in range(120)}},
        many, "20260714", "x.csv",
    )
    assert len(body) < 20_000, f"body grew to {len(body)} bytes with 2000 conflicts"


def test_digest_country_table_is_bounded_and_sums_the_tail():
    counts = {f"C{i}": 1 for i in range(50)}
    counts["CN"] = 900
    counts["HK"] = 100
    top = svc._top_countries(counts)

    assert top[0] == ("CN", 900)
    assert top[1] == ("HK", 100)
    assert len(top) == svc._DIGEST_TOP_COUNTRIES + 1     # +1 for the Other row
    name, total = top[-1]
    assert "Other" in name and total == 42               # 50 - 8 shown = 42 tail
    assert sum(c for _, c in top) == sum(counts.values())  # nothing lost


def test_no_attachment_when_there_are_no_conflicts(db, monkeypatch):
    monkeypatch.setattr(svc, "resolve_client_ids", lambda rows, s=None: {"5-333": 901})
    _stub_geo(monkeypatch)
    db.upsert_last_trade_ips([
        ("20260714", "MT5", 333, "3.3.3.3", "12:00:00.000", "close", "#3"),
    ])
    sent = _capture_send(monkeypatch)
    session = FakeCrmSession(users={901: None})
    client = CrmLastCloseIpClient("https://crm.test", "tok", session=session)
    svc.push_last_close_ips_to_crm(
        "20260714", settings=_settings(), client=client, send_email=True
    )

    assert sent["attachments"] == []
    assert "None today" in sent["body"]


def test_digest_subject_marks_dry_run_and_abort(db, monkeypatch, snapshot):
    subjects = []
    monkeypatch.setattr(svc, "resolve_client_ids", lambda rows, s=None: snapshot)
    _stub_geo(monkeypatch)
    monkeypatch.setattr(
        "app.services.email_service.send_email",
        lambda subject, body, to, cc=None, attachments=None: subjects.append(subject),
    )
    session = FakeCrmSession(users={900: None, 901: None})
    client = CrmLastCloseIpClient("https://crm.test", "tok", session=session)

    svc.push_last_close_ips_to_crm(
        "20260714", settings=_settings(LAST_CLOSE_IP_CRM_WRITE_ENABLED=False),
        client=client, send_email=True,
    )
    svc.push_last_close_ips_to_crm(
        "20260714", settings=_settings(LAST_CLOSE_IP_CRM_MAX_WRITES_PER_RUN=1),
        client=client, send_email=True,
    )

    assert "[DRY RUN]" in subjects[0]
    assert "[ABORTED]" in subjects[1]


def test_unexpected_crash_still_emails_and_does_not_raise(db, monkeypatch, snapshot):
    # The job's value is that nobody watches it, so a crash that emails nothing
    # is indistinguishable from a quiet successful night. Any unforeseen error
    # (SQLite locked, disk full, a bug) must still produce a digest.
    def boom(*a, **kw):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(db, "get_last_trade_ips_for_date", boom)
    sent = _capture_send(monkeypatch)

    summary = svc.push_last_close_ips_to_crm(
        "20260714", settings=_settings(), send_email=True
    )

    assert summary["aborted"] and "database is locked" in summary["aborted"]
    assert "OperationalError" in summary["aborted"]
    assert "[ABORTED]" in sent["subject"]
    assert "database is locked" in sent["body"]


def test_smtp_failure_does_not_break_the_push(db, monkeypatch, snapshot):
    monkeypatch.setattr(svc, "resolve_client_ids", lambda rows, s=None: snapshot)
    _stub_geo(monkeypatch)

    def boom(**kw):
        raise RuntimeError("SMTP down")

    monkeypatch.setattr("app.services.email_service.send_email", boom)
    session = FakeCrmSession(users={900: None, 901: None})
    client = CrmLastCloseIpClient("https://crm.test", "tok", session=session)

    summary = svc.push_last_close_ips_to_crm(
        "20260714", settings=_settings(), client=client, send_email=True
    )

    assert summary["pushed"] == 2  # writes landed; only the email was lost
