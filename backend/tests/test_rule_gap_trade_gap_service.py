"""Unit tests for the two hardcoded gap-trade rules (rule_id 81).

Covers the pure threshold evaluator `evaluate_gap_rules`:

    Rule A: net_deposit > 0  AND ratio >= 1x AND profit >= $1000  -> "ratio_x1"
    Rule B: net_deposit <= 0 AND profit >= $1000                  -> "neg_deposit"
    profit >= $1000 is a universal gate.
"""
from app.services.rule_gap_trade_gap_service import (
    evaluate_gap_rules,
    HARDCODED_PROFIT_GATE_USD,
    HARDCODED_RATIO_MIN,
)


def test_constants_pinned():
    # Guard against accidental drift of the hardcoded thresholds.
    assert HARDCODED_PROFIT_GATE_USD == 1000.0
    assert HARDCODED_RATIO_MIN == 1.0


# --- Rule A: positive net deposit ------------------------------------------

def test_rule_a_fires_ratio_and_profit_both_met():
    # ratio = 2000/1000 = 2.0 >= 1 AND profit 2000 >= 1000
    assert evaluate_gap_rules(2000.0, 1000.0) == (True, "ratio_x1")


def test_rule_a_exact_boundary_ratio_1x_profit_1000():
    # ratio exactly 1.0, profit exactly 1000 -> both boundaries inclusive
    assert evaluate_gap_rules(1000.0, 1000.0) == (True, "ratio_x1")


def test_rule_a_ratio_below_1x_does_not_fire():
    # Big-deposit client: $10k profit but only 0.1x of net deposit -> NO fire.
    # This is the key behavioral change (AND, not OR).
    assert evaluate_gap_rules(10000.0, 100000.0) == (False, None)


def test_rule_a_ratio_met_but_profit_below_gate_does_not_fire():
    # Tiny-deposit doubler: ratio 1.58 but profit only $158 -> below $1000 gate.
    assert evaluate_gap_rules(158.0, 100.0) == (False, None)


def test_rule_a_small_deposit_big_profit_fires():
    # $50 deposit, $1000 profit -> ratio 20x, profit >= gate. No $100 floor.
    assert evaluate_gap_rules(1000.0, 50.0) == (True, "ratio_x1")


# --- Rule B: net withdrawers (net_deposit <= 0) ----------------------------

def test_rule_b_negative_deposit_with_profit_fires():
    assert evaluate_gap_rules(1500.0, -5000.0) == (True, "neg_deposit")


def test_rule_b_zero_deposit_with_profit_fires():
    # net_deposit == 0 folds into Rule B (<= 0).
    assert evaluate_gap_rules(1200.0, 0.0) == (True, "neg_deposit")


def test_rule_b_negative_deposit_below_gate_does_not_fire():
    # Net withdrawer but only $500 profit -> below $1000 gate.
    assert evaluate_gap_rules(500.0, -5000.0) == (False, None)


def test_rule_b_takes_precedence_for_nonpositive_deposit():
    # Even a "ratio" never applies when deposit <= 0; labeled neg_deposit.
    triggered, by = evaluate_gap_rules(99999.0, -1.0)
    assert triggered is True
    assert by == "neg_deposit"


# --- Edge / guards ----------------------------------------------------------

def test_missing_net_deposit_never_fires():
    assert evaluate_gap_rules(50000.0, None) == (False, None)


def test_zero_profit_never_fires():
    assert evaluate_gap_rules(0.0, 1000.0) == (False, None)


def test_profit_just_below_gate_does_not_fire():
    assert evaluate_gap_rules(999.99, 100.0) == (False, None)
    assert evaluate_gap_rules(999.99, -100.0) == (False, None)
