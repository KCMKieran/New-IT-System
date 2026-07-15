"""Tests for balance + equity enrichment added to account_enrichment.

Covers the scan-time balance/equity snapshots wired into get_account_info_map
and the CEN ÷100 normalisation in apply_cen_conversion. balance and equity are
displayed as columns across the risk-monitor tabs (burst / quick-open-close /
quick-profit / hedge-open / leverage-abuse). Both are ACCOUNT-level.

NOTE: equity here no longer feeds the computed 淨賺 column — the 2026-07 audit
found that pairing this account-level equity with a client-level net deposit
was a level mismatch. 淨賺 now reads the client-level operands from
get_client_net_gain_map (see test_client_net_gain.py).
"""

from app.services.account_enrichment import (
    apply_cen_conversion,
    get_account_info_map,
)


class _FakeCursor:
    def __init__(self, rows):
        self._rows = rows

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, *a, **k):
        pass

    def fetchall(self):
        return self._rows


class _FakeConn:
    def __init__(self, rows):
        self._rows = rows

    def cursor(self):
        return _FakeCursor(self._rows)

    def close(self):
        pass


def test_get_account_info_map_returns_balance_and_equity():
    # MT5 → sid 5, so get_account_info_map computes loginsid "5-123".
    rows = [{
        "loginsid": "5-123",
        "currency": "USD",
        "zipcode": "111 90",
        "group": "KCM\\5Vc_L10",
        "name": "T",
        "balance": 12345.678,
        "equity": 11111.116,
    }]
    out = get_account_info_map(_FakeConn(rows), [{"server": "MT5", "login": 123}])
    assert "5-123" in out
    # Raw values, rounded to 2dp — NOT yet CEN-divided (caller does that).
    assert out["5-123"]["balance"] == 12345.68
    assert out["5-123"]["equity"] == 11111.12
    assert out["5-123"]["currency"] == "USD"


def test_get_account_info_map_balance_equity_none_when_null():
    rows = [{
        "loginsid": "1-9",
        "currency": "USD",
        "zipcode": None,
        "group": None,
        "name": None,
        "balance": None,
        "equity": None,
    }]
    out = get_account_info_map(_FakeConn(rows), [{"server": "MT4_Live", "login": 9}])
    assert out["1-9"]["balance"] is None
    assert out["1-9"]["equity"] is None


def test_apply_cen_conversion_divides_balance_for_cen():
    alert = {"equity": 50000.0, "balance": 60000.0}
    apply_cen_conversion(alert, "CEN")
    assert alert["balance"] == 600.0   # ÷100
    assert alert["equity"] == 500.0    # ÷100


def test_apply_cen_conversion_noop_for_usd():
    alert = {"equity": 500.0, "balance": 600.0}
    apply_cen_conversion(alert, "USD")
    assert alert["balance"] == 600.0   # untouched
    assert alert["equity"] == 500.0


def test_apply_cen_conversion_handles_none_balance():
    alert = {"balance": None}
    apply_cen_conversion(alert, "CEN")  # must not raise
    assert alert["balance"] is None
