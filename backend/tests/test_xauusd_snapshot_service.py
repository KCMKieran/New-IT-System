"""Unit tests for the XAUUSD snapshot read-path logic (OPT-0040).

Covers the pieces with non-trivial behaviour:
- net_position computation (Buy − Sell);
- downsampling: each bucket = company total at its most recent real instant;
- export range validation (7-day cap, parse/order guards);
- DB-level server/symbol filtering and the retention purge boundary.
"""

from __future__ import annotations

import pytest

from app.services.open_positions_service import compute_net_position
from app.services.xauusd_snapshot_service import (
    MAX_EXPORT_DAYS,
    aggregate_points,
    bucket_start,
    normalize_bucket_min,
    validate_export_range,
)


def test_compute_net_position():
    assert compute_net_position(10.0, 4.0) == 6.0
    assert compute_net_position(2.0, 5.0) == -3.0
    assert compute_net_position(0.0, 0.0) == 0.0
    # None operands coerce to 0 (defensive against NULL from the DB).
    assert compute_net_position(None, 3.0) == -3.0
    assert compute_net_position(3.0, None) == 3.0


def test_normalize_bucket_min():
    assert normalize_bucket_min(5) == 5
    assert normalize_bucket_min(10) == 10
    # Anything outside the allowed set falls back to the 5-min default.
    assert normalize_bucket_min(7) == 5
    assert normalize_bucket_min(None) == 5
    assert normalize_bucket_min(0) == 5


def test_bucket_start_floors_to_bucket():
    assert bucket_start("2026-06-29T06:32:10Z", 5) == "2026-06-29T06:30:00Z"
    assert bucket_start("2026-06-29T06:34:59Z", 5) == "2026-06-29T06:30:00Z"
    assert bucket_start("2026-06-29T06:35:00Z", 5) == "2026-06-29T06:35:00Z"
    assert bucket_start("2026-06-29T06:32:10Z", 10) == "2026-06-29T06:30:00Z"
    assert bucket_start("2026-06-29T06:41:10Z", 10) == "2026-06-29T06:40:00Z"


def test_aggregate_points_sums_across_series_at_same_instant():
    # Normal case: two series both present at the same minute in the bucket →
    # their stock values add up to the company total at that instant.
    rows = [
        {"captured_at": "2026-06-29T06:31:00Z", "server": "mt4_live",
         "symbol": "XAUUSD", "volume_buy": 5.0, "volume_sell": 1.0, "net_position": 4.0},
        {"captured_at": "2026-06-29T06:31:00Z", "server": "mt5",
         "symbol": "XAUUSD", "volume_buy": 2.0, "volume_sell": 3.0, "net_position": -1.0},
        # later bucket, single series
        {"captured_at": "2026-06-29T06:36:00Z", "server": "mt4_live",
         "symbol": "XAUUSD", "volume_buy": 6.0, "volume_sell": 1.0, "net_position": 5.0},
    ]
    points = aggregate_points(rows, 5)
    assert len(points) == 2
    assert points[0]["time"] == "2026-06-29T06:30:00Z"
    assert points[0]["buy"] == 7.0
    assert points[0]["sell"] == 4.0
    assert points[0]["net"] == 3.0
    assert points[1]["time"] == "2026-06-29T06:35:00Z"
    assert points[1]["net"] == 5.0


