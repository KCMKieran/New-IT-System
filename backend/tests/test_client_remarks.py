"""Route + service tests for Client Remarks (risk-watchlist 客户备注).

PG-backed port of the account-remarks feature (see test_account_remarks.py
for the SQLite original). The account-remarks harness runs against a real
temp SQLite file; the risk_cases layer is cloud PG, so this file follows the
test_risk_cases_api.py pattern instead:

- Route-level tests mock the service layer (no DB) and pin the API contract:
  envelope shape, 409 on RemarkConflict, 422 on bad note/user_id, 503 on
  RiskCasesUnavailable, X-Device-ID + server-generated trace id passthrough.
- Service-level tests drive client_remarks_service against a mocked
  psycopg2 connection and pin the R1 compare-and-swap SQL: rowcount-0 UPDATE
  → conflict, guarded INSERT ON CONFLICT DO NOTHING → conflict, token format
  'YYYY-MM-DDTHH:MM:SSZ#<history_id>', history row in the same transaction.
- One skip-if-no-env integration test runs the full upsert → conflict →
  delete → audit loop against the real PG.
"""

from __future__ import annotations

import os
from unittest import mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.v1.routes import risk_cases as risk_cases_route
from app.core.risk_cases_pg import RiskCasesUnavailable
from app.services import client_remarks_service as svc


@pytest.fixture()
def client() -> TestClient:
    app = FastAPI()
    # Mount TraceIDMiddleware so routes see a server-generated trace id in
    # the contextvar (F9). Without it trace_id_var.get() would be None.
    from app.core.trace_middleware import TraceIDMiddleware

    app.add_middleware(TraceIDMiddleware)
    app.include_router(risk_cases_route.router, prefix="/api/v1")
    return TestClient(app)


DEV_A = {"X-Device-ID": "device-A"}
DEV_B = {"X-Device-ID": "device-B"}

UID = 127582


def _remark_row(user_id: int = UID, **over) -> dict:
    base = {
        "user_id": user_id,
        "note": "watch this client",
        "author": "Kieran",
        "updated_at": "2026-07-29T00:00:00Z#7",
    }
    base.update(over)
    return base


# ── Route: GET full map ──────────────────────────────────────────────────


def test_list_remarks_envelope(client):
    with mock.patch.object(
        risk_cases_route.remarks_svc,
        "get_all_remarks",
        return_value=[_remark_row()],
    ) as q:
        res = client.get("/api/v1/risk-cases/remarks")
    assert res.status_code == 200
    body = res.json()
    assert body["total"] == 1
    assert body["data"][0] == _remark_row()
    q.assert_called_once()


def test_list_remarks_not_swallowed_by_user_id_route(client):
    # /remarks is declared before /{user_id}; if the ordering ever regresses,
    # the literal path parses as user_id and 422s.
    with mock.patch.object(
        risk_cases_route.remarks_svc, "get_all_remarks", return_value=[]
    ):
        res = client.get("/api/v1/risk-cases/remarks")
    assert res.status_code == 200
    assert res.json() == {"data": [], "total": 0}


def test_list_remarks_503_when_pg_down(client):
    with mock.patch.object(
        risk_cases_route.remarks_svc,
        "get_all_remarks",
        side_effect=RiskCasesUnavailable("down"),
    ):
        res = client.get("/api/v1/risk-cases/remarks")
    assert res.status_code == 503


# ── Route: PUT upsert ────────────────────────────────────────────────────


def test_upsert_passes_through_and_returns_row(client):
    with mock.patch.object(
        risk_cases_route.remarks_svc,
        "upsert_remark",
        return_value=_remark_row(),
    ) as q:
        res = client.put(
            f"/api/v1/risk-cases/remarks/{UID}",
            json={
                "note": "watch this client",
                "author": "Kieran",
                "expected_updated_at": "2026-07-28T00:00:00Z#3",
            },
            headers=DEV_A,
        )
    assert res.status_code == 200, res.text
    assert res.json() == _remark_row()
    kwargs = q.call_args.kwargs
    assert kwargs["user_id"] == UID
    assert kwargs["note"] == "watch this client"
    assert kwargs["author"] == "Kieran"
    assert kwargs["expected_updated_at"] == "2026-07-28T00:00:00Z#3"
    assert kwargs["device_id"] == "device-A"
    # F9: trace id is server-generated ('req-xxxxxxxx'), not a client header.
    assert kwargs["trace_id"] and kwargs["trace_id"].startswith("req-")


