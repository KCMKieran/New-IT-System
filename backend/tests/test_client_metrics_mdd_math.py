"""OPT-0060 — unit tests for the pure MDD/TWR math (client_metrics_math).

Families:
  1. The R1-corrected recurrence — the original formula pinned u to a fake
     permanent 100% MDD on a big-deposit/small-loss day; the corrected one must
     read it as the small loss it was.
  2. Re-base triggers ①②③ and the G5 segment-start floor.
  3. Window accumulators — local peaks, the wipeout-to-zero 100%, dead-window
     detection, negative-equity clamp.
  4. Client aggregation — MAX convention, per-window gate statuses (R6), the
     G1/G2/G3 gates, and the NULL-never-0% rule.
  5. Monotonicity regression: MDD_30d ≤ 90d ≤ 180d ≤ 365d ≤ all on RAW
     (pre-gate) per-account values — a constructional property, asserted over
     seeded random series so a future edit to the recurrence that breaks the
     suffix-window structure goes red here.
"""

from __future__ import annotations

import random

import pytest

from app.services.client_metrics_math import (
    G1_MIN_PEAK_OWN,
    G2_MIN_SAMPLES,
    G3_MIN_ACTIVE_TRADE_DAYS,
    G5_MIN_SEG_START_OWN,
    N_WINDOWS,
    REBASE_FLOW_MULT,
    STATUS_INSUFFICIENT,
    STATUS_LOW_ACTIVITY,
    STATUS_LOW_PEAK,
    STATUS_OK,
    STATUS_WIPED_OUT,
    WINDOW_KEYS,
    AccountSeries,
    aggregate_client,
    window_start_ordinals,
)

TODAY = 800_000  # arbitrary ordinal anchor
STARTS = window_start_ordinals(TODAY)


def _mask(ord_: int) -> list[bool]:
    return [ord_ >= s for s in STARTS]


def _feed(days: list[tuple[float, float]], *, end_ord: int = TODAY) -> AccountSeries:
    """Feed (own, flow) tuples on consecutive days ending at end_ord."""
    acc = AccountSeries()
    start = end_ord - len(days) + 1
    for i, (own, flow) in enumerate(days):
        o = start + i
        acc.push(o, own, flow, _mask(o))
    return acc


ALL = N_WINDOWS - 1  # index of the 'all' window


class TestRecurrence:
    def test_r1_big_deposit_small_loss_is_a_small_drawdown(self):
        """Cold-review R1: $1,000 own, deposit $5,000, day ends $5,700 (a ~5%
        loss on the $6,000 employed). Original formula: (5700−5000)/1000 = 0.7
        → −30%; corrected: 5700/(1000+5000) = 0.95 → −5%."""
        acc = _feed([(1_000.0, 0.0), (5_700.0, 5_000.0)])
        assert acc.unit == pytest.approx(0.95)
        assert acc.win_mdd[ALL] == pytest.approx(0.05)

    def test_net_outflow_day_uses_the_original_formula(self):
        """Withdrawal of 400 with own 10,000 → 9,700: (9700 − (−400)) / 10000
        = 1.01 — a profitable day, no drawdown."""
        acc = _feed([(10_000.0, 0.0), (9_700.0, -400.0)])
        assert acc.unit == pytest.approx(1.01)
        assert acc.win_mdd[ALL] == 0.0

    def test_withdrawal_of_profit_is_not_a_drawdown(self):
        """理由 1 from the item: deposit 10k → grow to 12k → withdraw 6k.
        Naked equity says 50% MDD; TWR says 0%."""
        acc = _feed([
            (10_000.0, 0.0),
            (12_000.0, 0.0),
            (6_000.0, -6_000.0),
        ])
        assert acc.win_mdd[ALL] == pytest.approx(0.0)
        assert acc.unit == pytest.approx(1.2)

    def test_losses_compound_across_a_deposit(self):
        """理由 2 (scaled to stay under the ③ multiplier): two loss legs on
        either side of a deposit multiply — the deposit must not reset the
        drawdown base."""
        acc = _feed([
            (10_000.0, 0.0),
            (8_000.0, 0.0),           # ×0.800
            (52_200.0, 50_000.0),     # deposit day: 52200/58000 = ×0.9
        ])
        assert acc.unit == pytest.approx(0.72)
        assert acc.win_mdd[ALL] == pytest.approx(0.28)


