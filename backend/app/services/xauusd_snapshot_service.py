"""Read-path business logic for XAUUSD position snapshots (OPT-0040).

Two consumers:
- the /history endpoint → downsampled three-line (Buy/Sell/Net) time series
  for the /position chart;
- the /export endpoint → CSV of the raw 1-min snapshots over a custom range.

Downsampling rationale: positions are **stock** values (the open position at a
moment), not flows. So across TIME we represent each bucket by a single real
instant — its most recent captured_at — and sum ONLY the rows captured at that
instant. Summing the per-series "last value" independently would carry a stale
value forward for a series that closed out mid-bucket (no row emitted at the
later minute), producing a company total that existed at no single instant.
Taking the latest-instant slice fixes that: a series that has no row at the
bucket's latest instant correctly contributes nothing.
"""

from __future__ import annotations

import csv
import io
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Iterator

# bucket sizes the /history endpoint accepts (minutes). Anything else falls
# back to the default. Matches the AC (5 or 10 min downsampling).
ALLOWED_BUCKET_MIN: tuple[int, ...] = (5, 10)
DEFAULT_BUCKET_MIN = 5

# /history time-window ceiling (hours). 48h keeps the downsample cheap.
MAX_HISTORY_HOURS = 48

# /export range ceiling (days). Caps how much raw 1-min detail one request can
# stream out so the endpoint cannot be used to dump the whole 60-day table.
MAX_EXPORT_DAYS = 7

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


def aggregate_points(
    rows: Iterable[dict[str, Any]], bucket_min: int
) -> list[dict[str, Any]]:
    """Reduce raw snapshot rows to one point per bucket = company total at the
    most recent real instant within that bucket.

    For each bucket we find the MAX captured_at present (across all series) and
    sum ONLY the rows captured at that exact instant. This is the company total
    at the latest real snapshot in the bucket: series that closed out before
    that instant (no later row emitted) correctly contribute nothing, instead
    of carrying a stale earlier value forward. Output is one point per bucket
    {time, buy, sell, net}, sorted by time ascending so the chart's x-axis is
    chronological. captured_at strings compare lexicographically =
    chronologically because they share one fixed UTC ISO8601 format.
    """
    by_bucket: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        b = bucket_start(r["captured_at"], bucket_min)
        by_bucket.setdefault(b, []).append(r)

    points: list[dict[str, Any]] = []
    for b, bucket_rows in by_bucket.items():
        latest = max(r["captured_at"] for r in bucket_rows)
        buy = sell = net = 0.0
        for r in bucket_rows:
            if r["captured_at"] != latest:
                continue
            buy += float(r.get("volume_buy") or 0.0)
            sell += float(r.get("volume_sell") or 0.0)
            net += float(r.get("net_position") or 0.0)
        points.append({"time": b, "buy": buy, "sell": sell, "net": net})

    return sorted(points, key=lambda p: p["time"])


def build_history(
    hours: int,
    bucket_min: int | None,
    server: str | None = None,
    symbol: str | None = None,
) -> dict[str, Any]:
    """Assemble the downsampled three-line history for the chart.

    Optional `server` / `symbol` filters drill the company total down to a
    single server / single product — these are pushed into SQL (indexed by
    idx_xau_snap_srv_sym), not filtered in Python. The response also lists the
    distinct servers/symbols present in the UNFILTERED window so the frontend
    can build its filter dropdowns, plus the last snapshot time for the
    'recording' badge.
    """
    from ..core.risk_monitor_db import (
        fetch_xauusd_distinct_dimensions,
        fetch_xauusd_snapshots,
        get_xauusd_last_captured_at,
    )

    bucket = normalize_bucket_min(bucket_min)
    hours = max(1, min(int(hours or 24), MAX_HISTORY_HOURS))  # clamp 1h..48h

    now = datetime.now(timezone.utc)
    start = now - timedelta(hours=hours)
    start_iso = start.strftime("%Y-%m-%dT%H:%M:%SZ")
    end_iso = now.strftime("%Y-%m-%dT%H:%M:%SZ")

    # Dropdown options come from the unfiltered window (cheap DISTINCTs) so
    # picking one server doesn't make the other options vanish.
    dims = fetch_xauusd_distinct_dimensions(start_iso, end_iso)

    rows = fetch_xauusd_snapshots(start_iso, end_iso, server=server, symbol=symbol)
    points = aggregate_points(rows, bucket)

    return {
        "ok": True,
        "points": points,
        "servers": dims["servers"],
        "symbols": dims["symbols"],
        "last_captured_at": get_xauusd_last_captured_at(),
        "bucket_min": bucket,
        "hours": hours,
    }


def validate_export_range(start: str, end: str) -> tuple[str, str]:
    """Validate + normalize a CSV export range, capping it at MAX_EXPORT_DAYS.

    Returns (start_iso, end_iso) in fixed-width UTC ISO8601 ("...Z"). Raises
    ValueError (which the route maps to HTTP 400) when the bounds are
    unparseable, inverted (end < start), or span more than MAX_EXPORT_DAYS.
    """
    try:
        start_dt = _parse_iso(start).astimezone(timezone.utc)
        end_dt = _parse_iso(end).astimezone(timezone.utc)
    except (ValueError, TypeError) as exc:
        raise ValueError(
            "start/end must be UTC ISO8601 timestamps (e.g. 2026-06-29T00:00:00Z)"
        ) from exc

    if end_dt < start_dt:
        raise ValueError("end must not be before start")
    if end_dt - start_dt > timedelta(days=MAX_EXPORT_DAYS):
        raise ValueError(f"export range must not exceed {MAX_EXPORT_DAYS} days")

    start_iso = start_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    end_iso = end_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    return start_iso, end_iso


def iter_export_csv(
    start_iso: str,
    end_iso: str,
    server: str | None = None,
    symbol: str | None = None,
) -> Iterator[str]:
    """Yield CSV lines (header first) for the raw 1-min snapshots in range.

    Streams straight from a server-side cursor so a large range is never fully
    materialized in memory. Returned to the route as a StreamingResponse.
    """
    from ..core.risk_monitor_db import iter_xauusd_snapshots

    buf = io.StringIO()
    writer = csv.writer(buf)

    def _flush() -> str:
        line = buf.getvalue()
        buf.seek(0)
        buf.truncate(0)
        return line

    writer.writerow(CSV_HEADER)
    yield _flush()

    for r in iter_xauusd_snapshots(start_iso, end_iso, server=server, symbol=symbol):
        writer.writerow([r.get(col, "") for col in CSV_HEADER])
        yield _flush()
