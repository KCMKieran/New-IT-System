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

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import FileResponse

from ....core import login_ip_db
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
async def create_watchlist_entries(body: MonitoredAccountBatchCreate):
    """Batch-add accounts (INSERT OR IGNORE dedupes)."""
    records = [
        (acc, body.server_name, body.remarks) for acc in body.account_ids
    ]
    inserted = login_ip_db.add_monitored_accounts(records)
    skipped = len(body.account_ids) - inserted
    return {
        "message": f"Added {inserted} account(s); skipped {skipped} duplicate(s)",
        "inserted": inserted,
        "skipped": skipped,
    }


@router.patch("/watchlist/{row_id}", status_code=status.HTTP_200_OK)
async def update_watchlist_entry(row_id: int, body: MonitoredAccountUpdate):
    """Update remarks only; account_id and server are immutable."""
    ok = login_ip_db.update_remark(row_id, body.remarks)
    if not ok:
        raise HTTPException(404, f"monitored_account id={row_id} not found")
    return {"message": "Remark updated"}


@router.delete("/watchlist/{row_id}", status_code=status.HTTP_200_OK)
async def delete_watchlist_entry(row_id: int):
    ok = login_ip_db.delete_monitored_account(row_id)
    if not ok:
        raise HTTPException(404, f"monitored_account id={row_id} not found")
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


@router.post("/scheduler/run-now", status_code=status.HTTP_202_ACCEPTED)
async def scheduler_run_now(req: SchedulerRunNowRequest):
    """Manually kick off a job right now (useful for ops recovery).

    Concurrency is already protected by `threading.Lock` inside
    `login_ip_scheduler.py`, so overlapping with the cron schedule is safe.
    """
    if req.job == "download":
        result = trigger_download_now(req.target_date)
    else:
        result = trigger_report_now(req.target_date)
    return {"job": req.job, "target_date": req.target_date, "result": result}


# ---------------------------------------------------------------------------
# B. Manual search (Tab 3)
# ---------------------------------------------------------------------------


@router.post("/search", response_model=SearchResponse)
async def search(req: SearchRequest):
    """Batch search by account id or IP across the last N days of JSONs."""
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
async def add_mail_recipient(body: MailRecipientCreate):
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
    return dict(row)


@router.delete("/mail/recipients/{recipient_id}")
async def deactivate_mail_recipient(recipient_id: int):
    """Soft-delete (is_active = 0) — keeps an audit trail."""
    ok = login_ip_db.deactivate_mail_recipient(recipient_id)
    if not ok:
        raise HTTPException(404, f"Mail recipient id={recipient_id} not found")
    return {"message": "Deactivated"}


# ---------------------------------------------------------------------------
# D. Async CSV export (Tab 3 "Export" button)
# ---------------------------------------------------------------------------


def _get_client_ip(request: Request) -> str | None:
    # X-Forwarded-For has priority (behind nginx); fall back to direct peer.
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else None


@router.post(
    "/export/tasks",
    response_model=ExportTaskCreateResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_export(body: ExportTaskCreateRequest, request: Request):
    payload = login_ip_export_service.create_export_task(
        search_type=body.search_type,
        terms=body.terms,
        days=body.days,
        requested_ip=_get_client_ip(request),
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
