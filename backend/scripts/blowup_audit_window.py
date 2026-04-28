#!/usr/bin/env python3
"""
Blown-up account audit for arbitrary MT time windows.

What this script does:
1) Hourly stats of blown-up accounts (loss order + current BALANCE < 0)
2) Blown-up account summary + all closed trades in window
3) AB counterpart matching for loser trades (same symbol, opposite side, close open-time)
4) Export to Excel with multiple sheets + summary

Notes:
- Time is MT server time (UTC+3), no timezone conversion in SQL.
- Default server filter is sid=5 (MT5). You can pass sid=1,6 etc.
- By default, only same clientid pairs are kept in AB matching.
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
        "--min-acc-loss-usd",
        type=float,
        default=50.0,
        help="Keep accounts whose absolute cumulative loss in window >= this value; set <=0 to disable",
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
        default=False,
        help="Send email report with generated xlsx attachment (default: false)",
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
        read_timeout=180,
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


def build_email_html(
    start_mt: dt.datetime,
    end_mt: dt.datetime,
    sid_filter: tuple[int, ...],
    min_acc_loss_usd: float,
    hourly: pd.DataFrame,
    blown_accounts_df: pd.DataFrame,
    ab_pairs_df: pd.DataFrame,
) -> tuple[str, str]:
    """Build Chinese HTML email body (aligned with notebook style)."""
    total_loss_usd = float(hourly["total_loss_usd"].sum()) if not hourly.empty else 0.0
    blown_count = int(len(blown_accounts_df))
    ab_count = int(len(ab_pairs_df))
    hours_back = max(1, int((end_mt - start_mt).total_seconds() // 3600))
    now_hkt = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=8)).strftime(
        "%Y-%m-%d %H:%M"
    )
    subject = f"[KCM 风控] 爆仓客户审计 — {end_mt:%Y-%m-%d %H:%M} (过去 {hours_back}h)"

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

    html = f"""
