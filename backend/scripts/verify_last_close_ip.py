"""Cross-check the `last_trade_ip` close snapshot against the CRM trade tables.

The journal only proves a line *matched a pattern*; it can't prove the close
actually executed. This script closes that gap from both directions:

  verify  (precision) — every row we recorded: does the CRM agree a close really
                        happened, for that account, at that second?
  recall  (recall)    — every close the CRM knows about: did we record it, and
                        if not, is the miss explained by the close being
                        server-initiated (SL/TP/stop-out/dealer), which carries
                        no client IP and is correctly skipped by design?

Two ID spaces, so two join paths (see docs/features/login-ip.md §3.4.1):

  MT4 / MT4_Live2 -> fxbackoffice.mt4_trades, PK ticketSid = '{sid}-{TICKET}'.
                     The journal's '#N' IS the MT4 order ticket.
  MT5             -> mt5_live.mt5_deals, index IDX_POSITION (Login, PositionID).
                     The journal's '#N' is the POSITION id (== the ticket of the
                     order that opened it), NOT mt4_trades.TICKET — the sid=5
                     mirror uses a separate numbering and joining on it returns
                     100% false "not found".

Timestamps need no tz conversion: mt4_trades.CLOSE_TIME and mt5_deals.Time are
both MT server local (UTC+3), the same clock the journal prints. But
mt5_deals.Timestamp is Windows FILETIME (100ns ticks since 1601-01-01 UTC), not
a unix timestamp — it is the only indexed time column, so ranges go through it.

Usage:
    python scripts/verify_last_close_ip.py verify [YYYYMMDD ...]
    python scripts/verify_last_close_ip.py recall [YYYYMMDD]
    python scripts/verify_last_close_ip.py report [--days N]

Run inside the backend container (needs app.core.config + DB reachability):
    docker exec new-it-backend-dev sh -c 'cd /app && python scripts/verify_last_close_ip.py verify'
"""

from __future__ import annotations

import argparse
import datetime as dt
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path

import pymysql

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.core.config import get_settings  # noqa: E402

DB_PATH = Path(__file__).resolve().parents[1] / "data" / "login_ip.db"

MT4_SID = {"MT4": 1, "MT4_Live2": 6}
SERVERS = ("MT4", "MT5", "MT4_Live2")

# Analyzer's parse-time demo/manager filter (login_ip_analyzer_service.py).
DEMO_PREFIX = {"MT5": "3", "MT4": "7", "MT4_Live2": "7"}
MIN_LOGIN_LEN = 5

# mt5_deals.Reason — which sources come from a terminal (so an IP is expected)?
REASON = {
    0: "CLIENT", 1: "EXPERT", 2: "DEALER", 3: "SL", 4: "TP", 5: "STOP_OUT",
    6: "ROLLOVER", 7: "EXTERNAL_CLIENT", 8: "VMARGIN", 9: "GATEWAY",
    10: "SIGNAL", 11: "SETTLEMENT", 12: "TRANSFER", 13: "SYNC",
    14: "EXTERNAL_SERVICE", 15: "MIGRATION", 16: "MOBILE", 17: "WEB", 18: "SPLIT",
}
CLIENT_REASONS = {0, 1, 7, 10, 16, 17}

FILETIME_EPOCH = dt.datetime(1601, 1, 1)


def to_filetime(mt_local: dt.datetime) -> int:
    """MT local (UTC+3) wall clock -> Windows FILETIME ticks (UTC-based)."""
    return int((mt_local - dt.timedelta(hours=3) - FILETIME_EPOCH).total_seconds() * 10_000_000)


def keep_login(server: str, login) -> bool:
    ls = str(login)
    return len(ls) >= MIN_LOGIN_LEN and not ls.startswith(DEMO_PREFIX[server])


def connect(db: str):
    s = get_settings()
    return pymysql.connect(
        host=s.MYSQL_HOST, user=s.MYSQL_USER, password=s.MYSQL_PASSWORD,
        database=db, port=s.MYSQL_PORT, charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor, connect_timeout=10, read_timeout=300,
    )


def sqlite_rows(sql: str, params=()):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(sql, params)]
    finally:
        conn.close()


def secs(t) -> int:
    return t.hour * 3600 + t.minute * 60 + t.second


def log_secs(event_time_mt: str) -> int:
    h, m, rest = event_time_mt.split(":")
    return int(h) * 3600 + int(m) * 60 + int(rest.split(".")[0])


def available_dates() -> list[str]:
    return [r["trade_date"] for r in sqlite_rows(
        "SELECT DISTINCT trade_date FROM last_trade_ip ORDER BY trade_date DESC")]


# ---------------------------------------------------------------------------
# verify — precision
# ---------------------------------------------------------------------------

