"""Tests for OPT-0046 Rebate Arbitrage detection (rule 121, band 121-130)
and the wide-net Rebate Watch roster rule (rule 122, same band): rebate-only
trigger (no combined condition), lower env-tunable floor, 121-wins-same-tick
exclusion, band-scoped daily dedup, per-rule_id labels, kill-switch env flag.

Covers (fixture data only — no MySQL):
- Client-level aggregation: multi-account fold, CEN /100 on the trades leg
  only (rebate is already USD), short-hold ratios + geometric-mean hold,
  rebate-only clients (no realized trades in window)
- The two-condition trigger (rebate >= floor AND combined > 0, BOTH required
  — user decision 2026-07-12) incl. the boss case 1-8614411 shape and the
  high-rebate-but-net-loser control case
- Per-client-per-trading-day dedup (already_alerted_userids)
- Baseline + today additive merge (_merge_trade_legs)
- Alert dict shaping (user_id set at detection time, primary account =
  top-rebate account, net-deposit split, wallet summary)
- SQLite round-trip: append_scan_and_events routes 121 into
  alert_rebate_arb_detail, the unified reader returns the detail columns,
  COALESCE keeps gap-trade rule-81 rows intact, per-day dedup helper,
  mail fetcher family
- Mail source registry entry + digest template (alert-email-style: English,
  no emojis, bilingual title)

⚠ Seed timestamps are relative to ``datetime.now()`` (OPT-0041 date-rot
lesson) — append_scan_and_events purges rows older than 30 days.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

import pytest

from app.core import risk_monitor_db as rm_db
from app.services import rule_rebate_arb_service as svc
from app.services.alert_mail import registry
from app.services.alert_mail.rebate_arb import (
    SOURCE,
    TEST_SEND_SAMPLE_ALERT,
    build_rebate_arb_digest_email,
    match_context,
)


NOW = datetime.now(timezone.utc).replace(microsecond=0)
TODAY = (NOW + timedelta(hours=3)).date().isoformat()  # MT trading day


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    db_path = tmp_path / "risk_monitor.db"
    monkeypatch.setattr(rm_db, "_DB_PATH", db_path)
    rm_db.init_risk_monitor_db()
    return db_path


def _trade_row(
    login_sid: str,
    userid: int,
    *,
    pnl: float = 0.0,
    lots: float = 10.0,
    n: int = 10,
    lots_lt_5m: float = 0.0,
    lots_lt_10m: float = 0.0,
    ln_hold_lots: float = 0.0,
    currency: str = "USD",
    user_name: str = "",
    group_name: str = "KCM_TEST_GRP",
) -> dict:
    return {
        "login_sid": login_sid,
        "userid": userid,
        "currency": currency,
        "user_name": user_name,
        "group_name": group_name,
        "pnl": pnl,
        "lots": lots,
        "n": n,
        "lots_lt_5m": lots_lt_5m,
        "lots_lt_10m": lots_lt_10m,
        "ln_hold_lots": ln_hold_lots,
    }


def _rebate_row(
    login_sid: str,
    userid: int,
    rebate_usd: float,
    *,
    wallet: str = "2-12109",
    currency: str = "USD",
    user_name: str = "",
    group_name: str = "KCM_TEST_GRP",
) -> dict:
    return {
        "login_sid": login_sid,
        "wallet_login_sid": wallet,
        "rebate_usd": rebate_usd,
        "userid": userid,
        "currency": currency,
        "user_name": user_name,
        "group_name": group_name,
    }


# ── aggregate_clients ──────────────────────────────────────────────────────

def test_aggregate_multi_account_client():
    trades = {
        "1-100": _trade_row("1-100", 7, pnl=-500.0, lots=10.0, n=5,
                            lots_lt_5m=4.0, lots_lt_10m=6.0),
        "5-200": _trade_row("5-200", 7, pnl=200.0, lots=30.0, n=15,
                            lots_lt_5m=6.0, lots_lt_10m=10.0),
    }
    rebates = [
        _rebate_row("1-100", 7, 300.0, wallet="2-1"),
        _rebate_row("5-200", 7, 700.0, wallet="2-2"),
    ]
    clients = svc.aggregate_clients(trades, rebates)
    assert set(clients) == {7}
    c = clients[7]
    assert c["total_pl_usd"] == pytest.approx(-300.0)
    assert c["rebate_usd"] == pytest.approx(1000.0)
    assert c["combined_usd"] == pytest.approx(700.0)
    assert c["order_count"] == 20
    assert c["total_lots"] == pytest.approx(40.0)
    assert c["ratio_5m"] == pytest.approx(10.0 / 40.0)
    assert c["ratio_10m"] == pytest.approx(16.0 / 40.0)
    assert c["login_sids"] == {"1-100", "5-200"}
    assert c["wallets"] == {"2-1": 300.0, "2-2": 700.0}


def test_cen_division_applies_to_trades_leg_only():
    # CEN account: raw pnl -38100 cents = -$381. Rebate is ALREADY USD from
    # SQL (the commissions table stores USD) — must NOT be divided again.
    trades = {"1-100": _trade_row("1-100", 9, pnl=-38100.0, currency="CEN")}
    rebates = [_rebate_row("1-100", 9, 15001.0, currency="CEN")]
    c = svc.aggregate_clients(trades, rebates)[9]
    assert c["total_pl_usd"] == pytest.approx(-381.0)
    assert c["rebate_usd"] == pytest.approx(15001.0)
    assert c["combined_usd"] == pytest.approx(14620.0)


def test_geo_mean_hold_from_additive_numerator():
    # Two lots-weighted holds: 100s x 2 lots, 10000s x 2 lots →
    # geo mean = exp((ln(100)*2 + ln(10000)*2) / 4) = exp(ln(1000)) = 1000
    lnl = math.log(100) * 2 + math.log(10000) * 2
    trades = {"1-1": _trade_row("1-1", 3, lots=4.0, ln_hold_lots=lnl)}
    c = svc.aggregate_clients(trades, [_rebate_row("1-1", 3, 1.0)])[3]
    assert c["hold_geo_mean_sec"] == pytest.approx(1000.0, rel=1e-9)


def test_rebate_only_client_has_no_ratio_but_combined_equals_rebate():
    clients = svc.aggregate_clients({}, [_rebate_row("1-5", 11, 900.0)])
    c = clients[11]
    assert c["total_pl_usd"] == 0.0
    assert c["ratio_5m"] is None and c["ratio_10m"] is None
    assert c["hold_geo_mean_sec"] is None
    assert c["combined_usd"] == pytest.approx(900.0)


# ── trigger (rule_rebate_arb_detect) ───────────────────────────────────────

def _detect(clients, **kw):
    kw.setdefault("min_rebate_usd", 500.0)
    kw.setdefault("min_combined_usd", 0.0)
    return svc.rule_rebate_arb_detect(clients, **kw)


def test_boss_case_shape_triggers():
    # 1-8614411 shape: rebate $15,001, realized P&L -$381 → combined > 0.
    trades = {"1-8614411": _trade_row("1-8614411", 166818, pnl=-381.0,
                                      lots=492.0, n=3200)}
    rebates = [_rebate_row("1-8614411", 166818, 15001.0)]
    hits = _detect(svc.aggregate_clients(trades, rebates))
    assert [h["userid"] for h in hits] == [166818]
    assert hits[0]["rule_id"] == svc.REBATE_ARB_RULE_ID


def test_high_rebate_net_loser_does_not_trigger():
    # Control case (userid 103881 shape): rebate $52.9k but P&L -$61.9k →
    # combined < 0 → company is net-positive on this client; leave him alone.
    trades = {"1-1": _trade_row("1-1", 103881, pnl=-61877.13)}
    rebates = [_rebate_row("1-1", 103881, 52863.72)]
    assert _detect(svc.aggregate_clients(trades, rebates)) == []


def test_both_conditions_required():
    # combined > 0 but rebate below the $500 floor → no trigger.
    trades = {"1-2": _trade_row("1-2", 5, pnl=10000.0)}
    rebates = [_rebate_row("1-2", 5, 400.0)]
    assert _detect(svc.aggregate_clients(trades, rebates)) == []
    # rebate over the floor but combined exactly 0 → strict > excludes.
    trades = {"1-3": _trade_row("1-3", 6, pnl=-600.0)}
    rebates = [_rebate_row("1-3", 6, 600.0)]
    assert _detect(svc.aggregate_clients(trades, rebates)) == []
    # boundary: rebate exactly at the floor AND combined > 0 → triggers.
    trades = {"1-4": _trade_row("1-4", 8, pnl=-100.0)}
    rebates = [_rebate_row("1-4", 8, 500.0)]
    assert [h["userid"] for h in _detect(svc.aggregate_clients(trades, rebates))] == [8]


def test_low_short_hold_ratio_still_triggers():
    # Stratification labels never gate the trigger (user decision): a
    # volume-based arbitrageur with r10 < 50% (127582 shape) still enters.
    trades = {"1-9": _trade_row("1-9", 127582, pnl=-1000.0, lots=100.0,
                                lots_lt_5m=5.0, lots_lt_10m=9.5)}
    rebates = [_rebate_row("1-9", 127582, 26460.0)]
    hits = _detect(svc.aggregate_clients(trades, rebates))
    assert [h["userid"] for h in hits] == [127582]
    assert hits[0]["ratio_10m"] == pytest.approx(0.095)


def test_daily_dedup_excludes_already_alerted():
    trades = {"1-1": _trade_row("1-1", 42, pnl=-100.0)}
    rebates = [_rebate_row("1-1", 42, 1000.0)]
    clients = svc.aggregate_clients(trades, rebates)
    assert _detect(clients, already_alerted_userids={42}) == []
    assert len(_detect(clients, already_alerted_userids={41})) == 1


def test_hits_sorted_by_combined_desc_and_rule_id_clamped():
    clients = svc.aggregate_clients(
        {
            "1-1": _trade_row("1-1", 1, pnl=-100.0),
            "1-2": _trade_row("1-2", 2, pnl=-100.0),
        },
        [_rebate_row("1-1", 1, 600.0), _rebate_row("1-2", 2, 9000.0)],
    )
    hits = _detect(clients, rule_id=999)  # out-of-band → clamped to 121
    assert [h["userid"] for h in hits] == [2, 1]
    assert all(h["rule_id"] == svc.REBATE_ARB_RULE_ID_BASE for h in hits)


def test_env_threshold_override(monkeypatch):
    monkeypatch.setenv("REBATE_ARB_MIN_REBATE_USD", "1000")
    assert svc.get_min_rebate_usd() == 1000.0
    monkeypatch.setenv("REBATE_ARB_MIN_REBATE_USD", "garbage")
    assert svc.get_min_rebate_usd() == svc.DEFAULT_MIN_REBATE_USD
    monkeypatch.delenv("REBATE_ARB_MIN_REBATE_USD")
    assert svc.get_min_rebate_usd() == svc.DEFAULT_MIN_REBATE_USD


# ── rule 122 (wide-net Rebate Watch) trigger ───────────────────────────────

def _detect_122(clients, **kw):
    kw.setdefault("min_rebate_usd", svc.DEFAULT_RULE2_MIN_REBATE_USD)
    kw.setdefault("min_combined_usd", None)  # rebate-only trigger
    kw.setdefault("rule_id", svc.REBATE_ARB_RULE2_ID)
    return svc.rule_rebate_arb_detect(clients, **kw)


def test_rule2_fires_on_negative_combined():
    # The key difference vs 121: a net-LOSING high-rebate client (combined
    # deeply negative) still enters the watch roster.
    trades = {"1-1": _trade_row("1-1", 20, pnl=-5000.0)}
    rebates = [_rebate_row("1-1", 20, 300.0)]
    hits = _detect_122(svc.aggregate_clients(trades, rebates))
    assert [h["userid"] for h in hits] == [20]
    assert hits[0]["rule_id"] == svc.REBATE_ARB_RULE2_ID == 122
    assert hits[0]["combined_usd"] < 0


def test_rule2_below_floor_does_not_fire_and_boundary_fires():
    trades = {"1-1": _trade_row("1-1", 21, pnl=-100.0)}
    assert _detect_122(svc.aggregate_clients(
        trades, [_rebate_row("1-1", 21, 199.99)])) == []
    # Boundary: rebate exactly at the floor → >= triggers.
    assert [h["userid"] for h in _detect_122(svc.aggregate_clients(
        trades, [_rebate_row("1-1", 21, 200.0)]))] == [21]


def test_rule2_respects_already_alerted_set():
    clients = svc.aggregate_clients(
        {"1-1": _trade_row("1-1", 22, pnl=-100.0)},
        [_rebate_row("1-1", 22, 900.0)],
    )
    assert _detect_122(clients, already_alerted_userids={22}) == []
    assert len(_detect_122(clients, already_alerted_userids={23})) == 1


def test_rule2_id_in_band_passes_unclamped_and_out_of_band_clamps():
    clients = svc.aggregate_clients(
        {"1-1": _trade_row("1-1", 24, pnl=-100.0)},
        [_rebate_row("1-1", 24, 900.0)],
    )
    assert _detect_122(clients)[0]["rule_id"] == 122  # in band: no clamp
    hits = _detect_122(clients, rule_id=131)          # out of band: clamped
    assert hits[0]["rule_id"] == svc.REBATE_ARB_RULE_ID_BASE


def test_rule2_env_threshold_override(monkeypatch):
    assert svc.get_rule2_min_rebate_usd() == 200.0
    monkeypatch.setenv("REBATE_ARB_RULE2_MIN_REBATE_USD", "350")
    assert svc.get_rule2_min_rebate_usd() == 350.0
    monkeypatch.setenv("REBATE_ARB_RULE2_MIN_REBATE_USD", "garbage")
    assert svc.get_rule2_min_rebate_usd() == svc.DEFAULT_RULE2_MIN_REBATE_USD


def test_rule2_enabled_flag_parsing(monkeypatch):
    monkeypatch.delenv("REBATE_ARB_RULE2_ENABLED", raising=False)
    assert svc.get_rule2_enabled() is True          # default on
    for raw in ("false", "FALSE", " False "):
        monkeypatch.setenv("REBATE_ARB_RULE2_ENABLED", raw)
        assert svc.get_rule2_enabled() is False
    for raw in ("true", "1", "0", "garbage", ""):   # only literal "false" disables
        monkeypatch.setenv("REBATE_ARB_RULE2_ENABLED", raw)
        assert svc.get_rule2_enabled() is True


# ── baseline + today merge ─────────────────────────────────────────────────

def test_merge_trade_legs_is_additive_and_refreshes_identity():
    baseline = {"1-1": _trade_row("1-1", 5, pnl=-500.0, lots=20.0, n=10,
                                  lots_lt_5m=2.0, ln_hold_lots=10.0,
                                  group_name="OLD_GRP")}
    today = {"1-1": _trade_row("1-1", 5, pnl=100.0, lots=5.0, n=3,
                               lots_lt_5m=1.0, ln_hold_lots=2.0,
                               group_name="NEW_GRP"),
             "1-2": _trade_row("1-2", 6, pnl=50.0)}
    merged = svc._merge_trade_legs(baseline, today)
    assert merged["1-1"]["pnl"] == pytest.approx(-400.0)
    assert merged["1-1"]["lots"] == pytest.approx(25.0)
    assert merged["1-1"]["n"] == 13
    assert merged["1-1"]["lots_lt_5m"] == pytest.approx(3.0)
    assert merged["1-1"]["ln_hold_lots"] == pytest.approx(12.0)
    assert merged["1-1"]["group_name"] == "NEW_GRP"  # today's snapshot wins
    assert merged["1-2"]["pnl"] == pytest.approx(50.0)
    # Inputs are not mutated (baseline cache must stay pristine).
    assert baseline["1-1"]["pnl"] == pytest.approx(-500.0)


# ── alert shaping (build_alerts) ───────────────────────────────────────────

def _one_hit() -> list[dict]:
    trades = {
        "1-8614411": _trade_row("1-8614411", 166818, pnl=-381.0, lots=492.0,
                                n=3200, lots_lt_5m=178.6, lots_lt_10m=211.6,
                                user_name="Boss Case", group_name="KCMc50_L4"),
        "5-777": _trade_row("5-777", 166818, pnl=0.0, lots=1.0, n=1),
    }
    rebates = [
        _rebate_row("1-8614411", 166818, 14470.0, wallet="2-12109"),
        _rebate_row("1-8614411", 166818, 531.0, wallet="2-10186"),
    ]
    return _detect(svc.aggregate_clients(trades, rebates))


def test_build_alerts_field_mapping():
    hits = _one_hit()
    alerts = svc.build_alerts(
        hits,
        window_date=TODAY,
        scanned_at=_iso(NOW),
        net_deposit_map={166818: {"trading_net_deposit": 2100.0,
                                  "ib_withdrawal": -50.0}},
        equity_map={166818: 1893.55},
    )
    assert len(alerts) == 1
    a = alerts[0]
    assert a["rule_id"] == 121
    assert a["rule_label"] == svc.REBATE_ARB_RULE_LABEL
    # Primary account = top-rebate producing account.
    assert (a["server"], a["login"]) == ("MT4_Live", 8614411)
    assert a["symbol"] == ""
    assert a["user_id"] == 166818            # OPT-0045: set at detection time
    assert a["client_userid"] == 166818
    assert a["client_name"] == "Boss Case"
    assert a["rebate_30d"] == pytest.approx(15001.0)
    assert a["total_pl_30d"] == pytest.approx(-381.0)
    assert a["combined_30d"] == pytest.approx(14620.0)
    assert a["total_profit_usd"] == pytest.approx(-381.0)
    assert a["ratio_5m"] == pytest.approx(178.6 / 493.0, abs=1e-4)
    assert a["trading_net_deposit"] == pytest.approx(2100.0)
    assert a["ib_withdrawal"] == pytest.approx(-50.0)
    assert a["net_deposit_hist"] == pytest.approx(2050.0)  # sum of the split
    assert a["equity"] == pytest.approx(1893.55)
    assert a["contributing_login_sids"] == "1-8614411,5-777"
    assert a["contributing_account_count"] == 2
    assert a["wallet_login_sids"].startswith("2-12109:14470.00")
    assert a["window_date"] == TODAY


def test_build_alerts_label_follows_rule_id():
    clients = svc.aggregate_clients(
        {
            "1-1": _trade_row("1-1", 31, pnl=-100.0),    # 121: combined +900
            "1-2": _trade_row("1-2", 32, pnl=-5000.0),   # 122: combined -4700
        },
        [_rebate_row("1-1", 31, 1000.0), _rebate_row("1-2", 32, 300.0)],
    )
    hits_121 = _detect(clients)
    hits_122 = _detect_122(clients, already_alerted_userids={31})
    alerts = svc.build_alerts(
        hits_121 + hits_122, window_date=TODAY, scanned_at=_iso(NOW),
        net_deposit_map={}, equity_map={},
    )
    by_rule = {a["rule_id"]: a for a in alerts}
    assert by_rule[121]["rule_label"] == svc.REBATE_ARB_RULE_LABEL
    assert by_rule[122]["rule_label"] == svc.REBATE_ARB_RULE2_LABEL
    assert by_rule[121]["client_userid"] == 31
    assert by_rule[122]["client_userid"] == 32


def test_build_alerts_missing_enrichment_is_none():
    alerts = svc.build_alerts(
        _one_hit(), window_date=TODAY, scanned_at=_iso(NOW),
        net_deposit_map={}, equity_map={},
    )
    a = alerts[0]
    assert a["trading_net_deposit"] is None
    assert a["ib_withdrawal"] is None
    assert a["net_deposit_hist"] is None
    assert a["equity"] is None


# ── SQLite round-trip ──────────────────────────────────────────────────────

def _persist_one(alerts: list[dict]) -> None:
    rm_db.append_scan_and_events(
        scanned_at=_iso(NOW),
        scan_interval_min=10,
        accounts_scanned=len(alerts),
        suspicious_count=len(alerts),
        scan_time_ms=1,
        alerts=alerts,
    )


def test_db_round_trip_and_dedup_helper(temp_db):
    alerts = svc.build_alerts(
        _one_hit(), window_date=TODAY, scanned_at=_iso(NOW),
        net_deposit_map={166818: {"trading_net_deposit": 2100.0,
                                  "ib_withdrawal": 0.0}},
        equity_map={166818: 1893.55},
    )
    _persist_one(alerts)

    # Detail routed into alert_rebate_arb_detail + readable via mail fetcher.
    rows = rm_db.fetch_recent_rebate_arb_alerts(limit=10)
    assert len(rows) == 1
    r = rows[0]
    assert r["client_userid"] == 166818
    assert r["rebate_30d"] == pytest.approx(15001.0)
    assert r["combined_30d"] == pytest.approx(14620.0)
    assert r["window_date"] == TODAY
    assert r["group"] == "KCMc50_L4"

    # Per-day dedup helper sees the client for TODAY only.
    assert rm_db.get_rebate_arb_alerted_userids(TODAY) == {166818}
    assert rm_db.get_rebate_arb_alerted_userids("1999-01-01") == set()

    # Unified reader returns the detail columns for the 121-130 band.
    entries, total = rm_db.query_alert_events(
        since=_iso(NOW - timedelta(hours=1)),
        until=_iso(NOW + timedelta(hours=1)),
        rule_id_min=121, rule_id_max=130,
    )
    assert total == 1
    e = entries[0]
    assert e["client_userid"] == 166818
    assert e["rebate_30d"] == pytest.approx(15001.0)
    assert e["ratio_10m"] is not None
    assert e["window_date"] == TODAY

    # Cursor fetcher family.
    after = rm_db.fetch_rebate_arb_alerts_after(0)
    assert [x["id"] for x in after] == [r["id"]]
    assert rm_db.fetch_rebate_arb_alerts_by_ids([r["id"]])[0]["client_userid"] == 166818
    assert len(rm_db.fetch_rebate_arb_alerts_for_day(TODAY)) == 1


def test_coalesce_keeps_gap_trade_rule81_rows_intact(temp_db):
    """The shared client_* columns COALESCE across gp/ra — a rule-81 row must
    still read ITS OWN client_userid after the rebate-arb JOIN was added."""
    gap_alert = {
        "rule_id": 81, "rule_label": "Gap Trade · 超额 Profit",
        "server": "MT4_Live", "login": 111, "symbol": "XAUUSD",
        "order_count": 2, "total_lots": 0.0, "orders": [],
        "total_profit_usd": 5000.0, "user_id": 555,
        "client_userid": 555, "client_name": "Gap Client",
        "client_groupsid": "grp", "contributing_login_sids": "1-111",
        "contributing_account_count": 1, "symbols": "XAUUSD",
        "symbol_count": 1, "profit_ratio": 2.5, "triggered_by": "ratio_x1",
        "window_date": TODAY,
    }
    ra_alerts = svc.build_alerts(
        _one_hit(), window_date=TODAY, scanned_at=_iso(NOW),
        net_deposit_map={}, equity_map={},
    )
    _persist_one([gap_alert] + ra_alerts)

    entries, total = rm_db.query_alert_events(
        since=_iso(NOW - timedelta(hours=1)),
        until=_iso(NOW + timedelta(hours=1)),
    )
    assert total == 2
    by_rule = {e["rule_id"]: e for e in entries}
    assert by_rule[81]["client_userid"] == 555
    assert by_rule[81]["client_name"] == "Gap Client"
    assert by_rule[81]["rebate_30d"] is None
    assert by_rule[121]["client_userid"] == 166818
    assert by_rule[121]["rebate_30d"] == pytest.approx(15001.0)
    # Both carry the same window_date through the 3-way COALESCE.
    assert by_rule[81]["window_date"] == TODAY
    assert by_rule[121]["window_date"] == TODAY


# ── Mail source registry + template ────────────────────────────────────────

def test_registry_has_rebate_arb_source():
    src = registry.get_source("rebate_arb")
    assert src is not None
    assert src["rule_id_range"] == (121, 130)
    assert callable(src["template_builder"])
    registry.validate_rule_ids("rebate_arb", [121])
    with pytest.raises(ValueError):
        registry.validate_rule_ids("rebate_arb", [120])
    with pytest.raises(ValueError):
        registry.validate_rule_ids("rebate_arb", [131])


def test_match_context_requires_detail_row():
    assert match_context({"rebate_30d": None, "combined_30d": 1.0}) is None
    ctx = match_context({"rebate_30d": 600.0, "combined_30d": 10.0})
    assert ctx == {"rebate_30d": 600.0, "combined_30d": 10.0}


def test_generic_conditions_evaluate_on_rebate_fields():
    from app.services.alert_mail_dispatcher import evaluate_generic_conditions
    alert = dict(TEST_SEND_SAMPLE_ALERT)
    tree = {"logic": "and", "conditions": [
        {"field": "rebate_30d", "op": ">=", "value": 10000},
        {"field": "total_pl_30d", "op": "<", "value": 0},
    ]}
    assert evaluate_generic_conditions(alert, tree, SOURCE) is not None
    tree_miss = {"logic": "and", "conditions": [
        {"field": "rebate_30d", "op": ">=", "value": 99999},
    ]}
    assert evaluate_generic_conditions(alert, tree_miss, SOURCE) is None


def test_digest_template_follows_alert_email_style():
    alert = dict(TEST_SEND_SAMPLE_ALERT)
    match = {**match_context(alert), "labels": ["A - rebate_30d >= 500"]}
    subject, body = build_rebate_arb_digest_email(
        [(alert, match)],
        subscription={"name": "test sub", "updated_at": "2026-07-12"},
        sibling_map={},
        test=True,
    )
    assert subject.startswith("[TEST] [风控告警] 返佣套利 Rebate Arbitrage")
    # No emojis anywhere (alert-email-style hard rule #1).
    assert all(ord(ch) < 0x2600 or 0x4E00 <= ord(ch) <= 0x9FFF for ch in subject + body)
    assert "返佣套利" in body                      # bilingual section title
    assert "166818" in body                        # client id rendered
    assert "15,001.00" in body                     # rebate amount
    assert "max-width:600px" in body               # mobile width bound
    assert "kieran.xiang@kohleservices.com" in body  # footer contact
    assert "MT Time" in body and "HK Time" in body


def test_rules_loader_reflects_env_thresholds(monkeypatch):
    monkeypatch.setenv("REBATE_ARB_MIN_REBATE_USD", "750")
    monkeypatch.setenv("REBATE_ARB_RULE2_MIN_REBATE_USD", "250")
    rules = SOURCE["rules_loader"]()
    assert len(rules) == 2
    assert rules[0]["id"] == 121
    assert rules[0]["params"]["min_rebate_usd"] == 750.0
    assert rules[1]["id"] == 122
    assert rules[1]["params"]["min_rebate_usd"] == 250.0
    assert "min_combined_usd" not in rules[1]["params"]  # 122 = rebate-only


def test_rules_loader_rule2_disabled_flag(monkeypatch):
    monkeypatch.setenv("REBATE_ARB_RULE2_ENABLED", "false")
    rules = SOURCE["rules_loader"]()
    assert [r["id"] for r in rules] == [121, 122]
    assert rules[1]["enabled"] is False


def test_test_send_uses_fallback_sample(temp_db, monkeypatch):
    """End-to-end test-send: no live alerts → the frozen sample renders."""
    from app.services import alert_mail_dispatcher as amd

    sub_id = rm_db.insert_mail_subscription({
        "name": "rebate arb watch", "module": "rebate_arb", "rule_ids": None,
        "conditions_json": "{}", "mail_to": "kieran.xiang@kohleservices.com",
        "mail_cc": None, "mode": "realtime", "cooldown_min": 0,
        "digest_time": None, "enabled": 1,
        "updated_at": _iso(NOW), "updated_by": "test",
    })
    sent = []

    def capture(*, subject, body, to, cc=None):
        sent.append({"subject": subject, "body": body, "to": to})

    result = amd.send_test_email_for_subscription(sub_id, send_fn=capture)
    assert result["used_fallback"] is True
    assert len(sent) == 1
    assert sent[0]["subject"].startswith("[TEST]")
    assert "166818" in sent[0]["body"]


# ── scan_rebate_arb orchestration (fake DB layer, real skeleton) ──────────
# This lazy-baseline + tick skeleton is the template for the next 2-3 rules,
# so the orchestration itself (window tiling, MT-day rollover rebuild,
# dedup-fetcher fail-open) gets covered here with the module-level query
# functions monkeypatched out.

class _FakeConn:
    def __init__(self):
        self.closed = 0

    def close(self):
        self.closed += 1


@pytest.fixture
def fresh_baseline():
    """The daily baseline is a module-level cache — isolate it per test."""
    svc.reset_baseline_cache()
    yield
    svc.reset_baseline_cache()


def _wire_scan_fakes(monkeypatch, mt_clock, trades_calls, rebate_calls):
    """Monkeypatch the DB layer under scan_rebate_arb; record window params.

    Trade leg: loginSid 1-100 (userid 7) appears in BOTH the baseline result
    and the increment result — the merge must add them once each, never
    double-count either leg. Rebate leg: same loginSid, $1,000 (>= $500 floor).
    """
    monkeypatch.setattr(svc, "_mt_now", lambda: mt_clock["now"])
    monkeypatch.setattr(svc, "_get_connection", lambda settings: _FakeConn())

    def fake_trades_agg(conn, *, date_from, date_to_exclusive):
        trades_calls.append((date_from, date_to_exclusive))
        if (date_to_exclusive - date_from).days == 2:
            # Mutable two-day increment [T-1, T+1).
            return {"1-100": _trade_row("1-100", 7, pnl=-100.0, lots=5.0, n=2)}
        # Frozen baseline [T-30, T-2].
        return {"1-100": _trade_row("1-100", 7, pnl=-500.0, lots=20.0, n=10)}

    def fake_rebate_agg(conn, *, date_from):
        rebate_calls.append(date_from)
        return [_rebate_row("1-100", 7, 1000.0)]

    monkeypatch.setattr(svc, "_query_trades_agg", fake_trades_agg)
    monkeypatch.setattr(svc, "_query_rebate_agg", fake_rebate_agg)
    monkeypatch.setattr(svc, "_query_net_deposit_split", lambda conn, u: {})
    monkeypatch.setattr(svc, "_query_equity_by_userid", lambda conn, u: {})


def test_scan_window_tiling_and_mt_day_rollover(monkeypatch, fresh_baseline):
    """Two ticks in one MT day, then one after MT midnight.

    Asserts: (a) the expensive baseline query runs once per MT day with the
    post-fix window [T-30, T-2] (half-open < T-1); (b) every tick reads the
    two-day increment [T-1, T+1); (c) a loginSid present on baseline +
    increment + rebate legs yields ONE client with additively merged numbers;
    (d) pushing _mt_now past MT midnight rebuilds the baseline on the new
    trading day's windows.
    """
    day1 = datetime(2026, 7, 10, 12, 0, 0)  # naive MT-local, mid-session
    t = day1.date()
    mt_clock = {"now": day1}
    trades_calls: list = []
    rebate_calls: list = []
    _wire_scan_fakes(monkeypatch, mt_clock, trades_calls, rebate_calls)

    # Tick 1: cold start → baseline rebuild + increment.
    r1 = svc.scan_rebate_arb(object())
    assert r1["baseline_rebuilt"] is True
    assert r1["window_date"] == t.isoformat()
    assert trades_calls == [
        (t - timedelta(days=30), t - timedelta(days=1)),   # baseline < T-1
        (t - timedelta(days=1), t + timedelta(days=1)),    # increment [T-1, T+1)
    ]
    assert rebate_calls == [t - timedelta(days=30)]
    # Baseline end == increment start: half-open ranges tile [T-30, T+1)
    # with no gap and no overlap.
    assert trades_calls[0][1] == trades_calls[1][0]
    # Same loginSid on all legs → one client, legs summed exactly once.
    assert len(r1["alerts"]) == 1
    a = r1["alerts"][0]
    assert a["client_userid"] == 7
    assert a["total_pl_30d"] == pytest.approx(-600.0)   # -500 + -100
    assert a["rebate_30d"] == pytest.approx(1000.0)     # rebate leg once
    assert a["combined_30d"] == pytest.approx(400.0)
    assert a["order_count"] == 12

    # Tick 2, same MT day: baseline query must NOT rerun.
    mt_clock["now"] = day1 + timedelta(minutes=10)
    r2 = svc.scan_rebate_arb(object())
    assert r2["baseline_rebuilt"] is False
    assert trades_calls[2:] == [(t - timedelta(days=1), t + timedelta(days=1))]
    assert len(trades_calls) == 3
    assert len(rebate_calls) == 2  # rebate is a full re-read every tick

    # Tick 3, past MT midnight: new trading day → baseline rebuilt on T+1.
    mt_clock["now"] = day1 + timedelta(days=1)
    t2 = t + timedelta(days=1)
    r3 = svc.scan_rebate_arb(object())
    assert r3["baseline_rebuilt"] is True
    assert r3["window_date"] == t2.isoformat()
    assert trades_calls[3:] == [
        (t2 - timedelta(days=30), t2 - timedelta(days=1)),
        (t2 - timedelta(days=1), t2 + timedelta(days=1)),
    ]
    assert rebate_calls[2] == t2 - timedelta(days=30)


def test_scan_dedup_fetcher_failure_fails_open(monkeypatch, fresh_baseline):
    """alerted_userids_fetcher raising must NOT drop alerts (documented
    fail-open: duplicates are the lesser evil for a risk feed)."""
    mt_clock = {"now": datetime(2026, 7, 10, 12, 0, 0)}
    _wire_scan_fakes(monkeypatch, mt_clock, [], [])

    def boom(window_date: str):
        raise RuntimeError("sqlite locked")

    result = svc.scan_rebate_arb(object(), alerted_userids_fetcher=boom)
    assert len(result["alerts"]) == 1
    assert result["alerts"][0]["client_userid"] == 7

    # Control: a WORKING fetcher that already knows the client suppresses it.
    result2 = svc.scan_rebate_arb(
        object(), alerted_userids_fetcher=lambda wd: {7}
    )
    assert result2["alerts"] == []


# ── rule 122 inside scan_rebate_arb (same fake DB layer) ───────────────────

def _wire_scan_fakes_two_clients(monkeypatch, mt_clock, enrich_calls=None):
    """Client 7: rebate $1,000, combined +$900 → 121. Client 8: rebate $300,
    combined -$4,700 → 122 only (fails 121 on both floor and combined)."""
    monkeypatch.setattr(svc, "_mt_now", lambda: mt_clock["now"])
    monkeypatch.setattr(svc, "_get_connection", lambda settings: _FakeConn())

    def fake_trades_agg(conn, *, date_from, date_to_exclusive):
        if (date_to_exclusive - date_from).days == 2:
            return {}  # increment empty; all history in the baseline
        return {
            "1-100": _trade_row("1-100", 7, pnl=-100.0),
            "1-200": _trade_row("1-200", 8, pnl=-5000.0),
        }

    monkeypatch.setattr(svc, "_query_trades_agg", fake_trades_agg)
    monkeypatch.setattr(svc, "_query_rebate_agg", lambda conn, *, date_from: [
        _rebate_row("1-100", 7, 1000.0),
        _rebate_row("1-200", 8, 300.0),
    ])

    def fake_nd(conn, userids):
        if enrich_calls is not None:
            enrich_calls.append(sorted(userids))
        return {}

    monkeypatch.setattr(svc, "_query_net_deposit_split", fake_nd)
    monkeypatch.setattr(svc, "_query_equity_by_userid", lambda conn, u: {})


def test_scan_121_wins_over_122_same_tick(monkeypatch, fresh_baseline):
    """Client 7 clears BOTH triggers (rebate 1000 >= 200 too) but must emit
    only the stronger 121; client 8 gets 122. Enrichment covers the union."""
    enrich_calls: list = []
    _wire_scan_fakes_two_clients(
        monkeypatch, {"now": datetime(2026, 7, 10, 12, 0, 0)}, enrich_calls
    )
    result = svc.scan_rebate_arb(object())
    by_client = {a["client_userid"]: a for a in result["alerts"]}
    assert len(result["alerts"]) == 2  # one alert per client, no 121+122 double
    assert by_client[7]["rule_id"] == 121
    assert by_client[7]["rule_label"] == svc.REBATE_ARB_RULE_LABEL
    assert by_client[8]["rule_id"] == 122
    assert by_client[8]["rule_label"] == svc.REBATE_ARB_RULE2_LABEL
    assert by_client[8]["combined_30d"] == pytest.approx(-4700.0)
    assert enrich_calls == [[7, 8]]  # net-deposit split queried for the union


def test_scan_already_alerted_suppresses_122(monkeypatch, fresh_baseline):
    """The band-scoped dedup set gates 122 too — client 8 already alerted
    today (by whichever band rule) must not re-emit."""
    _wire_scan_fakes_two_clients(
        monkeypatch, {"now": datetime(2026, 7, 10, 12, 0, 0)}
    )
    result = svc.scan_rebate_arb(
        object(), alerted_userids_fetcher=lambda wd: {8}
    )
    assert [a["client_userid"] for a in result["alerts"]] == [7]
    assert result["alerts"][0]["rule_id"] == 121


def test_scan_disabled_flag_suppresses_122(monkeypatch, fresh_baseline):
    _wire_scan_fakes_two_clients(
        monkeypatch, {"now": datetime(2026, 7, 10, 12, 0, 0)}
    )
    monkeypatch.setenv("REBATE_ARB_RULE2_ENABLED", "false")
    result = svc.scan_rebate_arb(object())
    assert [a["rule_id"] for a in result["alerts"]] == [121]


def test_scan_rule2_env_threshold_applies(monkeypatch, fresh_baseline):
    """Raising the 122 floor above client 8's $300 rebate drops the hit."""
    _wire_scan_fakes_two_clients(
        monkeypatch, {"now": datetime(2026, 7, 10, 12, 0, 0)}
    )
    monkeypatch.setenv("REBATE_ARB_RULE2_MIN_REBATE_USD", "301")
    result = svc.scan_rebate_arb(object())
    assert [a["rule_id"] for a in result["alerts"]] == [121]


