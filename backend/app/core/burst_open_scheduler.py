"""
APScheduler integration for Burst Open Detection (批量下单) periodic scanning.

Runs a background scan every `scan_interval_min` minutes, stores the latest
result in memory for fast API reads, and persists each scan to SQLite history.

Controlled by BURST_SCAN_ENABLED env var (default: "true").
Set to "false" in dev if you don't want background scans running.
"""

from __future__ import annotations

import logging
import os
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

logger = logging.getLogger(__name__)

JOB_ID = "burst_open_scan"
# OPT-0012: fast tier dedicated job id. Coexists with JOB_ID when
# BURST_FAST_TIER_ENABLED=true — fast tier runs only burst-open (60s),
# JOB_ID/_run_scan switches to slow-only mode (skips burst).
BURST_FAST_JOB_ID = "burst_open_fast_tier"
BURST_FAST_TIER_INTERVAL_SEC = 60
GAP_TRADE_JOB_ID = "gap_trade_daily_scan"
# OPT-0032: intraday gap-trade tier — ~5min scans inside the HKT market-open
# window so excess-profit clients get CRM-tagged before their withdrawal
# auto-approves, instead of waiting for the 07:20 final scan.
GAP_TRADE_INTRADAY_JOB_ID = "gap_trade_intraday_scan"
# Internal window guard (HKT, inclusive). The CronTrigger is bounded to
# hour 5-7 as a first fence; this guard does minute precision. Two layers on
# purpose — a trigger or timezone bug must not turn a money-impacting job
# into an all-day loop.
GAP_TRADE_INTRADAY_START_HKT = (5, 55)
GAP_TRADE_INTRADAY_END_HKT = (7, 5)

_scheduler: BackgroundScheduler | None = None
_latest_result: dict[str, Any] | None = None
_scan_lock = threading.Lock()
# OPT-0037 observability: a fast tick that finds the shared lock held (slow tier
# mid-flight) skips itself. At scale, frequent skips mean the fast tier is
# silently running below its 60s cadence — exactly when the settle-window blind
# spot reopens — so we count + log them at INFO instead of swallowing at DEBUG.
_fast_tier_skip_count = 0
# OPT-0038 R2: adaptive look-back for the two event-gated rules on the fast tier.
# We remember when the event-gated section last ran; the next tick's opens
# look-back = (now − last_scan) + buffer, so a skipped/late/long tick widens the
# next window instead of letting an open age out of the fixed ~180s coverage
# unseen (silent miss). Reset to None on process start → cold-start tick uses one
# cadence + buffer.
_event_gated_last_scan_at: datetime | None = None
_EVENT_GATED_LOOKBACK_BUFFER_SEC = 120
# Cap so a long outage / cold start can't fire a multi-hour opens scan; beyond
# this we accept that opens older than the cap during downtime are not re-scanned
# (they'd be stale snapshots anyway). 30 min comfortably covers normal skip runs.
_EVENT_GATED_MAX_LOOKBACK_SEC = 1800
# Independent lock so the Gap Trade daily job never blocks the 5-min burst
# tick (or vice versa). They write to the same `alert_events` table but
# touch different rule_id ranges, so concurrent runs are safe.
# OPT-0032: the intraday tier shares THIS lock — intraday ticks skip when
# held (non-blocking), the 07:20 final scan acquires it blocking-with-timeout
# so a long intraday tick can never silently cancel the authoritative daily
# scan, and intraday/final CRM read-modify-writes never interleave.
_gap_trade_lock = threading.Lock()
# How long the 07:20 final scan waits for an in-flight intraday tick before
# giving up with an ERROR (worst observed scan ≈ minutes; MySQL read_timeout
# is 600s, so 300s covers everything but a truly hung query).
_GAP_TRADE_FINAL_LOCK_TIMEOUT_SEC = 300


def _fast_tier_enabled() -> bool:
    # Default OFF — opt-in via env. Reads each call so test fixtures and
    # rolling restarts can toggle without re-importing the module.
    return os.getenv("BURST_FAST_TIER_ENABLED", "false").lower() == "true"


# ─── Tier ownership: SINGLE SOURCE OF TRUTH (OPT-0037) ────────────────────
# Which scheduler tier owns each allocated rule_id band. The cache-merge in
# _run_scan partitions `_latest_result.alerts` by this map, so it MUST stay in
# lockstep with the `include_*` flags in _run_scan (a band whose include_* runs
# on one tier but is classified to the other here will be dropped/duplicated in
# the cache on each tick).
#
# ⚠ ADDING A NEW RULE BAND (121+): add its (lo, hi) to exactly ONE tuple below
# AND wire its include_* flag in _run_scan to the matching tier. The total
# allocated range is [1, _MAX_ALLOCATED_RULE_ID]; bump that too. The invariant
# test `test_tier_ownership_partitions_all_bands` fails until all three agree.
_FAST_TIER_RULE_BANDS: tuple[tuple[int, int], ...] = ((1, 50), (101, 120))
_SLOW_TIER_RULE_BANDS: tuple[tuple[int, int], ...] = ((51, 100),)
# Highest rule_id any band currently reaches (martingale tops out at 120).
_MAX_ALLOCATED_RULE_ID = 120


