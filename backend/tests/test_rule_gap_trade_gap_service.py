"""Unit tests for the two OR'd hardcoded gap-trade rules (rule_id 81).

Covers the pure threshold evaluator `evaluate_gap_rules`:

    Rule 1 (net withdrawer):  net_deposit <= 0 AND profit >= $100           -> "neg_deposit"
    Rule 2 (positive deposit): net_deposit > 0 AND ratio >= 1x AND profit >= $100 -> "ratio_x1"

`profit >= $100` is a hard floor on BOTH rules. A missing net-deposit
lookup (None) matches neither rule.
"""
from datetime import datetime, timedelta

from app.services.rule_gap_trade_gap_service import (
    evaluate_gap_rules,
    _resolve_open_band,
    GAP81_OPEN_BAND_START_HOUR,
    HARDCODED_PROFIT_GATE_USD,
    HARDCODED_RATIO_MIN,
)


def _first_weekday(target_weekday: int) -> datetime:
    """Midnight of the first 2026-06 day matching ``target_weekday`` (Mon=0)."""
    d = datetime(2026, 6, 1)
    while d.weekday() != target_weekday:
        d += timedelta(days=1)
    return d


def test_constants_pinned():
    # Guard against accidental drift of the hardcoded thresholds.
    assert HARDCODED_PROFIT_GATE_USD == 100.0
    assert HARDCODED_RATIO_MIN == 1.0


# --- Rule 2: positive net deposit (ratio AND $100 floor) -------------------

def test_rule2_fires_ratio_and_profit_both_met():
    # ratio = 2000/1000 = 2.0 >= 1 AND profit 2000 >= 100
    assert evaluate_gap_rules(2000.0, 1000.0) == (True, "ratio_x1")


def test_rule2_exact_boundary_ratio_1x_profit_at_floor():
    # ratio exactly 1.0, profit exactly at the $100 floor -> both boundaries inclusive
    assert evaluate_gap_rules(100.0, 100.0) == (True, "ratio_x1")


def test_rule2_ratio_below_1x_does_not_fire_even_with_big_profit():
    # Big-deposit client: $10k profit but only 0.1x of net deposit -> NO fire.
    # The $100 floor does NOT rescue a sub-1x ratio (this is the AND, the key
    # reason there is no absolute-only path).
    assert evaluate_gap_rules(10000.0, 100000.0) == (False, None)


def test_rule2_ratio_met_but_profit_below_floor_does_not_fire():
    # Tiny-deposit doubler: ratio 1.58 but profit only $15.80 -> below $100 floor.
    assert evaluate_gap_rules(15.8, 10.0) == (False, None)


def test_rule2_small_deposit_big_profit_fires():
    # $50 deposit, $1000 profit -> ratio 20x, profit >= floor.
    assert evaluate_gap_rules(1000.0, 50.0) == (True, "ratio_x1")


# --- Rule 1: net withdrawers (net_deposit <= 0) ----------------------------

def test_rule1_negative_deposit_with_profit_fires():
    assert evaluate_gap_rules(1500.0, -5000.0) == (True, "neg_deposit")


def test_rule1_zero_deposit_with_profit_fires():
    # net_deposit == 0 folds into Rule 1 (<= 0).
    assert evaluate_gap_rules(1200.0, 0.0) == (True, "neg_deposit")


def test_rule1_negative_deposit_below_floor_does_not_fire():
    # Net withdrawer but only $50 profit -> below $100 floor.
    assert evaluate_gap_rules(50.0, -5000.0) == (False, None)


def test_rule1_takes_precedence_for_nonpositive_deposit():
    # The ratio never applies when deposit <= 0; labelled neg_deposit.
    assert evaluate_gap_rules(99999.0, -1.0) == (True, "neg_deposit")


# --- Edge / guards ----------------------------------------------------------

def test_missing_net_deposit_never_fires():
    assert evaluate_gap_rules(50000.0, None) == (False, None)


def test_zero_profit_never_fires():
    assert evaluate_gap_rules(0.0, 1000.0) == (False, None)


def test_profit_just_below_floor_does_not_fire():
    assert evaluate_gap_rules(99.99, 10.0) == (False, None)
    assert evaluate_gap_rules(99.99, -100.0) == (False, None)


def test_negative_profit_never_fires():
    # A loss must never trigger, whatever the deposit state.
    assert evaluate_gap_rules(-500.0, 1000.0) == (False, None)
    assert evaluate_gap_rules(-500.0, -100.0) == (False, None)


# --- Open band resolution (pre-break session, weekend stepback) ------------

def test_open_band_normal_weekday_is_previous_calendar_day():
    # Wednesday scan -> open band is Tuesday 23:00–24:00.
    wed = _first_weekday(2)
    open_start, open_end = _resolve_open_band(wed)
    assert open_start == (wed - timedelta(days=1)).replace(hour=GAP81_OPEN_BAND_START_HOUR)
    assert open_end == wed  # Tuesday 24:00 == Wednesday 00:00
    assert open_start.weekday() == 1  # Tuesday


def test_open_band_monday_steps_back_over_weekend_to_friday():
    # Monday scan -> open band is FRIDAY 23:00–24:00 (held across the weekend).
    mon = _first_weekday(0)
    open_start, open_end = _resolve_open_band(mon)
    assert open_start.weekday() == 4  # Friday
    assert open_start == (mon - timedelta(days=3)).replace(hour=GAP81_OPEN_BAND_START_HOUR)
    assert open_end == mon - timedelta(days=2)  # Saturday 00:00


def test_open_band_saturday_steps_back_to_friday():
    # Saturday scan -> open band is FRIDAY 23:00–24:00 (Sat has no 23:00 session
    # but Friday is the prior trading day).
    sat = _first_weekday(5)
    open_start, open_end = _resolve_open_band(sat)
    assert open_start.weekday() == 4  # Friday
    assert open_start == (sat - timedelta(days=1)).replace(hour=GAP81_OPEN_BAND_START_HOUR)


def test_open_band_is_exactly_one_hour():
    for wd in range(0, 6):  # Mon..Sat scan days
        start, end = _resolve_open_band(_first_weekday(wd))
        assert end - start == timedelta(hours=1)
        assert start.hour == 23