class TestRebase:
    def test_flow_dwarfing_the_stock_rebases(self):
        """③: |F| > 10× prev own → the ratio is garbage; new segment, u=1."""
        acc = _feed([(1_000.0, 0.0), (96_000.0, 100_000.0)])
        assert acc.unit == 1.0
        assert acc.win_mdd[ALL] == 0.0  # nothing was lost
        assert acc.seg_qualifies is True  # new segment starts at 96,000

    def test_rebase_resets_window_peaks(self):
        """Units are only comparable within a segment. Grow to u=2.0, then a
        ③ re-base (big deposit, nothing lost): without the peak reset the new
        segment's u=1.0 would read as a 50% drawdown against the old peak."""
        acc = _feed([
            (1_000.0, 0.0),
            (2_000.0, 0.0),          # u = 2.0
            (51_000.0, 49_000.0),    # ③ re-base: new segment, u = 1.0
            (48_960.0, 0.0),         # −4% in the NEW segment
        ])
        assert acc.win_mdd[ALL] == pytest.approx(0.04)

    def test_blowup_then_refund_rebases(self):
        """①: prev own ≤ 0 → next row opens a new segment."""
        acc = _feed([
            (2_000.0, 0.0),
            (0.0, 0.0),          # wiped
            (1_000.0, 1_000.0),  # refunded → re-base (prev_own == 0)
            (900.0, 0.0),        # −10% in the NEW segment
        ])
        assert acc.unit == pytest.approx(0.9)
        # the wipe itself was recorded before the re-base
        assert acc.win_mdd[ALL] == pytest.approx(1.0)

    def test_clamp_to_zero_then_deposit_rebases(self):
        """②: u clamped to 0 (own collapsed while staying positive is not
        enough — force it via a negative own), then a deposit revives the
        series instead of pinning a fake permanent 100%."""
        acc = _feed([
            (2_000.0, 0.0),
            (-50.0, 0.0),         # negative own → ret < 0 → u = 0
            (-50.0, 0.0),         # prev_own < 0 → re-base (dust segment)
            (1_000.0, 1_050.0),   # flow > 10×|prev|→ re-base, qualifying seg
            (950.0, 0.0),
        ])
        assert acc.unit == pytest.approx(0.95)
        assert acc.negative_equity is True

    def test_g5_dust_segment_produces_no_samples(self):
        """A segment starting under $500 produces no TWR samples — the
        uid-149035-style +140,271% artifact killer."""
        acc = _feed([(100.0, 0.0), (400.0, 0.0), (450.0, 0.0)])
        assert acc.win_samples[ALL] == 0
        assert acc.win_mdd[ALL] == 0.0

    def test_g5_boundary_exactly_500_qualifies(self):
        acc = _feed([(G5_MIN_SEG_START_OWN, 0.0), (450.0, 0.0)])
        # segment-start day observes u=1.0, day 2 observes 0.9
        assert acc.win_samples[ALL] == 2
        assert acc.win_mdd[ALL] == pytest.approx(0.1)


