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

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse

from ....core.audit import Auditor, get_auditor
from ....core.config import get_settings
from ....core.data_scope import (
    caller_cids,
    cid_for_crm_user_ids,
    require_cids_allowed,
    scope_cache_suffix,
)
from ....core.fund_flow_monitor_db import (
    count_alerts_by_batch,
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
    filter_alerts_to_scope,
    iso_to_mysql_dt,
    labels_for_cids,
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


def _query_cache_key(payload: dict, scope_suffix: str) -> str:
    """Hash the canonicalized payload for a stable Redis key, scoped to the caller.

    ``scope_suffix`` is NOT optional and is deliberately a required positional
    argument: a default would let a future caller reintroduce the shared key by
    omission, silently and with a passing test suite.

    Identity used to be absent from this key, and that alone defeats the whole
    row filter. Two callers sending the same filters shared one cache entry, so
    the first UNRESTRICTED colleague to run a query warmed Redis with the
    firm-wide result — CN rows included — and the next restricted caller sending
    the same payload was served that entry verbatim. No filtered query ever ran,
    nothing was logged, nothing 403'd, and a unit test exercising one user at a
    time could not see it. The leak's schedule was set by whoever queried first.

    The suffix rides OUTSIDE the md5 rather than inside the hashed payload so a
    human reading Redis (or the SingleFlight coalescing log line, which prints
    key[:50]) can tell at a glance which scope an entry belongs to. Hiding it in
    the digest would work identically and be unauditable.

    The same string is used for SingleFlight. That is not tidiness: coalescing
    two in-flight requests from differently-scoped callers onto one result is
    the identical bug with a shorter window, so the two keys must never diverge.
    """
    canonical = json.dumps(payload, sort_keys=True, default=str)
    return (
        f"app:fund_flow:query:{scope_suffix}:"
        f"{hashlib.md5(canonical.encode()).hexdigest()}"
    )


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


def _scope_snapshot(snapshot: dict | None, cids: frozenset[int] | None) -> dict | None:
    """Return a snapshot narrowed to ``cids``. ``None`` cids = unrestricted.

    Returns the SAME object when unrestricted — the 99% path allocates nothing
    and their response stays byte-identical.

    Builds a NEW dict when it does filter, and this is load-bearing rather than
    stylistic: ``fund_flow_scheduler.get_latest_snapshot()`` hands back the
    module-level ``_latest_snapshot`` object itself, shared by every request and
    by the weekly cron. Filtering it in place would let one restricted caller
    permanently delete CN alerts from the copy everybody else is served, until
    the next scan or process restart. The nested ``batch`` dict is copied for
    the same reason.

    ``batch.total_alerts`` is recomputed as the length of the filtered list, not
    fetched. For a snapshot that is exact by construction — ``get_latest_scan()``
    selects ALL alert rows for the batch, and ``finish_scan_batch`` stored
    ``len(alerts)`` as the total — and it guarantees the property that matters:
    the headline number always equals the number of rows the reader can count,
    so there is no difference left to subtract. /scans has no rows in hand and
    has to ask the database instead (``count_alerts_by_batch``).
    """
    if cids is None or not snapshot:
        return snapshot

    alerts = filter_alerts_to_scope(snapshot.get("alerts", []), cids)
    scoped: dict = dict(snapshot)
    scoped["alerts"] = alerts
    scoped["summary"] = compute_summary(alerts)
    batch = snapshot.get("batch")
    if batch:
        scoped_batch = dict(batch)
        scoped_batch["total_alerts"] = len(alerts)
        scoped["batch"] = scoped_batch
    return scoped


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
async def snapshot_latest(request: Request):
    """Return the latest successful weekly scan + flagged clients."""
    snapshot = get_latest_snapshot()
    if snapshot is None:
        snapshot = get_latest_scan()
        if snapshot is not None:
            snapshot["summary"] = compute_summary(snapshot.get("alerts", []))
    # Row-level country scope. No-op (and no copy) for an unrestricted caller.
    return _snapshot_to_model(_scope_snapshot(snapshot, caller_cids(request)))


@router.get("/scans", response_model=list[FundFlowScanBatch])
# Sync `def`, not `async def`: everything below is a blocking SQLite read
# (list_recent_scans / count_alerts_by_batch), and a blocking read inside an
# `async def` runs ON the event loop and stalls every other request in this
# worker for its duration. Sync handlers are dispatched to the threadpool.
def list_scans(request: Request, limit: int = Query(default=20, ge=1, le=100)):
    """Recent scan batches for the selector.

    Scoped even though it returns no client rows, because ``total_alerts`` is a
    firm-wide aggregate: leave it whole next to a filtered alert list and the
    reader recovers the CN count by subtraction. A filtered list beside an
    unfiltered total is a subtraction away from being no filter at all.
    """
    rows = list_recent_scans(limit=limit)

    cids = caller_cids(request)
    if cids is None:
        return [FundFlowScanBatch(**r) for r in rows]

    # One grouped COUNT for the whole page, not one per row: this endpoint
    # returns up to 100 batches and the SQLite file is also written by the
    # weekly scan.
    scoped_totals = count_alerts_by_batch(
        [int(r["id"]) for r in rows], labels_for_cids(cids)
    )
    out: list[FundFlowScanBatch] = []
    for r in rows:
        row = dict(r)
        # Absent from the map means "no rows of yours in that batch". Zero is
        # the truthful answer for an all-CN batch, a still-running one, and a
        # failed one alike — and it must NOT fall back to the firm-wide total.
        row["total_alerts"] = scoped_totals.get(int(r["id"]), 0)
        out.append(FundFlowScanBatch(**row))
    return out


@router.post("/scan-now", response_model=FundFlowSnapshot)
async def scan_now(request: Request, audit: Auditor = Depends(get_auditor)):
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
    # The SCAN is firm-wide and stays that way — a restricted user kicking one
    # off must not persist a snapshot that is missing CN alerts for everybody
    # else, and the audit row above deliberately records what the scan DID, not
    # what this caller was shown. Only the RESPONSE is narrowed, and only into a
    # copy: `result` is the same object fund_flow_scheduler keeps as its cached
    # `_latest_snapshot`.
    return _snapshot_to_model(_scope_snapshot(result, caller_cids(request)))


# ── Ad-hoc query ─────────────────────────────────────────

@router.post("/query", response_model=FundFlowQueryResponse)
async def ad_hoc_query(request: Request, req: FundFlowQueryRequest):
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
    # Two callers with different data scopes must never share a cache entry;
    # see _query_cache_key(). Resolved once and reused for the filter below so
    # the key and the rows cannot disagree about who is asking.
    cids = caller_cids(request)
    cache_key = _query_cache_key(cache_payload, scope_cache_suffix(request))
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
        # Belt and braces with the scoped cache key above. Filtering HERE means
        # what gets written to Redis under a scoped key is already scoped, so a
        # future refactor that loses the suffix leaks nothing that this compute
        # produced — the two defences fail independently.
        #
        # Note this also covers req.user_id (the single-account lookup): /query
        # is classified FILTER, so a restricted caller naming a CN client gets
        # an empty result rather than a 403. /detail is the LOOKUP that refuses.
        alerts = filter_alerts_to_scope(alerts, cids)
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
# Sync `def` — this handler makes TWO blocking MySQL round trips: the scope
# resolver below (`cid_for_crm_user_ids`, connect_timeout=5 + read_timeout=20)
# and `get_client_detail`. On the event loop that is up to ~25s of total stall
# for every other request this worker is serving, from one restricted caller
# clicking one row (OPT-0055 measured a 1.3s endpoint dragged to 2.7-5.2s by
# far less). Every callee here is sync; nothing is awaited.
def client_detail(
    request: Request,
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

    # A LOOKUP, not a filter: the caller already named the client, so the only
    # place a decision can be made is BEFORE the query. Checking the
    # `country_label` that get_client_detail() returns would mean the CN
    # client's transactions and trades had already been read out of MySQL, and
    # would answer 403 only after paying for the answer.
    #
    # The resolver is skipped entirely when unrestricted — this is an extra
    # MySQL round trip on a page that is otherwise nobody's exception.
    if caller_cids(request) is not None:
        resolved = cid_for_crm_user_ids(get_settings(), [user_id])
        require_cids_allowed(
            request, resolved.get(user_id), what=f"client {user_id}"
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
    # BOM first: country/name columns carry Chinese, and Excel decodes a
    # BOM-less UTF-8 CSV as the system codepage -> mojibake. Same purpose as
    # the `utf-8-sig` encoding used by login_ip_export_service.py.
    buf.write("\ufeff")
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
async def export_snapshot(request: Request):
    """Stream the latest snapshot as CSV."""
    snapshot = get_latest_snapshot() or get_latest_scan() or {"alerts": []}
    alerts = snapshot.get("alerts", []) if isinstance(snapshot, dict) else []
    # Filtered BEFORE the CSV is built. Once _alerts_to_csv has run the rows are
    # a flat text buffer and the only way back is to re-parse it, so this is the
    # last point where a row can be dropped — and an export is the one artefact
    # here that outlives the request and gets forwarded around.
    alerts = filter_alerts_to_scope(alerts, caller_cids(request))
    buf = _alerts_to_csv(alerts)
    filename = "fund_flow_snapshot.csv"
    headers = {
        "Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}",
    }
    return StreamingResponse(buf, media_type="text/csv; charset=utf-8", headers=headers)