def test_upsert_conflict_returns_409(client):
    with mock.patch.object(
        risk_cases_route.remarks_svc,
        "upsert_remark",
        side_effect=svc.RemarkConflict("someone else edited"),
    ):
        res = client.put(
            f"/api/v1/risk-cases/remarks/{UID}",
            json={"note": "blind overwrite", "author": "Kieran",
                  "expected_updated_at": "stale#1"},
            headers=DEV_A,
        )
    assert res.status_code == 409, res.text


def test_upsert_503_when_pg_down(client):
    with mock.patch.object(
        risk_cases_route.remarks_svc,
        "upsert_remark",
        side_effect=RiskCasesUnavailable("down"),
    ):
        res = client.put(
            f"/api/v1/risk-cases/remarks/{UID}",
            json={"note": "hi", "author": "Kieran"},
            headers=DEV_A,
        )
    assert res.status_code == 503


def test_upsert_note_over_2000_chars_rejected_422(client):
    with mock.patch.object(
        risk_cases_route.remarks_svc, "upsert_remark"
    ) as q:
        res = client.put(
            f"/api/v1/risk-cases/remarks/{UID}",
            json={"note": "x" * 2001, "author": "Kieran"},
            headers=DEV_A,
        )
    assert res.status_code == 422, res.text
    q.assert_not_called()  # rejected by Pydantic — SQL never touched (R2)


def test_upsert_note_exactly_2000_chars_accepted(client):
    with mock.patch.object(
        risk_cases_route.remarks_svc,
        "upsert_remark",
        return_value=_remark_row(note="x" * 2000),
    ):
        res = client.put(
            f"/api/v1/risk-cases/remarks/{UID}",
            json={"note": "x" * 2000, "author": "Kieran"},
            headers=DEV_A,
        )
    assert res.status_code == 200


@pytest.mark.parametrize("bad_note", ["", "   ", "\t\n  ", "　"])
def test_upsert_empty_or_whitespace_note_rejected_422(client, bad_note):
    with mock.patch.object(
        risk_cases_route.remarks_svc, "upsert_remark"
    ) as q:
        res = client.put(
            f"/api/v1/risk-cases/remarks/{UID}",
            json={"note": bad_note, "author": "Kieran"},
            headers=DEV_A,
        )
    assert res.status_code == 422, res.text
    q.assert_not_called()


def test_upsert_note_is_stripped(client):
    with mock.patch.object(
        risk_cases_route.remarks_svc,
        "upsert_remark",
        return_value=_remark_row(note="padded note"),
    ) as q:
        res = client.put(
            f"/api/v1/risk-cases/remarks/{UID}",
            json={"note": "  padded note  ", "author": "Kieran"},
            headers=DEV_A,
        )
    assert res.status_code == 200
    assert q.call_args.kwargs["note"] == "padded note"


@pytest.mark.parametrize("bad_uid", [0, -5])
def test_upsert_non_positive_user_id_rejected_422(client, bad_uid):
    with mock.patch.object(
        risk_cases_route.remarks_svc, "upsert_remark"
    ) as q:
        res = client.put(
            f"/api/v1/risk-cases/remarks/{bad_uid}",
            json={"note": "hi", "author": "Kieran"},
            headers=DEV_A,
        )
    assert res.status_code == 422, res.text
    q.assert_not_called()


# ── Route: DELETE ────────────────────────────────────────────────────────


