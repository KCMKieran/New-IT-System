"""CSV export for the leverage-abuse tab exports 保证金使用率 (margin usage %),
not MT's native MARGIN_LEVEL. usage = 已用保证金/净值 = 10000 / MARGIN_LEVEL, so
the exported number matches the frontend column. Guards mirror the UI: a level
<= 0 (no open positions) exports blank rather than a bogus reciprocal.
"""

from app.api.v1.routes.risk_monitor import (
    _LEVERAGE_ABUSE_CSV_HEADER,
    _csv_row_from_leverage_abuse,
)

_USAGE_COL = _LEVERAGE_ABUSE_CSV_HEADER.index("margin_usage_pct")


def test_header_uses_usage_not_level():
    # The legacy MARGIN_LEVEL column must be gone so consumers don't misread it.
    assert "margin_usage_pct" in _LEVERAGE_ABUSE_CSV_HEADER
    assert "margin_level" not in _LEVERAGE_ABUSE_CSV_HEADER


def test_level_converted_to_usage_reciprocal():
    # 105.3 level → 94.97% usage (the red-band boundary); 125 → 80%.
    assert _csv_row_from_leverage_abuse({"margin_level": 105.3})[_USAGE_COL] == 94.97
    assert _csv_row_from_leverage_abuse({"margin_level": 125.0})[_USAGE_COL] == 80.0
    assert _csv_row_from_leverage_abuse({"margin_level": 200.0})[_USAGE_COL] == 50.0


def test_non_positive_or_missing_level_exports_blank():
    # MT reports MARGIN_LEVEL 0 for flat accounts — guard against div-by-zero.
    assert _csv_row_from_leverage_abuse({"margin_level": 0})[_USAGE_COL] == ""
    assert _csv_row_from_leverage_abuse({"margin_level": None})[_USAGE_COL] == ""
    assert _csv_row_from_leverage_abuse({})[_USAGE_COL] == ""
