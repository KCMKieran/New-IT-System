"""OPT-0061 guardrails: floating-inclusive return + floating burden ratio.

Three families:

1. SQL-text anti-drift on the `rt` (profit_hist) leg — decision 1a narrowed its
   account scope to sid 1/5/6 non-demo so numerator and denominator
   (avg_daily_equity) cover the same accounts. Nothing else in the suite fails
   if a future edit drops the JOIN/filters again.

2. SQL-text anti-drift on the nightly refresh SQL — it full-scans
   stats_balances (21M rows) on the REPLICA and must carry a server-side
   MAX_EXECUTION_TIME hint (skill `db-timeout-guard`); the two-level
   (userId, date) aggregation is what makes first/last-day floats recoverable.

3. Unit tests for `_attach_roace_columns` — the low-equity gate semantics:
   ratio-gated rows get `capital_locked` (a signal, not missing data), noise
   filters (too young / too small) render null WITHOUT the flag, and the
   burden ratio is never gated.
"""

import re

from app.services.client_return_service import (
    _FLOAT_GATE_MIN_ACTIVE_DAYS,
    _FLOAT_GATE_MIN_AVG_EQUITY,
    _FLOAT_GATE_MIN_EQ_TO_BAL_RATIO,
    _attach_roace_columns,
    _build_phase2_sql,
)
from app.services.client_roace_refresh_service import (
    _MAX_EXECUTION_TIME_MS,
    _READ_TIMEOUT_SEC,
    _REFRESH_SQL,
)

_RAW_SQL = _build_phase2_sql("1,2,3", "2026-07-01", "2026-07-15")
_SQL = re.sub(r"--[^\n]*", "", _RAW_SQL)


def _rt_subquery() -> str:
    """Extract the LEFT JOIN (...) AS rt block from Phase 2 SQL."""
    m = re.search(r"LEFT JOIN \((.*?)\) AS rt\b", _SQL, re.DOTALL)
    assert m is not None, "profit_hist (rt) subquery not found"
    return m.group(1)


class TestProfitHistAccountScope:
    """Decision 1a: profit_hist must cover the same accounts as its ROACE
    denominator. Before OPT-0061 it summed EVERY account of the client (demo,
    wallet, other sids) — 2,044 clients differed, 540 by >$1,000."""

    def test_rt_leg_joins_mt4_users(self):
        rt = _rt_subquery()
        assert re.search(r"INNER JOIN\s+mt4_users", rt), (
            "profit_hist sums stats_trading_running_totals without joining "
            "mt4_users — there is no sid/demo information on that table, so the "
            "scope filter is impossible without the join"
        )

    def test_rt_leg_filters_sid(self):
        rt = _rt_subquery()
        assert "sid IN (1, 5, 6)" in rt, (
            "profit_hist leg lost the sid IN (1, 5, 6) filter — numerator scope "
            "no longer matches avg_daily_equity (denominator)"
        )

    def test_rt_leg_filters_demo(self):
        rt = _rt_subquery()
        assert re.search(r"NOT LIKE '%demo%'", rt), (
            "profit_hist leg lost the demo-group exclusion"
        )


