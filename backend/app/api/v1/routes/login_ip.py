"""REST API routes for the Login IP Monitor module.

All endpoints are mounted under `/api/v1/login-ip/*` by `api/v1/routers.py`.

Group map:
- A. Report + watchlist CRUD + scheduler ops + available-dates
- B. Manual search (Tab 3)
- C. Mail recipients CRUD
- D. Async CSV export (Tab 3)
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import FileResponse

from ....core import login_ip_db
from ....core.audit import Auditor, get_auditor
from ....core.auth_middleware import client_ip
from ....core.login_ip_scheduler import trigger_download_now, trigger_report_now
from ....schemas.login_ip import (
    AvailableDatesResponse,
    ExportTaskCreateRequest,
    ExportTaskCreateResponse,
    ExportTaskStatusResponse,
    LastTradeIpOut,
    LastTradeIpResponse,
    MailRecipientCreate,
    MailRecipientOut,
    MonitoredAccountBatchCreate,
    MonitoredAccountOut,
    MonitoredAccountUpdate,
    ReportResponse,
    SchedulerRunNowRequest,
    SchedulerRunOut,
    SchedulerRunsResponse,
    SearchRequest,
    SearchResponse,
)
from ....services import (
    login_ip_export_service,
    login_ip_report_service,
    login_ip_search_service,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/login-ip")


# ---------------------------------------------------------------------------
# A. Report + available dates
# ---------------------------------------------------------------------------


# Data dir for structured reports — matches login_ip_analyzer_service convention.
# __file__ = backend/app/api/v1/routes/login_ip.py → parents[4] = backend/
_DATA_BASE_DIR = Path(__file__).resolve().parents[4] / "data" / "login_ip"


@router.get("/available-dates", response_model=AvailableDatesResponse)
async def available_dates():
    """List YYYYMMDD subdirs under data/login_ip/ sorted newest first (skips `tmp/`)."""
    if not _DATA_BASE_DIR.is_dir():
        return AvailableDatesResponse(dates=[])
    dates = [
        d.name for d in _DATA_BASE_DIR.iterdir()
        if d.is_dir() and d.name != "tmp" and len(d.name) == 8 and d.name.isdigit()
    ]
    dates.sort(reverse=True)
    return AvailableDatesResponse(dates=dates)


@router.get("/report", response_model=ReportResponse)
async def get_report(date: str):
    """Return the structured correlation report for the given YYYYMMDD."""
    if len(date) != 8 or not date.isdigit():
        raise HTTPException(400, "date must be YYYYMMDD")
    day_dir = _DATA_BASE_DIR / date
    if not day_dir.is_dir():
        raise HTTPException(404, f"No analysis data for {date}")
    try:
        data = login_ip_report_service.build_structured_report(
            date, data_base_dir=_DATA_BASE_DIR
        )
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    return data


@router.get("/last-trade-ip", response_model=LastTradeIpResponse)
async def get_last_trade_ips(
    date: str | None = None,
    account_id: int | None = None,
    ip_address: str | None = None,
    limit: int = 500,
):
    """Per-account "last close order IP" rows (90-day window).

    All filters optional and ANDed. Without any filter this returns the most
    recent rows across all days, capped at `limit`.
    """
    if date is not None and (len(date) != 8 or not date.isdigit()):
        raise HTTPException(400, "date must be YYYYMMDD")
    rows = login_ip_db.search_last_trade_ips(
        trade_date=date,
        account_id=account_id,
        ip_address=ip_address,
        limit=limit,
    )
    return LastTradeIpResponse(
        rows=[LastTradeIpOut(**r) for r in rows], total=len(rows)
    )


# ---------------------------------------------------------------------------
# A. Watchlist — READ endpoints (unprotected)
# ---------------------------------------------------------------------------


# Audit helpers. These read rows the CRUD helpers in login_ip_db do not hand
# back — the audit trail needs the human-readable label ("MT5-8522845") and the
# value that is about to be overwritten, and both only exist BEFORE the write.
def _watchlist_rows(server_name: str, account_ids: list[int]) -> list[dict[str, Any]]:
    """Watchlist rows for these (server, account_id) pairs. Audit labels only."""
    if not account_ids:
        return []
    placeholders = ",".join("?" * len(account_ids))
    with login_ip_db.get_connection() as conn:
        rows = conn.execute(
            "SELECT id, account_id, server_name, remarks FROM monitored_accounts "
            f"WHERE server_name = ? AND account_id IN ({placeholders})",
            (server_name, *account_ids),
        ).fetchall()
    return [dict(r) for r in rows]


def _watchlist_row(row_id: int) -> dict[str, Any] | None:
    with login_ip_db.get_connection() as conn:
        row = conn.execute(
            "SELECT id, account_id, server_name, remarks FROM monitored_accounts "
            "WHERE id = ?",
            (row_id,),
        ).fetchone()
    return dict(row) if row else None


def _watchlist_target(row: dict[str, Any] | None, row_id: int) -> str:
    """`monitored_account:{id}:{server}-{login}` — the third segment on purpose.

    A deleted watchlist entry leaves nothing to join back to, so the label has
    to live inside the audit row or "monitored_account:42" is unreadable a year
    from now.
    """
    if not row:
        return f"monitored_account:{row_id}"
    return f"monitored_account:{row['id']}:{row['server_name']}-{row['account_id']}"


# How many account ids a batch-create row spells out before it stops. The UI is
# a paste-a-list textarea capped at 500 ids, and _stringify() truncates at 2000
# characters — which, with json sort_keys, would cut "account_ids" first and eat
# the server name and the remark along with it. Capping the list here instead
# keeps the scalars, and 50 ids is ~450 characters: comfortably inside the cap
# and far above any batch a person actually types.
_AUDIT_MAX_LISTED_ACCOUNTS = 50


def _batch_create_target(created: list[dict[str, Any]], server_name: str) -> str:
    """`monitored_account:{id}:{server}-{login}` for a single add, batch form above.

    Adding one account is the normal interaction and deserves the normal target;
    a paste of many is one act with no single subject, so it names the server and
    puts the accounts in the value.
    """
    if len(created) == 1:
        return _watchlist_target(created[0], created[0]["id"])
    return f"monitored_account:batch:{server_name}"


def _batch_create_value(created: list[dict[str, Any]], body: Any) -> dict[str, Any]:
    """One summary value for a batch add.

    ONE row per click, not one per account. The endpoint takes up to 500 ids and
    the frontend feeds it a pasted textarea, so per-account rows let a single
    click write 500 audit rows — a third of a whole YEAR's expected volume
    (§D5.1 budgets ~4/day) for one act, drowning every other entry around it.

    The act is genuinely singular: one person, one instant, one server, one
    shared remark. Nothing is lost by collapsing it, because the ids ride along
    inside `account_ids` — a normal batch is fully enumerated in this one row,
    and only a paste beyond `_AUDIT_MAX_LISTED_ACCOUNTS` gets trimmed, with
    `account_ids_omitted` saying by how much rather than silently.

    Per-account history is not the casualty either: update and delete stay one
    row each, keyed by `monitored_account:{id}`, and the watchlist table itself
    still says who is being watched.
    """
    account_ids = [row["account_id"] for row in created]
    value: dict[str, Any] = {
        "server_name": body.server_name,
        "remarks": body.remarks,
        "inserted": len(created),
        "account_ids": account_ids[:_AUDIT_MAX_LISTED_ACCOUNTS],
    }
    if len(account_ids) > _AUDIT_MAX_LISTED_ACCOUNTS:
        value["account_ids_omitted"] = len(account_ids) - _AUDIT_MAX_LISTED_ACCOUNTS
    return value


def _mail_recipient_row(recipient_id: int) -> dict[str, Any] | None:
    with login_ip_db.get_connection() as conn:
        row = conn.execute(
            "SELECT id, email, role, is_active, remarks FROM login_ip_mail_recipients "
            "WHERE id = ?",
            (recipient_id,),
        ).fetchone()
    return dict(row) if row else None


@router.get("/watchlist", response_model=list[MonitoredAccountOut])
async def list_watchlist():
    """Flat list for AG-Grid in Tab 2. Grouped shape isn't needed by the UI."""
    grouped = login_ip_db.get_monitored_accounts()
    flat: list[dict[str, Any]] = []
    for server, rows in grouped.items():
        for r in rows:
            flat.append({
                "id": r["id"],
                "account_id": r["account_id"],
                "server_name": server,
                "remarks": r.get("remarks"),
            })
    flat.sort(key=lambda r: (r["server_name"], r["account_id"]))
    return flat