def _is_fast_tier_rule_id(rule_id: Any) -> bool:
    """Which tier owns a rule_id's alerts for the in-memory cache merge.

    Fast tier (60s) owns burst (1-50) PLUS the two event-gated rules whose
    snapshot read must happen close to the open or it misses positions that
    open-and-close within the settle window (OPT-0037 blind spot): leverage
    abuse (101-110) + martingale (111-120). Slow tier owns quick-open-close
    (51-60) + quick-profit (61-70) + hedge-open (91-100). Gap Trade (71-90)
    never enters this cache (its cron path writes straight to alert_events),
    so it falls on the slow side and is a no-op here. Derived from
    `_FAST_TIER_RULE_BANDS` so the boundary has one source of truth.
    """
    rid = int(rule_id or 0)
    return any(lo <= rid <= hi for lo, hi in _FAST_TIER_RULE_BANDS)


def _gap_trade_intraday_enabled() -> bool:
    # Default OFF — dev containers run their own scheduler against the same
    # .env (flock election is per-container), so this must be opt-in per
    # environment to keep dev from double-scanning the market open.
    return os.getenv("GAP_TRADE_INTRADAY_ENABLED", "false").lower() == "true"


def _in_intraday_window_hkt(now_hkt_h: int, now_hkt_m: int) -> bool:
    """Pure window check, unit-tested for the boundary minutes."""
    hm = (now_hkt_h, now_hkt_m)
    return GAP_TRADE_INTRADAY_START_HKT <= hm <= GAP_TRADE_INTRADAY_END_HKT