def test_aggregate_points_bucket_total_is_a_real_instant():
    # Within one 5-min bucket (06:30): series X has rows at :30 and :32 but
    # NOT at :34; series Y has a row at :34. The bucket total must be the
    # company total at the LATEST instant (06:34) = only Y. X must NOT
    # contribute its stale :32 value (it had no open position at :34).
    rows = [
        {"captured_at": "2026-06-29T06:30:00Z", "server": "mt4_live",
         "symbol": "XAUUSD", "volume_buy": 10.0, "volume_sell": 0.0, "net_position": 10.0},
        {"captured_at": "2026-06-29T06:32:00Z", "server": "mt4_live",
         "symbol": "XAUUSD", "volume_buy": 8.0, "volume_sell": 0.0, "net_position": 8.0},
        {"captured_at": "2026-06-29T06:34:00Z", "server": "mt5",
         "symbol": "XAUUSD", "volume_buy": 3.0, "volume_sell": 1.0, "net_position": 2.0},
    ]
    points = aggregate_points(rows, 5)
    assert len(points) == 1
    p = points[0]
    assert p["time"] == "2026-06-29T06:30:00Z"
    # Only Y (the :34 row) counts; X's stale :32 value is excluded.
    assert p["buy"] == 3.0
    assert p["sell"] == 1.0
    assert p["net"] == 2.0


def test_aggregate_points_empty():
    assert aggregate_points([], 5) == []


def test_validate_export_range_ok():
    start, end = validate_export_range(
        "2026-06-20T00:00:00Z", "2026-06-25T23:59:59Z"
    )
    assert start == "2026-06-20T00:00:00Z"
    assert end == "2026-06-25T23:59:59Z"


def test_validate_export_range_rejects_over_cap():
    # > MAX_EXPORT_DAYS apart → ValueError.
    with pytest.raises(ValueError):
        validate_export_range(
            "2026-06-01T00:00:00Z",
            f"2026-06-{1 + MAX_EXPORT_DAYS + 1:02d}T00:00:01Z",
        )


def test_validate_export_range_rejects_inverted():
    with pytest.raises(ValueError):
        validate_export_range("2026-06-25T00:00:00Z", "2026-06-20T00:00:00Z")


def test_validate_export_range_rejects_unparseable():
    with pytest.raises(ValueError):
        validate_export_range("not-a-date", "2026-06-20T00:00:00Z")


# ── DB-level tests (server/symbol filter push-down + purge boundary) ──────────

@pytest.fixture
def rmdb(tmp_path, monkeypatch):
    db_file = tmp_path / "risk_monitor_test.db"
    from app.core import risk_monitor_db as rmdb
    monkeypatch.setattr(rmdb, "_DB_PATH", db_file)
    rmdb.init_risk_monitor_db()
    return rmdb


def test_fetch_filters_by_server_and_symbol(rmdb):
    captured_at = "2026-06-29T06:30:00Z"
    rows = [
        {"server": "mt4_live", "symbol": "XAUUSD",
         "volume_buy": 5.0, "volume_sell": 1.0, "net_position": 4.0},
        {"server": "mt4_live", "symbol": "XAUUSD.cent",
         "volume_buy": 2.0, "volume_sell": 0.0, "net_position": 2.0},
        {"server": "mt5", "symbol": "XAUUSD",
         "volume_buy": 3.0, "volume_sell": 2.0, "net_position": 1.0},
    ]
    rmdb.append_xauusd_snapshots(captured_at, rows)

    start, end = "2026-06-29T00:00:00Z", "2026-06-29T23:59:59Z"

    # Unfiltered → all three.
    assert len(rmdb.fetch_xauusd_snapshots(start, end)) == 3

    # server filter.
    only_mt5 = rmdb.fetch_xauusd_snapshots(start, end, server="mt5")
    assert len(only_mt5) == 1
    assert only_mt5[0]["server"] == "mt5"

    # symbol filter.
    only_cent = rmdb.fetch_xauusd_snapshots(start, end, symbol="XAUUSD.cent")
    assert len(only_cent) == 1
    assert only_cent[0]["symbol"] == "XAUUSD.cent"

    # both filters.
    both = rmdb.fetch_xauusd_snapshots(
        start, end, server="mt4_live", symbol="XAUUSD"
    )
    assert len(both) == 1
    assert both[0]["server"] == "mt4_live" and both[0]["symbol"] == "XAUUSD"

    # distinct dimensions come from the unfiltered window.
    dims = rmdb.fetch_xauusd_distinct_dimensions(start, end)
    assert dims["servers"] == ["mt4_live", "mt5"]
    assert dims["symbols"] == ["XAUUSD", "XAUUSD.cent"]


