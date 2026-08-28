"""Anti-drift guards for the Client Return Rate net-deposit 口径 (2026-07-15 audit fix).

The bug: `return_non_adjusted = (equity − net_deposit) / net_deposit` had a
numerator covering sid 1/5/6 (Excl. IB Wallet) but a denominator that folded in
'ib withdrawal' — commission cash-outs that only ever land on the sid=2 wallet.
Commission withdrawals shrank the denominator while the wallet balance stayed
out of the numerator, so IB-cum-traders' return rates were systematically
inflated (case 123261: +60.3% shown vs -32.1% actual).

The fix keeps the type split in the Phase 2 SQL, so these tests assert on the
generated SQL text rather than hitting MySQL. That's the drift that matters:
someone re-merging 'ib withdrawal' back into the withdrawals leg would silently
reintroduce the inflation with no test failing anywhere else.

口径 SSOT: rule_rebate_arb_service._query_net_deposit_split (`trading_net_deposit`)
and .cursor/skills/rebate-arbitrage/SKILL.md §2.2.
"""

import re

from app.services.client_return_service import _build_phase2_sql

_RAW_SQL = _build_phase2_sql("1,2,3", "2026-07-01", "2026-07-15")

# Strip `--` comments before asserting: the fix ships explanatory comments that
# legitimately mention 'ib withdrawal', and they must not satisfy (or trip) the
# assertions below. We are pinning executable SQL, not prose.
_SQL = re.sub(r"--[^\n]*", "", _RAW_SQL)


def _leg(alias: str) -> str:
    """Extract the SUM(...) aggregate expression assigned to a given SQL alias.

    An alias can appear twice — once on the outer SELECT (e.g.
    `ROUND(COALESCE(th.ib_withdrawal_hist, 0), 2) AS ib_withdrawal_hist`) and
    once as the aggregate inside the LEFT JOIN subquery. We want the aggregate:
    take the occurrence whose nearest preceding `SUM(` isn't separated from it
    by another alias (which would mean we crossed into a different column).
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


class TestTradingNetDepositExcludesIbWithdrawal:
    def test_withdrawal_legs_do_not_fold_in_ib_withdrawal(self):
        """The core regression: 'ib withdrawal' must not sit in the net-deposit legs."""
        for alias in ("withdrawals_hist", "withdrawals_month"):
            leg = _leg(alias)
            assert "ib withdrawal" not in leg, (
                f"{alias} folds in 'ib withdrawal' — this is the exact bug the "
                f"2026-07-15 audit fixed (inflates IB-cum-traders' return rate). "
                f"Keep the commission leg in ib_withdrawal_* instead."
            )
            assert "'withdrawal'" in leg

    def test_ib_withdrawal_is_reported_as_its_own_leg(self):
        """Legacy all-in figure must stay reconstructible: legacy = net_deposit + ib_withdrawal."""
        for alias in ("ib_withdrawal_hist", "ib_withdrawal_month"):
            leg = _leg(alias)
            assert "ib withdrawal" in leg

    def test_deposit_legs_are_deposit_only(self):
        for alias in ("deposits_hist", "deposits_month"):
            assert "'deposit'" in _leg(alias)

    def test_net_deposit_is_deposits_plus_withdrawals_only(self):
        """withdrawal amounts are already negative -> net is a PLUS, and the
        commission leg is absent from the expression entirely."""
        for out, dep, wd in (
            ("net_deposit_hist", "th.deposits_hist", "th.withdrawals_hist"),
            ("net_deposit_month", "txm.deposits_month", "txm.withdrawals_month"),
        ):
            m = re.search(rf"ROUND\((.*?), 2\) AS {out}", _SQL, re.DOTALL)
            assert m is not None, f"{out} not found"
            expr = m.group(1)
            assert dep in expr and wd in expr
            assert "+" in expr
            assert "ib_withdrawal" not in expr, (
                f"{out} must not include the IB commission leg"
            )


class TestNumeratorDenominatorSymmetry:
    def test_equity_subquery_excludes_ib_wallet(self):
        """Numerator stays Excl. IB Wallet — the other half of the symmetry."""
        m = re.search(
            r"SUM\(IF\(UPPER\(CURRENCY\) = 'CEN'.*?\) AS equity.*?FROM mt4_users(.*?)GROUP BY userId",
            _SQL,
            re.DOTALL,
        )
        assert m is not None
        assert "sid IN (1, 5, 6)" in m.group(1)

    def test_transaction_subqueries_keep_wallet_sid(self):
        """sid stays 1/2/5/6: a plain deposit/withdrawal booked on a wallet account
        is still real client money in/out of KCM and belongs in the denominator.
        Only the 'ib withdrawal' TYPE is commission rather than capital, so the
        exclusion is by type, not by sid."""
        assert _SQL.count("mu.sid IN (1, 2, 5, 6)") >= 2

    def test_return_non_adjusted_divides_by_the_trading_net_deposit(self):
        """The ratio must be built from the same deposits+withdrawals legs, with
        no ib_withdrawal term anywhere in it."""
        m = re.search(r"(IF\(\s*\(COALESCE\(th\.deposits_hist.*?\) AS return_non_adjusted)", _SQL, re.DOTALL)
        assert m is not None
        expr = m.group(1)
        assert "eq.equity" in expr
        assert "th.withdrawals_hist" in expr
        assert "ib_withdrawal" not in expr


class TestCacheVersionPinnedToTheFormula:
    def test_cache_prefix_bumped_past_superseded_versions(self):
        """A 口径 change MUST bump the Redis key prefix, otherwise blobs holding
        the old formula keep being served for up to the 3h TTL.

        v5 -> v6: net deposit dropped 'ib withdrawal' (2026-07-15).
        v6 -> v7: profit_hist divides CEN legs by 100 (2026-08-28).
        """
        import inspect

        from app.services import client_return_service

        src = inspect.getsource(client_return_service.get_client_return_rate_data)
        for stale in ("client_return_v5_usdt_", "client_return_v6_trading_nd_"):
            assert stale not in src, (
                f"cache prefix still {stale} while the result 口径 changed — "
                f"stale cached rows would be served under the old formula"
            )
        assert "client_return_v7_cen_profit_hist_" in src
