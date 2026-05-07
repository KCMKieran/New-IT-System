"""
Unit tests for the Quick Profit detection rule engine.

Targets ``rule_quick_profit_detect`` and ``_dedup_by_time_bucket`` directly so
the suite has zero MySQL dependency. SQL collectors are not exercised here —
they're integration-tested manually via the SQL probe in the plan §4.5.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.services.rule_quick_profit_service import (
    QUICK_PROFIT_RULE_ID_BASE,
    _classify_position_status,
    _dedup_by_time_bucket,
    rule_quick_profit_detect,
)

NOW = datetime(2026, 5, 7, 12, 0, 0, tzinfo=timezone.utc)


def _row(server: str, login: int, symbol: str, profit: float,
         minutes_ago: float, lots: float = 1.0) -> dict:
    """Compact factory for a closed-trade row (pre-aggregation)."""
    close_time = NOW - timedelta(minutes=minutes_ago)
    return {
        "server": server,
        "login": login,
        "symbol": symbol,
        "direction": "Buy",
        "lots": lots,
        "open_time": (close_time - timedelta(minutes=1)).isoformat().replace("+00:00", "Z"),
        "close_time": close_time.isoformat().replace("+00:00", "Z"),
        "profit": profit,
        "ticket": 100 + login,
    }


def _rule(idx: int, *, lookback_min: int = 30, min_profit_usd: float = 5000.0,
          include_floating: bool = True) -> dict:
    return {
        "id": QUICK_PROFIT_RULE_ID_BASE + idx,
        "lookback_min": lookback_min,
        "min_profit_usd": min_profit_usd,
        "include_floating": include_floating,
    }


# ── Trigger / no-trigger ───────────────────────────────────


def test_single_rule_triggers_above_threshold():
    rows = [_row("MT4_Live", 100001, "XAUUSD", 5001.0, minutes_ago=5)]
    alerts = rule_quick_profit_detect(rows, {}, [_rule(0)], now_utc=NOW)
    assert len(alerts) == 1
    a = alerts[0]
    assert a["rule_id"] == QUICK_PROFIT_RULE_ID_BASE
    assert a["login"] == 100001
    assert a["symbol"] == "XAUUSD"
    assert a["realized_profit"] == 5001.0
    assert a["floating_profit_snapshot"] == 0.0
    assert a["total_profit_usd"] == 5001.0
    assert a["position_status"] == "closed"


def test_below_threshold_no_alert():
    rows = [_row("MT4_Live", 100002, "EURUSD", 4999.0, minutes_ago=5)]
    alerts = rule_quick_profit_detect(rows, {}, [_rule(0)], now_utc=NOW)
    assert alerts == []


def test_old_rows_outside_window_are_ignored():
    # 31 minutes old but threshold met — should NOT fire because window is 30 min.
    rows = [_row("MT4_Live", 100003, "GBPUSD", 6000.0, minutes_ago=31)]
    alerts = rule_quick_profit_detect(
        rows, {}, [_rule(0, lookback_min=30)], now_utc=NOW,
    )
    assert alerts == []


# ── Multi-rule sharing one SQL pull ───────────────────────


def test_multiple_rules_slice_their_own_windows():
    """One row 5 min ago + one row 50 min ago.

    Rule A (10min, $1000) sees only the recent row → triggers if it alone clears.
    Rule B (60min, $1000) sees both rows → triggers on the larger total.
    """
    rows = [
        _row("MT4_Live", 100004, "XAUUSD", 800.0, minutes_ago=5),
        _row("MT4_Live", 100004, "XAUUSD", 700.0, minutes_ago=50),
    ]
    rules = [
        _rule(0, lookback_min=10, min_profit_usd=1000.0, include_floating=False),
        _rule(1, lookback_min=60, min_profit_usd=1000.0, include_floating=False),
    ]
    alerts = rule_quick_profit_detect(rows, {}, rules, now_utc=NOW)
    by_rule = {a["rule_id"]: a for a in alerts}
    # Rule A: only recent 800 → below 1000, should not fire.
    assert QUICK_PROFIT_RULE_ID_BASE not in by_rule
    # Rule B: 800 + 700 = 1500 → above 1000, should fire.
    assert QUICK_PROFIT_RULE_ID_BASE + 1 in by_rule
    assert by_rule[QUICK_PROFIT_RULE_ID_BASE + 1]["realized_profit"] == 1500.0


# ── include_floating switch ───────────────────────────────


def test_include_floating_true_adds_floating_to_total():
    rows = [_row("MT4_Live", 100005, "XAUUSD", 2000.0, minutes_ago=5)]
    floating = {("MT4_Live", 100005): 4000.0}
    alerts = rule_quick_profit_detect(
        rows, floating, [_rule(0, min_profit_usd=5000.0, include_floating=True)],
        now_utc=NOW,
    )
    assert len(alerts) == 1
    a = alerts[0]
    assert a["realized_profit"] == 2000.0
    assert a["floating_profit_snapshot"] == 4000.0
    assert a["total_profit_usd"] == 6000.0
    assert a["position_status"] == "mixed"


def test_include_floating_false_excludes_floating():
    rows = [_row("MT4_Live", 100006, "XAUUSD", 2000.0, minutes_ago=5)]
    floating = {("MT4_Live", 100006): 10000.0}
    alerts = rule_quick_profit_detect(
        rows, floating, [_rule(0, min_profit_usd=5000.0, include_floating=False)],
        now_utc=NOW,
    )
    # Realized alone (2000) does not clear 5000 → no alert.
    assert alerts == []


# ── Position status three-way ─────────────────────────────


def test_position_status_classifier():
    assert _classify_position_status(has_realized=True, floating=0.0) == "closed"
    assert _classify_position_status(has_realized=False, floating=100.0) == "open"
    assert _classify_position_status(has_realized=True, floating=100.0) == "mixed"
    # Edge: nothing on either side → still classified as mixed (caller never
    # invokes this branch but we lock the contract anyway).
    assert _classify_position_status(has_realized=False, floating=0.0) == "mixed"


# ── Cross-scan dedup by time-bucket ───────────────────────


def test_dedup_drops_alert_within_lookback_window():
    """A previous alert whose scanned_at is inside ``lookback_min`` blocks repeat."""
    rows = [_row("MT4_Live", 100007, "XAUUSD", 5500.0, minutes_ago=5)]
    rule = _rule(0, lookback_min=30)
    alerts = rule_quick_profit_detect(rows, {}, [rule], now_utc=NOW)
    assert len(alerts) == 1

    prev_alerts = [{
        "rule_id": rule["id"],
        "server": "MT4_Live",
        "login": 100007,
        "symbol": "XAUUSD",
        "scanned_at": NOW.isoformat().replace("+00:00", "Z"),
        "_lookback_min": 30,
    }]
    deduped = _dedup_by_time_bucket(alerts, prev_alerts, NOW)
    assert deduped == []


def test_dedup_allows_alert_outside_lookback_window():
    """Once more than ``lookback_min`` has passed, the same key fires again."""
    rows = [_row("MT4_Live", 100008, "XAUUSD", 5500.0, minutes_ago=5)]
    rule = _rule(0, lookback_min=30)
    alerts = rule_quick_profit_detect(rows, {}, [rule], now_utc=NOW)

    prev_scan = NOW - timedelta(minutes=31)
    prev_alerts = [{
        "rule_id": rule["id"],
        "server": "MT4_Live",
        "login": 100008,
        "symbol": "XAUUSD",
        "scanned_at": prev_scan.isoformat().replace("+00:00", "Z"),
        "_lookback_min": 30,
    }]
    deduped = _dedup_by_time_bucket(alerts, prev_alerts, NOW)
    assert len(deduped) == 1


def test_detect_output_self_dedups_without_external_scanned_at():
    """Regression: detect's alert dict must already carry ``scanned_at`` so
    the next scan can pass _latest_result.alerts straight back into dedup.

    Without this, ``_dedup_by_time_bucket`` skips every prev alert and
    dedup silently no-ops — manifesting in production as the same account
    re-firing on every manual "立即扫描" click within the lookback window.
    """
    rows = [_row("MT4_Live", 100030, "XAUUSD", 5500.0, minutes_ago=1)]
    rule = _rule(0, lookback_min=30)
    first = rule_quick_profit_detect(rows, {}, [rule], now_utc=NOW)
    assert len(first) == 1
    assert "scanned_at" in first[0], "detect must stamp scanned_at"

    # Second scan a few minutes later, same account/symbol/rule. Pass the
    # first scan's output verbatim — no external scanned_at injection.
    later = NOW + timedelta(minutes=4)
    second = rule_quick_profit_detect(rows, {}, [rule], now_utc=later)
    deduped = _dedup_by_time_bucket(second, first, later)
    assert deduped == []


def test_dedup_works_across_bucket_boundary():
    """Regression: a 30-min rule firing at 11:55 must NOT re-fire at 12:01.

    The previous bucket-based dedup keyed on ``floor(now / 30min)``, which
    produced a different bucket for 11:55 vs 12:01 and let the duplicate
    through. The current implementation compares elapsed time directly.
    """
    rows = [_row("MT4_Live", 100020, "XAUUSD", 5500.0, minutes_ago=1)]
    rule = _rule(0, lookback_min=30)
    alerts = rule_quick_profit_detect(rows, {}, [rule], now_utc=NOW)
    assert len(alerts) == 1

    # Previous alert fired only 6 min ago but on the other side of a 30-min
    # absolute bucket boundary (i.e. 11:55 → 12:01).
    prev_scan = NOW - timedelta(minutes=6)
    prev_alerts = [{
        "rule_id": rule["id"],
        "server": "MT4_Live",
        "login": 100020,
        "symbol": "XAUUSD",
        "scanned_at": prev_scan.isoformat().replace("+00:00", "Z"),
        "_lookback_min": 30,
    }]
    deduped = _dedup_by_time_bucket(alerts, prev_alerts, NOW)
    assert deduped == []


def test_dedup_ignores_non_quick_profit_rules():
    """A burst-open prev alert with the same login must NOT block QP."""
    rows = [_row("MT4_Live", 100009, "XAUUSD", 5500.0, minutes_ago=5)]
    rule = _rule(0)
    alerts = rule_quick_profit_detect(rows, {}, [rule], now_utc=NOW)
    prev_alerts = [{
        "rule_id": 1,    # burst-open
        "server": "MT4_Live",
        "login": 100009,
        "symbol": "XAUUSD",
        "scanned_at": NOW.isoformat().replace("+00:00", "Z"),
    }]
    deduped = _dedup_by_time_bucket(alerts, prev_alerts, NOW)
    assert len(deduped) == 1


# ── Per-symbol grouping ────────────────────────────────────


def test_separate_symbols_become_separate_alerts():
    rows = [
        _row("MT4_Live", 200001, "XAUUSD", 5500.0, minutes_ago=5),
        _row("MT4_Live", 200001, "EURUSD", 5500.0, minutes_ago=5),
    ]
    alerts = rule_quick_profit_detect(rows, {}, [_rule(0)], now_utc=NOW)
    symbols = sorted(a["symbol"] for a in alerts)
    assert symbols == ["EURUSD", "XAUUSD"]


# ── Detect: rule_id is normalised to QUICK_PROFIT_RULE_ID_BASE + idx ────


def test_detect_overrides_sqlite_pk_with_business_rule_id():
    """Regression: SQLite primary keys (1, 2, …) must not bleed into alerts.

    The scheduler hands rules straight from the SQLite ``quick_profit_rules``
    table, so ``rule.get("id")`` is the table PK, not the business rule id.
    ``rule_quick_profit_detect`` must always override low ids with
    ``QUICK_PROFIT_RULE_ID_BASE + idx`` so downstream code (filter chips,
    threshold maps, alert_events.rule_id) uses the right key.
    """
    rows = [_row("MT4_Live", 100099, "XAUUSD", 5500.0, minutes_ago=5)]
    rule_with_pk = {
        "id": 1,  # SQLite AUTOINCREMENT primary key, NOT a business rule id
        "lookback_min": 30,
        "min_profit_usd": 5000.0,
        "include_floating": True,
    }
    alerts = rule_quick_profit_detect(rows, {}, [rule_with_pk], now_utc=NOW)
    assert len(alerts) == 1
    assert alerts[0]["rule_id"] == QUICK_PROFIT_RULE_ID_BASE
