"""
Pure math for the nightly MDD leg of the client-metrics snapshot (OPT-0060).

Everything DB-free and unit-testable: the TWR (time-weighted return) unit-value
recurrence, the clamp, the 3-condition re-base, the 5 anchored-suffix window
accumulators, and the client-level MAX aggregation with its gates.

Money convention: `own equity` = endingEquity − endingCredit (bonus credit
stripped), CEN legs already ÷100 by the caller. One AccountSeries per loginSid
(2026-09-03 decision 1: client MDD = MAX over the client's accounts, each
account folded as its own independent series — summing dilutes: one blown
account + one parked account would average into a mid-sized drawdown).

TWR recurrence (2026-09-03 cold-review R1 corrected version — the original
`(own_t − F_t) / own_{t-1}` on a large-deposit/small-loss day clamps u to zero
forever and fakes a 100% MDD):

    F_t > 0 (net inflow day):   ret = own_t / (own_{t-1} + F_t)
    F_t ≤ 0:                    ret = (own_t − F_t) / own_{t-1}
    u_t = u_{t-1} × max(ret, 0)          # clamp fires only on a real blow-up

Re-base (open a new segment, u back to 1) when ANY of:
    ① own_{t-1} ≤ 0                      # blown up, client refunded
    ② u == 0 and F_t > 0                 # came back after a clamp-to-zero
    ③ |F_t| > 10 × own_{t-1}             # flow dwarfs the stock, ratio garbage

G5 (2026-09-03 decision 3): a segment only produces TWR samples when its
starting own equity ≥ $500 — kills the uid-149035-style +140,271% artifacts
born from near-zero starting capital.

MDD is computed on the u series per window, each window keeping its own local
peak, so `MDD_30d ≤ MDD_90d ≤ MDD_180d ≤ MDD_365d ≤ MDD_all` holds by
construction on the RAW values (gates can still NULL individual windows).

Multi-metric slot (decision 6/7: OPT-0020 Sharpe/Consistency NOT merged this
round): AccountSeries.push() is the single place every per-day sample flows
through — a future metric adds accumulators here and fields on the result
tuples, without touching the refresh job's streaming skeleton.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Anchored suffix windows, in days; None = full history (2021-07 onwards — the
# stats_balances coverage floor, NOT the account's true lifetime).
WINDOWS: tuple[int | None, ...] = (30, 90, 180, 365, None)
N_WINDOWS = len(WINDOWS)
WINDOW_KEYS: tuple[str, ...] = ("30d", "90d", "180d", "365d", "all")

# G2 — minimum in-window TWR samples per window before the value is shown.
G2_MIN_SAMPLES: dict[str, int] = {
    "30d": 20, "90d": 60, "180d": 120, "365d": 240, "all": 90,
}
# G1 — client-level peak own equity floor (accounts that never held money
# produce a flat, meaningless u series that reads as "very stable").
G1_MIN_PEAK_OWN = 500.0
# G3 — client-level active trading days floor (aligned with OPT-0020's Sharpe gate).
G3_MIN_ACTIVE_TRADE_DAYS = 30
# G5 — re-base segments only produce samples when they start with real money.
G5_MIN_SEG_START_OWN = 500.0
# Re-base condition ③ multiplier (calibration constant from the OPT-0060 item).
REBASE_FLOW_MULT = 10.0

# Per-window gate status enum (stored per window — R6: a client who blew up six
# months ago and re-funded has a perfectly valid 30d MDD; a single client-level
# flag would either hide it forever or gate nothing).
STATUS_OK = "ok"
STATUS_INSUFFICIENT = "insufficient_data"   # G2 failed / no samples at all
STATUS_WIPED_OUT = "wiped_out"              # window contains only dead (u=0) samples
STATUS_LOW_PEAK = "low_peak"                # G1 failed (client-level)
STATUS_LOW_ACTIVITY = "low_activity"        # G3 failed (client-level)


class AccountSeries:
    """Folds one account's chronological (date-ordered) balance rows.

    Feed rows strictly in ascending date order. `active_mask` marks which of
    the 5 windows the row's date falls into (all windows share "today" as their
    right edge, so the mask is a function of the date only and the caller can
    compute it once per calendar day, not once per row).
    """

    __slots__ = (
        "prev_own", "unit", "seg_qualifies",
        "peak_own", "negative_equity", "last_dead_ord", "died_broke", "started",
        "win_peak", "win_mdd", "win_samples", "win_live", "win_dead",
    )

    def __init__(self) -> None:
        self.started = False
        self.prev_own = 0.0
        self.unit = 1.0
        self.seg_qualifies = False
        self.peak_own = 0.0
        self.negative_equity = False
        self.last_dead_ord: int | None = None  # date u last hit 0 (wipeout date)
        # HOW the account last reached own ≤ 0: True = the unit clamped to 0
        # (the money was LOST), False = a withdrawal drained it (the money was
        # TAKEN OUT — u stays ~1, no loss). Without this split a client who
        # cleanly closed out reads as "blown up" (found on uid 100241 during
        # the 2026-09-03 reconciliation).
        self.died_broke = False
        self.win_peak: list[float | None] = [None] * N_WINDOWS
        self.win_mdd: list[float] = [0.0] * N_WINDOWS
        self.win_samples: list[int] = [0] * N_WINDOWS
        self.win_live: list[bool] = [False] * N_WINDOWS
        # A dead observation (own ≤ 0) fell inside the window. Dead stretches
        # re-base into non-qualifying dust segments and produce NO samples, so
        # without this flag a fully-dead window is indistinguishable from a
        # window with no data at all (STATUS_WIPED_OUT vs STATUS_INSUFFICIENT).
        self.win_dead: list[bool] = [False] * N_WINDOWS

    def _record(self, u: float, active_mask: tuple[bool, ...] | list[bool]) -> None:
        """Register one TWR observation into every active window. The
        segment-opening day contributes u = 1.0 — without it a decline that
        starts on the segment's second day would be measured against that
        day's already-fallen unit and the first leg of the drawdown vanishes.
        """
        for i in range(N_WINDOWS):
            if not active_mask[i]:
                continue
            self.win_samples[i] += 1
            if u > 0.0:
                self.win_live[i] = True
                pk = self.win_peak[i]
                if pk is None or u > pk:
                    self.win_peak[i] = u
                    pk = u
                dd = (pk - u) / pk
                if dd > self.win_mdd[i]:
                    self.win_mdd[i] = dd
            elif self.win_peak[i] is not None:
                # Dead observation after a live peak inside the window: the
                # fall to zero IS the drawdown. (peak−0)/peak = 100%.
                self.win_mdd[i] = 1.0

    def push(
        self,
        cur_ord: int,
        own: float,
        external_flow: float,
        active_mask: tuple[bool, ...] | list[bool],
    ) -> None:
        """One balance row. `own` = endingEquity − endingCredit (CEN ÷100 done
        by the caller). `external_flow` = deposits/withdrawals/transfers dated
        in (prev_row_date, this_date].

        ⚠ F3 correction (2026-09-03, deviates from the OPT-0060 item's wording
        on purpose): credit deltas do NOT enter F_t. The own-curve already
        strips credit — a grant (eq +C, cr +C) and a forfeit-on-withdrawal
        (eq −C, cr −C) both leave `own` untouched, so folding Δcredit into F_t
        double-counts them: a $1,000 grant would read as
        own/(own+1000) < 1 = a fake drawdown on a day nothing was lost, and a
        forfeit would read as a fake gain. The one event the own-curve books
        wrong (credit→balance conversion, a real inflow booked as trading
        profit) has the OPPOSITE Δcredit sign, so no single Δcredit term can
        fix it. See docs/features/client-return-mdd.md for the worked numbers.
        """
        if own > self.peak_own:
            self.peak_own = own
        if own < 0:
            self.negative_equity = True
        dead_row = own <= 0.0
        if dead_row:
            for i in range(N_WINDOWS):
                if active_mask[i]:
                    self.win_dead[i] = True

        recorded = False
        if not self.started:
            # First row opens the initial segment. It observes u = 1.0.
            self.started = True
            self.prev_own = own
            self.unit = 1.0
            self.seg_qualifies = own >= G5_MIN_SEG_START_OWN
            if self.seg_qualifies:
                self._record(1.0, active_mask)
                recorded = True
        else:
            f_t = external_flow  # F3 correction: credit deltas stay out, see above
            prev_own = self.prev_own

            # Re-base triggers — checked BEFORE the recurrence because each one
            # describes a day where the ratio own_t / prev_own is meaningless.
            if (
                prev_own <= 0.0
                or (self.unit == 0.0 and f_t > 0.0)
                or abs(f_t) > REBASE_FLOW_MULT * prev_own
            ):
                self.unit = 1.0
                self.seg_qualifies = own >= G5_MIN_SEG_START_OWN
                if dead_row and prev_own > 0.0:
                    # ③-triggered exit: a flow (huge withdrawal) drained the
                    # account to ≤ 0 — money taken out, not lost.
                    self.died_broke = False
                self.prev_own = own
                # Units are only comparable WITHIN a segment: without this
                # reset, a ③-triggered re-base (big deposit, nothing lost)
                # would measure the new segment's u=1.0 against the old
                # segment's peak (say 2.0) and fabricate a 50% drawdown. Each
                # segment brings its own peaks; the window keeps the max
                # drawdown across segments via win_mdd.
                self.win_peak = [None] * N_WINDOWS
                if self.seg_qualifies:
                    self._record(1.0, active_mask)
                    recorded = True
            else:
                if f_t > 0.0:
                    ret = own / (prev_own + f_t)
                else:
                    ret = (own - f_t) / prev_own
                was_alive = self.unit > 0.0
                u = self.unit * (ret if ret > 0.0 else 0.0)
                self.unit = u
                self.prev_own = own
                if was_alive and u == 0.0:
                    self.last_dead_ord = cur_ord
                if dead_row:
                    # u clamped to 0 on this dead row = the fall was a LOSS;
                    # u still positive (e.g. own hit 0 via a withdrawal whose
                    # F makes ret ≈ 1) = the money left through the door.
                    self.died_broke = u == 0.0
                if self.seg_qualifies:
                    self._record(u, active_mask)
                    recorded = True

        # A dead stretch re-bases into dust segments that record nothing, yet
        # the account IS being observed — a window that watched it alive and
        # then dead has plenty of data for its 100%. Count dead observations
        # toward G2 in windows that already saw the account alive (never in
        # never-alive windows: those stay sample-less so aggregation reads
        # them as wiped_out via win_dead, not as a fake candidate).
        if dead_row and not recorded:
            for i in range(N_WINDOWS):
                if active_mask[i] and self.win_live[i]:
                    self.win_samples[i] += 1

    @property
    def ended_dead(self) -> bool:
        """Currently wiped: the last observed own equity is ≤ 0. (Checking
        unit == 0 would be wrong — a dead stretch re-bases unit back to 1.0
        inside a dust segment while the account is still very much dead.)"""
        return self.started and self.prev_own <= 0.0


@dataclass
class ClientMddResult:
    """Client-level rollup of one or more AccountSeries (MAX convention)."""

    mdd: dict[str, float | None] = field(default_factory=dict)       # key -> value or None
    status: dict[str, str] = field(default_factory=dict)             # key -> STATUS_*
    samples: dict[str, int] = field(default_factory=dict)            # key -> max samples
    negative_equity: bool = False
    wipeout: bool = False                # ALL accounts currently dead
    wipeout_ord: int | None = None       # latest u→0 date among dead accounts
    account_count: int = 0
    peak_own: float = 0.0


def aggregate_client(
    accounts: list[AccountSeries],
    active_trade_days: int,
) -> ClientMddResult:
    """MAX over per-account window MDDs, gated.

    Per window: an account is a candidate when it clears G2 (samples) and has
    at least one live sample (a window that is one flat dead line has no
    drawdown by construction — surfacing its 0.0 would rank the deadest
    accounts as the most stable, the exact trap this OPT exists to avoid).
    """
    res = ClientMddResult()
    res.account_count = len(accounts)
    started = [a for a in accounts if a.started]
    for a in started:
        res.negative_equity = res.negative_equity or a.negative_equity
        if a.peak_own > res.peak_own:
            res.peak_own = a.peak_own
    dead = [a for a in started if a.ended_dead]
    # "Wiped out" needs BOTH: every account currently at ≤ 0 AND at least one
    # of them got there by LOSING the money (died_broke). All-dead via clean
    # full withdrawals is an account closure, not a blow-up.
    res.wipeout = (
        bool(started)
        and len(dead) == len(started)
        and any(a.died_broke for a in dead)
    )
    if res.wipeout:
        ords = [a.last_dead_ord for a in dead if a.died_broke and a.last_dead_ord is not None]
        res.wipeout_ord = max(ords) if ords else None

    client_gate: str | None = None
    if res.peak_own < G1_MIN_PEAK_OWN:
        client_gate = STATUS_LOW_PEAK
    elif active_trade_days < G3_MIN_ACTIVE_TRADE_DAYS:
        client_gate = STATUS_LOW_ACTIVITY

    for i, key in enumerate(WINDOW_KEYS):
        max_samples = max((a.win_samples[i] for a in started), default=0)
        res.samples[key] = max_samples
        if client_gate is not None:
            res.mdd[key] = None
            res.status[key] = client_gate
            continue
        candidates = [
            a.win_mdd[i]
            for a in started
            if a.win_samples[i] >= G2_MIN_SAMPLES[key] and a.win_live[i]
        ]
        if candidates:
            res.mdd[key] = max(candidates)
            res.status[key] = STATUS_OK
        elif any(a.win_dead[i] for a in started):
            # No qualifying live series, but the window did contain dead
            # (own ≤ 0) observations — a wiped stretch. Dead stretches re-base
            # into dust segments that produce no samples, so win_dead (not
            # sample counts) is what tells "wiped the whole window" apart from
            # "no data at all". NULL, never 0%.
            res.mdd[key] = None
            res.status[key] = STATUS_WIPED_OUT
        else:
            res.mdd[key] = None
            res.status[key] = STATUS_INSUFFICIENT
    return res


def window_start_ordinals(today_ord: int) -> list[int]:
    """Left edge (inclusive ordinal) of each window, anchored on today."""
    return [today_ord - w + 1 if w is not None else -(10**9) for w in WINDOWS]
