"""
Routes for IB Financial Monitor.

Provides endpoints for:
- Watchlist management (list / add / remove IBs)
- Real-time financial data query
- Report configuration (email recipients, schedule)
- Email verification for protected operations
- Manual report sending
- Audit log viewing
"""

from __future__ import annotations

import hashlib
import logging
import os
import random
import string
from datetime import date

import redis
from fastapi import APIRouter, Depends, HTTPException, status

from ....core.config import Settings, get_settings
from ....schemas.ib_financial import (
    AddIBRequest,
    AdminWhitelistResponse,
    AuditLogResponse,
    FinancialQueryResponse,
    FinancialRecord,
    RemoveIBRequest,
    ReportConfigResponse,
    ReportConfigUpdate,
    RequestCodeRequest,
    VerifyActionRequest,
    WatchlistItem,
    WatchlistResponse,
)
from ....services import ib_financial_service as svc
from ....core.scheduler import reschedule
from ....services.email_service import send_email, send_verification_code

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/ib-financial")

_REDIS_HOST = os.getenv("REDIS_HOST", "localhost")

def _get_redis():
    return redis.Redis(host=_REDIS_HOST, port=6379, decode_responses=True)


# ── Watchlist ─────────────────────────────────────────────

@router.get("/watchlist", response_model=WatchlistResponse)
async def list_watchlist():
    items = svc.get_watchlist()
    return WatchlistResponse(
        items=[WatchlistItem(**item) for item in items],
        total=len(items),
    )


# ── Financial Query ───────────────────────────────────────

@router.get("/query", response_model=FinancialQueryResponse)
async def query_financial(
    target_date: date | None = None,
    settings: Settings = Depends(get_settings),
):
    """Query financial data for all active watchlist IBs on a given date."""
    try:
        date_str, records = svc.query_financial_data(settings, target_date)
        return FinancialQueryResponse(
            date=date_str,
            records=[FinancialRecord(**r) for r in records],
            total=len(records),
        )
    except Exception as exc:
        logger.error(f"Financial query failed: {exc}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        ) from exc


# ── Report Config ─────────────────────────────────────────

@router.get("/config", response_model=ReportConfigResponse)
async def get_config():
    cfg = svc.get_report_config()
    return ReportConfigResponse(**cfg)


# ── Verification Flow ─────────────────────────────────────

@router.post("/request-code", status_code=status.HTTP_200_OK)
async def request_code(req: RequestCodeRequest):
    """Send a 6-digit verification code to a whitelisted admin email."""
    if not svc.is_whitelisted(req.email):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Email not in admin whitelist",
        )

    code = "".join(random.choices(string.digits, k=6))

    r = _get_redis()
    action_hash = hashlib.sha256(req.action.encode()).hexdigest()[:16]
    cache_key = f"ib_fin_verify:{req.email}:{action_hash}"
    r.setex(cache_key, 300, code)

    send_verification_code(req.email, code)
    logger.info(f"Verification code sent to {req.email} for action: {req.action}")
    return {"message": "Verification code sent"}


@router.post("/verify-action", status_code=status.HTTP_200_OK)
async def verify_action(req: VerifyActionRequest):
    """Verify code and execute the requested action (add/remove IB, update config)."""
    r = _get_redis()
    action_hash = hashlib.sha256(req.action.encode()).hexdigest()[:16]
    cache_key = f"ib_fin_verify:{req.email}:{action_hash}"

    stored_code = r.get(cache_key)
    if not stored_code or stored_code != req.code:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid or expired verification code",
        )
    # Code used — delete immediately
    r.delete(cache_key)

    payload = req.payload or {}

    try:
        if req.action.startswith("batch_add_ib"):
            ibs = payload.get("ibs", [])
            if not ibs:
                raise ValueError("No IBs provided")
            added, skipped = svc.batch_add_ib(ibs=ibs, operator=req.email)
            msg = f"成功添加 {added} 个 IB"
            if skipped:
                msg += f"，跳过已存在: {', '.join(skipped)}"
            return {"message": msg, "added": added, "skipped": skipped}

        elif req.action.startswith("add_ib"):
            svc.add_ib(
                ib_id=payload.get("ib_id", ""),
                ib_name=payload.get("ib_name"),
                operator=req.email,
            )
            return {"message": f"IB {payload.get('ib_id')} added"}

        elif req.action.startswith("remove_ib"):
            svc.remove_ib(ib_id=payload.get("ib_id", ""), operator=req.email)
            return {"message": f"IB {payload.get('ib_id')} removed"}

        elif req.action.startswith("update_config"):
            result = svc.update_report_config(payload, operator=req.email)
            reschedule()
            return {"message": "Config updated", "config": result}

        else:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Unknown action: {req.action}",
            )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc


