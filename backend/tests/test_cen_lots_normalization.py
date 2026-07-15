"""CEN (cent-account) unit normalisation across risk-monitor detection rules.

Phase A0 (risk-V2): all rule thresholds must be compared in unified units —
amounts in USD, lot counts in STANDARD-lot equivalents (1 CEN lot = 0.01
standard lot). The 2026-07 independent review overturned the old "contract
size is identical for USD and CEN" assumption; comparing raw CEN lots
against standard-lot thresholds was the #1 burst-open noise source (98.5%
of burst alerts came from cent groups).

Locked behaviors:
- burst-open: CEN raw lots ÷100 BEFORE min_lots_per_order → the boundary
  false positive (raw 5 CEN lots vs a 5-standard-lot floor) disappears,
  while a true signal (raw 500 CEN lots = 5 standard lots) still fires
  and is persisted in standard-lot units.
- hedge-open: matched lots ÷100 BEFORE min_total_lots; the "perfect 1:1"
  EPS is scaled by the same divisor so the balance semantics (raw diff
  < 0.01 broker lot) are unchanged.
- burst enrichment: total_open_lots ÷100 for CEN so equity_per_lot is
  USD per standard lot.
- leverage-abuse: min_equity_usd compares against CEN equity already
  ÷100 by _get_margin_snapshot (pre-existing; pinned here end-to-end).
- quick-profit: min_profit_usd re-applied AFTER CEN ÷100 normalisation
  (pre-existing scan-level behavior; pinned here with fakes).
- lots_divisor: CEN → 100.0, anything else (USD / None / unknown) → 1.0.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import app.services.rule_quick_profit_service as qp_svc
from app.services.account_enrichment import lots_divisor
from app.services.risk_monitor_service import rule_burst_open_detect
from app.services.rule_hedge_open_service import rule_hedge_open_detect
from app.services.rule_leverage_abuse_service import (
    _get_margin_snapshot,
    rule_leverage_abuse_detect,
)

BASE = datetime(2026, 7, 14, 8, 0, 0, tzinfo=timezone.utc)


# ── helpers ────────────────────────────────────────────────────────────

def _iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def _open_row(
    *,
    server: str = "MT5",
    login: int = 67000001,
    symbol: str = "XAUUSD.cent",
    direction: str = "Buy",
    lots: float = 5.0,
    offset_sec: float = 0.0,
    ticket: int = 0,
) -> dict[str, Any]:
    """Row shaped like _query_mt4/mt5_recent_opens output (shared by
    burst-open and hedge-open detection)."""
    return {
        "server": server,
        "login": login,
        "symbol": symbol,
        "direction": direction,
        "lots": lots,
        "open_time": BASE + timedelta(seconds=offset_sec),
        "ticket": ticket,
    }


def _burst_rule(*, min_lots: float = 5.0, min_count: int = 3, window: int = 3):
    return {
        "id": 1,
        "burst_window_sec": window,
        "min_order_count": min_count,
        "min_lots_per_order": min_lots,
    }


def _hedge_rule(*, min_total_lots: float = 0.01):
    return {
        "id": 91,
        "name": "对冲",
        "enabled": True,
        "window_sec": 3,
        "min_orders_per_side": 1,
        "min_total_lots": min_total_lots,
    }


# MT5 sid = 5 → loginsid "5-67000001"
_CEN_MAP = {"5-67000001": "CEN"}
_USD_MAP = {"5-67000001": "USD"}


# ── lots_divisor ───────────────────────────────────────────────────────

def test_lots_divisor_cen_100_else_1():
    assert lots_divisor("CEN") == 100.0
    assert lots_divisor("cen") == 100.0
    assert lots_divisor("USD") == 1.0
    assert lots_divisor(None) == 1.0   # fail-open: never divide on unknown
    assert lots_divisor("") == 1.0


# ── burst-open: min_lots_per_order in standard lots ────────────────────

def test_burst_cen_boundary_false_positive_gone():
    """CEN account, 3 orders of raw 5 lots (= 0.05 standard) vs a 5-lot
    floor: pre-normalisation this fired (raw 5 >= 5); now it must not."""
    rows = [
        _open_row(lots=5.0, offset_sec=i, ticket=i) for i in range(3)
    ]
    # Pre-fix behavior (no currency map): raw compare fires — documents
    # exactly the false positive the normalisation removes.
    assert len(rule_burst_open_detect(
        [dict(r) for r in rows], [_burst_rule(min_lots=5.0)]
    )) == 1
    # With the CEN map: 0.05 standard lots < 5.0 → no alert.
    assert rule_burst_open_detect(
        rows, [_burst_rule(min_lots=5.0)], currency_map=_CEN_MAP
    ) == []


def test_burst_cen_true_signal_survives_in_standard_units():
    """CEN raw 500 lots = 5.0 standard lots → still fires, and the alert
    carries standard-lot values (orders / total_lots)."""
    rows = [
        _open_row(lots=500.0, offset_sec=i, ticket=i) for i in range(3)
    ]
    alerts = rule_burst_open_detect(
        rows, [_burst_rule(min_lots=5.0)], currency_map=_CEN_MAP
    )
    assert len(alerts) == 1
    a = alerts[0]
    assert a["total_lots"] == 15.0            # 3 × 5.0 standard lots
    assert all(o["lots"] == 5.0 for o in a["orders"])


def test_burst_usd_account_unaffected_by_map():
    rows = [
        _open_row(symbol="XAUUSD", lots=5.0, offset_sec=i, ticket=i)
        for i in range(3)
    ]
    alerts = rule_burst_open_detect(
        rows, [_burst_rule(min_lots=5.0)], currency_map=_USD_MAP
    )
    assert len(alerts) == 1
    assert alerts[0]["total_lots"] == 15.0    # raw lots untouched


# ── hedge-open: min_total_lots in standard lots, EPS semantics kept ────

def test_hedge_cen_matched_lots_below_floor_after_normalization():
    """CEN buy 5 / sell 5 raw (= 0.05 standard matched) vs min_total_lots
    0.5: pre-normalisation fired (matched 5.0 >= 0.5); now it must not."""
    rows = [
        _open_row(direction="Buy", lots=5.0, offset_sec=0, ticket=1),
        _open_row(direction="Sell", lots=5.0, offset_sec=1, ticket=2),
    ]
    # Pre-fix behavior (no currency map) fires.
    assert len(rule_hedge_open_detect(
        [dict(r) for r in rows], [_hedge_rule(min_total_lots=0.5)]
    )) == 1
    # Normalised: matched 0.05 < 0.5 → gone.
    assert rule_hedge_open_detect(
        rows, [_hedge_rule(min_total_lots=0.5)], currency_map=_CEN_MAP
    ) == []


def test_hedge_cen_true_signal_survives_in_standard_units():
    """CEN buy 100 / sell 100 raw (= 1.0 standard matched) clears a 0.5
    floor and the alert carries standard-lot values."""
    rows = [
        _open_row(direction="Buy", lots=100.0, offset_sec=0, ticket=1),
        _open_row(direction="Sell", lots=100.0, offset_sec=1, ticket=2),
    ]
    alerts = rule_hedge_open_detect(
        rows, [_hedge_rule(min_total_lots=0.5)], currency_map=_CEN_MAP
    )
    assert len(alerts) == 1
    a = alerts[0]
    assert a["buy_lots"] == 1.0
    assert a["sell_lots"] == 1.0
    assert a["total_lots"] == 2.0


def test_hedge_cen_eps_stays_in_raw_units():
    """Balance tolerance must NOT loosen 100× for CEN: buy 5.0 / sell 5.5
    raw differs by 0.5 raw lot (>= 0.01 EPS) — after ÷100 the normalised
    diff is 0.005 which a naive fixed EPS would call 'perfect 1:1'. The
    scaled EPS keeps it unbalanced → no alert."""
    rows = [
        _open_row(direction="Buy", lots=5.0, offset_sec=0, ticket=1),
        _open_row(direction="Sell", lots=5.5, offset_sec=1, ticket=2),
    ]
    assert rule_hedge_open_detect(
        rows, [_hedge_rule(min_total_lots=0.01)], currency_map=_CEN_MAP
    ) == []


# ── leverage-abuse: min_equity_usd vs CEN equity (÷100 pinned e2e) ─────

class _FakeCursor:
    def __init__(self, rows): self._rows = rows
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def execute(self, *a, **k): pass
    def fetchall(self): return self._rows


class _FakeConn:
    def __init__(self, rows): self._rows = rows
    def cursor(self): return _FakeCursor(self._rows)
    def close(self): pass


def _snapshot_row(*, equity_raw: float) -> dict[str, Any]:
    return {
        "loginsid": "5-67000001", "currency": "CEN",
        "account_group": "KCM\\5Vc_L10", "name": "T", "zipcode": "9",
        "leverage": 1000, "equity": equity_raw, "balance": equity_raw,
        "margin_used": equity_raw * 0.9, "free_margin": equity_raw * 0.1,
        "margin_level": 111.0, "modify_time": _iso(BASE + timedelta(seconds=5)),
    }


def _lev_rule():
    return {"id": 101, "name": "高杠杆重仓", "enabled": True,
            "max_margin_level": 200.0, "min_equity_usd": 100.0}


def _lev_open_account():
    return ("MT5", 67000001), {
        "orders": [{"direction": "Buy", "lots": 5.0, "open_time": _iso(BASE),
                    "symbol": "XAUUSD.cent", "ticket": 1}],
        "total_lots": 5.0,
        "first_open_dt": BASE,
        "last_open_dt": BASE,
    }


def test_leverage_cen_equity_dust_filtered_in_usd():
    """CEN raw equity 5,000 cents = $50 < min_equity $100 → no alert.
    (A raw-unit compare would have passed 5000 >= 100 — the distortion
    the review flagged.)"""
    margin_map = _get_margin_snapshot(
        _FakeConn([_snapshot_row(equity_raw=5000.0)]), {"5-67000001"}
    )
    assert margin_map["5-67000001"]["equity"] == 50.0
    k, a = _lev_open_account()
    assert rule_leverage_abuse_detect({k: a}, margin_map, [_lev_rule()]) == []


def test_leverage_cen_equity_real_signal_fires():
    """CEN raw equity 50,000 cents = $500 >= $100 → fires, equity stored
    in USD."""
    margin_map = _get_margin_snapshot(
        _FakeConn([_snapshot_row(equity_raw=50000.0)]), {"5-67000001"}
    )
    k, a = _lev_open_account()
    alerts = rule_leverage_abuse_detect({k: a}, margin_map, [_lev_rule()])
    assert len(alerts) == 1
    assert alerts[0]["equity"] == 500.0


# ── quick-profit: min_profit_usd re-applied after CEN ÷100 ─────────────

def _qp_realized_row(*, profit_raw: float) -> dict[str, Any]:
    return {
        "server": "MT5", "login": 67000001, "symbol": "XAUUSD.cent",
        "direction": "Buy", "lots": 1.0,
        "open_time": _iso(BASE - timedelta(minutes=5)),
        "close_time": _iso(BASE - timedelta(minutes=1)),
        "profit": profit_raw,
    }


def _run_qp_scan(monkeypatch, *, profit_raw: float):
    monkeypatch.setattr(qp_svc, "_get_connection", lambda settings: _FakeConn([]))
    monkeypatch.setattr(
        qp_svc, "_query_mt4_realized", lambda conn, **kw: []
    )
    monkeypatch.setattr(
        qp_svc, "_query_mt5_realized",
        lambda conn, **kw: [_qp_realized_row(profit_raw=profit_raw)],
    )
    monkeypatch.setattr(qp_svc, "_query_mt4_floating", lambda conn, **kw: {})
    monkeypatch.setattr(qp_svc, "_query_mt5_floating", lambda conn, **kw: {})
    monkeypatch.setattr(
        qp_svc, "get_account_info_map",
        lambda conn, alerts: {"5-67000001": {"currency": "CEN"}},
    )
    monkeypatch.setattr(
        qp_svc, "get_net_deposit_hist_map", lambda conn, alerts: {}
    )
    return qp_svc.scan_quick_profit(
        None,
        rules=[{"lookback_min": 30, "min_profit_usd": 5000.0,
                "include_floating": False}],
        now_utc=BASE,
    )


def test_quick_profit_cen_raw_cents_above_floor_but_usd_below_dropped(monkeypatch):
    """CEN raw profit 6,000 cents = $60: the pre-normalisation engine pass
    clears the 5000 floor (6000 >= 5000) but the post-÷100 re-filter must
    drop it."""
    result = _run_qp_scan(monkeypatch, profit_raw=6000.0)
    assert result["alerts"] == []


def test_quick_profit_cen_true_signal_kept_in_usd(monkeypatch):
    """CEN raw profit 600,000 cents = $6,000 >= $5,000 → kept, stored USD."""
    result = _run_qp_scan(monkeypatch, profit_raw=600000.0)
    assert len(result["alerts"]) == 1
    assert result["alerts"][0]["total_profit_usd"] == 6000.0
