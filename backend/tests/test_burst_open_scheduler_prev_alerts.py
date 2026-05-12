"""Regression tests for the Quick Profit dedup-pool builder.

`_build_quick_profit_prev_alerts` decides what `previous_alerts` list gets
passed into the Quick Profit scan each tick. The earlier scheduler only
seeded from SQLite when in-memory had zero QP alerts, which silently broke
dedup whenever a tick emitted ≥1 new QP alert alongside still-in-window older
ones — and also whenever a second backend process wrote concurrently. These
tests pin the new behaviour (unconditional SQLite seed) before either case
can regress.
"""

from __future__ import annotations

from typing import Any

from app.core.burst_open_scheduler import _build_quick_profit_prev_alerts


def _fake_sqlite_fetch(rows: list[dict[str, Any]]):
    """Return a stub fetch_recent that captures the arg it was called with."""
    calls: list[int] = []

    def _fetch(minutes: int) -> list[dict[str, Any]]:
        calls.append(minutes)
        return rows

    _fetch.calls = calls  # type: ignore[attr-defined]
    return _fetch


def test_seeds_from_sqlite_when_in_memory_already_has_qp_alert():
    """Bug fix: SQLite seed must run even when memory has a fresh QP alert.

    Before the fix, the scheduler skipped the SQLite seed whenever
    `_latest_result.alerts` contained any rule_id ≥ 61. That left older
    still-in-window same-key alerts invisible to dedup the next tick.
    """
    in_memory_new = {
        "rule_id": 62,
        "server": "MT4_Live",
        "login": 200,
        "symbol": "XAUUSD",
        "scanned_at": "2026-05-12T05:24:29Z",
    }
    older_from_sqlite = {
        "rule_id": 61,
        "server": "MT4_Live",
        "login": 100,
        "symbol": "XAUUSD",
        "scanned_at": "2026-05-12T05:01:05Z",
    }
    fetch = _fake_sqlite_fetch([older_from_sqlite])

    out = _build_quick_profit_prev_alerts(
        latest_result={"alerts": [in_memory_new]},
        qp_rules=[{"lookback_min": 60}, {"lookback_min": 30}],
        fetch_recent=fetch,
    )

    logins = sorted(a["login"] for a in out)
    assert logins == [100, 200], (
        "Both the in-memory new alert and the SQLite older alert must be "
        "in the dedup pool"
    )
    assert fetch.calls == [60], "Seed window = max(lookback_min)"


def test_seeds_from_sqlite_when_in_memory_empty():
    """Cold start path: empty memory still pulls SQLite history."""
    older = {"rule_id": 61, "server": "MT5", "login": 9, "symbol": "EURUSD",
             "scanned_at": "2026-05-12T04:55:00Z"}
    fetch = _fake_sqlite_fetch([older])

    out = _build_quick_profit_prev_alerts(
        latest_result=None,
        qp_rules=[{"lookback_min": 30}],
        fetch_recent=fetch,
    )

    assert out == [older]
    assert fetch.calls == [30]


def test_no_qp_rules_skips_sqlite_call():
    """When QP is disabled there's nothing to dedup against — don't query."""
    fetch = _fake_sqlite_fetch([{"rule_id": 61}])

    out = _build_quick_profit_prev_alerts(
        latest_result={"alerts": [{"rule_id": 1, "login": 5}]},
        qp_rules=[],
        fetch_recent=fetch,
    )

    assert out == [{"rule_id": 1, "login": 5}]
    assert fetch.calls == [], "Must not hit SQLite when QP rules are absent"


def test_sqlite_failure_falls_back_to_in_memory():
    """A flaky SQLite read must not crash the scan; in-memory still flows."""
    def _boom(_minutes: int) -> list[dict[str, Any]]:
        raise RuntimeError("disk on fire")

    in_memory = [{"rule_id": 61, "server": "MT5", "login": 7,
                  "symbol": "XAUUSD", "scanned_at": "2026-05-12T05:00:00Z"}]
    out = _build_quick_profit_prev_alerts(
        latest_result={"alerts": in_memory},
        qp_rules=[{"lookback_min": 30}],
        fetch_recent=_boom,
    )

    assert out == in_memory


def test_carries_non_qp_alerts_through_unchanged():
    """Burst/quick-open-close prev alerts pass through — only QP gets seed."""
    in_memory = [
        {"rule_id": 1, "server": "MT4_Live", "login": 1, "symbol": "XAUUSD"},
        {"rule_id": 51, "server": "MT4_Live", "login": 2, "symbol": "EURUSD"},
    ]
    fetch = _fake_sqlite_fetch([])
    out = _build_quick_profit_prev_alerts(
        latest_result={"alerts": in_memory},
        qp_rules=[{"lookback_min": 30}],
        fetch_recent=fetch,
    )
    assert out == in_memory
