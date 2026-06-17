"""Integration + service tests for Account Remarks (风控账户备注).

Minimal FastAPI app mounting only the risk-monitor router (no API-key
middleware), with risk_monitor SQLite redirected to a temp path per test so it
never touches backend/data/risk_monitor.db.

Covers the security requirements from docs/features/account-remarks.md §4 plus
the review-finding fixes:
  R1 — optimistic-lock conflict → 409; F1 atomic CAS concurrency regression
  R2 — note > 2000 chars → 422
  F4 — empty / whitespace-only note → 422; note stripped on store
  R8 — invalid server → 422
  R7 — append-only audit row on every upsert/delete; delete preserves old_note
  F6 — delete records the author in the audit row
  F9 — trace id is server-generated (not a client header)
  idempotent startup — init_risk_monitor_db() re-runs cleanly, no dup seeds

Mirrors the harness pattern in test_view_profiles_api.py.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture
def rmdb(tmp_path, monkeypatch):
    """Redirect risk_monitor_db._DB_PATH at a fresh per-test SQLite file and
    create the schema. The service reads the patched path via get_risk_monitor_db,
    so importing the service after patching is unnecessary (it resolves at call
    time through the module attribute)."""
    db_file = tmp_path / "risk_monitor_test.db"
    from app.core import risk_monitor_db as rmdb_mod
    monkeypatch.setattr(rmdb_mod, "_DB_PATH", db_file)
    rmdb_mod.init_risk_monitor_db()
    return rmdb_mod


@pytest.fixture
def client(rmdb) -> TestClient:
    app = FastAPI()
    # Mount TraceIDMiddleware so routes see a server-generated trace id in the
    # contextvar (F9). Without it trace_id_var.get() would be None.
    from app.core.trace_middleware import TraceIDMiddleware
    app.add_middleware(TraceIDMiddleware)
    from app.api.v1.routes.risk_monitor import router as rm_router
    app.include_router(rm_router, prefix="/api/v1")
    return TestClient(app)


# X-Device-ID is the client-supplied (best-effort) attribution header. The trace
# id is no longer client-settable (F9): the server captures it from the request
# trace contextvar, so tests assert device_id but not a client-controlled trace.
DEV_A = {"X-Device-ID": "device-A"}
DEV_B = {"X-Device-ID": "device-B"}

SERVER = "MT4_Live"
LOGIN = 8522845


def _history_rows(rmdb, server: str, login: int) -> list[dict]:
    with rmdb.get_risk_monitor_db() as conn:
        rows = conn.execute(
            "SELECT action, old_note, new_note, author, device_id, trace_id "
            "FROM account_remarks_history WHERE server = ? AND login = ? ORDER BY id",
            (server, login),
        ).fetchall()
        return [dict(r) for r in rows]


# ── Round-trip ────────────────────────────────────────────────────────────────

def test_upsert_then_read_round_trip(client):
    r = client.put(
        f"/api/v1/risk-monitor/remarks/{SERVER}/{LOGIN}",
        json={"note": "watch this account", "author": "Kieran"},
        headers=DEV_A,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["server"] == SERVER
    assert body["login"] == LOGIN
    assert body["note"] == "watch this account"
    assert body["author"] == "Kieran"
    assert body["updated_at"]

    # Full-map GET returns the row.
    lst = client.get("/api/v1/risk-monitor/remarks")
    assert lst.status_code == 200
    data = lst.json()
    assert data["total"] == 1
    assert data["data"][0]["note"] == "watch this account"


def test_upsert_updates_existing(client):
    client.put(
        f"/api/v1/risk-monitor/remarks/{SERVER}/{LOGIN}",
        json={"note": "first", "author": "Kieran"}, headers=DEV_A,
    )
    r = client.put(
        f"/api/v1/risk-monitor/remarks/{SERVER}/{LOGIN}",
        json={"note": "second", "author": "Sammy"}, headers=DEV_B,
    )
    assert r.status_code == 200
    assert r.json()["note"] == "second"
    lst = client.get("/api/v1/risk-monitor/remarks").json()
    assert lst["total"] == 1  # still one row (upsert, not insert)


# ── R1 optimistic lock ────────────────────────────────────────────────────────

def test_optimistic_lock_conflict_returns_409(client):
    r1 = client.put(
        f"/api/v1/risk-monitor/remarks/{SERVER}/{LOGIN}",
        json={"note": "original", "author": "Kieran"}, headers=DEV_A,
    )
    stale_token = r1.json()["updated_at"]

    # Someone else updates the row → updated_at moves on.
    client.put(
        f"/api/v1/risk-monitor/remarks/{SERVER}/{LOGIN}",
        json={"note": "concurrent edit", "author": "Sammy"}, headers=DEV_B,
    )

    # Our write carries the now-stale token → 409 Conflict, nothing overwritten.
    r3 = client.put(
        f"/api/v1/risk-monitor/remarks/{SERVER}/{LOGIN}",
        json={"note": "blind overwrite", "author": "Kieran",
              "expected_updated_at": stale_token},
        headers=DEV_A,
    )
    assert r3.status_code == 409, r3.text
    # The concurrent edit survived (no silent last-write-wins).
    assert client.get("/api/v1/risk-monitor/remarks").json()["data"][0]["note"] == "concurrent edit"


def test_optimistic_lock_matching_token_succeeds(client):
    r1 = client.put(
        f"/api/v1/risk-monitor/remarks/{SERVER}/{LOGIN}",
        json={"note": "v1", "author": "Kieran"}, headers=DEV_A,
    )
    token = r1.json()["updated_at"]
    r2 = client.put(
        f"/api/v1/risk-monitor/remarks/{SERVER}/{LOGIN}",
        json={"note": "v2", "author": "Kieran", "expected_updated_at": token},
        headers=DEV_A,
    )
    assert r2.status_code == 200
    assert r2.json()["note"] == "v2"


# ── R2 oversize note ──────────────────────────────────────────────────────────

def test_note_over_2000_chars_rejected_422(client):
    r = client.put(
        f"/api/v1/risk-monitor/remarks/{SERVER}/{LOGIN}",
        json={"note": "x" * 2001, "author": "Kieran"}, headers=DEV_A,
    )
    assert r.status_code == 422, r.text
    # Nothing written.
    assert client.get("/api/v1/risk-monitor/remarks").json()["total"] == 0


def test_note_exactly_2000_chars_accepted(client):
    r = client.put(
        f"/api/v1/risk-monitor/remarks/{SERVER}/{LOGIN}",
        json={"note": "x" * 2000, "author": "Kieran"}, headers=DEV_A,
    )
    assert r.status_code == 200


# ── R8 key validation ─────────────────────────────────────────────────────────

def test_invalid_server_rejected_422(client):
    r = client.put(
        f"/api/v1/risk-monitor/remarks/BOGUS_SERVER/{LOGIN}",
        json={"note": "hi", "author": "Kieran"}, headers=DEV_A,
    )
    assert r.status_code == 422, r.text


def test_non_positive_login_rejected_422(client):
    r = client.put(
        f"/api/v1/risk-monitor/remarks/{SERVER}/0",
        json={"note": "hi", "author": "Kieran"}, headers=DEV_A,
    )
    assert r.status_code == 422, r.text


# ── R7 audit trail ────────────────────────────────────────────────────────────

def test_history_row_written_on_upsert(client, rmdb):
    client.put(
        f"/api/v1/risk-monitor/remarks/{SERVER}/{LOGIN}",
        json={"note": "first", "author": "Kieran"}, headers=DEV_A,
    )
    client.put(
        f"/api/v1/risk-monitor/remarks/{SERVER}/{LOGIN}",
        json={"note": "second", "author": "Sammy"}, headers=DEV_B,
    )
    hist = _history_rows(rmdb, SERVER, LOGIN)
    assert len(hist) == 2
    # First upsert: no prior note.
    assert hist[0]["action"] == "upsert"
    assert hist[0]["old_note"] is None
    assert hist[0]["new_note"] == "first"
    assert hist[0]["device_id"] == "device-A"
    # F9: trace id is server-generated (format 'req-xxxxxxxx'), not the client's.
    assert hist[0]["trace_id"] and hist[0]["trace_id"].startswith("req-")
    # Second upsert: captures the previous note as old_note.
    assert hist[1]["action"] == "upsert"
    assert hist[1]["old_note"] == "first"
    assert hist[1]["new_note"] == "second"
    assert hist[1]["device_id"] == "device-B"


def test_delete_removes_live_row_but_history_preserves_old_note(client, rmdb):
    client.put(
        f"/api/v1/risk-monitor/remarks/{SERVER}/{LOGIN}",
        json={"note": "to be deleted", "author": "Kieran"}, headers=DEV_A,
    )
    r = client.request(
        "DELETE",
        f"/api/v1/risk-monitor/remarks/{SERVER}/{LOGIN}",
        headers=DEV_B,
    )
    assert r.status_code == 200, r.text
    assert r.json()["deleted"] is True

    # Live row gone.
    assert client.get("/api/v1/risk-monitor/remarks").json()["total"] == 0

    # History preserves the deleted note + the deleter's device id.
    hist = _history_rows(rmdb, SERVER, LOGIN)
    delete_rows = [h for h in hist if h["action"] == "delete"]
    assert len(delete_rows) == 1
    assert delete_rows[0]["old_note"] == "to be deleted"
    assert delete_rows[0]["new_note"] is None
    assert delete_rows[0]["device_id"] == "device-B"


def test_delete_nonexistent_is_noop(client, rmdb):
    r = client.request(
        "DELETE",
        f"/api/v1/risk-monitor/remarks/{SERVER}/{LOGIN}",
        headers=DEV_A,
    )
    assert r.status_code == 200
    assert r.json()["deleted"] is False
    # No history row for a no-op delete.
    assert _history_rows(rmdb, SERVER, LOGIN) == []


def test_invalid_server_delete_rejected_422(client):
    r = client.request(
        "DELETE",
        f"/api/v1/risk-monitor/remarks/BOGUS/{LOGIN}",
        headers=DEV_A,
    )
    assert r.status_code == 422


# ── Service-layer optimistic lock (direct call) ──────────────────────────────

def test_service_optimistic_lock_raises_conflict(rmdb):
    from app.services import account_remarks_service as svc
    row = svc.upsert_remark(SERVER, LOGIN, "a", "Kieran", device_id="d1", trace_id="t1")
    svc.upsert_remark(SERVER, LOGIN, "b", "Sammy", device_id="d2", trace_id="t2")
    with pytest.raises(svc.RemarkConflict):
        svc.upsert_remark(
            SERVER, LOGIN, "c", "Kieran",
            device_id="d1", trace_id="t1",
            expected_updated_at=row["updated_at"],
        )


# ── F1 concurrency regression: same token, exactly one winner ─────────────────

def test_concurrent_same_token_exactly_one_succeeds(rmdb):
    """Two writers holding the SAME expected_updated_at token race to upsert.
    Exactly one must win; the other must raise RemarkConflict (the regression
    guard for the F1 atomic compare-and-swap — no TOCTOU last-write-wins)."""
    import threading

    from app.services import account_remarks_service as svc

    base = svc.upsert_remark(SERVER, LOGIN, "base", "Kieran", device_id="d0", trace_id="t0")
    token = base["updated_at"]

    results: list[str] = []
    errors: list[Exception] = []
    lock = threading.Lock()
    barrier = threading.Barrier(2)

    def worker(note: str) -> None:
        # Line both threads up so they hit the write path as close together as
        # possible — maximises the chance of catching a non-atomic guard.
        barrier.wait()
        try:
            svc.upsert_remark(
                SERVER, LOGIN, note, "racer",
                device_id="dX", trace_id="tX",
                expected_updated_at=token,
            )
            with lock:
                results.append(note)
        except svc.RemarkConflict as exc:  # noqa: PERF203
            with lock:
                errors.append(exc)

    t1 = threading.Thread(target=worker, args=("winner-a",))
    t2 = threading.Thread(target=worker, args=("winner-b",))
    t1.start(); t2.start()
    t1.join(); t2.join()

    assert len(results) == 1, f"expected exactly one winner, got {results}"
    assert len(errors) == 1, f"expected exactly one conflict, got {errors}"
    assert isinstance(errors[0], svc.RemarkConflict)
    # The surviving live note is the winner's, and the row moved off the token.
    live = svc.get_remark(SERVER, LOGIN)
    assert live["note"] == results[0]
    assert live["updated_at"] != token


# ── F4 empty / whitespace-only note rejected ──────────────────────────────────

@pytest.mark.parametrize("bad_note", ["", "   ", "\t\n  ", "　"])
def test_empty_or_whitespace_note_rejected_422(client, bad_note):
    r = client.put(
        f"/api/v1/risk-monitor/remarks/{SERVER}/{LOGIN}",
        json={"note": bad_note, "author": "Kieran"}, headers=DEV_A,
    )
    assert r.status_code == 422, r.text
    assert client.get("/api/v1/risk-monitor/remarks").json()["total"] == 0


def test_note_is_stripped_on_store(client):
    r = client.put(
        f"/api/v1/risk-monitor/remarks/{SERVER}/{LOGIN}",
        json={"note": "  padded note  ", "author": "Kieran"}, headers=DEV_A,
    )
    assert r.status_code == 200, r.text
    assert r.json()["note"] == "padded note"


# ── F6 delete records the author ──────────────────────────────────────────────

def test_delete_records_author_in_history(client, rmdb):
    client.put(
        f"/api/v1/risk-monitor/remarks/{SERVER}/{LOGIN}",
        json={"note": "doomed", "author": "Kieran"}, headers=DEV_A,
    )
    r = client.request(
        "DELETE",
        f"/api/v1/risk-monitor/remarks/{SERVER}/{LOGIN}?author=Sammy",
        headers=DEV_B,
    )
    assert r.status_code == 200, r.text
    assert r.json()["deleted"] is True

    delete_rows = [h for h in _history_rows(rmdb, SERVER, LOGIN) if h["action"] == "delete"]
    assert len(delete_rows) == 1
    # F6: the deleter's author is forwarded (was always '' before the fix).
    assert delete_rows[0]["author"] == "Sammy"
    assert delete_rows[0]["device_id"] == "device-B"


# ── Idempotent startup ────────────────────────────────────────────────────────

def test_init_db_is_idempotent_no_duplicate_seeds(rmdb):
    """init_risk_monitor_db() can run twice with no error and no duplicate seed
    rows (a re-deploy / restart re-runs it)."""
    def _counts():
        with rmdb.get_risk_monitor_db() as conn:
            return {
                t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                for t in (
                    "burst_open_rules", "quick_open_close_rules",
                    "quick_profit_rules", "hedge_open_rules",
                    "leverage_abuse_rules", "martingale_rules",
                )
            }

    before = _counts()
    rmdb.init_risk_monitor_db()  # second call (fixture already ran it once)
    after = _counts()
    assert before == after, f"seed rows changed on re-init: {before} -> {after}"
    # Every rule table still has at least its seed row.
    assert all(v >= 1 for v in after.values())