<div style="font-family:Arial,'Microsoft YaHei',sans-serif;color:#1f2937;max-width:900px;line-height:1.55;">
  <h2 style="color:#991B1B;border-bottom:2px solid #FECACA;padding-bottom:6px;margin-bottom:8px">
    爆仓客户审计 — 过去 {hours_back} 小时
  </h2>
  <p style="margin:4px 0;color:#6b7280;font-size:13px">
    窗口 (MT): <b>{start_mt:%Y-%m-%d %H:%M} → {end_mt:%Y-%m-%d %H:%M}</b>
    &nbsp;·&nbsp; 服务器 sid={sid_filter}
    &nbsp;·&nbsp; 噪音阈值: |亏损|≥{min_acc_loss_usd} USD
  </p>
  <table role="presentation" cellpadding="0" cellspacing="0" border="0"
         style="border-collapse:collapse;width:100%;margin:14px 0;background:#FEF3C7;">
    <tr>
      <td style="border-left:4px solid #F59E0B;padding:10px 14px;">
    <b>结论：</b>窗口内共 <b>{blown_count}</b> 个爆仓账户，
    合计亏损 <b style="color:#991B1B">{total_loss_usd:,.2f} USD</b>，
    疑似 AB 配对 <b>{ab_count}</b> 对。
      </td>
    </tr>
  </table>
  <h3 style="color:#1F2937;margin-bottom:4px">① 小时分布</h3>
  {_table(hourly_show, 24, "窗口内无爆仓事件")}
  <h3 style="color:#1F2937;margin:18px 0 4px">② 爆仓账户清单</h3>
  {_table(acc_show, 50, "无爆仓账户")}
  <h3 style="color:#1F2937;margin:18px 0 4px">③ 疑似 AB 对家配对（Top 20）</h3>
  {_table(ab_show, 20, "未发现 AB 对家配对")}
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

    # 1) hourly raw (losing trades + current negative balance)
    log_step("STEP1", "Query hourly raw loser trades")
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
    log_step("STEP1", f"Hourly raw rows: {len(raw)}")

    kept_login_sids: set[str] = set()
    if raw.empty:
        hourly = pd.DataFrame(
            columns=[
                "hour_bucket",
                "loss_orders",
                "blown_accounts",
                "blown_clients",
                "total_loss_usd",
                "worst_single_loss_usd",
                "min_balance_usd",
            ]
        )
    else:
        raw["is_cent"] = raw["SYMBOL"].apply(is_cent)
        raw["loss_usd"] = raw.apply(lambda r: to_usd_eq(r["totalProfit"], r["is_cent"]), axis=1)
        raw["balance_usd"] = raw.apply(lambda r: to_usd_eq(r["BALANCE"], r["is_cent"]), axis=1)
        raw["hour_bucket"] = pd.to_datetime(raw["CLOSE_TIME"]).dt.floor("h")

        min_acc_loss = args.min_acc_loss_usd
        if min_acc_loss and min_acc_loss > 0:
            acc_loss = raw.groupby("loginSid")["loss_usd"].sum()
            kept_login_sids = set(acc_loss[acc_loss.abs() >= float(min_acc_loss)].index)
            raw = raw[raw["loginSid"].isin(kept_login_sids)].reset_index(drop=True)
        else:
            kept_login_sids = set(raw["loginSid"].unique())

        if raw.empty:
            hourly = pd.DataFrame(
                columns=[
                    "hour_bucket",
                    "loss_orders",
                    "blown_accounts",
                    "blown_clients",
                    "total_loss_usd",
                    "worst_single_loss_usd",
                    "min_balance_usd",
                ]
            )
        else:
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

    # 2) blown account summary
    log_step("STEP2", "Query blown account summary")
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
    log_step("STEP2", f"Blown account rows: {len(blown_accounts_df)}")
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

    # 3) all trades for blown accounts in window
    log_step("STEP3", "Query all trades for blown accounts")
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
        log_step("STEP3", f"Blown trade rows: {len(blown_trades_df)}")
        if not blown_trades_df.empty:
            blown_trades_df["is_cent"] = blown_trades_df["SYMBOL"].apply(is_cent)
            blown_trades_df["profit_usd"] = blown_trades_df.apply(
                lambda r: to_usd_eq(r["totalProfit"], r["is_cent"]), axis=1
            )
            blown_trades_df["balance_usd"] = blown_trades_df.apply(
                lambda r: to_usd_eq(r["BALANCE"], r["is_cent"]), axis=1
            )

    # 4) AB pair matching
    log_step("STEP4", "Query AB counterpart pairs")
    ab_pairs_df = pd.DataFrame()
    if kept_login_sids:
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
        log_step("STEP4", f"AB pair rows: {len(ab_pairs_df)}")
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

    # Summary sheet
    total_loss_usd = float(hourly["total_loss_usd"].sum()) if not hourly.empty else 0.0
    summary = pd.DataFrame(
        [
            {"metric": "window_start_mt", "value": start_mt.strftime("%Y-%m-%d %H:%M:%S")},
            {"metric": "window_end_mt", "value": end_mt.strftime("%Y-%m-%d %H:%M:%S")},
            {"metric": "sid_filter", "value": ",".join(str(x) for x in sid_filter)},
            {"metric": "exclude_demo_test", "value": str(args.exclude_demo_test)},
            {"metric": "same_client_only", "value": str(args.same_client_only)},
            {"metric": "min_acc_loss_usd", "value": args.min_acc_loss_usd},
            {"metric": "hourly_rows", "value": int(len(hourly))},
            {"metric": "blown_accounts", "value": int(len(blown_accounts_df))},
            {"metric": "all_trades_rows", "value": int(len(blown_trades_df))},
            {"metric": "ab_pairs", "value": int(len(ab_pairs_df))},
            {"metric": "total_loss_usd", "value": round(total_loss_usd, 2)},
        ]
    )

    log_step("STEP5", "Write Excel report")
    out = args.out or (BACKEND_ROOT / "scripts" / f"blowup_audit_{end_mt:%Y%m%d_%H%M}.xlsx")
    out.parent.mkdir(parents=True, exist_ok=True)
    with pd.ExcelWriter(out, engine="openpyxl") as writer:
        summary.to_excel(writer, sheet_name="summary", index=False)
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
        fmt_sheet(
            ab_pairs_df,
            [
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
            ],
        ).to_excel(writer, sheet_name="ab_pairs", index=False)

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
            min_acc_loss_usd=args.min_acc_loss_usd,
            hourly=hourly,
            blown_accounts_df=blown_accounts_df,
            ab_pairs_df=ab_pairs_df,
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
        log_step("STEP6", "Skip email (--send-email not enabled)")

    log_step("DONE", f"Total elapsed: {time.perf_counter() - all_t0:.2f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
