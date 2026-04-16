#!/usr/bin/env python3
"""
Export risk-monitor suspicious account details from SQLite to CSV.

Default behavior:
- Input timezone: UTC+3
- Time range: yesterday 16:00:00 to 17:00:00 (in input timezone)
"""

from __future__ import annotations

import argparse
import csv
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path


DEFAULT_DB = Path("/opt/myproject/New-IT-System/backend/data/risk_monitor.db")


@dataclass
class ExportWindow:
    start_utc: datetime
    end_utc: datetime
    input_tz: timezone


def parse_offset_tz(offset_text: str) -> timezone:
    """Parse timezone offset string, e.g. '+03:00' or '-08:00'."""
    text = offset_text.strip()
    sign = 1
    if text.startswith("+"):
        text = text[1:]
    elif text.startswith("-"):
        sign = -1
        text = text[1:]

    parts = text.split(":")
    if len(parts) != 2:
        raise ValueError("tz must be like +03:00 or -08:00")
    hours = int(parts[0])
    minutes = int(parts[1])
    return timezone(sign * timedelta(hours=hours, minutes=minutes))


def parse_local_datetime(dt_text: str, tzinfo: timezone) -> datetime:
    """Parse local datetime format 'YYYY-MM-DD HH:MM:SS' with given tz."""
    naive = datetime.strptime(dt_text, "%Y-%m-%d %H:%M:%S")
    return naive.replace(tzinfo=tzinfo)


def build_window(args: argparse.Namespace) -> ExportWindow:
    tzinfo = parse_offset_tz(args.tz)

    if args.start_local and args.end_local:
        start_local = parse_local_datetime(args.start_local, tzinfo)
        end_local = parse_local_datetime(args.end_local, tzinfo)
    elif not args.start_local and not args.end_local:
        # Default: yesterday 16:00-17:00 in input timezone.
        now_local = datetime.now(tzinfo)
        yesterday = (now_local - timedelta(days=1)).date()
        start_local = datetime(
            yesterday.year, yesterday.month, yesterday.day, 16, 0, 0, tzinfo=tzinfo
        )
        end_local = datetime(
            yesterday.year, yesterday.month, yesterday.day, 17, 0, 0, tzinfo=tzinfo
        )
    else:
        raise ValueError("start_local and end_local must be provided together")

    if end_local <= start_local:
        raise ValueError("end_local must be greater than start_local")

    return ExportWindow(
        start_utc=start_local.astimezone(timezone.utc),
        end_utc=end_local.astimezone(timezone.utc),
        input_tz=tzinfo,
    )


def to_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def export_alerts(db_path: Path, output_csv: Path, window: ExportWindow) -> tuple[int, int]:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT id, scanned_at, suspicious_count, alerts
            FROM scan_history
            WHERE scanned_at >= ?
              AND scanned_at < ?
            ORDER BY id DESC
            """,
            (to_z(window.start_utc), to_z(window.end_utc)),
        ).fetchall()
    finally:
        conn.close()

    output_csv.parent.mkdir(parents=True, exist_ok=True)

    exported_alerts = 0
    with output_csv.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(
            [
                "scan_id",
                "scanned_at_utc",
                "scan_suspicious_count",
                "rule_label",
                "server",
                "login",
                "symbol",
                "order_count",
                "total_lots",
                "first_open",
                "last_open",
                "equity",
                "balance",
                "equity_per_lot",
                "group",
            ]
        )

        for row in rows:
            alerts = json.loads(row["alerts"]) if row["alerts"] else []
            for alert in alerts:
                writer.writerow(
                    [
                        row["id"],
                        row["scanned_at"],
                        row["suspicious_count"],
                        alert.get("rule_label"),
                        alert.get("server"),
                        alert.get("login"),
                        alert.get("symbol"),
                        alert.get("order_count"),
                        alert.get("total_lots"),
                        alert.get("first_open"),
                        alert.get("last_open"),
                        alert.get("equity"),
                        alert.get("balance"),
                        alert.get("equity_per_lot"),
                        alert.get("group"),
                    ]
                )
                exported_alerts += 1

    return len(rows), exported_alerts


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Export risk monitor suspicious alert details to CSV."
    )
    parser.add_argument(
        "--db",
        default=str(DEFAULT_DB),
        help=f"SQLite db path (default: {DEFAULT_DB})",
    )
    parser.add_argument(
        "--tz",
        default="+03:00",
        help="Input timezone offset, e.g. +03:00 (default: +03:00)",
    )
    parser.add_argument(
        "--start-local",
        help="Start time in input timezone, format: YYYY-MM-DD HH:MM:SS",
    )
    parser.add_argument(
        "--end-local",
        help="End time in input timezone, format: YYYY-MM-DD HH:MM:SS",
    )
    parser.add_argument(
        "--out",
        default="backend/data/risk_monitor_alerts_export.csv",
        help="Output CSV path",
    )
    args = parser.parse_args()

    window = build_window(args)
    scan_count, alert_count = export_alerts(
        Path(args.db),
        Path(args.out),
        window,
    )

    print(f"input_tz: {args.tz}")
    print(f"utc_range: [{to_z(window.start_utc)}, {to_z(window.end_utc)})")
    print(f"scan_rows: {scan_count}")
    print(f"alert_rows: {alert_count}")
    print(f"csv: {args.out}")


if __name__ == "__main__":
    main()