def _build_quick_profit_prev_alerts(
    latest_result: dict[str, Any] | None,
    qp_rules: list[dict[str, Any]],
    fetch_recent: Callable[[int], list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    """Build the prev_alerts pool for Quick Profit cross-scan dedup.

    Always merges in-memory ``latest_result.alerts`` with the most recent
    SQLite rows in the largest rule lookback window. ``_dedup_by_time_bucket``
    keeps the newest scanned_at per (rule_id, server, login, symbol), so a
    plain concatenation is safe.

    Why unconditional: ``latest_result.alerts`` only carries the last tick's
    EMITTED alerts. Accounts emitted earlier and then suppressed in the last
    tick are missing from it, so any tick that emits ≥1 new QP alert would
    otherwise blind dedup to older same-key alerts that are still in their
    lookback window. The earlier "seed only when no QP in memory" branch
    silently broke whenever new + old QP alerts coexisted within a tick.
    """
    prev = list((latest_result or {}).get("alerts") or [])
    if not qp_rules:
        return prev
    try:
        seed_minutes = max(int(r["lookback_min"]) for r in qp_rules)
    except (KeyError, ValueError, TypeError):
        return prev
    try:
        return prev + fetch_recent(seed_minutes)
    except Exception:
        logger.warning("Quick profit dedup seed failed", exc_info=True)
        return prev


def _run_scan(*, tier: str = "all") -> None:
    """Execute one scan cycle.

    OPT-0012 / OPT-0037 tier modes:
    - 'all'         → legacy, runs every detector (default when fast tier
                      disabled; preserves prior behavior; also the scan-now path)
    - 'fast_burst'  → burst + leverage-abuse + martingale (called by fast tier
                      60s job). The two event-gated rules joined the fast tier in
                      OPT-0037 so their margin/position snapshot is read close to
                      the open (settle 30s + 60s cadence ⇒ holds >~90s reliably
                      caught), shrinking the "closed before settle" blind spot.
    - 'slow'        → quick_oc + quick_profit + hedge only (skips the rules the
                      fast tier owns)

    Reads config from SQLite, runs SQL + rule engine, merges into the
    in-memory cache, writes to scan_history, and cleans up old records.
    """
    global _latest_result, _event_gated_last_scan_at

    from ..core.config import get_settings
    from ..core.risk_monitor_db import (
        append_scan_and_events,
        get_recent_quick_profit_alerts,
        load_config,
        load_hedge_open_config,
        load_leverage_abuse_config,
        load_martingale_config,
        load_quick_open_close_config,
        load_quick_profit_config,
    )
    from ..services.rule_hedge_open_service import scan_hedge_open
    from ..services.rule_leverage_abuse_service import scan_leverage_abuse
    from ..services.rule_martingale_service import scan_martingale
    from ..services.rule_quick_open_close_service import scan_quick_open_close
    from ..services.rule_quick_profit_service import scan_quick_profit
    from ..services.risk_monitor_service import scan_burst_open

    include_burst = tier in ("all", "fast_burst")
    include_quick_oc = tier in ("all", "slow")
    include_qp = tier in ("all", "slow")
    # Hedge Open (rule_id 91-100): goes in slow tier next to QOC + QP.
    # Wash-trading detection doesn't need sub-minute responsiveness; 5-10
    # min cadence matches its analyst-followup mental model.
    include_hedge = tier in ("all", "slow")
    # Leverage Abuse (rule_id 101-110): event-gated (OPT-0030 Phase 2) — finds
    # recently-OPENED accounts and reads their margin level at open. OPT-0037
    # moved it to the FAST tier (60s): the snapshot must be read close to the
    # open, else an open-then-close inside the settle window reads a flat
    # account and the abuse is missed. 'all' branch lets scan-now refresh it.
    include_leverage = tier in ("all", "fast_burst")
    # Martingale (rule_id 111-120): event-gated like Leverage Abuse — a recent
    # add gates evaluation against the open-position snapshot. OPT-0037 moved it
    # to the FAST tier too (an add-then-close inside the settle window left the
    # snapshot empty → the whole ladder vanished). 'all' lets scan-now refresh.
    include_martingale = tier in ("all", "fast_burst")

    try:
        config = load_config()
        quick_config = load_quick_open_close_config()
        qp_config = load_quick_profit_config()
        hedge_config = load_hedge_open_config()
        leverage_config = load_leverage_abuse_config()
        martingale_config = load_martingale_config()
        settings = get_settings()

        # OPT-0037: the two event-gated rules now run on the 60s fast tier. Feed
        # them a 1-min cadence so their opens look-back shrinks to ~180s
        # (1*60 + _LOOKBACK_BUFFER_SEC) instead of re-scanning a full 5-min
        # window every 60s — the overlap is still de-duped on open_time, this
        # just trims redundant MySQL reads. scan-now ('all') keeps the full
        # configured interval so a manual refresh covers a wider window.
        event_gated_interval_min = (
            1 if tier == "fast_burst" else int(config["scan_interval_min"])
        )
        # OPT-0038 R2: on the fast tier, replace the fixed ~180s window with an
        # adaptive look-back = (now − last successful event-gated scan) + buffer,
        # capped. A skipped/late tick (lock contention, slow query, restart) thus
        # auto-widens the next window so no open slips past unseen. Other tiers
        # ('all' scan-now, 'slow') keep the fixed window → override stays None.
        event_gated_lookback_sec: int | None = None
        now_tick: datetime | None = None
        if tier == "fast_burst":
            now_tick = datetime.now(timezone.utc)
            if _event_gated_last_scan_at is None:
                gap_sec = float(BURST_FAST_TIER_INTERVAL_SEC)  # cold start
            else:
                gap_sec = (now_tick - _event_gated_last_scan_at).total_seconds()
            event_gated_lookback_sec = int(min(
                max(gap_sec, BURST_FAST_TIER_INTERVAL_SEC)
                + _EVENT_GATED_LOOKBACK_BUFFER_SEC,
                _EVENT_GATED_MAX_LOOKBACK_SEC,
            ))
            logger.debug(
                "Event-gated adaptive lookback: %ds (gap since last scan %.0fs)",
                event_gated_lookback_sec, gap_sec,
            )

        # Always merge SQLite-recent QP alerts into the dedup pool — see
        # `_build_quick_profit_prev_alerts` for why in-memory alone is unsafe.
        prev_alerts = _build_quick_profit_prev_alerts(
            _latest_result,
            qp_config.get("rules") or [],
            get_recent_quick_profit_alerts,
        )

        burst_result: dict[str, Any] | None = None
        if include_burst:
            burst_result = scan_burst_open(
                settings,
                scan_interval_min=config["scan_interval_min"],
                rules=config["rules"],
                previous_alerts=prev_alerts,
            )
        quick_result: dict[str, Any] | None = None
        if include_quick_oc and quick_config.get("enabled", True):
            try:
                quick_result = scan_quick_open_close(
                    settings,
                    scan_interval_min=config["scan_interval_min"],
                    rules=quick_config.get("rules", []),
                    previous_alerts=prev_alerts,
                )
            except Exception:
                logger.error("Quick open-close scan failed", exc_info=True)

        # Quick Profit decouples its lookback from scan_interval — it picks
        # max(rule.lookback_min) inside the service, so the scheduler just
        # forwards the rules and the previous-alert dedup pool.
        qp_result: dict[str, Any] | None = None
        if include_qp and qp_config.get("enabled", True) and qp_config.get("rules"):
            try:
                qp_result = scan_quick_profit(
                    settings,
                    rules=qp_config["rules"],
                    previous_alerts=prev_alerts,
                )
            except Exception:
                logger.error("Quick profit scan failed", exc_info=True)

        # Hedge Open (rule_id 91-100). Independent SQL because burst-fast-tier
        # owns its own cursor — sharing data with burst would fail when burst
        # advances HWM past data hedge hasn't yet processed. Own cursor
        # namespace ("hedge_open") keeps the two scans isolated.
        hedge_result: dict[str, Any] | None = None
        if include_hedge and hedge_config.get("enabled", True) and hedge_config.get("rules"):
            try:
                hedge_result = scan_hedge_open(
                    settings,
                    scan_interval_min=config["scan_interval_min"],
                    rules=hedge_config["rules"],
                    previous_alerts=prev_alerts,
                )
            except Exception:
                logger.error("Hedge open scan failed", exc_info=True)

        # Leverage Abuse (rule_id 101-110, event-gated). Dedup on
        # (rule_id, server, login, open_time). Seed previous_alerts with the
        # in-memory last tick (via prev_alerts) PLUS recent leverage rows from
        # SQLite, so the first scan after a restart doesn't re-emit opens that
        # were already alerted (prev_alerts' SQLite portion only covers QP).
        # OPT-0038 R2: track whether the event-gated section actually completed
        # this tick. If a scan raises (e.g. MySQL unreachable), we must NOT
        # advance `_event_gated_last_scan_at` — otherwise during a multi-tick
        # outage the timestamp would keep advancing past opens we never scanned,
        # and on recovery the adaptive window would be too narrow to catch up
        # (silent miss). A failed tick leaves the timestamp where it was so the
        # next successful tick widens to cover the whole gap (outsider-review #2).
        event_gated_ok = True
        leverage_result: dict[str, Any] | None = None
        if include_leverage and leverage_config.get("enabled", True) and leverage_config.get("rules"):
            try:
                from ..core.risk_monitor_db import get_recent_leverage_abuse_alerts
                leverage_prev = list(prev_alerts) + get_recent_leverage_abuse_alerts(
                    int(config["scan_interval_min"]) + 5
                )
                leverage_result = scan_leverage_abuse(
                    settings,
                    scan_interval_min=event_gated_interval_min,
                    rules=leverage_config["rules"],
                    previous_alerts=leverage_prev,
                    lookback_override_sec=event_gated_lookback_sec,
                )
            except Exception:
                event_gated_ok = False
                logger.error("Leverage abuse scan failed", exc_info=True)

        # Martingale (rule_id 111-120, event-gated). Dedup on
        # (rule_id, server, login, symbol, direction, last_open). Seed the
        # previous-alert pool with the in-memory last tick PLUS recent
        # martingale rows from SQLite so a restart doesn't re-emit adds already
        # alerted (prev_alerts' SQLite portion only covers QP).
        martingale_result: dict[str, Any] | None = None
        if include_martingale and martingale_config.get("enabled", True) and martingale_config.get("rules"):
            try:
                from ..core.risk_monitor_db import get_recent_martingale_alerts
                martingale_prev = list(prev_alerts) + get_recent_martingale_alerts(
                    int(config["scan_interval_min"]) + 5
                )
                martingale_result = scan_martingale(
                    settings,
                    scan_interval_min=event_gated_interval_min,
                    rules=martingale_config["rules"],
                    previous_alerts=martingale_prev,
                    lookback_override_sec=event_gated_lookback_sec,
                )
            except Exception:
                event_gated_ok = False
                logger.error("Martingale scan failed", exc_info=True)

        # OPT-0038 R2: mark this fast tick as the new "last successful event-gated
        # scan" so the NEXT tick's adaptive look-back starts from here — but ONLY
        # if the section completed without a swallowed scan failure. On failure we
        # leave the timestamp untouched so the next tick's window widens to cover
        # this tick's interval too (no open ages out of coverage unseen during an
        # outage). Advanced even when both rules are disabled / emitted nothing —
        # then there were simply no opens to miss.
        if tier == "fast_burst" and now_tick is not None and event_gated_ok:
            _event_gated_last_scan_at = now_tick

        # Build this tick's alerts. For tiered modes ('fast_burst'/'slow'),
        # we MERGE with the not-touched alerts from _latest_result so the
        # cached snapshot stays consistent (frontend "立即扫描" reads it).
        this_tick_alerts: list[dict[str, Any]] = []
        if burst_result:
            this_tick_alerts.extend(burst_result["alerts"])
        if quick_result:
            this_tick_alerts.extend(quick_result["alerts"])
        if qp_result:
            this_tick_alerts.extend(qp_result["alerts"])
        if hedge_result:
            this_tick_alerts.extend(hedge_result["alerts"])
        if leverage_result:
            this_tick_alerts.extend(leverage_result["alerts"])
        if martingale_result:
            this_tick_alerts.extend(martingale_result["alerts"])

        if tier == "fast_burst":
            # Keep slow-tier-owned alerts (51-100) from previous result;
            # replace the fast-tier-owned portion (burst 1-50 + leverage
            # 101-110 + martingale 111-120) with this tick's. See
            # _is_fast_tier_rule_id (OPT-0037 broadened fast-tier ownership).
            kept = [
                a for a in ((_latest_result or {}).get("alerts") or [])
                if not _is_fast_tier_rule_id(a.get("rule_id", 0))
            ]
            merged_alerts = list(this_tick_alerts) + kept
        elif tier == "slow":
            # Keep fast-tier-owned portion (1-50, 101-120) from previous
            # result; replace the slow portion (51-100) with this tick's.
            kept = [
                a for a in ((_latest_result or {}).get("alerts") or [])
                if _is_fast_tier_rule_id(a.get("rule_id", 0))
            ]
            merged_alerts = kept + list(this_tick_alerts)
        else:
            merged_alerts = this_tick_alerts

        burst_pairs = burst_result.pop("_universe_pairs", set()) if burst_result else set()
        quick_pairs = quick_result.pop("_universe_pairs", set()) if quick_result else set()
        qp_pairs = qp_result.pop("_universe_pairs", set()) if qp_result else set()
        hedge_pairs = hedge_result.pop("_universe_pairs", set()) if hedge_result else set()
        leverage_pairs = leverage_result.pop("_universe_pairs", set()) if leverage_result else set()
        martingale_pairs = martingale_result.pop("_universe_pairs", set()) if martingale_result else set()

        # For tier modes, the scan_time_ms reflects only what this tick ran.
        ran_results = [r for r in (burst_result, quick_result, qp_result, hedge_result, leverage_result, martingale_result) if r]
        if not ran_results:
            # tier='slow' with everything disabled — nothing to persist, but
            # still safe to return without touching state.
            return
        primary = ran_results[0]
        _latest_result = {
            "alerts": merged_alerts,
            "summary": {
                "suspicious_count": len(merged_alerts),
                "total_accounts_scanned": len(
                    set(burst_pairs) | set(quick_pairs) | set(qp_pairs)
                    | set(hedge_pairs) | set(leverage_pairs)
                    | set(martingale_pairs)
                ),
            },
            "burst_summary": (burst_result["summary"] if burst_result
                              else (_latest_result or {}).get("burst_summary")),
            "config": (burst_result["config"] if burst_result
                       else (_latest_result or {}).get("config")
                       or {"scan_interval_min": config["scan_interval_min"],
                           "rules": config["rules"]}),
            "scan_time_ms": sum(r["scan_time_ms"] for r in ran_results),
            "scanned_at": primary["scanned_at"],
            "tier": tier,
        }

        # Persist the alerts EMITTED THIS TICK (not the merged snapshot —
        # the snapshot keeps stale alerts from the other tier visible in
        # cache, but they were already written by their own tick).
        append_scan_and_events(
            scanned_at=primary["scanned_at"],
            scan_interval_min=config["scan_interval_min"],
            accounts_scanned=_latest_result["summary"]["total_accounts_scanned"],
            suspicious_count=len(this_tick_alerts),
            scan_time_ms=_latest_result["scan_time_ms"],
            alerts=this_tick_alerts,
        )

        # OPT-0013: notify SSE subscribers of the new tick. Lightweight
        # payload (no full alert bodies) — frontend does an incremental
        # /alerts fetch on receipt. publish() is thread-safe.
        if os.getenv("SSE_ENABLED", "false").lower() == "true":
            try:
                from .alerts_pubsub import publish as _sse_publish
                _sse_publish({
                    "type": "scan",
                    "tier": tier,
                    "scanned_at": primary["scanned_at"],
                    "new_alert_count": len(this_tick_alerts),
                    "rule_ids": sorted({a.get("rule_id") for a in this_tick_alerts
                                        if a.get("rule_id") is not None}),
                })
            except Exception:
                logger.exception("SSE publish failed (non-fatal)")

        logger.info(
            "Scan complete [%s]: %d new (%d cached), %d scanned, %dms",
            tier,
            len(this_tick_alerts),
            len(merged_alerts),
            _latest_result["summary"]["total_accounts_scanned"],
            _latest_result["scan_time_ms"],
        )
    except Exception:
        logger.error("Scan failed [tier=%s]", tier, exc_info=True)


def _locked_scan() -> None:
    """Slow-tier (or all-in-one when fast tier disabled) scan with lock."""
    acquired = _scan_lock.acquire(blocking=False)
    if not acquired:
        logger.info("Slow-tier scan already running, skipping this tick")
        return
    try:
        # When fast tier owns burst, slow tier explicitly skips it; else
        # legacy 'all' mode keeps prior behavior exactly.
        _run_scan(tier="slow" if _fast_tier_enabled() else "all")
    finally:
        _scan_lock.release()


def _locked_fast_burst_scan() -> None:
    """OPT-0012 fast tier: every 60s. Runs burst + (OPT-0037) the two
    event-gated rules leverage-abuse + martingale.

    Uses the SAME _scan_lock as slow tier — they share `_latest_result`
    state. If a slow tick is mid-flight, the fast tick skips (no big deal
    at 60s cadence; next tick picks it up). Worst case under heavy load:
    one fast tick skipped every ~10 min when slow tier runs. The added
    leverage/martingale scans make each fast tick a bit heavier (extra opens
    query + margin/position snapshot every 60s) — the accepted cost of
    closing the settle-window blind spot.
    """
    global _fast_tier_skip_count
    if not _fast_tier_enabled():
        return
    acquired = _scan_lock.acquire(blocking=False)
    if not acquired:
        _fast_tier_skip_count += 1
        logger.info(
            "Fast tier: scan_lock held by slow tier, skipping this tick "
            "(cumulative skips=%d)",
            _fast_tier_skip_count,
        )
        return
    try:
        _run_scan(tier="fast_burst")
    finally:
        _scan_lock.release()


def get_latest_result() -> dict[str, Any] | None:
    """Return the most recent scan result (read from in-memory cache)."""
    return _latest_result


def trigger_scan_now() -> dict[str, Any] | None:
    """Trigger an immediate scan. Blocks until complete (with lock)."""
    global _latest_result
    with _scan_lock:
        _run_scan()
    return _latest_result


def trigger_gap_trade_scan_now() -> None:
    """Trigger an immediate Gap Trade scan (cron path, no UI button).

    Kept as a separate helper so a debug shell / unit test can fire one
    deterministic scan without nudging APScheduler. Does NOT return the
    alert list — Gap Trade results live in `alert_events`, not in a
    cached snapshot.
    """
    with _gap_trade_lock:
        _run_gap_trade_scan()


def _run_gap_trade_scan() -> None:
    """Execute one Gap Trade daily scan (rules 71 + 81).

    Window = [today 00:00 MT, today 02:00 MT) inclusive of close_time.
    Runs Mon–Sat 07:20 HKT via cron — 20 min after the MT window closes
    (MT 02:00 = HKT 07:00) so we capture today's gap event in real time.
    SO+AB IP enrichment only uses the **open date** of each L leg (which
    is always in the past for a closed position), so the close-day IP
    file not being ready yet doesn't block anything. Manual trigger on
    Sunday is aborted by the `weekdays_only` flag (no real trading on
    Sunday MT — Saturday catches Friday's NY-close gap and is kept).

    Both sub-detectors share one `append_scan_and_events` write so the
    scan_history row covers them together.
    """
    from ..core.config import get_settings
    from ..core.risk_monitor_db import (
        append_scan_and_events,
        load_gap_trade_config,
    )
    from ..services.rule_gap_trade_so_service import detect_gap_trade_so
    from ..services.rule_gap_trade_gap_service import detect_gap_trade_gap_profit
    from ..schemas.risk_monitor import GapTradeConfig

    try:
        # Apply Pydantic defaults so a brand-new install (config_json = {})
        # still has window hours / sid_list / sub-rules populated.
        config = GapTradeConfig(**load_gap_trade_config())
        settings = get_settings()

        # MT is UTC+3 with no DST. Compute "now in MT" and snap to today
        # MT's [start, end) window — cron at HKT 07:20 = MT 02:20 sits
        # 20 min after the window closes, so today MT's data is final.
        # Real-time monitoring trumps the older "scan yesterday for full
        # IP coverage" design because IP enrichment now uses open-date
        # files (always available) — see rule_gap_trade_so_service.
        now_mt = (datetime.now(timezone.utc) + timedelta(hours=3)).replace(
            tzinfo=None, microsecond=0
        )
        window_day = now_mt.replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        # Only skip Sunday — Saturday MT 00-02 maps to Fri 21-23 UTC which
        # is still NY-Friday-afternoon active trading (the weekly close
        # period), and we want to catch any final-hour AB-arb there.
        if config.weekdays_only and window_day.weekday() == 6:
            logger.info(
                "Gap Trade scan skipped: window day %s is Sunday (no trading)",
                window_day.date(),
            )
            return

        start_mt = window_day.replace(hour=config.window_start_hour_mt)
        end_mt = window_day + timedelta(hours=config.window_end_hour_mt)
        if start_mt >= end_mt:
            logger.warning(
                "Gap Trade invalid window: start_mt=%s end_mt=%s", start_mt, end_mt
            )
            return

        merged_alerts: list[dict[str, Any]] = []
        total_ms = 0

        if config.so_ab.enabled:
            try:
                so_result = detect_gap_trade_so(
                    settings,
                    start_mt=start_mt,
                    end_mt=end_mt,
                    sid_list=list(config.sid_list),
                    max_open_diff_sec=config.so_ab.max_open_diff_sec,
                    min_lot_ratio=config.so_ab.min_lot_ratio,
                    max_lot_ratio=config.so_ab.max_lot_ratio,
                    cross_client_only=config.so_ab.cross_client_only,
                    min_l_loss_usd=config.so_ab.min_l_loss_usd,
                )
                merged_alerts.extend(so_result["alerts"])
                total_ms += int(so_result.get("scan_time_ms") or 0)
            except Exception:
                logger.error("Gap Trade SO+AB detect failed", exc_info=True)

        if config.gap_profit.enabled:
            try:
                gp_result = detect_gap_trade_gap_profit(
                    settings,
                    start_mt=start_mt,
                    end_mt=end_mt,
                    sid_list=list(config.sid_list),
                    profit_ratio_min=config.gap_profit.profit_ratio_min,
                    min_profit_usd=config.gap_profit.min_profit_usd,
                    min_net_deposit_hist=config.gap_profit.min_net_deposit_hist,
                )
                merged_alerts.extend(gp_result["alerts"])
                total_ms += int(gp_result.get("scan_time_ms") or 0)
            except Exception:
                logger.error("Gap Trade gap-profit detect failed", exc_info=True)

        scanned_at = (
            datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        )
        append_scan_and_events(
            scanned_at=scanned_at,
            # We log the window-length-in-minutes here so the scan_history
            # row stays self-describing — Gap Trade doesn't have a real
            # "scan_interval", the daily cron is the scheduling unit.
            scan_interval_min=(config.window_end_hour_mt - config.window_start_hour_mt) * 60,
            accounts_scanned=len({(a.get("server"), a.get("login")) for a in merged_alerts}),
            suspicious_count=len(merged_alerts),
            scan_time_ms=total_ms,
            alerts=merged_alerts,
        )
        so_count = sum(1 for a in merged_alerts if int(a.get("rule_id") or 0) == 71)
        gp_count = sum(1 for a in merged_alerts if int(a.get("rule_id") or 0) == 81)
        logger.info(
            "Gap Trade scan complete: %d alerts (SO+AB=%d, gap-profit=%d) "
            "window MT %s ~ %s, %dms",
            len(merged_alerts), so_count, gp_count, start_mt, end_mt, total_ms,
        )

        # OPT-0032: CRM risk-tag pipeline on the final rule-81 hit set.
        # Shares the audit-table dedup with the intraday tier, so clients
        # already tagged intraday come back skipped_dedup here (no second
        # POST, no duplicate email). heartbeat=True → this is the one
        # unconditional daily email proving the pipeline ran.
        try:
            from ..services.gap_trade_crm_tag_service import process_gap_trade_crm_tags
            from .risk_monitor_db import get_crm_tag_rows_for_window
            window_date = start_mt.date().isoformat()
            gp_alerts = [a for a in merged_alerts if int(a.get("rule_id") or 0) == 81]
            # Reconciliation: clients tagged intraday on a partial window but
            # NOT confirmed by this authoritative full-window scan — closed
            # P&L is not monotonic (later losing closes shrink it), so these
            # need a human review/untag decision, prominently in the email.
            final_uids = {int(a.get("client_userid") or 0) for a in gp_alerts}
            tagged_rows = [r for r in get_crm_tag_rows_for_window(window_date)
                           if r.get("result") == "tagged"]
            diverged = sorted({int(r["client_userid"]) for r in tagged_rows}
                              - final_uids)
            note = (
                f"⚠ Tagged intraday but NOT confirmed by the 07:20 final scan "
                f"(review / untag manually): {', '.join(map(str, diverged))}"
                if diverged else ""
            )
            process_gap_trade_crm_tags(
                settings,
                alerts=gp_alerts,
                window_date=window_date,
                scan_label="final 07:20 HKT",
                crm_tag_config=config.crm_tag.model_dump(),
                extra_note=note,
                heartbeat=True,
            )
        except Exception:
            # Tagging must never break the detection scan that feeds the
            # dashboard — but it must be loud in the logs.
            logger.error("Gap Trade CRM tag pipeline failed (final scan)",
                         exc_info=True)
    except Exception:
        logger.error("Gap Trade scan failed", exc_info=True)


def _locked_gap_trade_scan() -> None:
    """07:20 final scan — waits for an in-flight intraday tick.

    Blocking acquire with timeout (NOT the old non-blocking skip): the final
    scan is the authoritative daily record AND carries rule 71 SO+AB, so a
    long intraday tick at 07:05 must delay it, never silently cancel it.
    """
    acquired = _gap_trade_lock.acquire(timeout=_GAP_TRADE_FINAL_LOCK_TIMEOUT_SEC)
    if not acquired:
        # An intraday tick has been holding the lock for 5+ minutes — that
        # is itself an incident (hung MySQL query?). ERROR level so log
        # alerting picks it up; the missing daily heartbeat email is the
        # second signal.
        logger.error(
            "Gap Trade FINAL scan could not acquire lock after %ds — "
            "skipped for the day. Investigate the hung intraday tick.",
            _GAP_TRADE_FINAL_LOCK_TIMEOUT_SEC,
        )
        return
    try:
        _run_gap_trade_scan()
    finally:
        _gap_trade_lock.release()


def _run_gap_trade_intraday_scan() -> None:
    """One intraday tick (OPT-0032): growing-window rule-81 detect → CRM tag.

    Differences vs the 07:20 final scan, all deliberate:
    - ``end_mt`` is clamped to min(now, window end) — a growing window.
    - Only the gap-profit detector runs (rule 71 SO+AB needs closed pairs
      and IP files; it stays daily-only).
    - Alerts are NOT persisted to alert_events — the 07:20 final scan is
      the authoritative dashboard record; persisting partial-window hits
      every 5 min would spam the UI with duplicates. The CRM tag audit
      table is this tick's only durable output.
    - ``strict_deposit=True`` — a net-deposit DB error raises (tick fails,
      next tick retries) instead of silently dropping every client.
    """
    from ..core.config import get_settings
    from ..core.risk_monitor_db import load_gap_trade_config
    from ..services.rule_gap_trade_gap_service import detect_gap_trade_gap_profit
    from ..services.gap_trade_crm_tag_service import process_gap_trade_crm_tags
    from ..schemas.risk_monitor import GapTradeConfig

    try:
        now_utc = datetime.now(timezone.utc)
        now_hkt = now_utc + timedelta(hours=8)
        if not _in_intraday_window_hkt(now_hkt.hour, now_hkt.minute):
            return

        config = GapTradeConfig(**load_gap_trade_config())
        if not (config.gap_profit.enabled and config.crm_tag.enabled):
            return
        settings = get_settings()

        now_mt = (now_utc + timedelta(hours=3)).replace(tzinfo=None, microsecond=0)
        window_day = now_mt.replace(hour=0, minute=0, second=0, microsecond=0)
        # Same Sunday skip as the final scan (no trading on MT Sunday).
        if config.weekdays_only and window_day.weekday() == 6:
            return

        start_mt = window_day.replace(hour=config.window_start_hour_mt)
        window_end_mt = window_day + timedelta(hours=config.window_end_hour_mt)
        # Clamp: at HKT 07:05 now_mt is 02:05, past the MT 02:00 window end —
        # keep parity with the final scan's window instead of overrunning it.
        end_mt = min(now_mt, window_end_mt)
        if start_mt >= end_mt:
            return

        gp_result = detect_gap_trade_gap_profit(
            settings,
            start_mt=start_mt,
            end_mt=end_mt,
            sid_list=list(config.sid_list),
            profit_ratio_min=config.gap_profit.profit_ratio_min,
            min_profit_usd=config.gap_profit.min_profit_usd,
            min_net_deposit_hist=config.gap_profit.min_net_deposit_hist,
            strict_deposit=True,
        )
        process_gap_trade_crm_tags(
            settings,
            alerts=gp_result["alerts"],
            window_date=start_mt.date().isoformat(),
            # Email content is English-only (user requirement 2026-06-05).
            scan_label=f"intraday {now_hkt:%H:%M} HKT",
            crm_tag_config=config.crm_tag.model_dump(),
        )
    except Exception:
        # Tick failure is recoverable — the growing window means the next
        # tick re-covers everything this one missed.
        logger.error("Gap Trade intraday tick failed", exc_info=True)


def _locked_gap_trade_intraday_scan() -> None:
    """Intraday tick with non-blocking skip (next tick re-covers the window)."""
    acquired = _gap_trade_lock.acquire(blocking=False)
    if not acquired:
        logger.info("Gap Trade intraday: lock held, skipping this tick")
        return
    try:
        _run_gap_trade_intraday_scan()
    finally:
        _gap_trade_lock.release()


def start_burst_scheduler() -> None:
    """Start the background scheduler. Runs first scan immediately on startup."""
    global _scheduler
    if _scheduler is not None:
        return

    if os.getenv("BURST_SCAN_ENABLED", "true").lower() == "false":
        logger.info("Burst scanner disabled by BURST_SCAN_ENABLED=false")
        return

    from ..core.risk_monitor_db import load_config
    config = load_config()
    interval_min = config["scan_interval_min"]

    _scheduler = BackgroundScheduler()
    _scheduler.add_job(
        _locked_scan,
        IntervalTrigger(minutes=interval_min),
        id=JOB_ID,
        replace_existing=True,
    )
    # OPT-0012: dedicated fast tier. Runs every 60s when
    # BURST_FAST_TIER_ENABLED=true. The slow-tier job above switches to
    # 'slow' mode (skips the fast-owned rules) so detectors don't double-run.
    # OPT-0037: fast tier now also runs leverage-abuse + martingale.
    if _fast_tier_enabled():
        _scheduler.add_job(
            _locked_fast_burst_scan,
            IntervalTrigger(seconds=BURST_FAST_TIER_INTERVAL_SEC),
            id=BURST_FAST_JOB_ID,
            replace_existing=True,
            # OPT-0037: be explicit now that the fast tick is heavier (burst +
            # leverage + martingale). max_instances=1 = never overlap a tick
            # with itself; coalesce=True = if the scheduler thread stalls and
            # several runs come due, collapse them to one (don't replay a backlog
            # of identical overlap-window scans).
            max_instances=1,
            coalesce=True,
        )
        logger.info(
            "Fast tier started: every %ds (burst + leverage + martingale; "
            "slow tier skips them)",
            BURST_FAST_TIER_INTERVAL_SEC,
        )
    # Gap Trade: Mon–Sat at 07:20 HKT (Asia/Hong_Kong). Cron fires
    # 20 min after the MT window closes (MT 02:00 = HKT 07:00), giving
    # real-time visibility on today's gap event (gold opens MT 01:00 =
    # HKT 06:00, so MT 01-02 is the active gap hour). IP enrichment uses
    # open-date files only (always available) — see SO service.
    if os.getenv("GAP_TRADE_SCAN_ENABLED", "true").lower() != "false":
        _scheduler.add_job(
            _locked_gap_trade_scan,
            CronTrigger(
                day_of_week="mon-sat",
                hour=7,
                minute=20,
                timezone="Asia/Hong_Kong",
            ),
            id=GAP_TRADE_JOB_ID,
            replace_existing=True,
            # APScheduler's default misfire_grace_time (~1s) silently skips
            # a once-a-day cron whenever the executor is briefly busy at
            # 07:20:00 — explicit kwargs match core/scheduler.py's pattern.
            misfire_grace_time=300,
            coalesce=True,
            max_instances=1,
        )
        logger.info(
            "Gap Trade scanner started: Mon–Sat 07:20 HKT "
            "(scans current MT day 00:00–02:00 window, real-time after gap close)"
        )
        # OPT-0032 intraday tier: every 5 min inside HKT 05:55–07:05 (gold
        # opens MT 01:00 = HKT 06:00; the window brackets the active gap
        # hour). Trigger bounded to hour 5-7 as the first fence; the
        # in-function `_in_intraday_window_hkt` guard does minute precision.
        # Registration is gated on the FINAL-scan flag too: intraday without
        # the authoritative daily scan would tag with no reconciliation.
        if _gap_trade_intraday_enabled():
            _scheduler.add_job(
                _locked_gap_trade_intraday_scan,
                CronTrigger(
                    day_of_week="mon-sat",
                    hour="5-7",
                    minute="*/5",
                    timezone="Asia/Hong_Kong",
                ),
                id=GAP_TRADE_INTRADAY_JOB_ID,
                replace_existing=True,
                misfire_grace_time=60,
                coalesce=True,
                max_instances=1,
            )
            logger.info(
                "Gap Trade INTRADAY tier started: Mon–Sat HKT %02d:%02d–%02d:%02d "
                "every 5 min (growing window, CRM tag pipeline; live writes "
                "gated by GAP_TRADE_CRM_WRITE_ENABLED + crm_tag.write_enabled)",
                *GAP_TRADE_INTRADAY_START_HKT, *GAP_TRADE_INTRADAY_END_HKT,
            )
    _scheduler.start()
    logger.info("Burst scanner started: every %d minutes", interval_min)

    # Run first scan immediately in a background thread so startup isn't blocked
    threading.Thread(target=_locked_scan, daemon=True).start()


def stop_burst_scheduler() -> None:
    """Shut down the background scheduler."""
    global _scheduler
    if _scheduler:
        _scheduler.shutdown(wait=False)
        _scheduler = None
        logger.info("Burst scanner stopped")


def reschedule_burst(new_interval_min: int) -> None:
    """Update the scan interval. Takes effect on the next tick."""
    if not _scheduler:
        return

    _scheduler.reschedule_job(
        JOB_ID,
        trigger=IntervalTrigger(minutes=new_interval_min),
    )
    logger.info("Burst scanner rescheduled: every %d minutes", new_interval_min)