class TestWindows:
    def test_wipeout_shows_100_in_windows_containing_the_fall(self):
        acc = _feed([(5_000.0, 0.0)] * 10 + [(0.0, 0.0)] * 5)
        assert acc.win_mdd[ALL] == pytest.approx(1.0)
        assert acc.ended_dead is True
        assert acc.last_dead_ord == TODAY - 4

    def test_window_fully_dead_is_flagged_dead_not_sampled(self):
        """Account died long ago: dead stretches re-base into dust segments
        producing NO samples, so the 30d window has zero samples but carries
        the dead flag — that flag is what lets aggregation say wiped_out
        instead of insufficient_data."""
        days = [(5_000.0, 0.0)] * 5 + [(0.0, 0.0)] * 300
        acc = _feed(days)
        i30 = WINDOW_KEYS.index("30d")
        assert acc.win_live[i30] is False
        assert acc.win_samples[i30] == 0
        assert acc.win_dead[i30] is True
        assert acc.win_live[ALL] is True  # the fall is visible in 'all'

    def test_local_peak_per_window(self):
        """A drawdown that happened before the window opened must not leak in:
        each window measures against its own local peak."""
        # 400 days: big early crash, then a mild dip inside the last 30 days
        days = [(10_000.0, 0.0), (4_000.0, 0.0)]          # −60% long ago
        days += [(4_000.0, 0.0)] * 370
        days += [(3_800.0, 0.0)] * 28                      # −5% recently
        acc = _feed(days)
        i30 = WINDOW_KEYS.index("30d")
        assert acc.win_mdd[i30] == pytest.approx(0.05, abs=1e-9)
        assert acc.win_mdd[ALL] == pytest.approx(0.62)     # 10000 → 3800

    def test_negative_equity_clamps_at_100(self):
        acc = _feed([(5_000.0, 0.0), (-1_200.0, 0.0)])
        assert acc.win_mdd[ALL] == pytest.approx(1.0)
        assert acc.negative_equity is True
        assert acc.unit == 0.0


class TestMonotonicity:
    """MDD_30d ≤ 90d ≤ 180d ≤ 365d ≤ all on the RAW per-account values.

    Asserted pre-gate on purpose (cold-review note): G2 NULLs individual
    windows independently, so the post-gate row is not monotone — the
    guarantee only exists on the raw accumulators.
    """

    @pytest.mark.parametrize("seed", range(20))
    def test_random_series_stay_monotone(self, seed):
        rng = random.Random(seed)
        n_days = rng.randint(5, 900)
        acc = AccountSeries()
        own = rng.uniform(0, 5_000)
        start = TODAY - n_days + 1
        for i in range(n_days):
            o = start + i
            flow = 0.0
            r = rng.random()
            if r < 0.05:
                flow = rng.uniform(1, 20_000)
            elif r < 0.10:
                flow = -rng.uniform(1, max(own, 1))
            own = max(-500.0, own + flow + rng.uniform(-0.3, 0.28) * max(own, 100))
            if rng.random() < 0.02:
                own = 0.0  # hard wipe
            acc.push(o, own, flow, _mask(o))
        raw = acc.win_mdd
        for i in range(1, N_WINDOWS):
            assert raw[i - 1] <= raw[i] + 1e-12, (
                f"seed={seed}: window {WINDOW_KEYS[i-1]} MDD {raw[i-1]} > "
                f"{WINDOW_KEYS[i]} MDD {raw[i]} — suffix-window structure broken"
            )


def _stable_account(n_days: int = 400, own: float = 10_000.0) -> AccountSeries:
    return _feed([(own, 0.0)] * n_days)