@router.post("/watchlist", status_code=status.HTTP_200_OK)
async def create_watchlist_entries(
    body: MonitoredAccountBatchCreate,
    audit: Auditor = Depends(get_auditor),
):
    """Batch-add accounts (INSERT OR IGNORE dedupes)."""
    # Snapshot which ids already exist. add_monitored_accounts() reports a count
    # and nothing else, and an ignored duplicate changed no state at all — an
    # audit row for it would document a change that never happened.
    existing_ids = {r["id"] for r in _watchlist_rows(body.server_name, body.account_ids)}

    records = [
        (acc, body.server_name, body.remarks) for acc in body.account_ids
    ]
    inserted = login_ip_db.add_monitored_accounts(records)
    skipped = len(body.account_ids) - inserted

    created = [
        row
        for row in _watchlist_rows(body.server_name, body.account_ids)
        if row["id"] not in existing_ids
    ]
    if created:
        audit.record(
            "login_ip.watchlist.create",
            target=_batch_create_target(created, body.server_name),
            new_value=_batch_create_value(created, body),
        )

    return {
        "message": f"Added {inserted} account(s); skipped {skipped} duplicate(s)",
        "inserted": inserted,
        "skipped": skipped,
    }


@router.patch("/watchlist/{row_id}", status_code=status.HTTP_200_OK)
async def update_watchlist_entry(
    row_id: int,
    body: MonitoredAccountUpdate,
    audit: Auditor = Depends(get_auditor),
):
    """Update remarks only; account_id and server are immutable."""
    before = _watchlist_row(row_id)  # the old remark is gone the moment we write
    ok = login_ip_db.update_remark(row_id, body.remarks)
    if not ok:
        raise HTTPException(404, f"monitored_account id={row_id} not found")

    # record_diff, not record: the UI posts the whole row back, so re-saving a
    # remark nobody edited is common and must not leave a row claiming it moved.
    if before is not None:
        audit.record_diff(
            "login_ip.watchlist.update",
            target=_watchlist_target(before, row_id),
            old={"remarks": before["remarks"]},
            new={"remarks": body.remarks},
        )
    return {"message": "Remark updated"}