def test_iter_snapshots_streams_same_rows(rmdb):
    captured_at = "2026-06-29T06:30:00Z"
    rows = [
        {"server": "mt4_live", "symbol": "XAUUSD",
         "volume_buy": 5.0, "volume_sell": 1.0, "net_position": 4.0},
        {"server": "mt5", "symbol": "XAUUSD",
         "volume_buy": 3.0, "volume_sell": 2.0, "net_position": 1.0},
    ]
    rmdb.append_xauusd_snapshots(captured_at, rows)
    streamed = list(
        rmdb.iter_xauusd_snapshots(
            "2026-06-29T00:00:00Z", "2026-06-29T23:59:59Z"
        )
    )
    assert len(streamed) == 2
    assert {r["server"] for r in streamed} == {"mt4_live", "mt5"}


def test_purge_boundary_uses_z_format(rmdb):
    # A row well past the 60-day window must be purged; a fresh row survives.
    # The purge cutoff is emitted as "...T...Z" to match captured_at exactly.
    import datetime as _dt

    old_row = [{"server": "mt4_live", "symbol": "XAUUSD",
                "volume_buy": 1.0, "volume_sell": 0.0, "net_position": 1.0}]
    rmdb.append_xauusd_snapshots("2020-01-01T00:00:00Z", old_row)

    # Write a fresh row at "now" (same fixed-width "...Z" format).
    now_iso = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    fresh_row = [{"server": "mt5", "symbol": "XAUUSD",
                  "volume_buy": 2.0, "volume_sell": 0.0, "net_position": 2.0}]
    rmdb.append_xauusd_snapshots(now_iso, fresh_row)

    remaining = rmdb.fetch_xauusd_snapshots(
        "2000-01-01T00:00:00Z", "2999-01-01T00:00:00Z"
    )
    captured = {r["captured_at"] for r in remaining}
    assert "2020-01-01T00:00:00Z" not in captured  # purged
    assert now_iso in captured  # survived


# ── Partial-server resilience (one server failing must not zero the batch) ────

class _FakeCursor:
    def __init__(self, fail_sid: int):
        self._fail_sid = fail_sid
        self._rows: list[dict] = []

    def execute(self, sql, params=None):
        if params and params.get("sid") == self._fail_sid:
            raise RuntimeError("simulated server failure")
        self._rows = [{"symbol": "XAUUSD", "volume_buy": 1.0, "volume_sell": 0.0}]

    def fetchall(self):
        return self._rows

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeConn:
    def __init__(self, fail_sid: int):
        self._fail_sid = fail_sid

    def cursor(self):
        return _FakeCursor(self._fail_sid)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_partial_server_failure_returns_other_servers(monkeypatch):
    from types import SimpleNamespace

    from app.services import open_positions_service as svc

    # No demo/test exclusion DB hit.
    monkeypatch.setattr(svc, "_get_excluded_groupsids", lambda settings: [])
    # mt5 (sid 5) raises; mt4_live (1) and mt4_live2 (6) return one row each.
    monkeypatch.setattr(
        svc.pymysql, "connect", lambda **kw: _FakeConn(fail_sid=5)
    )

    settings = SimpleNamespace(
        DB_HOST="x", DB_USER="x", DB_PASSWORD="x",
        FXBACK_DB_NAME="x", DB_PORT=3306, DB_CHARSET="utf8mb4",
    )
    result = svc.get_xauusd_position_detail(settings)

    assert result["ok"] is True
    # The two healthy servers still contribute their rows.
    servers = {r["server"] for r in result["items"]}
    assert servers == {"mt4_live", "mt4_live2"}
    assert result.get("failed_servers") == ["mt5"]