def test_delete_returns_deleted_flag_and_forwards_author(client):
    with mock.patch.object(
        risk_cases_route.remarks_svc, "delete_remark", return_value=True
    ) as q:
        res = client.request(
            "DELETE",
            f"/api/v1/risk-cases/remarks/{UID}?author=Sammy",
            headers=DEV_B,
        )
    assert res.status_code == 200, res.text
    assert res.json() == {"deleted": True}
    kwargs = q.call_args.kwargs
    assert kwargs["user_id"] == UID
    assert kwargs["author"] == "Sammy"  # F6
    assert kwargs["device_id"] == "device-B"
    assert kwargs["trace_id"] and kwargs["trace_id"].startswith("req-")


def test_delete_nonexistent_reports_false(client):
    with mock.patch.object(
        risk_cases_route.remarks_svc, "delete_remark", return_value=False
    ):
        res = client.request(
            "DELETE", f"/api/v1/risk-cases/remarks/{UID}", headers=DEV_A
        )
    assert res.status_code == 200
    assert res.json() == {"deleted": False}


def test_delete_503_when_pg_down(client):
    with mock.patch.object(
        risk_cases_route.remarks_svc,
        "delete_remark",
        side_effect=RiskCasesUnavailable("down"),
    ):
        res = client.request(
            "DELETE", f"/api/v1/risk-cases/remarks/{UID}", headers=DEV_A
        )
    assert res.status_code == 503


@pytest.mark.parametrize("bad_uid", [0, -5])
def test_delete_non_positive_user_id_rejected_422(client, bad_uid):
    with mock.patch.object(
        risk_cases_route.remarks_svc, "delete_remark"
    ) as q:
        res = client.request(
            "DELETE",
            f"/api/v1/risk-cases/remarks/{bad_uid}",
            headers=DEV_A,
        )
    assert res.status_code == 422
    q.assert_not_called()


# ── Service layer against a mocked PG connection ─────────────────────────


def _mock_conn(fetchone_results: list, rowcount: int = 1):
    """Build (context-manager, cursor, executed) fakes for risk_cases_conn.

    `fetchone_results` scripts cur.fetchone() call by call; every execute
    (sql, params) pair is recorded so tests can pin the CAS SQL and check
    all values travel as bound parameters.
    """
    executed: list[tuple[str, object]] = []
    cur = mock.MagicMock()
    cur.execute.side_effect = lambda sql, params=None: executed.append(
        (sql, params)
    )
    cur.fetchone.side_effect = fetchone_results
    cur.rowcount = rowcount
    conn = mock.MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    cm = mock.MagicMock()
    cm.__enter__.return_value = conn
    cm.__exit__.return_value = False
    return cm, cur, executed


HIST = {"id": 7, "at": "2026-07-29T00:00:00Z"}


def test_service_upsert_new_row_token_format_and_guarded_insert():
    final = _remark_row(updated_at="2026-07-29T00:00:00Z#7")
    cm, _cur, executed = _mock_conn([None, HIST, final])
    with mock.patch.object(svc, "risk_cases_conn", return_value=cm):
        row = svc.upsert_remark(
            UID, "watch this client", "Kieran",
            device_id="d1", trace_id="t1",
        )
    assert row == final
    # Token = timestamp + '#' + BIGSERIAL history id (unique per write).
    insert_sql, insert_params = next(
        (s, p) for s, p in executed if "INSERT INTO client_remarks " in s
    )
    assert "ON CONFLICT (user_id) DO NOTHING" in insert_sql
    assert "2026-07-29T00:00:00Z#7" in insert_params
    # R7 audit row in the same transaction, old_note NULL for a first write.
    hist_sql, hist_params = next(
        (s, p) for s, p in executed if "client_remarks_history" in s
    )
    assert "RETURNING id, at" in hist_sql
    assert hist_params == (UID, None, "watch this client", "Kieran", "d1", "t1")
    # Every statement is parameterized — no user value ever lands in SQL.
    for sql, _params in executed:
        assert "watch this client" not in sql
        assert str(UID) not in sql


