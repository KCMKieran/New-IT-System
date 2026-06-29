"""Read-path business logic for XAUUSD position snapshots (OPT-0040).

Two consumers:
- the /history endpoint → downsampled three-line (Buy/Sell/Net) time series
  for the /position chart;
- the /export endpoint → CSV of the raw 1-min snapshots over a custom range.

Downsampling rationale: positions are **stock** values (the open position at a
moment), not flows. So across TIME we take the LAST snapshot in each bucket
(the most recent state), never a sum/avg — summing a position over time is
meaningless. Across SERIES (different server×symbol) within the same bucket we
DO sum, because independent positions add up to the company total.
"""

from __future__ import annotations

import csv
import io
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

# bucket sizes the /history endpoint accepts (minutes). Anything else falls
# back to the default. Matches the AC (5 or 10 min downsampling).
ALLOWED_BUCKET_MIN: tuple[int, ...] = (5, 10)
DEFAULT_BUCKET_MIN = 5

CSV_HEADER = ["captured_at", "server", "symbol", "volume_buy", "volume_sell", "net_position"]


def normalize_bucket_min(bucket_min: int | None) -> int:
    """Clamp an incoming bucket size to an allowed value (default 5)."""
    return bucket_min if bucket_min in ALLOWED_BUCKET_MIN else DEFAULT_BUCKET_MIN


def _parse_iso(ts: str) -> datetime:
    """Parse a UTC ISO8601 timestamp that may end in 'Z'."""
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def bucket_start(captured_at: str, bucket_min: int) -> str:
    """Floor a timestamp down to the start of its bucket_min-minute bucket.

    e.g. bucket_start("2026-06-29T06:32:10Z", 5) -> "2026-06-29T06:30:00Z".
    Returns the same fixed-width UTC ISO8601 ("...Z") format so the result is
    safe to sort/compare as a string.
    """
    dt = _parse_iso(captured_at).astimezone(timezone.utc)
    floored_minute = (dt.minute // bucket_min) * bucket_min
    b = dt.replace(minute=floored_minute, second=0, microsecond=0)
    return b.strftime("%Y-%m-%dT%H:%M:%SZ")


def downsample_last_per_bucket(
    rows: Iterable[dict[str, Any]], bucket_min: int
) -> list[dict[str, Any]]:
    """Keep one row per (server, symbol, bucket): the LATEST captured_at in it.

    Positions are stock values, so the representative of a time bucket is its
    most recent snapshot — never a sum or average. Returns rows enriched with a
    `bucket` key. captured_at strings compare lexicographically = chronologically
    because they share one fixed UTC ISO8601 format.
    """
    best: dict[tuple[str, str, str], dict[str, Any]] = {}
    for r in rows:
        b = bucket_start(r["captured_at"], bucket_min)
        key = (r["server"], r["symbol"], b)
        cur = best.get(key)
        if cur is None or r["captured_at"] > cur["captured_at"]:
            best[key] = {**r, "bucket": b}
    return list(best.values())


def aggregate_points(downsampled: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Sum the per-series stock values within each bucket → company total.

    Output is one point per bucket: {time, buy, sell, net}, sorted by time
    ascending so the chart's x-axis is chronological.
    """
    by_bucket: dict[str, dict[str, float]] = {}
    for r in downsampled:
        agg = by_bucket.setdefault(r["bucket"], {"buy": 0.0, "sell": 0.0, "net": 0.0})
        agg["buy"] += float(r.get("volume_buy") or 0.0)
        agg["sell"] += float(r.get("volume_sell") or 0.0)
        agg["net"] += float(r.get("net_position") or 0.0)
    return [
        {"time": b, "buy": v["buy"], "sell": v["sell"], "net": v["net"]}
        for b, v in sorted(by_bucket.items())
    ]


def build_history(
    hours: int,
    bucket_min: int | None,
    server: str | None = None,
    symbol: str | None = None,
) -> dict[str, Any]:
    """Assemble the downsampled three-line history for the chart.

    Optional `server` / `symbol` filters drill the company total down to a
    single server / single product. The response also lists the distinct
    servers/symbols present in the window so the frontend can build its filter
    dropdowns, plus the last snapshot time for the 'recording' badge.
    """
    from ..core.risk_monitor_db import (
        fetch_xauusd_snapshots,
        get_xauusd_last_captured_at,
    )

    bucket = normalize_bucket_min(bucket_min)
    hours = max(1, min(int(hours or 24), 24 * 60))  # clamp 1h..60d

    now = datetime.now(timezone.utc)
    start = now - timedelta(hours=hours)
    start_iso = start.strftime("%Y-%m-%dT%H:%M:%SZ")
    end_iso = now.strftime("%Y-%m-%dT%H:%M:%SZ")

    rows = fetch_xauusd_snapshots(start_iso, end_iso)

    # Distinct dimensions across the unfiltered window (for the dropdowns).
    servers = sorted({r["server"] for r in rows})
    symbols = sorted({r["symbol"] for r in rows})

    if server:
        rows = [r for r in rows if r["server"] == server]
    if symbol:
        rows = [r for r in rows if r["symbol"] == symbol]

    points = aggregate_points(downsample_last_per_bucket(rows, bucket))

    return {
        "ok": True,
        "points": points,
        "servers": servers,
        "symbols": symbols,
        "last_captured_at": get_xauusd_last_captured_at(),
        "bucket_min": bucket,
        "hours": hours,
    }


def build_export_csv(start_iso: str, end_iso: str) -> str:
    """Build a CSV string of the raw 1-min snapshots in [start_iso, end_iso]."""
    from ..core.risk_monitor_db import fetch_xauusd_snapshots

    rows = fetch_xauusd_snapshots(start_iso, end_iso)
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(CSV_HEADER)
    for r in rows:
        writer.writerow([r.get(col, "") for col in CSV_HEADER])
    return buf.getvalue()
