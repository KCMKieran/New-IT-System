#!/usr/bin/env python3
"""
Login IP Monitor - One-shot backfill script.

从 MT4 / MT5 / MT4_Live2 三台服务器把过去 N 天的登录日志拉下来，
解析为 3 份 JSON 存档，**处理完立即删除原始 .log** 避免磁盘爆。

设计原则
--------
- 一天一天串行处理：同一时刻最多只有 1 天的 .log 在磁盘上。
- 单 server 失败不影响其他 server；单天失败不影响其他天。
- 已经有 JSON 的日期默认跳过（--force 可重跑）。
- **只产出 JSON，不写 SQLite**：DB 层留给 Phase 1（core/login_ip_db.py）。

Usage
-----
    cd /opt/myproject/New-IT-System/backend
    source .venv/bin/activate

    # 只跑一天（第一次调试用，强烈推荐先跑这个）
    python scripts/backfill_login_ip.py --date 20260422

    # 默认：回填截至昨天的过去 15 天
    python scripts/backfill_login_ip.py

    # 指定区间
    python scripts/backfill_login_ip.py --start 20260408 --end 20260422

    # 强制覆盖已有 JSON
    python scripts/backfill_login_ip.py --days 15 --force

目录
----
    backend/data/login_ip/tmp/YYYYMMDD/        # 临时 .log，处理完 rmtree
    backend/data/login_ip/YYYYMMDD/*.json      # 长期保留
    backend/logs_login_ip/backfill_*.log       # 本脚本的执行日志
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import shutil
import sys
from pathlib import Path

from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
# backend/ root (this file lives at backend/scripts/backfill_login_ip.py)
BACKEND_ROOT = Path(__file__).resolve().parent.parent
# Make `from app.services...` work when invoked as a plain script (not -m).
sys.path.insert(0, str(BACKEND_ROOT))

from app.services.login_ip_analyzer_service import (  # noqa: E402
    ACCOUNT_LOGINS_FILE,
    IP_MAPPING_FILE,
    RAW_LOGINS_FILE,
    analyze_date,
)
from app.services.login_ip_ftp_service import (  # noqa: E402
    download_daily_logs,
    load_ftp_configs,
)

DATA_DIR = BACKEND_ROOT / "data" / "login_ip"
TMP_ROOT = DATA_DIR / "tmp"
# Script-level logs live OUTSIDE backend/logs so they never mix with the main
# FastAPI log. This keeps `ls backend/logs_login_ip/` scannable at a glance.
SCRIPT_LOG_DIR = BACKEND_ROOT / "logs_login_ip"

# Logger is configured in main() after we know the run-id timestamp.
logger = logging.getLogger("backfill_login_ip")


# ---------------------------------------------------------------------------
# Per-day orchestration
# ---------------------------------------------------------------------------
# Parse + JSON save + login_history writes have been extracted to
# backend/app/services/login_ip_analyzer_service.py in Phase 3. This script
# now does just: download → call analyze_date → wipe tmp .log files.
# ---------------------------------------------------------------------------


def process_one_day(
    target_date: str,
    force: bool,
) -> dict:
    """Download + analyze + cleanup one calendar day.

    Returns a summary dict used for the final report.
    """
    summary: dict = {
        "date": target_date,
        "status": "skipped",
        "downloaded": {},
        "parsed": {},
        "login_history_inserted": 0,
        "error": None,
    }

    out_dir = DATA_DIR / target_date
    tmp_dir = TMP_ROOT / target_date

    # --- 0. Skip if fully done already ---
    existing_jsons = [
        out_dir / IP_MAPPING_FILE,
        out_dir / ACCOUNT_LOGINS_FILE,
        out_dir / RAW_LOGINS_FILE,
    ]
    if not force and all(p.exists() for p in existing_jsons):
        logger.info("[%s] all JSON files exist, skipping. Use --force to overwrite.", target_date)
        return summary

    summary["status"] = "processing"

    try:
        # --- 1. Download each server into <TMP_ROOT>/<target_date>/ ---
        # download_daily_logs internally creates the <target_date>/ subdir,
        # so we pass TMP_ROOT here, not tmp_dir.
        summary["downloaded"] = download_daily_logs(target_date, TMP_ROOT)

        # --- 2. Parse + save JSON + write login_history (delegated) ---
        # analyze_date reads from <TMP_ROOT>/<target_date>/ and writes JSON
        # to <DATA_DIR>/<target_date>/ and login_history rows into login_ip.db.
        analysis = analyze_date(target_date, log_dir=TMP_ROOT, out_dir=DATA_DIR)
        summary["parsed"] = analysis["servers"] or {}
        summary["login_history_inserted"] = analysis["login_history_inserted"]
        # Map the service's status back to the script's status vocabulary.
        summary["status"] = "empty" if analysis["status"] == "empty" else "ok"

    except Exception as exc:
        logger.exception("[%s] day FAILED: %s", target_date, exc)
        summary["status"] = "error"
        summary["error"] = str(exc)

    finally:
        # --- 3. ALWAYS wipe the tmp dir (the disk-saving guarantee) ---
        if tmp_dir.exists():
            try:
                shutil.rmtree(tmp_dir)
                logger.info("[%s] wiped tmp dir %s", target_date, tmp_dir)
            except Exception as exc:
                logger.warning("[%s] tmp cleanup FAILED: %s", target_date, exc)

    return summary


# ---------------------------------------------------------------------------
# CLI & main
# ---------------------------------------------------------------------------


def parse_date_range(args: argparse.Namespace) -> list[str]:
    """Resolve CLI args into a list of YYYYMMDD strings (newest first)."""
    fmt = "%Y%m%d"

    if args.date:
        dt.datetime.strptime(args.date, fmt)
        return [args.date]

    if args.start and args.end:
        start = dt.datetime.strptime(args.start, fmt).date()
        end = dt.datetime.strptime(args.end, fmt).date()
        if start > end:
            start, end = end, start
    else:
        end = (dt.datetime.now() - dt.timedelta(days=1)).date()
        start = end - dt.timedelta(days=args.days - 1)

    dates: list[str] = []
    cur = end
    while cur >= start:
        dates.append(cur.strftime(fmt))
        cur -= dt.timedelta(days=1)
    return dates


def setup_logging(run_id: str, verbose: bool) -> Path:
    """Configure root logger to write to both console and a per-run file."""
    SCRIPT_LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_file = SCRIPT_LOG_DIR / f"backfill_{run_id}.log"

    level = logging.DEBUG if verbose else logging.INFO
    fmt = "%(asctime)s [%(levelname)s] %(message)s"
    datefmt = "%Y-%m-%d %H:%M:%S"

    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(logging.Formatter(fmt, datefmt))
    root.addHandler(console)

    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(logging.Formatter(fmt, datefmt))
    root.addHandler(file_handler)

    return log_file


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Backfill Login IP Monitor JSON from MT4/MT5/MT4_Live2 FTP logs.",
        formatter_class=argparse.RawTextHelpFormatter,
    )
    parser.add_argument("--date", help="Single day (YYYYMMDD). Overrides --start/--end/--days.")
    parser.add_argument("--start", help="Inclusive start date (YYYYMMDD). Requires --end.")
    parser.add_argument("--end", help="Inclusive end date (YYYYMMDD). Requires --start.")
    parser.add_argument(
        "--days",
        type=int,
        default=15,
        help="If --start/--end not given, backfill this many days ending yesterday. Default: 15.",
    )
    parser.add_argument("--force", action="store_true", help="Re-run even if JSON already exists.")
    parser.add_argument("-v", "--verbose", action="store_true", help="DEBUG-level logging.")
    args = parser.parse_args()

    run_id = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file = setup_logging(run_id, args.verbose)

    # Load .env from backend/ (pointed to explicitly so running from any CWD works)
    load_dotenv(BACKEND_ROOT / ".env")

    logger.info("=" * 70)
    logger.info("Login IP backfill run_id=%s", run_id)
    logger.info("log file: %s", log_file)

    # Fail fast on missing .env values before looping through dates.
    try:
        load_ftp_configs()
    except RuntimeError as exc:
        logger.error(str(exc))
        return 2

    try:
        dates = parse_date_range(args)
    except ValueError as exc:
        logger.error("invalid date arg: %s", exc)
        return 2

    logger.info("target dates (%s): %s", len(dates), ", ".join(dates))
    logger.info("data dir:   %s", DATA_DIR)
    logger.info("tmp dir:    %s", TMP_ROOT)
    logger.info("=" * 70)

    summaries: list[dict] = []
    for d in dates:
        logger.info("")
        logger.info("===== %s =====", d)
        summaries.append(process_one_day(d, args.force))

    # --- Final report ---
    logger.info("")
    logger.info("=" * 70)
    logger.info("FINAL SUMMARY")
    logger.info("=" * 70)
    ok = sum(1 for s in summaries if s["status"] == "ok")
    skipped = sum(1 for s in summaries if s["status"] == "skipped")
    empty = sum(1 for s in summaries if s["status"] == "empty")
    errored = sum(1 for s in summaries if s["status"] == "error")
    total_hist = sum(s.get("login_history_inserted", 0) for s in summaries)
    logger.info(
        "days ok=%d skipped=%d empty=%d error=%d total=%d | login_history rows inserted: %d",
        ok, skipped, empty, errored, len(summaries), total_hist,
    )

    for s in summaries:
        if s["status"] == "skipped":
            continue
        dl_ok = [k for k, v in s["downloaded"].items() if v]
        dl_fail = [k for k, v in s["downloaded"].items() if not v]
        parts = [f"dl=[{','.join(dl_ok) or '-'}]"]
        if dl_fail:
            parts.append(f"dl_fail=[{','.join(dl_fail)}]")
        for srv, info in (s["parsed"] or {}).items():
            if info:
                # monitored / correlated are the values actually worth eyeballing
                # during a backfill run; unique_ips/accs are just sanity numbers.
                parts.append(
                    f"{srv}:ips={info['unique_ips']},accs={info['unique_accounts']},"
                    f"mon={info['monitored_logins']},corr={info['correlated_accounts']}"
                )
        if s.get("login_history_inserted"):
            parts.append(f"hist+={s['login_history_inserted']}")
        logger.info("  %s %-10s %s", s["date"], s["status"], "  ".join(parts))

    # exit 1 if any day errored (cron / CI friendly)
    return 1 if errored else 0


if __name__ == "__main__":
    sys.exit(main())
