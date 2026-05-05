#!/usr/bin/env python3
"""
Blown-up account audit for arbitrary MT time windows.

What this script does:
1) Hourly stats + account summary + all window trades + AB matching (see --audit-mode)
2) balance_loss: losing close + current BALANCE < 0 (business 口径 B)
3) so: Stop-out rows via COMMENT rules from blowup-so-monitoring.md (口径 A)
4) both: runs the two pipelines; Excel / email include both when applicable

Notes:
- Time is MT server time (UTC+3), no timezone conversion in SQL.
- Default server filter is sid=5 (MT5). You can pass sid=1,6 etc.
- By default, only same clientid pairs are kept in AB matching.
- Email with Excel attachment is sent by default; pass --no-send-email to skip.
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import sys
import time
from pathlib import Path

import pandas as pd
import pymysql
import pymysql.cursors

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

CENT_SUFFIXES = (".cent", ".kcmc")


def parse_dt(s: str) -> dt.datetime:
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return dt.datetime.strptime(s, fmt)
        except ValueError:
            continue
    raise argparse.ArgumentTypeError(
        f"Invalid datetime '{s}', expected 'YYYY-MM-DD HH:MM[:SS]'"
    )


def parse_sid_list(s: str) -> tuple[int, ...]:
    parts = [p.strip() for p in s.split(",") if p.strip()]
    if not parts:
        raise argparse.ArgumentTypeError("sid list cannot be empty")
    vals: list[int] = []
    for p in parts:
        v = int(p)
        if v <= 0:
            raise argparse.ArgumentTypeError("sid must be positive")
        vals.append(v)
    return tuple(sorted(set(vals)))


def build_args() -> argparse.Namespace:
    env_mail_to = os.environ.get("BLOWUP_AUDIT_MAIL_TO", "").strip()
    env_mail_cc = os.environ.get("BLOWUP_AUDIT_MAIL_CC", "").strip()
    p = argparse.ArgumentParser(description="Audit blown-up accounts in MT window")
    p.add_argument(
        "--end-mt",
        type=parse_dt,
        default=None,
        help="Window end in MT time, e.g. '2026-04-27 02:00:00' (default: now in MT)",
    )
    p.add_argument(
        "--hours-back",
        type=int,
        default=24,
        help="Lookback hours when --start-mt is not provided (default: 24)",
    )
    p.add_argument(
        "--start-mt",
        type=parse_dt,
        default=None,
        help="Window start in MT time, overrides --hours-back",
    )
    p.add_argument(
        "--sid",
        type=parse_sid_list,
        default=(5,),
        help="Server sid list. Example: '5' (MT5), '1,6', '1,5,6'",
    )
    p.add_argument(
        "--audit-mode",
        choices=("balance_loss", "so", "both"),
        default="balance_loss",
        help="balance_loss=亏损+当前负余额; so=COMMENT强平; both=两套并行 (default: balance_loss)",
    )
    p.add_argument(
        "--min-acc-loss-usd",
        type=float,
        default=0.0,
        help="Deprecated: loss-threshold filtering is disabled and this value is ignored",
    )
    p.add_argument(
        "--exclude-demo-test",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Exclude accounts whose groupsid/name contains demo/test (default: true)",
    )
    p.add_argument(
        "--same-client-only",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Keep AB pairs only when counterpart clientid == loser clientid (default: true)",
    )
    p.add_argument("--max-open-diff-sec", type=int, default=60)
    p.add_argument("--min-lot-ratio", type=float, default=0.5)
    p.add_argument("--max-lot-ratio", type=float, default=2.0)
    p.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Output xlsx path (default: backend/scripts/blowup_audit_<END_MT>.xlsx)",
    )
    p.add_argument(
        "--send-email",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Send email report with generated xlsx attachment (default: true; use --no-send-email to skip)",
    )
    p.add_argument(
        "--mail-to",
        type=str,
        default=env_mail_to,
        help="Email recipients (comma-separated). Default from .env BLOWUP_AUDIT_MAIL_TO",
    )
    p.add_argument(
        "--mail-cc",
        type=str,
        default=env_mail_cc,
        help="Email CC recipients (comma-separated). Default from .env BLOWUP_AUDIT_MAIL_CC",
    )
    return p.parse_args()


def is_cent(symbol: str | None) -> bool:
    if not symbol:
        return False
    s = symbol.lower()
    return any(s.endswith(suf) for suf in CENT_SUFFIXES)


def to_usd_eq(v, cent_flag: bool) -> float:
    try:
        value = float(v or 0)
    except (TypeError, ValueError):
        return 0.0
    return value / 100 if cent_flag else value


def demo_filter_sql(alias: str, enabled: bool) -> str:
    if not enabled:
        return ""
    # Escape % for pymysql %-formatting parser.
    return (
        f"\n      AND LOWER({alias}.groupsid) NOT LIKE '%%demo%%'"
        f"\n      AND LOWER({alias}.groupsid) NOT LIKE '%%test%%'"
        f"\n      AND LOWER({alias}.NAME)     NOT LIKE '%%demo%%'"
        f"\n      AND LOWER({alias}.NAME)     NOT LIKE '%%test%%'"
    )


def so_comment_filter_sql(alias: str) -> str:
    """Stop-out rows: fixed COMMENT prefixes (see blowup-so-monitoring.md). Use %% for PyMySQL."""
    return (
        f"\n      AND (\n"
        f"        {alias}.COMMENT LIKE '[so%%'\n"
        f"        OR TRIM({alias}.COMMENT) LIKE 'so:%%'\n"
        f"        OR TRIM({alias}.COMMENT) LIKE 'cso:%%'\n"
        f"      )"
    )


def get_conn():
    from app.core.config import get_settings

    s = get_settings()
    return pymysql.connect(
        host=s.DB_HOST,
        user=s.DB_USER,
        password=s.DB_PASSWORD,
        database=s.MYSQL_DATABASE_FXBACKOFFICE,
        port=int(s.DB_PORT),
        charset=s.DB_CHARSET,
        cursorclass=pymysql.cursors.DictCursor,
        connect_timeout=10,
        read_timeout=600,
    )


def run_sql(sql: str, params: tuple | list = ()) -> pd.DataFrame:
    t0 = dt.datetime.now()
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    print(f"[sql] {len(rows):>6,} rows in {(dt.datetime.now()-t0).total_seconds():.2f}s")
    return pd.DataFrame(rows)


def log_step(step: str, message: str) -> None:
    now = dt.datetime.now().strftime("%H:%M:%S")
    print(f"[{now}] [{step}] {message}", flush=True)


def fmt_sheet(df: pd.DataFrame, cols: list[str]) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame(columns=cols)
    keep = [c for c in cols if c in df.columns]
    return df[keep].copy()


HOURLY_BALANCE_COLS = [
    "hour_bucket",
    "loss_orders",
    "blown_accounts",
    "blown_clients",
    "total_loss_usd",
    "worst_single_loss_usd",
    "min_balance_usd",
]
HOURLY_SO_COLS = [
    "hour_bucket",
    "so_orders",
    "so_accounts",
    "so_clients",
    "so_pnl_usd",
    "worst_so_usd",
    "min_balance_usd",
]


def run_balance_loss_audit(
    db: str,
    date_sql: str,
    sid_sql: str,
    start_mt: dt.datetime,
    end_mt: dt.datetime,
    args: argparse.Namespace,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame, set[str]]:
    """口径 B: window内亏损平仓 + mt4_users 当前 BALANCE < 0。"""
    log_step("BAL", "STEP1 Query hourly raw (loss + negative balance)")
    sql_hourly_raw = f"""
    SELECT
        L.CLOSE_TIME, L.SYMBOL, L.totalProfit, L.loginSid, U.userid, U.BALANCE
    FROM {db}.mt4_trades L
    JOIN {db}.mt4_users  U ON U.loginsid = L.loginSid
    WHERE L.closeDate IN {date_sql}
      AND L.CLOSE_TIME >= %s
      AND L.CLOSE_TIME <  %s
      AND L.sid       IN {sid_sql}
      AND L.CMD       IN (0, 1)
      AND L.totalProfit < 0
      AND (L.isDeleted = 0 OR L.isDeleted IS NULL)
      AND U.BALANCE   < 0{demo_filter_sql('U', args.exclude_demo_test)}
    """
    raw = run_sql(sql_hourly_raw, (start_mt, end_mt))
    log_step("BAL", f"STEP1 Hourly raw rows: {len(raw)}")

    kept_login_sids: set[str] = set()
    if raw.empty:
        hourly = pd.DataFrame(columns=HOURLY_BALANCE_COLS)
    else:
        raw["is_cent"] = raw["SYMBOL"].apply(is_cent)
        raw["loss_usd"] = raw.apply(lambda r: to_usd_eq(r["totalProfit"], r["is_cent"]), axis=1)
        raw["balance_usd"] = raw.apply(lambda r: to_usd_eq(r["BALANCE"], r["is_cent"]), axis=1)
        raw["hour_bucket"] = pd.to_datetime(raw["CLOSE_TIME"]).dt.floor("h")
        kept_login_sids = set(raw["loginSid"].unique())
        hourly = (
            raw.groupby("hour_bucket", as_index=False)
            .agg(
                loss_orders=("loginSid", "count"),
                blown_accounts=("loginSid", "nunique"),
                blown_clients=("userid", "nunique"),
                total_loss_usd=("loss_usd", "sum"),
                worst_single_loss_usd=("loss_usd", "min"),
                min_balance_usd=("balance_usd", "min"),
            )
            .sort_values("hour_bucket")
        )

    log_step("BAL", "STEP2 Query blown account summary")
    loser_acc_filter = ""
    if kept_login_sids:
        sid_list_sql = ",".join(f"'{x}'" for x in sorted(kept_login_sids))
        loser_acc_filter = f"\n      AND L.loginSid IN ({sid_list_sql})"

    sql_accounts = f"""
    SELECT
        L.loginSid, U.userid, U.NAME AS name, U.groupsid, U.BALANCE,
        COUNT(*) AS loss_orders, SUM(L.totalProfit) AS total_loss_raw, MIN(L.totalProfit) AS worst_single_raw
    FROM {db}.mt4_trades L
    JOIN {db}.mt4_users  U ON U.loginsid = L.loginSid
    WHERE L.closeDate IN {date_sql}
      AND L.CLOSE_TIME >= %s
      AND L.CLOSE_TIME <  %s
      AND L.sid       IN {sid_sql}
      AND L.CMD       IN (0, 1)
      AND L.totalProfit < 0
      AND (L.isDeleted = 0 OR L.isDeleted IS NULL)
      AND U.BALANCE   < 0{demo_filter_sql('U', args.exclude_demo_test)}{loser_acc_filter}
    GROUP BY L.loginSid, U.userid, U.NAME, U.groupsid, U.BALANCE
    ORDER BY total_loss_raw ASC
    """
    blown_accounts_df = run_sql(sql_accounts, (start_mt, end_mt))
    log_step("BAL", f"STEP2 Blown account rows: {len(blown_accounts_df)}")
    if not blown_accounts_df.empty:
        blown_accounts_df["is_cent"] = blown_accounts_df["loginSid"].str.startswith("6-")
        blown_accounts_df["balance_usd"] = blown_accounts_df.apply(
            lambda r: to_usd_eq(r["BALANCE"], r["is_cent"]), axis=1
        )
        blown_accounts_df["total_loss_usd"] = blown_accounts_df.apply(
            lambda r: to_usd_eq(r["total_loss_raw"], r["is_cent"]), axis=1
        )
        blown_accounts_df["worst_single_loss_usd"] = blown_accounts_df.apply(
            lambda r: to_usd_eq(r["worst_single_raw"], r["is_cent"]), axis=1
        )

    log_step("BAL", "STEP3 Query all trades for blown accounts in window")
    blown_trades_df = pd.DataFrame()
    if not blown_accounts_df.empty:
        login_sid_sql = ",".join(f"'{x}'" for x in blown_accounts_df["loginSid"].tolist())
        sql_trades = f"""
        SELECT
            L.loginSid, U.userid, U.NAME AS name, U.groupsid,
            L.TICKET, L.SYMBOL, L.CMD, L.OPEN_TIME, L.CLOSE_TIME, L.lots, L.totalProfit, U.BALANCE
        FROM {db}.mt4_trades L
        JOIN {db}.mt4_users  U ON U.loginsid = L.loginSid
        WHERE L.closeDate IN {date_sql}
          AND L.CLOSE_TIME >= %s
          AND L.CLOSE_TIME <  %s
          AND L.sid       IN {sid_sql}
          AND L.CMD       IN (0, 1)
          AND (L.isDeleted = 0 OR L.isDeleted IS NULL)
          AND L.loginSid IN ({login_sid_sql})
        ORDER BY L.loginSid, L.CLOSE_TIME
        """
        blown_trades_df = run_sql(sql_trades, (start_mt, end_mt))
        log_step("BAL", f"STEP3 Blown trade rows: {len(blown_trades_df)}")
        if not blown_trades_df.empty:
            blown_trades_df["is_cent"] = blown_trades_df["SYMBOL"].apply(is_cent)
            blown_trades_df["profit_usd"] = blown_trades_df.apply(
                lambda r: to_usd_eq(r["totalProfit"], r["is_cent"]), axis=1
            )
            blown_trades_df["balance_usd"] = blown_trades_df.apply(
                lambda r: to_usd_eq(r["BALANCE"], r["is_cent"]), axis=1
            )

    log_step("BAL", "STEP4 Query AB counterpart pairs (balance-loss losers)")
    ab_pairs_df = pd.DataFrame()
    if kept_login_sids:
        sid_list_sql = ",".join(f"'{x}'" for x in sorted(kept_login_sids))
        same_client_filter = "\n          AND Cu.userid = Ls.L_userid" if args.same_client_only else ""
        sql_ab = f"""
        WITH losers AS (
            SELECT
                L.loginSid AS L_loginSid, L.TICKET AS L_ticket, L.SYMBOL,
                L.CMD AS L_cmd, L.lots AS L_lots, L.OPEN_TIME AS L_open_time,
                L.openDate AS L_open_date, L.CLOSE_TIME AS L_close_time, L.totalProfit AS L_profit,
                U.userid AS L_userid, U.BALANCE AS L_balance, U.NAME AS L_name
            FROM {db}.mt4_trades L
            JOIN {db}.mt4_users U ON U.loginsid = L.loginSid
            WHERE L.closeDate IN {date_sql}
              AND L.CLOSE_TIME >= %s
              AND L.CLOSE_TIME <  %s
              AND L.sid       IN {sid_sql}
              AND L.CMD       IN (0, 1)
              AND L.totalProfit < 0
              AND (L.isDeleted = 0 OR L.isDeleted IS NULL)
              AND U.BALANCE < 0
              AND L.loginSid IN ({sid_list_sql}){demo_filter_sql('U', args.exclude_demo_test)}
        )
        SELECT
            Ls.L_loginSid, Ls.L_userid, Ls.L_name, Ls.L_ticket, Ls.SYMBOL,
            Ls.L_open_time, Ls.L_close_time, Ls.L_cmd, Ls.L_lots, Ls.L_profit, Ls.L_balance,
            C.loginSid AS C_loginSid, Cu.userid AS C_userid, Cu.NAME AS C_name,
            C.TICKET AS C_ticket, C.OPEN_TIME AS C_open_time, C.CLOSE_TIME AS C_close_time,
            C.CMD AS C_cmd, C.lots AS C_lots, C.totalProfit AS C_profit,
            TIMESTAMPDIFF(SECOND, Ls.L_open_time, C.OPEN_TIME) AS open_diff_sec,
            ROUND(C.lots / Ls.L_lots, 2) AS lot_ratio
        FROM losers Ls
        JOIN {db}.mt4_trades C
          ON C.openDate = Ls.L_open_date
         AND C.sid IN {sid_sql}
         AND C.SYMBOL = Ls.SYMBOL
         AND C.CMD != Ls.L_cmd
         AND C.CMD IN (0, 1)
         AND C.totalProfit > 0
         AND C.OPEN_TIME BETWEEN Ls.L_open_time - INTERVAL {int(args.max_open_diff_sec)} SECOND
                             AND Ls.L_open_time + INTERVAL {int(args.max_open_diff_sec)} SECOND
         AND (C.isDeleted = 0 OR C.isDeleted IS NULL)
        JOIN {db}.mt4_users Cu
          ON Cu.loginsid = C.loginSid
        WHERE C.lots BETWEEN Ls.L_lots * %s AND Ls.L_lots * %s{same_client_filter}
          {demo_filter_sql('Cu', args.exclude_demo_test)}
        ORDER BY Ls.L_profit ASC, ABS(open_diff_sec) ASC
        """
        ab_pairs_df = run_sql(sql_ab, (start_mt, end_mt, args.min_lot_ratio, args.max_lot_ratio))
        log_step("BAL", f"STEP4 AB pair rows: {len(ab_pairs_df)}")
        if not ab_pairs_df.empty:
            ab_pairs_df["is_cent"] = ab_pairs_df["SYMBOL"].apply(is_cent)
            ab_pairs_df["L_profit_usd"] = ab_pairs_df.apply(
                lambda r: to_usd_eq(r["L_profit"], r["is_cent"]), axis=1
            )
            ab_pairs_df["C_profit_usd"] = ab_pairs_df.apply(
                lambda r: to_usd_eq(r["C_profit"], r["is_cent"]), axis=1
            )
            ab_pairs_df["L_balance_usd"] = ab_pairs_df.apply(
                lambda r: to_usd_eq(r["L_balance"], r["is_cent"]), axis=1
            )
            ab_pairs_df["net_usd"] = (ab_pairs_df["L_profit_usd"] + ab_pairs_df["C_profit_usd"]).round(2)

    return hourly, blown_accounts_df, blown_trades_df, ab_pairs_df, kept_login_sids


def run_so_audit(
    db: str,
    date_sql: str,
    sid_sql: str,
    start_mt: dt.datetime,
    end_mt: dt.datetime,
    args: argparse.Namespace,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame, set[str]]:
    """口径 A: COMMENT 命中强平(SO)，不要求当前负余额。AB 的 loser 腿为 SO 且单笔亏损。"""
    log_step("SO ", "STEP1 Query hourly raw (COMMENT stop-out)")
    sql_so_raw = f"""
    SELECT
        L.CLOSE_TIME, L.SYMBOL, L.totalProfit, L.loginSid, U.userid, U.BALANCE, L.COMMENT
    FROM {db}.mt4_trades L
    JOIN {db}.mt4_users U ON U.loginsid = L.loginSid
    WHERE L.closeDate IN {date_sql}
      AND L.CLOSE_TIME >= %s
      AND L.CLOSE_TIME <  %s
      AND L.sid       IN {sid_sql}
      AND L.CMD       IN (0, 1)
      AND (L.isDeleted = 0 OR L.isDeleted IS NULL)
      {so_comment_filter_sql('L')}{demo_filter_sql('U', args.exclude_demo_test)}
    """
    raw_so = run_sql(sql_so_raw, (start_mt, end_mt))
    log_step("SO ", f"STEP1 SO raw rows: {len(raw_so)}")

    kept_so_sids: set[str] = set()
    if raw_so.empty:
        so_hourly = pd.DataFrame(columns=HOURLY_SO_COLS)
    else:
        raw_so["is_cent"] = raw_so["SYMBOL"].apply(is_cent)
        raw_so["pnl_usd"] = raw_so.apply(lambda r: to_usd_eq(r["totalProfit"], r["is_cent"]), axis=1)
        raw_so["balance_usd"] = raw_so.apply(lambda r: to_usd_eq(r["BALANCE"], r["is_cent"]), axis=1)
        raw_so["hour_bucket"] = pd.to_datetime(raw_so["CLOSE_TIME"]).dt.floor("h")
        kept_so_sids = set(raw_so["loginSid"].unique())
        so_hourly = (
            raw_so.groupby("hour_bucket", as_index=False)
            .agg(
                so_orders=("loginSid", "count"),
                so_accounts=("loginSid", "nunique"),
                so_clients=("userid", "nunique"),
                so_pnl_usd=("pnl_usd", "sum"),
                worst_so_usd=("pnl_usd", "min"),
                min_balance_usd=("balance_usd", "min"),
            )
            .sort_values("hour_bucket")
        )

    log_step("SO ", "STEP2 Query SO account summary")
    so_acc_filter = ""
    if kept_so_sids:
        sid_list_sql_so = ",".join(f"'{x}'" for x in sorted(kept_so_sids))
        so_acc_filter = f"\n      AND L.loginSid IN ({sid_list_sql_so})"

    sql_so_accounts = f"""
    SELECT
        L.loginSid, U.userid, U.NAME AS name, U.groupsid, U.BALANCE,
        COUNT(*) AS so_orders, SUM(L.totalProfit) AS so_profit_raw, MIN(L.totalProfit) AS worst_so_raw
    FROM {db}.mt4_trades L
    JOIN {db}.mt4_users U ON U.loginsid = L.loginSid
    WHERE L.closeDate IN {date_sql}
      AND L.CLOSE_TIME >= %s
      AND L.CLOSE_TIME <  %s
      AND L.sid       IN {sid_sql}
      AND L.CMD       IN (0, 1)
      AND (L.isDeleted = 0 OR L.isDeleted IS NULL)
      {so_comment_filter_sql('L')}{demo_filter_sql('U', args.exclude_demo_test)}{so_acc_filter}
    GROUP BY L.loginSid, U.userid, U.NAME, U.groupsid, U.BALANCE
    ORDER BY so_profit_raw ASC
    """
    so_accounts_df = run_sql(sql_so_accounts, (start_mt, end_mt))
    log_step("SO ", f"STEP2 SO account rows: {len(so_accounts_df)}")
    if not so_accounts_df.empty:
        so_accounts_df["is_cent"] = so_accounts_df["loginSid"].str.startswith("6-")
        so_accounts_df["balance_usd"] = so_accounts_df.apply(
            lambda r: to_usd_eq(r["BALANCE"], r["is_cent"]), axis=1
        )
        so_accounts_df["so_pnl_usd"] = so_accounts_df.apply(
            lambda r: to_usd_eq(r["so_profit_raw"], r["is_cent"]), axis=1
        )
        so_accounts_df["worst_so_usd"] = so_accounts_df.apply(
            lambda r: to_usd_eq(r["worst_so_raw"], r["is_cent"]), axis=1
        )

    log_step("SO ", "STEP3 Query all window trades for SO-hit accounts")
    so_trades_df = pd.DataFrame()
    if not so_accounts_df.empty:
        login_sid_sql_so = ",".join(f"'{x}'" for x in so_accounts_df["loginSid"].tolist())
        sql_so_trades = f"""
        SELECT
            L.loginSid, U.userid, U.NAME AS name, U.groupsid,
            L.TICKET, L.SYMBOL, L.CMD, L.OPEN_TIME, L.CLOSE_TIME, L.lots, L.totalProfit, U.BALANCE, L.COMMENT
        FROM {db}.mt4_trades L
        JOIN {db}.mt4_users U ON U.loginsid = L.loginSid
        WHERE L.closeDate IN {date_sql}
          AND L.CLOSE_TIME >= %s
          AND L.CLOSE_TIME <  %s
          AND L.sid       IN {sid_sql}
          AND L.CMD       IN (0, 1)
          AND (L.isDeleted = 0 OR L.isDeleted IS NULL)
          AND L.loginSid IN ({login_sid_sql_so})
        ORDER BY L.loginSid, L.CLOSE_TIME
        """
        so_trades_df = run_sql(sql_so_trades, (start_mt, end_mt))
        log_step("SO ", f"STEP3 SO-related trade rows: {len(so_trades_df)}")
        if not so_trades_df.empty:
            so_trades_df["is_cent"] = so_trades_df["SYMBOL"].apply(is_cent)
            so_trades_df["profit_usd"] = so_trades_df.apply(
                lambda r: to_usd_eq(r["totalProfit"], r["is_cent"]), axis=1
            )
            so_trades_df["balance_usd"] = so_trades_df.apply(
                lambda r: to_usd_eq(r["BALANCE"], r["is_cent"]), axis=1
            )

    log_step("SO ", "STEP4 Query AB pairs (SO COMMENT anchor only)")
    so_ab_pairs_df = pd.DataFrame()
    if kept_so_sids:
        sid_list_sql = ",".join(f"'{x}'" for x in sorted(kept_so_sids))
        same_client_filter = "\n          AND Cu.userid = Ls.L_userid" if args.same_client_only else ""
        # SO mode: anchor leg is purely COMMENT-based (no totalProfit<0/BALANCE<0 gating);
        # counterpart still requires totalProfit>0 to keep AB-pair semantics meaningful.
        sql_so_ab = f"""
        WITH losers AS (
            SELECT
                L.loginSid AS L_loginSid, L.TICKET AS L_ticket, L.SYMBOL,
                L.CMD AS L_cmd, L.lots AS L_lots, L.OPEN_TIME AS L_open_time,
                L.openDate AS L_open_date, L.CLOSE_TIME AS L_close_time, L.totalProfit AS L_profit,
                U.userid AS L_userid, U.BALANCE AS L_balance, U.NAME AS L_name
            FROM {db}.mt4_trades L
            JOIN {db}.mt4_users U ON U.loginsid = L.loginSid
            WHERE L.closeDate IN {date_sql}
              AND L.CLOSE_TIME >= %s
              AND L.CLOSE_TIME <  %s
              AND L.sid       IN {sid_sql}
              AND L.CMD       IN (0, 1)
              AND (L.isDeleted = 0 OR L.isDeleted IS NULL)
              {so_comment_filter_sql('L')}
              AND L.loginSid IN ({sid_list_sql}){demo_filter_sql('U', args.exclude_demo_test)}
        )
        SELECT
            Ls.L_loginSid, Ls.L_userid, Ls.L_name, Ls.L_ticket, Ls.SYMBOL,
            Ls.L_open_time, Ls.L_close_time, Ls.L_cmd, Ls.L_lots, Ls.L_profit, Ls.L_balance,
            C.loginSid AS C_loginSid, Cu.userid AS C_userid, Cu.NAME AS C_name,
            C.TICKET AS C_ticket, C.OPEN_TIME AS C_open_time, C.CLOSE_TIME AS C_close_time,
            C.CMD AS C_cmd, C.lots AS C_lots, C.totalProfit AS C_profit,
            TIMESTAMPDIFF(SECOND, Ls.L_open_time, C.OPEN_TIME) AS open_diff_sec,
            ROUND(C.lots / Ls.L_lots, 2) AS lot_ratio
        FROM losers Ls
        JOIN {db}.mt4_trades C
          ON C.openDate = Ls.L_open_date
         AND C.sid IN {sid_sql}
         AND C.SYMBOL = Ls.SYMBOL
         AND C.CMD != Ls.L_cmd
         AND C.CMD IN (0, 1)
         AND C.totalProfit > 0
         AND C.OPEN_TIME BETWEEN Ls.L_open_time - INTERVAL {int(args.max_open_diff_sec)} SECOND
                             AND Ls.L_open_time + INTERVAL {int(args.max_open_diff_sec)} SECOND
         AND (C.isDeleted = 0 OR C.isDeleted IS NULL)
        JOIN {db}.mt4_users Cu
          ON Cu.loginsid = C.loginSid
        WHERE C.lots BETWEEN Ls.L_lots * %s AND Ls.L_lots * %s{same_client_filter}
          {demo_filter_sql('Cu', args.exclude_demo_test)}
        ORDER BY Ls.L_profit ASC, ABS(open_diff_sec) ASC
        """
        so_ab_pairs_df = run_sql(sql_so_ab, (start_mt, end_mt, args.min_lot_ratio, args.max_lot_ratio))
        log_step("SO ", f"STEP4 SO AB pair rows: {len(so_ab_pairs_df)}")
        if not so_ab_pairs_df.empty:
            so_ab_pairs_df["is_cent"] = so_ab_pairs_df["SYMBOL"].apply(is_cent)
            so_ab_pairs_df["L_profit_usd"] = so_ab_pairs_df.apply(
                lambda r: to_usd_eq(r["L_profit"], r["is_cent"]), axis=1
            )
            so_ab_pairs_df["C_profit_usd"] = so_ab_pairs_df.apply(
                lambda r: to_usd_eq(r["C_profit"], r["is_cent"]), axis=1
            )
            so_ab_pairs_df["L_balance_usd"] = so_ab_pairs_df.apply(
                lambda r: to_usd_eq(r["L_balance"], r["is_cent"]), axis=1
            )
            so_ab_pairs_df["net_usd"] = (so_ab_pairs_df["L_profit_usd"] + so_ab_pairs_df["C_profit_usd"]).round(2)

    return so_hourly, so_accounts_df, so_trades_df, so_ab_pairs_df, kept_so_sids


def build_email_html(
    start_mt: dt.datetime,
    end_mt: dt.datetime,
    sid_filter: tuple[int, ...],
    audit_mode: str,
    hourly: pd.DataFrame,
    blown_accounts_df: pd.DataFrame,
    ab_pairs_df: pd.DataFrame,
    so_hourly: pd.DataFrame,
    so_accounts_df: pd.DataFrame,
    so_ab_pairs_df: pd.DataFrame,
) -> tuple[str, str]:
    """Build Chinese HTML email body (balance_loss / so / both)."""
    total_loss_usd = float(hourly["total_loss_usd"].sum()) if not hourly.empty else 0.0
    blown_count = int(len(blown_accounts_df))
    ab_count = int(len(ab_pairs_df))
    so_pnl_total = float(so_hourly["so_pnl_usd"].sum()) if not so_hourly.empty else 0.0
    so_acc_count = int(len(so_accounts_df))
    so_ab_count = int(len(so_ab_pairs_df))
    now_hkt = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=8)).strftime(
        "%Y-%m-%d %H:%M"
    )
    window_tag = f"{start_mt:%Y-%m-%d %H:%M}~{end_mt:%H:%M} MT"
    if audit_mode == "so":
        subject = f"[KCM 风控] 强平(SO)审计 — {window_tag}"
    elif audit_mode == "both":
        subject = f"[KCM 风控] 爆仓+强平审计 — {window_tag}"
    else:
        subject = f"[KCM 风控] 爆仓客户审计 — {window_tag}"

    def _table(df: pd.DataFrame, max_rows: int = 20, empty_msg: str = "无数据") -> str:
        if df.empty:
            return f"<p style='color:#6b7280'>{empty_msg}</p>"
        show = df.head(max_rows).fillna("")
        th_style = (
            "padding:6px 8px;border:1px solid #d1d5db;background:#f3f4f6;"
            "font-size:12px;text-align:left;white-space:nowrap;"
        )
        td_style = "padding:6px 8px;border:1px solid #e5e7eb;font-size:12px;text-align:left;"
        headers = "".join(f"<th style='{th_style}'>{c}</th>" for c in show.columns)
        rows_html = []
        for row in show.itertuples(index=False, name=None):
            tds = "".join(f"<td style='{td_style}'>{v}</td>" for v in row)
            rows_html.append(f"<tr>{tds}</tr>")
        return (
            "<table role='presentation' cellpadding='0' cellspacing='0' border='0' "
            "style='border-collapse:collapse;mso-table-lspace:0pt;mso-table-rspace:0pt;"
            "width:100%;margin:6px 0 12px;'>"
            f"<thead><tr>{headers}</tr></thead>"
            f"<tbody>{''.join(rows_html)}</tbody>"
            "</table>"
        )

    hourly_show = fmt_sheet(
        hourly,
        [
            "hour_bucket",
            "loss_orders",
            "blown_accounts",
            "blown_clients",
            "total_loss_usd",
            "worst_single_loss_usd",
            "min_balance_usd",
        ],
    )
    acc_show = fmt_sheet(
        blown_accounts_df,
        [
            "loginSid",
            "userid",
            "name",
            "groupsid",
            "balance_usd",
            "loss_orders",
            "total_loss_usd",
            "worst_single_loss_usd",
        ],
    )
    ab_show = fmt_sheet(
        ab_pairs_df,
        [
            "L_loginSid",
            "L_userid",
            "L_name",
            "C_loginSid",
            "C_userid",
            "C_name",
            "SYMBOL",
            "open_diff_sec",
            "lot_ratio",
            "L_profit_usd",
            "C_profit_usd",
            "net_usd",
        ],
    )
    so_hourly_show = fmt_sheet(
        so_hourly,
        [
            "hour_bucket",
            "so_orders",
            "so_accounts",
            "so_clients",
            "so_pnl_usd",
            "worst_so_usd",
            "min_balance_usd",
        ],
    )
    so_acc_show = fmt_sheet(
        so_accounts_df,
        [
            "loginSid",
            "userid",
            "name",
            "groupsid",
            "balance_usd",
            "so_orders",
            "so_pnl_usd",
            "worst_so_usd",
        ],
    )
    so_ab_show = fmt_sheet(
        so_ab_pairs_df,
        [
            "L_loginSid",
            "L_userid",
            "L_name",
            "C_loginSid",
            "C_userid",
            "C_name",
            "SYMBOL",
            "open_diff_sec",
            "lot_ratio",
            "L_profit_usd",
            "C_profit_usd",
            "net_usd",
        ],
    )

    mode_note = {
        "balance_loss": "audit_mode=balance_loss（亏损+当前负余额）",
        "so": "audit_mode=so（COMMENT 强平）",
        "both": "audit_mode=both（两套并行）",
    }.get(audit_mode, audit_mode)

    bal_box = ""
    if audit_mode in ("balance_loss", "both"):
        bal_box = f"""
  <table role="presentation" cellpadding="0" cellspacing="0" border="0"
         style="border-collapse:collapse;width:100%;margin:14px 0;background:#FEF3C7;">
    <tr>
      <td style="border-left:4px solid #F59E0B;padding:10px 14px;">
    <b>口径 B（亏损+负余额）：</b>窗口内 <b>{blown_count}</b> 个账户，
    亏损单合计约 <b style="color:#991B1B">{total_loss_usd:,.2f} USD</b>，
    疑似 AB 配对 <b>{ab_count}</b> 对。
      </td>
    </tr>
  </table>"""

    so_box = ""
    if audit_mode in ("so", "both"):
        so_box = f"""
  <table role="presentation" cellpadding="0" cellspacing="0" border="0"
         style="border-collapse:collapse;width:100%;margin:14px 0;background:#E0F2FE;">
    <tr>
      <td style="border-left:4px solid #0284C7;padding:10px 14px;">
    <b>口径 A（COMMENT 强平）：</b>窗口内 <b>{so_acc_count}</b> 个账户，
    SO 成交盈亏合计约 <b style="color:#0369A1">{so_pnl_total:,.2f} USD</b>，
    疑似 AB 配对 <b>{so_ab_count}</b> 对（锚点为 SO 且单笔亏损）。
      </td>
    </tr>
  </table>"""

    bal_sections = ""
    if audit_mode in ("balance_loss", "both"):
        bal_sections = f"""
  <h3 style="color:#1F2937;margin-bottom:4px">① 口径 B — 小时分布</h3>
  {_table(hourly_show, 24, "窗口内无口径 B 事件")}
  <h3 style="color:#1F2937;margin:18px 0 4px">② 口径 B — 账户清单</h3>
  {_table(acc_show, 50, "无口径 B 账户")}
  <h3 style="color:#1F2937;margin:18px 0 4px">③ 口径 B — 疑似 AB（Top 20）</h3>
  {_table(ab_show, 20, "无口径 B AB 配对")}"""

    so_sections = ""
    if audit_mode in ("so", "both"):
        so_sections = f"""
  <h3 style="color:#0369A1;margin:18px 0 4px">④ 口径 A — SO 小时分布</h3>
  {_table(so_hourly_show, 24, "窗口内无 SO 成交")}
  <h3 style="color:#0369A1;margin:18px 0 4px">⑤ 口径 A — SO 账户清单</h3>
  {_table(so_acc_show, 50, "无 SO 账户")}
  <h3 style="color:#0369A1;margin:18px 0 4px">⑥ 口径 A — 疑似 AB（Top 20）</h3>
  {_table(so_ab_show, 20, "无口径 A AB 配对")}"""

    title = (
        "爆仓 + 强平审计"
        if audit_mode == "both"
        else ("强平(SO)审计" if audit_mode == "so" else "爆仓客户审计")
    )

    html = f"""