# ── Scheduler wiring invariants ────────────────────────────────────────────

def test_band_121_130_owned_by_slow_tier_partition():
    from app.core import burst_open_scheduler as bs
    assert bs._MAX_ALLOCATED_RULE_ID == 130
    for rid in range(121, 131):
        assert not bs._is_fast_tier_rule_id(rid)


def test_scheduler_run_persists_and_dedups(temp_db, monkeypatch):
    """_run_rebate_arb_scan wires scan → dedup fetcher → append."""
    from app.core import burst_open_scheduler as bs

    calls = {}

    def fake_scan(settings, *, alerted_userids_fetcher=None, **kw):
        calls["fetcher"] = alerted_userids_fetcher
        alerts = svc.build_alerts(
            _one_hit(), window_date=TODAY, scanned_at=_iso(NOW),
            net_deposit_map={}, equity_map={},
        )
        # Apply dedup the way the real scan does.
        alerted = alerted_userids_fetcher(TODAY) if alerted_userids_fetcher else set()
        alerts = [a for a in alerts if a["client_userid"] not in alerted]
        return {"alerts": alerts, "scan_time_ms": 5, "window_date": TODAY,
                "baseline_rebuilt": False, "clients_evaluated": 1}

    monkeypatch.setattr(
        "app.services.rule_rebate_arb_service.scan_rebate_arb", fake_scan
    )
    monkeypatch.setattr(
        "app.core.config.get_settings", lambda: object(), raising=False
    )

    bs._run_rebate_arb_scan()
    assert rm_db.get_rebate_arb_alerted_userids(TODAY) == {166818}
    assert calls["fetcher"] is rm_db.get_rebate_arb_alerted_userids

    # Second tick: the durable dedup set suppresses a duplicate row.
    bs._run_rebate_arb_scan()
    rows = rm_db.fetch_recent_rebate_arb_alerts(limit=10)
    assert len(rows) == 1