def test_service_upsert_existing_row_cas_on_updated_at():
    existing = {"note": "old", "updated_at": "2026-07-28T00:00:00Z#3"}
    final = _remark_row(note="new", updated_at="2026-07-29T00:00:00Z#7")
    cm, _cur, executed = _mock_conn([existing, HIST, final])
    with mock.patch.object(svc, "risk_cases_conn", return_value=cm):
        row = svc.upsert_remark(
            UID, "new", "Kieran",
            expected_updated_at="2026-07-28T00:00:00Z#3",
        )
    assert row["note"] == "new"
    upd_sql, upd_params = next(
        (s, p) for s, p in executed if s.startswith("UPDATE client_remarks")
    )
    # The atomic compare-and-swap: WHERE guards on user_id AND the token.
    assert "WHERE user_id = %s AND updated_at = %s" in upd_sql
    assert upd_params[-1] == "2026-07-28T00:00:00Z#3"
    # History captured the old note.
    _s, hist_params = next(
        (s, p) for s, p in executed if "client_remarks_history" in s
    )
    assert hist_params[1] == "old"


def test_service_upsert_stale_token_fails_fast_before_writing():
    existing = {"note": "old", "updated_at": "2026-07-29T00:00:00Z#9"}
    cm, _cur, executed = _mock_conn([existing])
    with mock.patch.object(svc, "risk_cases_conn", return_value=cm):
        with pytest.raises(svc.RemarkConflict):
            svc.upsert_remark(
                UID, "blind overwrite", "Kieran",
                expected_updated_at="2026-07-28T00:00:00Z#3",
            )
    # Pre-check tripped: only the SELECT ran — no history, no UPDATE.
    assert len(executed) == 1
    assert executed[0][0].startswith("SELECT")


def test_service_upsert_update_rowcount_zero_raises_conflict():
    # Pre-check passes (token matches the read) but a concurrent writer wins
    # the row lock first → the CAS UPDATE matches 0 rows → RemarkConflict.
    existing = {"note": "old", "updated_at": "2026-07-28T00:00:00Z#3"}
    cm, _cur, _executed = _mock_conn([existing, HIST], rowcount=0)
    with mock.patch.object(svc, "risk_cases_conn", return_value=cm):
        with pytest.raises(svc.RemarkConflict):
            svc.upsert_remark(
                UID, "loser write", "Kieran",
                expected_updated_at="2026-07-28T00:00:00Z#3",
            )


def test_service_upsert_concurrent_create_raises_conflict():
    # Brand-new-row race: SELECT sees nothing, but the guarded INSERT hits
    # ON CONFLICT DO NOTHING (a concurrent create committed first) →
    # rowcount 0 → RemarkConflict, never a silent double-write.
    cm, _cur, _executed = _mock_conn([None, HIST], rowcount=0)
    with mock.patch.object(svc, "risk_cases_conn", return_value=cm):
        with pytest.raises(svc.RemarkConflict):
            svc.upsert_remark(UID, "racer", "Kieran")


def test_service_delete_existing_writes_history():
    cm, _cur, executed = _mock_conn([{"note": "to be deleted"}])
    with mock.patch.object(svc, "risk_cases_conn", return_value=cm):
        assert svc.delete_remark(
            UID, author="Sammy", device_id="d2", trace_id="t2"
        ) is True
    del_sql, del_params = next(
        (s, p) for s, p in executed if s.startswith("DELETE")
    )
    assert del_params == (UID,)
    # Atomic read-and-delete: RETURNING closes the SELECT-then-DELETE race
    # (concurrent deleters can't both claim the deletion / record stale notes).
    assert "RETURNING note" in del_sql
    _s, hist_params = next(
        (s, p) for s, p in executed if "client_remarks_history" in s
    )
    # R7/F6: old note preserved + deleter attribution recorded.
    assert hist_params == (UID, "to be deleted", "Sammy", "d2", "t2")