class TestRefreshSqlGuards:
    def test_refresh_sql_has_server_side_timeout_hint(self):
        assert f"MAX_EXECUTION_TIME({_MAX_EXECUTION_TIME_MS})" in _REFRESH_SQL, (
            "nightly refresh SQL full-scans stats_balances on the replica with "
            "no server-side kill switch — a stuck statement outlives the client "
            "read_timeout as a zombie thread (2026-08-09/08-15 incidents)"
        )

    def test_hint_is_below_client_read_timeout(self):
        """Server must give up before the client walks away."""
        assert _MAX_EXECUTION_TIME_MS / 1000 < _READ_TIMEOUT_SEC

    def test_refresh_sql_selects_balance_credit_and_floats(self):
        for col in ("avg_daily_balance", "avg_daily_credit", "first_float", "last_float"):
            assert col in _REFRESH_SQL, f"refresh SQL no longer produces {col}"

    def test_refresh_sql_aggregates_per_user_and_date(self):
        """First/last-day floats need per-(userId, date) rows; the old
        'SUM everything / COUNT(DISTINCT date)' shortcut cannot produce them."""
        assert re.search(r"GROUP BY\s+mu2\.userId,\s*sb\.date", _REFRESH_SQL)

    def test_refresh_invalidates_client_return_cache_on_success(self):
        """Cold-review F2: a successful refresh must drop the cached /query
        blobs — they embed the previous snapshot's columns and would otherwise
        be served for up to their remaining 3h TTL."""
        import inspect

        from app.services import client_roace_refresh_service

        src = inspect.getsource(client_roace_refresh_service.refresh_all_clients)
        assert "app:client_return:cache:*" in src, (
            "refresh_all_clients no longer invalidates the client-return Redis "
            "cache after a successful run"
        )

    def test_refresh_sql_normalizes_cen_on_all_three_columns(self):
        for col in ("endingEquity", "endingBalance", "endingCredit"):
            assert re.search(
                rf"IF\(sb\.currency = 'CEN', sb\.{col} / 100\.0,\s*sb\.{col}\)",
                _REFRESH_SQL,
            ), f"{col} is summed without the CEN /100 branch"


def _snap(**overrides) -> dict:
    """A healthy snapshot row that passes every gate by default."""
    base = {
        "avg_daily_equity": 10_000.0,
        "avg_daily_balance": 12_000.0,
        "avg_daily_credit": 0.0,
        "first_float": -500.0,
        "last_float": -2_500.0,
        "active_days": 200,
        "refreshed_at": "2026-08-31 06:00:00",
    }
    base.update(overrides)
    return base


def _row(profit_hist: float = 1_000.0, client_id: int = 1) -> dict:
    return {"client_id": client_id, "profit_hist": profit_hist}


