"""Anti-drift guards for CEN (US-cent) unit conversion in Client Return Rate SQL.

The bug (found 2026-08-28): the `rt` LEFT JOIN summed
`stats_trading_running_totals.plClosedHavingActivityRunningTotal` raw:

    SUM(plClosedHavingActivityRunningTotal) AS profit_hist_trades

That table holds one row per loginSid **in the account's own currency** and has a
`currency` column — it is NOT pre-normalized, despite a stale comment in
docs/features/client-return-rate.md claiming otherwise. So every CEN account leg
was counted 100x. Replica measurement: 3,781 of 27,476 clients (13.8%) carry a
non-zero CEN leg; client 128535 showed profit_hist 27,702.02 instead of 28,453.00
(a -$7.59 cent leg read as -$758.57), and 116 clients had profit_hist land on the
wrong SIGN.

It also leaked into ROACE: `return_on_avg_equity = profit_hist / avg_daily_equity`
and the denominator (client_roace_refresh_service) *is* CEN-adjusted, so the ratio
was inflated on exactly the mixed USD/CEN clients.

These tests assert on generated SQL text rather than hitting MySQL — SQL text is
where this class of drift happens, and nothing else in the suite would fail if a
future edit dropped the IF(currency='CEN', ...) wrapper again.

Convention SSOT: CLAUDE.md "CEN accounts: amounts in cents — divide by 100" and
backend/scripts/fxbo_table_notes/stats_trading_running_totals.md.
"""

import re

from app.services.client_return_service import _build_phase2_sql

_RAW_SQL = _build_phase2_sql("1,2,3", "2026-07-01", "2026-07-15")

# Strip `--` comments: the fix ships explanatory prose that mentions CEN and /100,
# and that prose must not be able to satisfy these assertions. Pin executable SQL.
_SQL = re.sub(r"--[^\n]*", "", _RAW_SQL)

# Every money aggregate in Phase 2 that reads a currency-tagged fxbackoffice
# table, mapped to the raw column it must normalize.
_MONEY_LEGS = {
    "equity": "EQUITY",
    "deposits_hist": "st.amount",
    "withdrawals_hist": "st.amount",
    "ib_withdrawal_hist": "st.amount",
    "deposits_month": "st.amount",
    "withdrawals_month": "st.amount",
    "ib_withdrawal_month": "st.amount",
    "deposits_90d": "st.amount",
    "profit_hist_trades": "plClosedHavingActivityRunningTotal",
}


def _leg(alias: str) -> str:
    """Extract the SUM(...) aggregate expression assigned to a given SQL alias.

    Mirrors the helper in test_client_return_trading_net_deposit.py: an alias can
    appear both on the outer SELECT and as the aggregate inside a LEFT JOIN
    subquery, and we want the aggregate.
    """
    for m in re.finditer(rf"\) AS {re.escape(alias)}\b", _SQL):
        end = m.start()
        start = _SQL.rfind("SUM(", 0, end)
        if start == -1:
            continue
        span = _SQL[start + len("SUM(") : end]
        if " AS " not in span:
            return span
    raise AssertionError(f"no SUM(...) aggregate found for alias {alias!r}")


class TestProfitHistDividesCenByHundred:
    """The specific 2026-08-28 regression."""

    def test_profit_hist_leg_has_a_cen_branch(self):
        leg = _leg("profit_hist_trades")
        assert "CEN" in leg, (
            "profit_hist sums stats_trading_running_totals without a CEN branch. "
            "That table is per-loginSid in the ACCOUNT'S OWN currency and is NOT "
            "pre-normalized — cent accounts get counted 100x (3,781 clients "
            "affected, 116 of them flipping sign). Restore "
            "SUM(IF(currency = 'CEN', col / 100.0, col))."
        )

    def test_profit_hist_leg_divides_by_100(self):
        leg = _leg("profit_hist_trades")
        assert "/ 100.0" in leg or "/100.0" in leg, (
            "profit_hist has a CEN branch but no /100 divisor"
        )

    def test_profit_hist_divides_the_right_column(self):
        """Guard against a /100 that lands on the wrong operand."""
        leg = _leg("profit_hist_trades")
        assert re.search(
            r"plClosedHavingActivityRunningTotal\s*/\s*100\.0", leg
        ), f"the /100 is not applied to plClosedHavingActivityRunningTotal: {leg!r}"

    def test_profit_hist_keeps_the_unconverted_fallback(self):
        """USD/USDT rows must pass through untouched — the IF needs both branches."""
        leg = _leg("profit_hist_trades")
        assert re.search(
            r"IF\(\s*currency\s*=\s*'CEN'\s*,\s*plClosedHavingActivityRunningTotal\s*/\s*100\.0\s*,"
            r"\s*plClosedHavingActivityRunningTotal\s*\)",
            leg,
        ), f"CEN branch is not the canonical two-branch IF: {leg!r}"


class TestEveryMoneyLegNormalizesCen:
    """Blanket guard so the next money column added here can't skip the rule."""

    def test_all_money_aggregates_branch_on_cen(self):
        missing = [alias for alias in _MONEY_LEGS if "CEN" not in _leg(alias)]
        assert not missing, (
            f"money aggregates with no CEN branch: {missing}. Every SUM over a "
            f"currency-tagged fxbackoffice table must be "
            f"SUM(IF(currency = 'CEN', col / 100.0, col)) — see CLAUDE.md."
        )

    def test_all_money_aggregates_divide_by_100(self):
        missing = [
            alias
            for alias in _MONEY_LEGS
            if "/ 100.0" not in _leg(alias) and "/100.0" not in _leg(alias)
        ]
        assert not missing, f"money aggregates with a CEN branch but no /100: {missing}"

    def test_each_money_leg_divides_its_own_raw_column(self):
        wrong = []
        for alias, column in _MONEY_LEGS.items():
            leg = _leg(alias)
            if not re.search(rf"{re.escape(column)}\s*/\s*100\.0", leg):
                wrong.append((alias, column))
        assert not wrong, f"the /100 is applied to the wrong operand for: {wrong}"