@router.delete("/watchlist/{row_id}", status_code=status.HTTP_200_OK)
async def delete_watchlist_entry(row_id: int, audit: Auditor = Depends(get_auditor)):
    before = _watchlist_row(row_id)  # after the DELETE this row is unrecoverable
    ok = login_ip_db.delete_monitored_account(row_id)
    if not ok:
        raise HTTPException(404, f"monitored_account id={row_id} not found")

    audit.record(
        "login_ip.watchlist.delete",
        target=_watchlist_target(before, row_id),
        old_value=before,
        # new_value stays NULL — that is what "deleted" means in this table.
    )
    return {"message": "Monitored account deleted"}


# ---------------------------------------------------------------------------
# A. Scheduler ops
# ---------------------------------------------------------------------------


@router.get("/scheduler/runs", response_model=SchedulerRunsResponse)
async def list_scheduler_runs(job: str | None = None, limit: int = 30):
    """Last N rows of `login_ip_scheduler_runs`. Used by UI ops timeline."""
    limit = max(1, min(200, limit))
    runs = login_ip_db.list_recent_runs(job_name=job, limit=limit)
    return SchedulerRunsResponse(runs=[SchedulerRunOut(**r) for r in runs])


# Deliberately sync (`def`, not `async def`): despite the 202 status this runs
# the whole job inline — blocking FTPS pulls of raw logs from 3 MT servers, parse,
# SQLite writes, CRM push via `requests`, then SMTP. That is minutes of blocking
# IO. FastAPI runs sync handlers in the threadpool, so one ops-triggered run
# cannot freeze the event loop for every other endpoint.
@router.post("/scheduler/run-now", status_code=status.HTTP_202_ACCEPTED)
def scheduler_run_now(
    req: SchedulerRunNowRequest,
    audit: Auditor = Depends(get_auditor),
):
    """Manually kick off a job right now (useful for ops recovery).

    Concurrency is already protected by `threading.Lock` inside
    `login_ip_scheduler.py`, so overlapping with the cron schedule is safe.
    """
    if req.job == "download":
        result = trigger_download_now(req.target_date)
    else:
        result = trigger_report_now(req.target_date)

    # Only the human-triggered path is audited; the cron firing of the same job
    # has no actor and belongs in the application log. `result is None` means
    # the lock was held and nothing ran, so nothing is recorded either.
    if result is not None:
        audit.record(
            "login_ip.job.run_now",
            target=f"login_ip_job:{req.job}:{req.target_date or 'yesterday HKT'}",
            new_value=result,
        )
    return {"job": req.job, "target_date": req.target_date, "result": result}


