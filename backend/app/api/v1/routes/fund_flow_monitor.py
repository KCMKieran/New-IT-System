"""
Routes for Frequent Fund Flow Monitor (CS 频繁出入金监控).

Endpoints (all under /cs/fund-flow):
  GET  /snapshot/latest   — Last successful weekly scan + summary
  GET  /scans             — Recent scan_history rows (selector)
  POST /scan-now          — Trigger an immediate ad-hoc scan
  POST /query             — Run ad-hoc detection on a custom window
  GET  /detail/{user_id}  — Single-client transactions + trades in window
  GET  /config            — Current rules
  POST /config            — Update rules
  GET  /export            — Streamed CSV of the latest snapshot
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import threading
import time
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse

from ....core.audit import Auditor, get_auditor
from ....core.fund_flow_monitor_db import (
    get_latest_scan,
    list_recent_scans,
    load_rules,
    save_rules,
)
from ....core.fund_flow_scheduler import get_latest_snapshot, trigger_scan_now
from ....core.singleflight import SingleFlight
from ....schemas.fund_flow_monitor import (
    FundFlowAlert,
    FundFlowConfig,
    FundFlowDetailResponse,
    FundFlowQueryRequest,
    FundFlowQueryResponse,
    FundFlowRule,
    FundFlowScanBatch,
    FundFlowSnapshot,
    FundFlowSummary,
)
from ....services.clickhouse_service import clickhouse_service
from ....services.fund_flow_detail_service import get_client_detail
from ....services.fund_flow_monitor_service import (
    compute_summary,
    iso_to_mysql_dt,
    run_detection,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/cs/fund-flow")

MAX_RULES = 10

# ── Concurrency control (page-level) ───────────────────────
#
# When many CS users hit /query simultaneously, three things protect MySQL:
#
#  1. Redis cache (5 min TTL) — same (start, end, thresholds, rule_id, user_id)
#     payload reuses an earlier response without re-running SQL.
#  2. SingleFlight — N concurrent identical requests collapse to 1 real run;
#     the other (N-1) callers block on a threading.Event and share the result.
#  3. Semaphore — at most _MAX_INFLIGHT_QUERIES requests run Phase 1+2 at the
#     same time. Excess callers wait up to _SEMAPHORE_TIMEOUT_S; if they time
#     out the API returns 503 so CS gets a fast "稍后再试" instead of a
#     30-second pending request.
#
# These together cap MySQL load even if 20 CS click "执行查询" at once.

_QUERY_CACHE_TTL_S = 300                    # 5 minutes
_MAX_INFLIGHT_QUERIES = 4                   # at most 4 real Phase 1+2 at a time
_SEMAPHORE_TIMEOUT_S = 12                   # excess callers wait at most 12s
_MAX_QUERY_RANGE_DAYS = 90                  # hard cap on /query window

_query_singleflight = SingleFlight()
_query_semaphore = threading.Semaphore(_MAX_INFLIGHT_QUERIES)


def _query_cache_key(payload: dict) -> str:
    """Hash the canonicalized payload for a stable Redis key."""
    canonical = json.dumps(payload, sort_keys=True, default=str)
    return f"app:fund_flow:query:{hashlib.md5(canonical.encode()).hexdigest()}"


def _parse_iso(value: str, field: str) -> str:
    """Validate ISO8601 and return the normalized string (with offset)."""
    raw = value.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid datetime for {field}: {value}",
        ) from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def _empty_summary() -> FundFlowSummary:
    return FundFlowSummary()


def _snapshot_to_model(snapshot: dict | None) -> FundFlowSnapshot:
    if not snapshot or not snapshot.get("batch"):
        return FundFlowSnapshot(batch=None, alerts=[], summary=_empty_summary())
    summary = snapshot.get("summary") or compute_summary(snapshot.get("alerts", []))
    return FundFlowSnapshot(
        batch=FundFlowScanBatch(**snapshot["batch"]),
        alerts=[FundFlowAlert(**a) for a in snapshot.get("alerts", [])],
        summary=FundFlowSummary(**summary),
    )


# ── Snapshot ──────────────────────────────────────────────

@router.get("/snapshot/latest", response_model=FundFlowSnapshot)
async def snapshot_latest():
    """Return the latest successful weekly scan + flagged clients."""
    snapshot = get_latest_snapshot()
    if snapshot is None:
        snapshot = get_latest_scan()
        if snapshot is not None:
            snapshot["summary"] = compute_summary(snapshot.get("alerts", []))
    return _snapshot_to_model(snapshot)


@router.get("/scans", response_model=list[FundFlowScanBatch])
async def list_scans(limit: int = Query(default=20, ge=1, le=100)):
    rows = list_recent_scans(limit=limit)
    return [FundFlowScanBatch(**r) for r in rows]


@router.post("/scan-now", response_model=FundFlowSnapshot)
async def scan_now(audit: Auditor = Depends(get_auditor)):
    """Run an immediate ad-hoc scan and return the resulting snapshot."""
    try:
        result = trigger_scan_now()
    except Exception as exc:
        logger.error("fund_flow scan-now failed: %s", exc, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="internal error while running fund-flow scan",
        ) from exc
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Another scan is already running; try again in a moment.",
        )

    # Same detection code the Monday cron runs; the difference that matters here
    # is who started it. The cron path has no actor and stays in the app log —
    # this one is a person, so it gets a row. The alert list is deliberately not
    # copied in: the scan batch is persisted and joinable by id.
    batch = result.get("batch") or {}
    audit.record(
        "fund_flow.scan.run_now",
        target=f"fund_flow_scan:{batch.get('id')}:manual",
        new_value={
            "batch_id": batch.get("id"),
            "window_start": batch.get("window_start"),
            "window_end": batch.get("window_end"),
            "total_alerts": len(result.get("alerts") or []),
        },
    )
    return _snapshot_to_model(result)


# ── Ad-hoc query ─────────────────────────────────────────

@router.post("/query", response_model=FundFlowQueryResponse)
async def ad_hoc_query(req: FundFlowQueryRequest):
    """Ad-hoc detection query. Layered concurrency protection:
    Redis cache → SingleFlight dedup → Semaphore for MySQL.

    Not audited: POST carries the filter payload, and nothing here changes state.
    """
    start_iso = _parse_iso(req.start, "start")
    end_iso = _parse_iso(req.end, "end")
    if end_iso <= start_iso:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="end must be greater than start.",
        )

    # Cap range to keep one query from monopolising MySQL.
    start_dt = datetime.fromisoformat(start_iso)
    end_dt = datetime.fromisoformat(end_iso)
    if (end_dt - start_dt).days > _MAX_QUERY_RANGE_DAYS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"日期范围最大 {_MAX_QUERY_RANGE_DAYS} 天，请缩小后再查。",
        )

    # Build the rule list. Either ref an existing rule or assemble inline.
    if req.rule_id is not None:
        rules_all = [r for r in load_rules() if int(r["id"]) == int(req.rule_id)]
        if not rules_all:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Rule {req.rule_id} not found.",
            )
        rules = rules_all
    elif req.user_id is not None:
        # Single-account lookup bypasses thresholds — run_detection handles it.
        rules = []
    else:
        rules = [
            {
                "id": 0,
                "name": "Ad-hoc inline rule",
                "enabled": True,
                "lookback_days": 0,
                "min_deposit_count": req.min_deposit_count,
                "min_withdrawal_count": req.min_withdrawal_count,
                "combine_logic": req.combine_logic,
                "max_trade_count": req.max_trade_count if req.max_trade_count is not None else 10**9,
                "min_deposit_amount_usd": req.min_deposit_amount_usd,
                "min_withdrawal_amount_usd": req.min_withdrawal_amount_usd,
            }
        ]

    # ── Cache layer ────────────────────────────────────────
    cache_payload = {
        "start": start_iso, "end": end_iso,
        "rule_id": req.rule_id, "user_id": req.user_id,
        "min_dep": req.min_deposit_count, "min_wd": req.min_withdrawal_count,
        "combine": req.combine_logic,
        "max_trade": req.max_trade_count,
        "min_dep_amt": req.min_deposit_amount_usd,
        "min_wd_amt": req.min_withdrawal_amount_usd,
    }
    cache_key = _query_cache_key(cache_payload)
    redis_client = clickhouse_service.redis_client
    if redis_client is not None:
        try:
            cached = redis_client.get(cache_key)
            if cached:
                logger.info("fund_flow query cache HIT key=%s", cache_key[-12:])
                data = json.loads(cached)
                data["from_cache"] = True
                return FundFlowQueryResponse(**data)
        except Exception as exc:
            logger.warning("Redis read failed: %s", exc)

    # ── Compute via SingleFlight + Semaphore ──────────────
    def _compute() -> dict:
        # Bound concurrent MySQL load — after _MAX_INFLIGHT_QUERIES the
        # rest wait up to _SEMAPHORE_TIMEOUT_S then 503.
        if not _query_semaphore.acquire(timeout=_SEMAPHORE_TIMEOUT_S):
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="后台繁忙，请稍后再试（>4 个查询正在处理中）",
            )
        started = time.perf_counter()
        try:
            alerts = run_detection(
                rules,
                window_start=start_iso,
                window_end=end_iso,
                user_id=req.user_id,
            )
        finally:
            _query_semaphore.release()
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        return {
            "alerts": alerts,
            "summary": compute_summary(alerts),
            "query_time_ms": elapsed_ms,
            "from_cache": False,
        }

    try:
        result = _query_singleflight.do(cache_key, _compute)
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("fund_flow ad-hoc query failed: %s", exc, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="internal error while querying fund flow",
        ) from exc

    # ── Write back to cache (only the singleflight owner reaches here for a given key) ──
    if redis_client is not None:
        try:
            redis_client.setex(
                cache_key,
                _QUERY_CACHE_TTL_S,
                json.dumps(result, default=str),
            )
        except Exception as exc:
            logger.warning("Redis write failed: %s", exc)

    return FundFlowQueryResponse(
        alerts=[FundFlowAlert(**a) for a in result["alerts"]],
        summary=FundFlowSummary(**result["summary"]),
        query_time_ms=result["query_time_ms"],
        from_cache=False,
    )


# ── Detail (single client) ───────────────────────────────

@router.get("/detail/{user_id}", response_model=FundFlowDetailResponse)
async def client_detail(
    user_id: int,
    start: str = Query(..., description="ISO8601 UTC lower bound"),
    end: str = Query(..., description="ISO8601 UTC upper bound (exclusive)"),
):
    start_iso = _parse_iso(start, "start")
    end_iso = _parse_iso(end, "end")
    if end_iso <= start_iso:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="end must be greater than start.",
        )
    try:
        payload = get_client_detail(user_id, start_iso, end_iso)
    except Exception as exc:
        logger.error("fund_flow detail failed: %s", exc, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="internal error while fetching client fund-flow detail",
        ) from exc
    return FundFlowDetailResponse(**payload)


# ── Config (rules CRUD) ──────────────────────────────────

@router.get("/config", response_model=FundFlowConfig)
async def get_config():
    rules = load_rules()
    return FundFlowConfig(rules=[FundFlowRule(**r) for r in rules])


# Columns that save_rules() reassigns on every save. It DELETEs the whole table
# and re-INSERTs, so ids and sort_order shift whenever any rule is added or
# reordered — comparing them would report a change on rules nobody touched.
_AUDIT_VOLATILE_RULE_KEYS = frozenset({"id", "sort_order"})


def _rules_by_name(rules: list[dict]) -> dict[str, dict]:
    """Key the rule list by name, so a diff reads "which rule moved", not "row 3".

    Names are not unique by schema, so a repeat gets its index appended instead
    of silently swallowing the earlier rule — a diff that loses a rule is worse
    than one with an odd-looking key.
    """
    out: dict[str, dict] = {}
    for i, rule in enumerate(rules):
        key = str(rule.get("name") or f"rule#{i}")
        if key in out:
            key = f"{key}#{i}"
        out[key] = {
            k: v for k, v in rule.items() if k not in _AUDIT_VOLATILE_RULE_KEYS
        }
    return out


@router.post("/config", response_model=FundFlowConfig)
async def update_config(
    config: FundFlowConfig,
    audit: Auditor = Depends(get_auditor),
):
    if len(config.rules) > MAX_RULES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Maximum {MAX_RULES} rules allowed.",
        )
    before = _rules_by_name(load_rules())  # gone the instant save_rules() runs
    rule_dicts = [r.model_dump(exclude={"id"}) for r in config.rules]
    save_rules(rule_dicts)
    after = load_rules()

    # The form posts every rule back on every save. Without the diff, nudging
    # one threshold writes a row per rule; with it, one row naming the rule that
    # actually changed. Both sides are read through load_rules() so the two
    # dicts are shaped identically and cannot differ by serialisation alone.
    audit.record_diff(
        "fund_flow.config.update",
        target="fund_flow_rules",
        old=before,
        new=_rules_by_name(after),
    )
    return FundFlowConfig(rules=[FundFlowRule(**r) for r in after])


# ── CSV export ───────────────────────────────────────────

def _alerts_to_csv(alerts: list[dict]) -> io.StringIO:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "user_id", "country", "name", "email", "phone", "mt_logins",
        "deposit_count", "deposit_amount_usd",
        "withdraw_count", "withdraw_amount_usd",
        "net_flow_usd", "trade_count",
        "rule_id", "rule_label", "window_start", "window_end",
    ])
    for a in alerts:
        writer.writerow([
            a.get("user_id"), a.get("country_label"), a.get("full_name") or "",
            a.get("email") or "", a.get("phone") or "", a.get("mt_logins") or "",
            a.get("deposit_count", 0), a.get("deposit_amount_usd", 0.0),
            a.get("withdraw_count", 0), a.get("withdraw_amount_usd", 0.0),
            a.get("net_flow_usd", 0.0), a.get("trade_count", 0),
            a.get("rule_id"), a.get("rule_label"),
            a.get("window_start"), a.get("window_end"),
        ])
    buf.seek(0)
    return buf


@router.get("/export")
async def export_snapshot():
    """Stream the latest snapshot as CSV."""
    snapshot = get_latest_snapshot() or get_latest_scan() or {"alerts": []}
    alerts = snapshot.get("alerts", []) if isinstance(snapshot, dict) else []
    buf = _alerts_to_csv(alerts)
    filename = "fund_flow_snapshot.csv"
    headers = {
        "Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}",
    }
    return StreamingResponse(buf, media_type="text/csv; charset=utf-8", headers=headers)
