"""Unit tests for the XAUUSD snapshot read-path logic (OPT-0040).

Covers the two pieces with non-trivial behaviour and no DB dependency:
- net_position computation (Buy − Sell);
- downsampling: LAST snapshot per (series, bucket), then SUM across series.
"""

from app.services.open_positions_service import compute_net_position
from app.services.xauusd_snapshot_service import (
    aggregate_points,
    bucket_start,
    downsample_last_per_bucket,
    normalize_bucket_min,
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


def test_downsample_keeps_last_snapshot_in_bucket():
    # Three snapshots in the same 5-min bucket for one series — only the
    # latest (06:34) survives. A position is a stock value, so the bucket's
    # representative is its most recent state, never a sum/avg.
    rows = [
        {"captured_at": "2026-06-29T06:30:00Z", "server": "mt4_live",
         "symbol": "XAUUSD", "volume_buy": 10.0, "volume_sell": 2.0, "net_position": 8.0},
        {"captured_at": "2026-06-29T06:32:00Z", "server": "mt4_live",
         "symbol": "XAUUSD", "volume_buy": 12.0, "volume_sell": 2.0, "net_position": 10.0},
        {"captured_at": "2026-06-29T06:34:00Z", "server": "mt4_live",
         "symbol": "XAUUSD", "volume_buy": 15.0, "volume_sell": 3.0, "net_position": 12.0},
    ]
    out = downsample_last_per_bucket(rows, 5)
    assert len(out) == 1
    assert out[0]["volume_buy"] == 15.0
    assert out[0]["net_position"] == 12.0
    assert out[0]["bucket"] == "2026-06-29T06:30:00Z"


def test_downsample_separates_series_and_buckets():
    rows = [
        # series A, bucket 06:30
        {"captured_at": "2026-06-29T06:31:00Z", "server": "mt4_live",
         "symbol": "XAUUSD", "volume_buy": 5.0, "volume_sell": 1.0, "net_position": 4.0},
        # series A, bucket 06:35
        {"captured_at": "2026-06-29T06:36:00Z", "server": "mt4_live",
         "symbol": "XAUUSD", "volume_buy": 6.0, "volume_sell": 1.0, "net_position": 5.0},
        # series B (different symbol), bucket 06:30
        {"captured_at": "2026-06-29T06:31:00Z", "server": "mt4_live",
         "symbol": "XAUUSD.cent", "volume_buy": 2.0, "volume_sell": 0.0, "net_position": 2.0},
    ]
    out = downsample_last_per_bucket(rows, 5)
    # 3 distinct (series, bucket) keys → 3 rows kept.
    assert len(out) == 3


def test_aggregate_points_sums_across_series_per_bucket():
    # Two independent series in the same bucket add up to the company total.
    downsampled = [
        {"bucket": "2026-06-29T06:30:00Z", "volume_buy": 5.0, "volume_sell": 1.0, "net_position": 4.0},
        {"bucket": "2026-06-29T06:30:00Z", "volume_buy": 2.0, "volume_sell": 3.0, "net_position": -1.0},
        {"bucket": "2026-06-29T06:35:00Z", "volume_buy": 6.0, "volume_sell": 1.0, "net_position": 5.0},
    ]
    points = aggregate_points(downsampled)
    assert len(points) == 2
    # sorted by time ascending
    assert points[0]["time"] == "2026-06-29T06:30:00Z"
    assert points[0]["buy"] == 7.0
    assert points[0]["sell"] == 4.0
    assert points[0]["net"] == 3.0
    assert points[1]["time"] == "2026-06-29T06:35:00Z"
    assert points[1]["net"] == 5.0