# ---------------------------------------------------------------------------
# B. Manual search (Tab 3)
# ---------------------------------------------------------------------------


@router.post("/search", response_model=SearchResponse)
def search(req: SearchRequest):
    """Batch search by account id or IP across the last N days of JSONs.

    Sync `def` on purpose: `perform_search()` is fully blocking (reads the
    per-day JSONs off disk, then a synchronous PyMySQL enrichment query). In an
    `async def` that work runs on the event loop and stalls every other request
    for its duration — which grows with the `days` window. FastAPI runs a plain
    `def` route in the threadpool instead.

    Deliberately NOT audited: POST here carries a filter payload, not a write.
    The project uses POST for complex queries all over, so "it is a POST" is
    never the test — "did it change state" is.
    """
    result = login_ip_search_service.perform_search(
        search_type=req.search_type,
        terms=req.terms,
        days=req.days,
        data_base_dir=_DATA_BASE_DIR,
    )
    # The service returns one of three shapes; normalize to SearchResponse.
    if "error" in result:
        return SearchResponse(error=result["error"])
    if "not_found" in result:
        return SearchResponse(not_found=result["not_found"])
    return SearchResponse(results=result.get("results", []))


# ---------------------------------------------------------------------------
# C. Mail recipients CRUD
# ---------------------------------------------------------------------------


@router.get("/mail/recipients", response_model=list[MailRecipientOut])
async def list_mail_recipients(active_only: bool = True):
    """Return raw rows (not grouped by role) for UI editing."""
    # get_mail_recipients() groups by role and loses ids, so query directly.
    with login_ip_db.get_connection() as conn:
        sql = (
            "SELECT id, email, role, is_active, remarks, created_at "
            "FROM login_ip_mail_recipients"
        )
        if active_only:
            sql += " WHERE is_active = 1"
        sql += " ORDER BY role, email"
        rows = [dict(r) for r in conn.execute(sql).fetchall()]
    return rows