class TestAttachRoaceColumns:
    def test_happy_path_computes_both_new_columns(self):
        row = _row(profit_hist=3_000.0)
        _attach_roace_columns([row], {1: _snap()})
        # (3000 + (-2500 - -500)) / 10000 × 100 = 10.0
        assert row["return_with_floating"] == 10.0
        # (10000 - 12000 - 0) / 12000 × 100 = -16.67
        assert row["floating_burden_ratio"] == -16.67
        assert row["capital_locked"] is False
        assert row["return_on_avg_equity"] == 30.0

    def test_roace_unchanged_by_new_columns(self):
        """ROACE stays realized-only — the new column sits beside it."""
        row = _row(profit_hist=3_000.0)
        _attach_roace_columns([row], {1: _snap()})
        assert row["return_on_avg_equity"] == 30.0  # no floating term

    def test_ratio_gate_sets_capital_locked(self):
        """Client-125420 shape: 99.5% of the money pinned under floating losses.
        The value is suppressed but the row is FLAGGED — that cohort is the
        strongest signal on the page, not missing data."""
        snap = _snap(avg_daily_equity=1_379.0, avg_daily_balance=76_308.0)
        row = _row()
        _attach_roace_columns([row], {1: snap})
        assert row["return_with_floating"] is None
        assert row["capital_locked"] is True
        # Burden ratio is exactly the "how locked" readout — must survive the gate.
        assert row["floating_burden_ratio"] is not None
        assert row["floating_burden_ratio"] < -90

    def test_gate_ratio_boundary(self):
        """avg_eq exactly at 20% of avg_bal passes (gate is strict <)."""
        snap = _snap(
            avg_daily_balance=50_000.0,
            avg_daily_equity=50_000.0 * _FLOAT_GATE_MIN_EQ_TO_BAL_RATIO,
        )
        row = _row()
        _attach_roace_columns([row], {1: snap})
        assert row["capital_locked"] is False
        assert row["return_with_floating"] is not None

    def test_dust_account_is_not_flagged_capital_locked(self):
        """Cold-review F1: a $5-equity / $500-balance dust account fails the
        ratio gate but must NOT light up as 'capital locked' — the flag is for
        meaningful money (avg_bal >= min-equity threshold), dust blanks."""
        snap = _snap(avg_daily_equity=5.0, avg_daily_balance=500.0)
        row = _row()
        _attach_roace_columns([row], {1: snap})
        assert row["return_with_floating"] is None
        assert row["capital_locked"] is False

    def test_deep_locked_whale_below_min_equity_keeps_the_flag(self):
        """A whale whose avg equity was dragged UNDER the min-equity bar by the
        very locking we want to surface must still be flagged, not blanked —
        the flag keys off avg_bal, not avg_eq."""
        snap = _snap(avg_daily_equity=800.0, avg_daily_balance=76_000.0)
        row = _row()
        _attach_roace_columns([row], {1: snap})
        assert row["capital_locked"] is True
        assert row["return_with_floating"] is None

    def test_young_locked_account_blanks_without_flag(self):
        """Too-young vetoes the flag too: 10-day averages are too noisy to make
        ANY claim, including 'capital locked'."""
        snap = _snap(
            avg_daily_equity=1_379.0,
            avg_daily_balance=76_308.0,
            active_days=10,
        )
        row = _row()
        _attach_roace_columns([row], {1: snap})
        assert row["return_with_floating"] is None
        assert row["capital_locked"] is False

    def test_too_few_active_days_blanks_without_flag(self):
        """A 10-day-old account is not 'capital locked' — it is just too young
        to rate. Null, no flag."""
        snap = _snap(active_days=_FLOAT_GATE_MIN_ACTIVE_DAYS - 1)
        row = _row()
        _attach_roace_columns([row], {1: snap})
        assert row["return_with_floating"] is None
        assert row["capital_locked"] is False

    def test_too_small_equity_blanks_without_flag(self):
        snap = _snap(
            avg_daily_equity=_FLOAT_GATE_MIN_AVG_EQUITY - 1,
            avg_daily_balance=2_000.0,  # keeps the ratio condition passing
        )
        row = _row()
        _attach_roace_columns([row], {1: snap})
        assert row["return_with_floating"] is None
        assert row["capital_locked"] is False

    def test_pre_v2_snapshot_row_degrades_gracefully(self):
        """v2 table not refreshed yet → balance/float columns are None. ROACE
        must still compute; new columns stay null with no flag."""
        snap = _snap(
            avg_daily_balance=None,
            avg_daily_credit=None,
            first_float=None,
            last_float=None,
        )
        row = _row(profit_hist=3_000.0)
        _attach_roace_columns([row], {1: snap})
        assert row["return_on_avg_equity"] == 30.0
        assert row["return_with_floating"] is None
        assert row["floating_burden_ratio"] is None
        assert row["capital_locked"] is False

    def test_missing_snapshot_nulls_everything(self):
        row = _row()
        _attach_roace_columns([row], {})
        assert row["avg_daily_equity"] is None
        assert row["return_on_avg_equity"] is None
        assert row["return_with_floating"] is None
        assert row["floating_burden_ratio"] is None
        assert row["capital_locked"] is False

    def test_zero_balance_blanks_burden_ratio(self):
        snap = _snap(avg_daily_balance=0.0)
        row = _row()
        _attach_roace_columns([row], {1: snap})
        assert row["floating_burden_ratio"] is None

    def test_case_128535_shape(self):
        """The founding case: Closed +29,288 but floating went 0 → −29,351.
        ROACE reads +177.7% while the floating-inclusive return is ~0."""
        snap = _snap(
            avg_daily_equity=16_480.0,  # implied by 29288/16480 ≈ 177.7%
            avg_daily_balance=28_000.0,
            first_float=0.0,
            last_float=-29_351.0,
            active_days=548,
        )
        row = _row(profit_hist=29_288.0)
        _attach_roace_columns([row], {1: snap})
        assert row["return_on_avg_equity"] > 170
        assert -1.0 < row["return_with_floating"] < 0.0  # −63/16480 ≈ −0.38%