def test_service_delete_nonexistent_is_noop():
    cm, _cur, executed = _mock_conn([None])
    with mock.patch.object(svc, "risk_cases_conn", return_value=cm):
        assert svc.delete_remark(UID) is False
    # Only the atomic DELETE ... RETURNING ran (0 rows) — no history row.
    assert len(executed) == 1
    assert executed[0][0].startswith("DELETE")
    assert "RETURNING note" in executed[0][0]


def test_service_get_all_remarks_maps_rows():
    cur = mock.MagicMock()
    cur.fetchall.return_value = [_remark_row(), _remark_row(user_id=UID + 1)]
    conn = mock.MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    cm = mock.MagicMock()
    cm.__enter__.return_value = conn
    cm.__exit__.return_value = False
    with mock.patch.object(svc, "risk_cases_conn", return_value=cm):
        rows = svc.get_all_remarks()
    assert [r["user_id"] for r in rows] == [UID, UID + 1]
    sql = cur.execute.call_args.args[0]
    assert "ORDER BY user_id" in sql


# ── Real-PG integration (skipped without env) ────────────────────────────


def _real_pg_available() -> bool:
    return bool(
        os.environ.get("POSTGRES_HOST")
        and os.environ.get("RISK_CASES_PG_DBNAME")
        and os.environ.get("RISK_CASES_PG_USER")
        and os.environ.get("RISK_CASES_PG_PASSWORD")
    )


# Far outside any real userId range; cleaned up in finally.
FIXTURE_UID = 999990001


@pytest.mark.skipif(not _real_pg_available(), reason="RISK_CASES_PG_* env not set")
def test_client_remarks_end_to_end():
    from app.core.risk_cases_pg import init_risk_cases_pg, risk_cases_conn

    assert init_risk_cases_pg() is True

    def _cleanup():
        with risk_cases_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM client_remarks WHERE user_id = %s",
                    (FIXTURE_UID,),
                )
                cur.execute(
                    "DELETE FROM client_remarks_history WHERE user_id = %s",
                    (FIXTURE_UID,),
                )

    _cleanup()
    try:
        # Create → token format
        row = svc.upsert_remark(
            FIXTURE_UID, "e2e note", "Kieran",
            device_id="d1", trace_id="req-e2e1",
        )
        assert row["user_id"] == FIXTURE_UID
        assert "#" in row["updated_at"]

        # Full map contains it
        assert any(
            r["user_id"] == FIXTURE_UID for r in svc.get_all_remarks()
        )

        # Update with matching token succeeds; stale token then 409s
        row2 = svc.upsert_remark(
            FIXTURE_UID, "e2e note v2", "Sammy",
            device_id="d2", trace_id="req-e2e2",
            expected_updated_at=row["updated_at"],
        )
        assert row2["note"] == "e2e note v2"
        assert row2["updated_at"] != row["updated_at"]
        with pytest.raises(svc.RemarkConflict):
            svc.upsert_remark(
                FIXTURE_UID, "blind overwrite", "Kieran",
                expected_updated_at=row["updated_at"],
            )
        # The concurrent edit survived.
        assert svc.get_remark(FIXTURE_UID)["note"] == "e2e note v2"

        # Delete → live row gone, history keeps everything
        assert svc.delete_remark(
            FIXTURE_UID, author="Sammy", device_id="d2", trace_id="req-e2e3"
        ) is True
        assert svc.get_remark(FIXTURE_UID) is None
        with risk_cases_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT action, old_note, new_note, author, device_id "
                    "FROM client_remarks_history "
                    "WHERE user_id = %s ORDER BY id",
                    (FIXTURE_UID,),
                )
                hist = [dict(r) for r in cur.fetchall()]
        # The conflicted upsert rolled back — its history row must NOT exist.
        assert [h["action"] for h in hist] == ["upsert", "upsert", "delete"]
        assert hist[0]["old_note"] is None
        assert hist[1]["old_note"] == "e2e note"
        assert hist[2]["old_note"] == "e2e note v2"
        assert hist[2]["new_note"] is None
        assert hist[2]["author"] == "Sammy"
    finally:
        _cleanup()