@router.post("/mail/recipients", response_model=MailRecipientOut, status_code=status.HTTP_201_CREATED)
async def add_mail_recipient(
    body: MailRecipientCreate,
    audit: Auditor = Depends(get_auditor),
):
    try:
        rid = login_ip_db.add_mail_recipient(
            email=body.email, role=body.role, remarks=body.remarks
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    with login_ip_db.get_connection() as conn:
        row = conn.execute(
            "SELECT id, email, role, is_active, remarks, created_at "
            "FROM login_ip_mail_recipients WHERE id = ?",
            (rid,),
        ).fetchone()
    payload = dict(row)

    # Who receives the login-IP report is a distribution-list change: it decides
    # who sees monitored clients' login locations. Adding an address is the kind
    # of change worth being able to attribute.
    audit.record(
        "login_ip.mail_recipient.create",
        target=f"mail_recipient:{rid}:{payload['email']}",
        new_value={
            "email": payload["email"],
            "role": payload["role"],
            "remarks": payload["remarks"],
        },
    )
    return payload


@router.delete("/mail/recipients/{recipient_id}")
async def deactivate_mail_recipient(
    recipient_id: int,
    audit: Auditor = Depends(get_auditor),
):
    """Soft-delete (is_active = 0) — keeps an audit trail."""
    before = _mail_recipient_row(recipient_id)  # for the address in the label
    ok = login_ip_db.deactivate_mail_recipient(recipient_id)
    if not ok:
        raise HTTPException(404, f"Mail recipient id={recipient_id} not found")

    audit.record(
        "login_ip.mail_recipient.deactivate",
        target=f"mail_recipient:{recipient_id}:{before['email'] if before else 'unknown'}",
        old_value=before,
    )
    return {"message": "Deactivated"}


# ---------------------------------------------------------------------------
# D. Async CSV export (Tab 3 "Export" button)
# ---------------------------------------------------------------------------


def _get_client_ip(request: Request) -> str | None:
    """Thin alias over the one canonical implementation.

    This used to be a third private copy of "read X-Forwarded-For, take the
    first element, fall back to the peer". The reason that is safe here — nginx
    OVERWRITES the header rather than appending to whatever the caller sent —
    is documented once, on client_ip(). Copies of the logic are how a later fix
    to it lands in two of three places and leaves the third a spoofing hole.

    Kept as a wrapper only to preserve this column's "unknown means we truly do
    not know" semantics: client_ip() reports the string "unknown", export task
    rows have always stored NULL for that.
    """
    ip = client_ip(request)
    return None if ip == "unknown" else ip


@router.post(
    "/export/tasks",
    response_model=ExportTaskCreateResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_export(
    body: ExportTaskCreateRequest,
    request: Request,
    audit: Auditor = Depends(get_auditor),
):
    payload = login_ip_export_service.create_export_task(
        search_type=body.search_type,
        terms=body.terms,
        days=body.days,
        requested_ip=_get_client_ip(request),
    )

    # Audited even though the caller is "only reading": this is the endpoint
    # that turns login-IP history into a file that leaves the system. The export
    # task row already stores the IP, but not who — the row below is the who.
    audit.record(
        "login_ip.export.create",
        target=f"export_task:{payload['task_id']}:{body.search_type}",
        new_value={
            "search_type": body.search_type,
            "terms": body.terms,
            "days": body.days,
        },
    )
    return ExportTaskCreateResponse(
        task_id=payload["task_id"],
        status="queued",
        created_at=payload["created_at"],
    )


@router.get("/export/tasks/{task_id}", response_model=ExportTaskStatusResponse)
async def get_export_status(task_id: str):
    payload = login_ip_export_service.get_task_status(task_id)
    if not payload:
        raise HTTPException(404, "Export task not found")
    return payload


@router.get("/export/tasks/{task_id}/download")
async def download_export(task_id: str):
    http_status, body, filename = login_ip_export_service.resolve_download_path(task_id)
    if http_status != 200:
        raise HTTPException(http_status, body)
    return FileResponse(
        path=body,
        media_type="text/csv",
        filename=filename,
    )