# ── Manual Send Report ────────────────────────────────────

@router.post("/send-report", status_code=status.HTTP_200_OK)
async def send_report(
    target_date: date | None = None,
    settings: Settings = Depends(get_settings),
):
    """Manually trigger the IB financial report email."""
    cfg = svc.get_report_config()
    if not cfg.get("mail_to"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No recipients configured. Update report config first.",
        )

    date_str, records = svc.query_financial_data(settings, target_date)

    # Build simple HTML table for email body
    html = _build_report_html(date_str, records)

    send_email(
        subject=f"CS Report - IB Financial - {date_str}",
        body=html,
        to=cfg["mail_to"],
        cc=cfg.get("mail_cc"),
    )
    return {"message": f"Report sent for {date_str}", "recipients": cfg["mail_to"]}


# ── Audit Log ─────────────────────────────────────────────

@router.get("/audit-log", response_model=AuditLogResponse)
async def list_audit_log(limit: int = 50):
    entries = svc.get_audit_log(limit)
    return AuditLogResponse(
        entries=entries,
        total=len(entries),
    )


# ── Admin Whitelist ───────────────────────────────────────

@router.get("/whitelist", response_model=AdminWhitelistResponse)
async def list_whitelist():
    return AdminWhitelistResponse(emails=svc.get_admin_whitelist())


# ── Helpers ───────────────────────────────────────────────

def _build_report_html(
    date_str: str, records: list[dict], *, is_scheduled: bool = False
) -> str:
    """Build a simple HTML table for the email report body."""
    if not records:
        return f"<p>No data found for {date_str}.</p>"

    header_map = {
        "ib_name": "IB",
        "currency": "Currency",
        "today_deposit": "今天入金",
        "today_withdrawal": "今天出金",
        "total_deposit": "总入金",
        "total_withdrawal": "总出金",
        "mt4_equity": "MT4净值",
        "ib_wallet_equity": "IB钱包净值",
        "difference": "差异",
    }
    columns = list(header_map.keys())

    rows_html = ""
    for rec in records:
        cells = "".join(
            f"<td style='padding:6px 10px;border:1px solid #ddd;text-align:right;'>"
            f"{rec.get(col, '')}</td>"
            for col in columns
        )
        rows_html += f"<tr>{cells}</tr>"

    headers_html = "".join(
        f"<th style='padding:6px 10px;border:1px solid #ddd;background:#f5f5f5;"
        f"text-align:center;'>{header_map[col]}</th>"
        for col in columns
    )

    if is_scheduled:
        footer = "此邮件由 KCM Analysis 系统每日 17:00 (HKT) 自动发送"
    else:
        footer = "此邮件由 KCM Analysis 系统手动发送"

    return f"""
    <div style="font-family:Arial,sans-serif;">
        <h3>IB Financial Report — {date_str}</h3>
        <table style="border-collapse:collapse;font-size:13px;">
            <thead><tr>{headers_html}</tr></thead>
            <tbody>{rows_html}</tbody>
        </table>
        <p style="color:#999;font-size:11px;margin-top:12px;">
            {footer}
        </p>
    </div>
    """