<div style="font-family:Arial,'Microsoft YaHei',sans-serif;color:#1f2937;max-width:900px;line-height:1.55;">
  <h2 style="color:#991B1B;border-bottom:2px solid #FECACA;padding-bottom:6px;margin-bottom:8px">
    {title}
  </h2>
  <p style="margin:4px 0;color:#6b7280;font-size:13px">
    窗口 (MT): <b>{start_mt:%Y-%m-%d %H:%M} → {end_mt:%Y-%m-%d %H:%M}</b>
    &nbsp;·&nbsp; 服务器 sid={sid_filter}
    &nbsp;·&nbsp; {mode_note}
  </p>
  {bal_box}
  {so_box}
  {bal_sections}
  {so_sections}
  <p style="font-size:12px;color:#6b7280;margin-top:24px;border-top:1px solid #e5e7eb;padding-top:8px;">
    生成于 {now_hkt} HKT &nbsp;·&nbsp; 完整数据见附件 Excel &nbsp;·&nbsp; 自动生成，请勿回复
  </p>
</div>
"""
    return subject, html


def main() -> int:
    from app.core.config import get_settings

    all_t0 = time.perf_counter()
    args = build_args()
    log_step("INIT", "Start blowup audit script")
    end_mt = args.end_mt or (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=3)).replace(
        tzinfo=None
    )
    start_mt = args.start_mt or (end_mt - dt.timedelta(hours=args.hours_back))
    if start_mt >= end_mt:
        raise SystemExit("start_mt must be < end_mt")

    sid_filter = args.sid
    sid_sql = "(" + ",".join(str(int(x)) for x in sid_filter) + ")"
    close_dates = sorted({start_mt.date(), end_mt.date()})
    date_sql = "(" + ",".join(f"'{d.isoformat()}'" for d in close_dates) + ")"
    db = get_settings().MYSQL_DATABASE_FXBACKOFFICE

    log_step("INIT", f"window(MT): {start_mt:%Y-%m-%d %H:%M:%S} -> {end_mt:%Y-%m-%d %H:%M:%S}")
    log_step("INIT", f"sid filter: {sid_filter}")
    log_step("INIT", f"closeDate IN: {[d.isoformat() for d in close_dates]}")
    log_step("INIT", f"audit_mode: {args.audit_mode}")

    hourly = pd.DataFrame(columns=HOURLY_BALANCE_COLS)
    blown_accounts_df = pd.DataFrame()
    blown_trades_df = pd.DataFrame()
    ab_pairs_df = pd.DataFrame()
    so_hourly = pd.DataFrame(columns=HOURLY_SO_COLS)
    so_accounts_df = pd.DataFrame()
    so_trades_df = pd.DataFrame()
    so_ab_pairs_df = pd.DataFrame()

    if args.audit_mode in ("balance_loss", "both"):
        hourly, blown_accounts_df, blown_trades_df, ab_pairs_df, _ = run_balance_loss_audit(
            db, date_sql, sid_sql, start_mt, end_mt, args
        )
    if args.audit_mode in ("so", "both"):
        so_hourly, so_accounts_df, so_trades_df, so_ab_pairs_df, _ = run_so_audit(
            db, date_sql, sid_sql, start_mt, end_mt, args
        )

    total_loss_usd = float(hourly["total_loss_usd"].sum()) if not hourly.empty else 0.0
    so_pnl_total = float(so_hourly["so_pnl_usd"].sum()) if not so_hourly.empty else 0.0

    summary_rows: list[dict[str, object]] = [
        {"metric": "audit_mode", "value": args.audit_mode},
        {"metric": "window_start_mt", "value": start_mt.strftime("%Y-%m-%d %H:%M:%S")},
        {"metric": "window_end_mt", "value": end_mt.strftime("%Y-%m-%d %H:%M:%S")},
        {"metric": "sid_filter", "value": ",".join(str(x) for x in sid_filter)},
        {"metric": "exclude_demo_test", "value": str(args.exclude_demo_test)},
        {"metric": "same_client_only", "value": str(args.same_client_only)},
        {"metric": "min_acc_loss_usd_deprecated", "value": args.min_acc_loss_usd},
    ]
    if args.audit_mode in ("balance_loss", "both"):
        summary_rows.extend(
            [
                {"metric": "bal_hourly_rows", "value": int(len(hourly))},
                {"metric": "bal_blown_accounts", "value": int(len(blown_accounts_df))},
                {"metric": "bal_all_trades_rows", "value": int(len(blown_trades_df))},
                {"metric": "bal_ab_pairs", "value": int(len(ab_pairs_df))},
                {"metric": "bal_total_loss_usd", "value": round(total_loss_usd, 2)},
            ]
        )
    if args.audit_mode in ("so", "both"):
        summary_rows.extend(
            [
                {"metric": "so_hourly_rows", "value": int(len(so_hourly))},
                {"metric": "so_accounts", "value": int(len(so_accounts_df))},
                {"metric": "so_all_trades_rows", "value": int(len(so_trades_df))},
                {"metric": "so_ab_pairs", "value": int(len(so_ab_pairs_df))},
                {"metric": "so_pnl_sum_usd", "value": round(so_pnl_total, 2)},
            ]
        )
    summary = pd.DataFrame(summary_rows)

    ab_sheet_cols = [
        "L_loginSid",
        "L_userid",
        "L_name",
        "L_ticket",
        "SYMBOL",
        "L_open_time",
        "L_close_time",
        "L_cmd",
        "L_lots",
        "L_profit_usd",
        "L_balance_usd",
        "C_loginSid",
        "C_userid",
        "C_name",
        "C_ticket",
        "C_open_time",
        "C_close_time",
        "C_cmd",
        "C_lots",
        "C_profit_usd",
        "open_diff_sec",
        "lot_ratio",
        "net_usd",
    ]

    log_step("STEP5", "Write Excel report")
    out = args.out or (BACKEND_ROOT / "scripts" / f"blowup_audit_{end_mt:%Y%m%d_%H%M}.xlsx")
    out.parent.mkdir(parents=True, exist_ok=True)
    with pd.ExcelWriter(out, engine="openpyxl") as writer:
        summary.to_excel(writer, sheet_name="summary", index=False)
        if args.audit_mode in ("balance_loss", "both"):
            hourly.to_excel(writer, sheet_name="hourly", index=False)
            fmt_sheet(
                blown_accounts_df,
                [
                    "loginSid",
                    "userid",
                    "name",
                    "groupsid",
                    "balance_usd",
                    "loss_orders",
                    "total_loss_usd",
                    "worst_single_loss_usd",
                ],
            ).to_excel(writer, sheet_name="blown_accounts", index=False)
            fmt_sheet(
                blown_trades_df,
                [
                    "loginSid",
                    "userid",
                    "name",
                    "groupsid",
                    "TICKET",
                    "SYMBOL",
                    "CMD",
                    "OPEN_TIME",
                    "CLOSE_TIME",
                    "lots",
                    "profit_usd",
                    "balance_usd",
                ],
            ).to_excel(writer, sheet_name="blown_trades", index=False)
            fmt_sheet(ab_pairs_df, ab_sheet_cols).to_excel(writer, sheet_name="ab_pairs", index=False)
        if args.audit_mode in ("so", "both"):
            so_hourly.to_excel(writer, sheet_name="so_hourly", index=False)
            fmt_sheet(
                so_accounts_df,
                [
                    "loginSid",
                    "userid",
                    "name",
                    "groupsid",
                    "balance_usd",
                    "so_orders",
                    "so_pnl_usd",
                    "worst_so_usd",
                ],
            ).to_excel(writer, sheet_name="so_accounts", index=False)
            fmt_sheet(
                so_trades_df,
                [
                    "loginSid",
                    "userid",
                    "name",
                    "groupsid",
                    "TICKET",
                    "SYMBOL",
                    "CMD",
                    "OPEN_TIME",
                    "CLOSE_TIME",
                    "lots",
                    "profit_usd",
                    "balance_usd",
                    "COMMENT",
                ],
            ).to_excel(writer, sheet_name="so_trades", index=False)
            fmt_sheet(so_ab_pairs_df, ab_sheet_cols).to_excel(writer, sheet_name="so_ab_pairs", index=False)

    log_step("STEP5", f"Excel written: {out}")
    print("SID mapping reminder: sid=1 (MT4 Live), sid=5 (MT5 Live), sid=6 (CEN).")

    if args.send_email:
        from app.services.email_service import send_email

        if not args.mail_to.strip():
            raise SystemExit(
                "mail-to is empty. Set --mail-to or define BLOWUP_AUDIT_MAIL_TO in backend/.env"
            )
        log_step("STEP6", f"Send email -> to={args.mail_to} cc={args.mail_cc or '(none)'}")
        subject, html = build_email_html(
            start_mt=start_mt,
            end_mt=end_mt,
            sid_filter=sid_filter,
            audit_mode=args.audit_mode,
            hourly=hourly,
            blown_accounts_df=blown_accounts_df,
            ab_pairs_df=ab_pairs_df,
            so_hourly=so_hourly,
            so_accounts_df=so_accounts_df,
            so_ab_pairs_df=so_ab_pairs_df,
        )
        send_email(
            subject=subject,
            body=html,
            to=args.mail_to,
            cc=args.mail_cc,
            attachments=[str(out)],
        )
        log_step("STEP6", f"Email sent with subject: {subject}")
    else:
        log_step("STEP6", "Skip email (--no-send-email)")

    log_step("DONE", f"Total elapsed: {time.perf_counter() - all_t0:.2f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