def cmd_verify(dates: list[str]) -> int:
    grand = Counter()
    for trade_date in dates:
        rows = sqlite_rows(
            "SELECT * FROM last_trade_ip WHERE trade_date = ? AND order_ref IS NOT NULL",
            (trade_date,),
        )
        mt4_rows, mt5_rows = {}, {}
        for r in rows:
            ref = int(r["order_ref"].lstrip("#"))
            if r["server_name"] == "MT5":
                mt5_rows[(int(r["account_id"]), ref)] = r
            else:
                mt4_rows[f"{MT4_SID[r['server_name']]}-{ref}"] = r

        verdict, bad = Counter(), []

        found4 = {}
        if mt4_rows:
            keys = list(mt4_rows)
            with connect("fxbackoffice") as conn, conn.cursor() as cur:
                for i in range(0, len(keys), 500):
                    chunk = keys[i:i + 500]
                    ph = ",".join(["%s"] * len(chunk))
                    cur.execute(
                        f"SELECT ticketSid, LOGIN, CLOSE_TIME FROM mt4_trades "
                        f"WHERE ticketSid IN ({ph})", chunk)
                    for t in cur.fetchall():
                        found4[t["ticketSid"]] = t
        for k, r in mt4_rows.items():
            t = found4.get(k)
            if t is None:
                verdict["missing_in_crm"] += 1
                bad.append(("missing", k, r["account_id"]))
            elif str(t["LOGIN"]) != str(r["account_id"]):
                verdict["login_mismatch"] += 1
                bad.append(("login", k, r["account_id"], t["LOGIN"]))
            elif t["CLOSE_TIME"] is None or t["CLOSE_TIME"].year <= 1970:
                verdict["still_open_in_crm"] += 1
                bad.append(("open", k, r["account_id"]))
            elif t["CLOSE_TIME"].strftime("%Y%m%d") != trade_date:
                verdict["close_date_mismatch"] += 1
                bad.append(("date", k, trade_date, str(t["CLOSE_TIME"])))
            elif abs(secs(t["CLOSE_TIME"]) - log_secs(r["event_time_mt"])) <= 2:
                verdict["exact_match"] += 1
            else:
                verdict["time_off"] += 1
                bad.append(("time", k, r["event_time_mt"], str(t["CLOSE_TIME"])))

        found5 = defaultdict(list)
        if mt5_rows:
            keys = list(mt5_rows)
            with connect("mt5_live") as conn, conn.cursor() as cur:
                for i in range(0, len(keys), 300):
                    chunk = keys[i:i + 300]
                    ph = ",".join(["(%s,%s)"] * len(chunk))
                    flat = [x for pair in chunk for x in pair]
                    cur.execute(
                        f"SELECT Login, PositionID, Entry, Time FROM mt5_deals "
                        f"WHERE (Login, PositionID) IN ({ph}) AND Entry IN (1,2,3)", flat)
                    for d in cur.fetchall():
                        found5[(int(d["Login"]), int(d["PositionID"]))].append(d)
        for k, r in mt5_rows.items():
            deals = found5.get(k)
            if not deals:
                verdict["missing_in_crm"] += 1
                bad.append(("missing5", k, r["event_time_mt"]))
                continue
            same_day = [d for d in deals if d["Time"].strftime("%Y%m%d") == trade_date]
            if not same_day:
                verdict["close_date_mismatch"] += 1
                bad.append(("date5", k, trade_date, [str(d["Time"]) for d in deals][:3]))
                continue
            delta = min(abs(secs(d["Time"]) - log_secs(r["event_time_mt"])) for d in same_day)
            if delta <= 2:
                verdict["exact_match"] += 1
            elif delta <= 60:
                verdict["match_within_60s"] += 1
            else:
                verdict["time_off"] += 1
                bad.append(("time5", k, r["event_time_mt"], [str(d["Time"]) for d in same_day][:3]))

        total = sum(verdict.values())
        ok = verdict["exact_match"]
        print(f"{trade_date}  {total:5d} rows  exact {ok:5d} ({ok/total*100:6.2f}%)"
              f"  other {total-ok}")
        for x in bad[:5]:
            print("      ", x)
        grand.update(verdict)

    total = sum(grand.values())
    print(f"\n=== total {total} rows ===")
    for k, v in grand.most_common():
        print(f"  {k:24s} {v:6d}  ({v/total*100:6.2f}%)")
    return 0


# ---------------------------------------------------------------------------
# recall
# ---------------------------------------------------------------------------

