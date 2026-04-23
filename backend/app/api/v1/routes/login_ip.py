"""REST API routes for the Login IP Monitor module.

All endpoints are mounted under `/api/v1/login-ip/*` by `api/v1/routers.py`.

Group map (matches the migration doc §6.1):
- A. Report + watchlist CRUD + scheduler ops + available-dates
- B. Manual search (Tab 3)
- C. Mail recipients CRUD
- D. Async CSV export + email verification code flow
     (watchlist WRITE ops — add / update / delete — are gated behind the
     verification code; read-only endpoints are not)

Verification code plumbing mirrors `ib_financial.py` 1:1 — same Redis client,
same admin_whitelist table (via `ib_financial_service.is_whitelisted`),
same 300-second TTL, same one-shot consumption. Only the Redis key prefix is
different (`login_ip_verify:*`) so the two modules' codes can't collide.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import random
import string
from pathlib import Path
from typing import Any

import redis
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import FileResponse

from ....core import login_ip_db
from ....core.login_ip_scheduler import trigger_download_now, trigger_report_now
from ....schemas.login_ip import (
    AdminWhitelistResponse,
    AvailableDatesResponse,
    ExportTaskCreateRequest,
    ExportTaskCreateResponse,
    ExportTaskStatusResponse,
    MailRecipientCreate,
    MailRecipientOut,
    MonitoredAccountBatchCreate,
    MonitoredAccountOut,
    MonitoredAccountUpdate,
    ReportResponse,
    RequestCodeRequest,
    SchedulerRunNowRequest,
    SchedulerRunOut,
    SchedulerRunsResponse,
    SearchRequest,
    SearchResponse,
    VerifyActionRequest,
)
from ....services import (
    ib_financial_service,
    login_ip_export_service,
    login_ip_report_service,
    login_ip_search_service,
)
from ....services.email_service import send_verification_code

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/login-ip")


# ---------------------------------------------------------------------------
# Verification code (Redis) — used by watchlist write ops
# ---------------------------------------------------------------------------

_REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
_KEY_PREFIX = "login_ip_verify"  # separate namespace from IB Financial


def _get_redis():
    return redis.Redis(host=_REDIS_HOST, port=6379, decode_responses=True)


def _cache_key(email: str, action: str) -> str:
    action_hash = hashlib.sha256(action.encode()).hexdigest()[:16]
    return f"{_KEY_PREFIX}:{email}:{action_hash}"


def _consume_code(email: str, code: str, action: str) -> None:
    """Validate & delete the code. Raises 400 on mismatch/missing."""
    r = _get_redis()
    key = _cache_key(email, action)
    stored = r.get(key)
    if not stored or stored != code:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid or expired verification code",
        )
    r.delete(key)


@router.get("/whitelist", response_model=AdminWhitelistResponse)
async def list_whitelist():
    """Read-only list of whitelisted operator emails.

    Module-local endpoint that internally delegates to IB Financial's service —
    the underlying `admin_whitelist` table is shared across risk-control
    modules, but exposing it here keeps the frontend's API surface decoupled
    (Login IP doesn't need to know about `/api/v1/ib-financial/*`).
    """
    return AdminWhitelistResponse(emails=ib_financial_service.get_admin_whitelist())


@router.post("/request-code", status_code=status.HTTP_200_OK)
async def request_code(req: RequestCodeRequest):
    """Send a 6-digit code to a whitelisted admin email.

    Reuses IB Financial's admin_whitelist — a single operator whitelist for
    all risk-control modules keeps ops surface minimal.
    """
    if not ib_financial_service.is_whitelisted(req.email):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Email not in admin whitelist",
        )
    code = "".join(random.choices(string.digits, k=6))
    r = _get_redis()
    r.setex(_cache_key(req.email, req.action), 300, code)
    send_verification_code(req.email, code)
    logger.info("login-ip: verification code sent to %s for action=%s", req.email, req.action)
    return {"message": "Verification code sent"}


@router.post("/verify-action", status_code=status.HTTP_200_OK)
async def verify_action(req: VerifyActionRequest):
    """Consume the code and execute the requested watchlist write.

    Supported actions (each must match the action used in /request-code):
    - `add_monitored_account`      payload: {account_ids, server_name, remarks?}
    - `update_monitored_account`   payload: {id, remarks}
    - `delete_monitored_account`   payload: {id}
    """
    _consume_code(req.email, req.code, req.action)
    payload = req.payload or {}

    if req.action == "add_monitored_account":
        try:
            body = MonitoredAccountBatchCreate(**payload)
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
            ) from exc
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

    if req.action == "update_monitored_account":
        row_id = payload.get("id")
        remarks = payload.get("remarks")
        if not isinstance(row_id, int):
            raise HTTPException(400, "payload.id must be int")
        ok = login_ip_db.update_remark(row_id, remarks)
        if not ok:
            raise HTTPException(404, f"monitored_account id={row_id} not found")
        return {"message": "Remark updated"}

    if req.action == "delete_monitored_account":
        row_id = payload.get("id")
        if not isinstance(row_id, int):
            raise HTTPException(400, "payload.id must be int")
        ok = login_ip_db.delete_monitored_account(row_id)
        if not ok:
            raise HTTPException(404, f"monitored_account id={row_id} not found")
        return {"message": "Monitored account deleted"}

    raise HTTPException(400, f"Unknown action: {req.action}")


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


# Watchlist WRITE endpoints are intentionally omitted — all writes go through
# POST /verify-action so ops can't sidestep the verification step.


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