class TestAggregateClient:
    def test_max_convention_takes_the_worst_account(self):
        """One blown account + one parked account: SUM would average this into
        a mid-sized drawdown; MAX must report the blow-up."""
        blown = _feed([(5_000.0, 0.0)] * 350 + [(0.0, 0.0)] * 50)
        parked = _stable_account()
        res = aggregate_client([blown, parked], active_trade_days=100)
        assert res.mdd["all"] == pytest.approx(1.0)
        assert res.status["all"] == STATUS_OK
        assert res.account_count == 2
        assert res.wipeout is False  # one account is alive

    def test_r6_blown_then_refunded_client_keeps_a_valid_30d(self):
        """Blew up half a year ago, refunded, trading again: 'all' shows 100%,
        the 30d window is a legal fresh value — a client-level dead flag would
        have hidden it forever."""
        days = [(5_000.0, 0.0)] * 100 + [(0.0, 0.0)]
        days += [(3_000.0, 3_000.0)] + [(2_940.0 - i, 0.0) for i in range(299)]
        acc = _feed(days)
        res = aggregate_client([acc], active_trade_days=100)
        assert res.mdd["all"] == pytest.approx(1.0)
        assert res.status["30d"] == STATUS_OK
        assert res.mdd["30d"] is not None and res.mdd["30d"] < 0.15

    def test_wiped_out_window_is_null_never_zero(self):
        """The single most important rendering rule: a dead-flat window must
        come out as NULL/wiped_out, because 0% is the BEST score in the
        pick-the-stable direction."""
        acc = _feed([(5_000.0, 0.0)] * 60 + [(0.0, 0.0)] * 300)
        res = aggregate_client([acc], active_trade_days=100)
        assert res.mdd["30d"] is None
        assert res.status["30d"] == STATUS_WIPED_OUT
        assert res.mdd["all"] == pytest.approx(1.0)
        assert res.wipeout is True
        assert res.wipeout_ord == TODAY - 299

    def test_g2_insufficient_samples_null(self):
        acc = _stable_account(n_days=10)
        res = aggregate_client([acc], active_trade_days=100)
        assert res.mdd["30d"] is None
        assert res.status["30d"] == STATUS_INSUFFICIENT
        assert res.samples["30d"] == 10  # segment-start day observes u=1.0

    def test_g1_low_peak_gates_every_window(self):
        # G5 would suppress samples below $500 anyway; use a peak between the
        # two floors to isolate G1 (they share the $500 default today, so pin
        # the relationship: a client whose peak sits below G1 gets low_peak).
        acc = _stable_account(own=G1_MIN_PEAK_OWN - 1)
        res = aggregate_client([acc], active_trade_days=100)
        for key in WINDOW_KEYS:
            assert res.mdd[key] is None
            assert res.status[key] == STATUS_LOW_PEAK

    def test_g3_low_activity_gates_every_window(self):
        acc = _stable_account()
        res = aggregate_client([acc], active_trade_days=G3_MIN_ACTIVE_TRADE_DAYS - 1)
        for key in WINDOW_KEYS:
            assert res.mdd[key] is None
            assert res.status[key] == STATUS_LOW_ACTIVITY

    def test_clean_full_withdrawal_is_not_a_wipeout(self):
        """uid-100241 shape (2026-09-03 reconciliation find): a client who
        withdraws EVERYTHING ends at own = 0, but the money left through the
        door — u stays ~1 (no drawdown) and the client must not be flagged
        wiped out."""
        days = [(10_000.0, 0.0)] * 300 + [(0.0, -10_000.0)] + [(0.0, 0.0)] * 60
        acc = _feed(days)
        assert acc.ended_dead is True
        assert acc.died_broke is False
        assert acc.win_mdd[ALL] == pytest.approx(0.0)
        res = aggregate_client([acc], active_trade_days=100)
        assert res.wipeout is False
        assert res.mdd["all"] == pytest.approx(0.0)

    def test_blown_account_is_a_wipeout(self):
        acc = _feed([(10_000.0, 0.0)] * 300 + [(0.0, 0.0)] * 61)
        assert acc.died_broke is True
        res = aggregate_client([acc], active_trade_days=100)
        assert res.wipeout is True

    def test_negative_equity_bubbles_up(self):
        acc = _feed([(5_000.0, 0.0)] * 200 + [(-100.0, 0.0)] + [(5_000.0, 5_000.0)] * 199)
        res = aggregate_client([acc], active_trade_days=100)
        assert res.negative_equity is True

    def test_g2_thresholds_match_the_item(self):
        assert G2_MIN_SAMPLES == {"30d": 20, "90d": 60, "180d": 120, "365d": 240, "all": 90}
        assert REBASE_FLOW_MULT == 10.0