def cmd_recall(trade_date: str) -> int:
    day = dt.datetime.strptime(trade_date, "%Y%m%d")
    ft_start, ft_end = to_filetime(day), to_filetime(day + dt.timedelta(days=1))

    ours = defaultdict(set)
    for r in sqlite_rows(
        "SELECT server_name, account_id FROM last_trade_ip WHERE trade_date = ?", (trade_date,)
    ):
        ours[r["server_name"]].add(int(r["account_id"]))

    mt5_closes = defaultdict(list)
    with connect("mt5_live") as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT Login, PositionID, Reason, Time FROM mt5_deals "
            "WHERE Timestamp >= %s AND Timestamp < %s AND Entry IN (1,2,3)",
            (ft_start, ft_end),
        )
        for d in cur.fetchall():
            if keep_login("MT5", d["Login"]):
                mt5_closes[int(d["Login"])].append(d)

    mt4_closes = defaultdict(set)
    with connect("fxbackoffice") as conn, conn.cursor() as cur:
        for server, sid in MT4_SID.items():
            cur.execute(
                "SELECT DISTINCT LOGIN FROM mt4_trades "
                "WHERE sid = %s AND closeDate = %s AND CMD IN (0,1)",
                (sid, day.strftime("%Y-%m-%d")),
            )
            for t in cur.fetchall():
                if keep_login(server, t["LOGIN"]):
                    mt4_closes[server].add(int(t["LOGIN"]))

    print(f"=== recall {trade_date} (analyzer demo/manager filter applied) ===\n")
    crm5, rec5 = set(mt5_closes), ours["MT5"]
    miss5 = crm5 - rec5
    print(f"MT5        CRM {len(crm5):5d}   ours {len(rec5):5d}   "
          f"extra {len(rec5 - crm5):3d}   missed {len(miss5):4d}")
    bucket = Counter()
    suspects = []
    for acc in miss5:
        reasons = {d["Reason"] for d in mt5_closes[acc]}
        if reasons & CLIENT_REASONS:
            bucket["client-initiated close but no IP logged by MT5"] += 1
            suspects.append((acc, sorted(REASON.get(r, r) for r in reasons)))
        else:
            bucket["server-side: " + "/".join(sorted(REASON.get(r, str(r)) for r in reasons))] += 1
    for k, v in bucket.most_common():
        print(f"    {v:5d}  {k}")
    if suspects:
        print(f"\n  no-IP client closes (see §3.4.1 known gap 2), up to 10:")
        for x in suspects[:10]:
            print("   ", x)

    for server in ("MT4", "MT4_Live2"):
        crm4, rec4 = mt4_closes[server], ours[server]
        miss4 = crm4 - rec4
        print(f"\n{server:10s} CRM {len(crm4):5d}   ours {len(rec4):5d}   "
              f"extra {len(rec4 - crm4):3d}   missed {len(miss4):4d}"
              f"   (no Reason column; misses are SL/TP/stop-out — verify via journal)")
    return 0


# ---------------------------------------------------------------------------
# report — what we actually captured, per server per day
# ---------------------------------------------------------------------------

def cmd_report(days: int) -> int:
    dates = available_dates()[:days]
    if not dates:
        print("no data in last_trade_ip")
        return 1
    rows = sqlite_rows(
        "SELECT trade_date, server_name, COUNT(*) n, COUNT(DISTINCT ip_address) ips "
        "FROM last_trade_ip WHERE trade_date >= ? GROUP BY trade_date, server_name",
        (min(dates),),
    )
    grid = defaultdict(dict)
    for r in rows:
        grid[r["trade_date"]][r["server_name"]] = (r["n"], r["ips"])

    print("客户主动 close 且有 IP 的账户数（每账户当日最后一笔）\n")
    print(f"{'MT 日':<10} {'MT4':>14} {'MT5':>14} {'MT4_Live2':>14} {'合计':>8}")
    print("-" * 64)
    tot = Counter()
    for d in sorted(dates, reverse=True):
        cells, day_total = [], 0
        for s in SERVERS:
            n, ips = grid[d].get(s, (0, 0))
            cells.append(f"{n:5d} / {ips:4d} IP" if n else f"{'—':>14}")
            day_total += n
            tot[s] += n
        print(f"{d:<10} " + " ".join(f"{c:>14}" for c in cells) + f" {day_total:8d}")
    print("-" * 64)
    print(f"{'合计':<10} " + " ".join(f"{tot[s]:>14d}" for s in SERVERS)
          + f" {sum(tot.values()):8d}")
    uniq = sqlite_rows(
        "SELECT COUNT(DISTINCT account_id) a, COUNT(DISTINCT ip_address) i "
        "FROM last_trade_ip WHERE trade_date >= ?", (min(dates),))[0]
    print(f"\n不重复账户 {uniq['a']} · 不重复 IP {uniq['i']}")
    print("\n口径：CRM 交叉验证精确率 99.91%（§3.4.1）。服务端平仓（SL/TP/爆仓）无客户 IP，")
    print("      按设计不入表；KCM\\5LS_* 组约一半客户平仓 MT5 未记 IP，属已知盲区。")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    pv = sub.add_parser("verify", help="precision: every recorded row vs the CRM")
    pv.add_argument("dates", nargs="*", help="YYYYMMDD (default: every date in the table)")
    pr = sub.add_parser("recall", help="recall: every CRM close vs what we recorded")
    pr.add_argument("date", nargs="?", help="YYYYMMDD (default: newest)")
    prep = sub.add_parser("report", help="what we captured, per server per day")
    prep.add_argument("--days", type=int, default=7)
    a = p.parse_args()

    if a.cmd == "verify":
        return cmd_verify(a.dates or available_dates())
    if a.cmd == "recall":
        return cmd_recall(a.date or available_dates()[0])
    return cmd_report(a.days)


if __name__ == "__main__":
    raise SystemExit(main())
