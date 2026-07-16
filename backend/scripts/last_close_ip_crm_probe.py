"""Read-only probe for the last-close-IP -> CRM push.

Both subcommands mutate nothing. Run these before writing any push code: the
CRM's failure mode for a mistyped custom-field key is HTTP 200 + silent no-op,
so "the field exists and is spelled exactly this way" has to be proven by a
read, not assumed.

  resolve [DATE]  Map last_trade_ip rows -> CRM client ids and report the
                  many:1 picture (one client can hold several MT accounts, so
                  several accounts' closes can compete for one client's single
                  custom field). Shows how often the "latest close wins"
                  tie-break actually fires.
  read CLIENT_ID  CRM read probe: proves the dedicated token is accepted, the
                  IP allowlist lets us in, and whether
                  custom_last_close_position_ip is present on the user object.
                  POSTs {"user": id} only -- no writable key, no mutation.

Run inside the backend container (needs app.core.config + DB + CRM reach; a
host shell behind the tunnel proxy gets 403 Client IP is not allowed):
    docker exec new-it-backend-dev sh -c 'cd /app && python scripts/last_close_ip_crm_probe.py resolve'
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional

import pymysql
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.core.config import get_settings  # noqa: E402

DB_PATH = Path(__file__).resolve().parents[1] / "data" / "login_ip.db"

FIELD_KEY = "custom_last_close_position_ip"

# last_trade_ip.server_name -> mt4_users.sid.
# NOTE the naming mismatch: the analyzer writes "MT4" (from the log filename),
# while app/core/sql_helpers.py SID_MAP keys the same server as "MT4_Live".
# SID_MAP.get("MT4") is None, so this module keeps its own map rather than
# importing one that would silently drop every MT4 row.
SERVER_SID = {"MT4": 1, "MT5": 5, "MT4_Live2": 6}


def connect_mysql(db: str):
    s = get_settings()
    return pymysql.connect(
        host=s.MYSQL_HOST, user=s.MYSQL_USER, password=s.MYSQL_PASSWORD,
        database=db, port=s.MYSQL_PORT, charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor, connect_timeout=10, read_timeout=300,
    )


def latest_trade_date(conn: sqlite3.Connection) -> Optional[str]:
    row = conn.execute("SELECT MAX(trade_date) AS d FROM last_trade_ip").fetchone()
    return row["d"] if row and row["d"] else None


def load_snapshot(conn: sqlite3.Connection, trade_date: str) -> List[Dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT trade_date, server_name, account_id, ip_address, event_time_mt, order_ref
        FROM last_trade_ip
        WHERE trade_date = ?
        """,
        (trade_date,),
    ).fetchall()
    return [dict(r) for r in rows]


def resolve_client_ids(rows: List[Dict[str, Any]]) -> Dict[str, int]:
    """Batch loginsid -> userId. Missing loginsid is simply absent from the map."""
    loginsids = set()
    for r in rows:
        sid = SERVER_SID.get(r["server_name"])
        if sid is None:
            continue
        loginsids.add(f"{sid}-{r['account_id']}")
    if not loginsids:
        return {}

    placeholders = ",".join(["%s"] * len(loginsids))
    sql = f"SELECT loginsid, userId FROM fxbackoffice.mt4_users WHERE loginsid IN ({placeholders})"
    conn = connect_mysql("fxbackoffice")
    try:
        with conn.cursor() as cur:
            cur.execute(sql, tuple(loginsids))
            return {
                r["loginsid"]: int(r["userId"])
                for r in cur.fetchall()
                if r.get("userId") is not None
            }
    finally:
        conn.close()


def pick_winners(
    rows: List[Dict[str, Any]], id_map: Dict[str, int]
) -> tuple[Dict[int, Dict[str, Any]], List[Dict[str, Any]]]:
    """Group by client, keep the latest close per client.

    All three servers stamp event_time_mt in MT local time (UTC+3), the same
    clock, so the raw HH:MM:SS.mmm string compares correctly across servers
    within one MT day -- no tz conversion needed.
    """
    by_client: Dict[int, List[Dict[str, Any]]] = defaultdict(list)
    unresolved: List[Dict[str, Any]] = []
    for r in rows:
        sid = SERVER_SID.get(r["server_name"])
        if sid is None:
            unresolved.append({**r, "reason": f"unknown server_name {r['server_name']!r}"})
            continue
        client_id = id_map.get(f"{sid}-{r['account_id']}")
        if client_id is None:
            unresolved.append({**r, "reason": "loginsid not in mt4_users"})
            continue
        by_client[client_id].append(r)

    winners: Dict[int, Dict[str, Any]] = {}
    for client_id, cand in by_client.items():
        best = max(cand, key=lambda r: r["event_time_mt"])
        winners[client_id] = {
            **best,
            "contenders": len(cand),
            "distinct_ips": len({c["ip_address"] for c in cand}),
        }
    return winners, unresolved


def cmd_resolve(args: argparse.Namespace) -> int:
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        trade_date = args.date or latest_trade_date(conn)
        if not trade_date:
            print("last_trade_ip is empty -- nothing to resolve.")
            return 1
        rows = load_snapshot(conn, trade_date)
    finally:
        conn.close()

    print(f"MT day {trade_date}: {len(rows)} account rows in last_trade_ip")
    by_server = defaultdict(int)
    for r in rows:
        by_server[r["server_name"]] += 1
    for server, n in sorted(by_server.items()):
        print(f"  {server:<12} {n:>5}")

    id_map = resolve_client_ids(rows)
    winners, unresolved = pick_winners(rows, id_map)

    print()
    print(f"resolved to {len(winners)} unique CRM clients")
    print(f"unresolved accounts: {len(unresolved)}")
    for u in unresolved[:5]:
        print(f"  {u['server_name']}/{u['account_id']}: {u['reason']}")
    if len(unresolved) > 5:
        print(f"  ... and {len(unresolved) - 5} more")

    multi = {c: w for c, w in winners.items() if w["contenders"] > 1}
    conflicting = {c: w for c, w in multi.items() if w["distinct_ips"] > 1}
    print()
    print(f"clients with >1 account closing that day: {len(multi)}")
    print(f"  ... of which the accounts disagree on IP: {len(conflicting)}")
    print("     (these are the rows where 'latest close wins' actually decides)")
    for client_id, w in list(conflicting.items())[:5]:
        print(f"  client {client_id}: winner {w['ip_address']} @ {w['event_time_mt']} "
              f"({w['server_name']}/{w['account_id']}, {w['contenders']} accounts, "
              f"{w['distinct_ips']} distinct IPs)")

    if args.sample:
        print()
        print(f"--- {args.sample} sample winners (what a live run would push) ---")
        for client_id, w in list(winners.items())[: args.sample]:
            print(f"  user={client_id:<8} {FIELD_KEY}={w['ip_address']:<16} "
                  f"(from {w['server_name']}/{w['account_id']} @ {w['event_time_mt']})")
    return 0


def cmd_read(args: argparse.Namespace) -> int:
    s = get_settings()
    if not s.CRM_LAST_CLOSE_IP_API_URL or not s.CRM_LAST_CLOSE_IP_API_TOKEN:
        print("CRM_LAST_CLOSE_IP_API_URL / CRM_LAST_CLOSE_IP_API_TOKEN not configured")
        return 1

    resp = requests.post(
        s.CRM_LAST_CLOSE_IP_API_URL,
        json={"user": int(args.client_id)},
        headers={
            "Authorization": f"Bearer {s.CRM_LAST_CLOSE_IP_API_TOKEN}",
            "Content-Type": "application/json",
        },
        timeout=(10, 30),
    )
    print(f"HTTP {resp.status_code}")
    if resp.status_code != 200:
        # 401 = bad token, 403 = IP not allowlisted, 422 = bad payload.
        print(f"body: {resp.text[:500]}")
        return 1

    try:
        data = resp.json()
    except ValueError:
        print(f"body is not JSON: {resp.text[:500]}")
        return 1
    if not isinstance(data, dict):
        print(f"unexpected response shape: {type(data).__name__}")
        return 1

    print(f"user object keys: {len(data)}")
    if args.shape:
        # Key NAMES only -- a CRM user object is full of PII, and the shape is
        # all we need to tell "field absent" from "token can't see any custom
        # fields at all" (the two have identical symptoms on one field).
        cf_probe = data.get("customFields")
        cf_desc = type(cf_probe).__name__
        if hasattr(cf_probe, "__len__"):
            cf_desc += f" len={len(cf_probe)}"
        print(f"  customFields: {cf_desc}")
        if isinstance(cf_probe, dict) and cf_probe:
            print(f"  customFields keys: {sorted(cf_probe)}")
        print(f"  top-level key names: {sorted(data)}")
    # Custom fields read back FLAT at the top level even though they must be
    # WRITTEN nested under customFields -- check both shapes.
    flat = data.get(FIELD_KEY, "<absent>")
    nested = "<no customFields object>"
    cf = data.get("customFields")
    if isinstance(cf, dict):
        nested = cf.get(FIELD_KEY, "<absent>")
    print(f"  flat   {FIELD_KEY} = {flat!r}")
    print(f"  nested customFields.{FIELD_KEY} = {nested!r}")

    custom_keys = sorted(k for k in data if k.startswith("custom_"))
    print(f"\ncustom_* keys visible on this user ({len(custom_keys)}):")
    for k in custom_keys:
        print(f"  {k}")
    if FIELD_KEY not in custom_keys and not isinstance(cf, dict):
        print(f"\n{FIELD_KEY} is NOT present. Either the CRM field was never "
              f"created, it is spelled differently, or it only materialises "
              f"once written. Confirm the exact key with the CRM admin before "
              f"any write -- a mistyped key returns 200 and writes nothing.")
    return 0


def crm_read_raw(client_id: int) -> Dict[str, Any]:
    s = get_settings()
    resp = requests.post(
        s.CRM_LAST_CLOSE_IP_API_URL,
        json={"user": int(client_id)},
        headers={
            "Authorization": f"Bearer {s.CRM_LAST_CLOSE_IP_API_TOKEN}",
            "Content-Type": "application/json",
        },
        timeout=(10, 30),
    )
    resp.raise_for_status()
    return resp.json()


def current_field_value(raw: Dict[str, Any]) -> Any:
    """Read the field, tolerating both shapes.

    Verified 2026-07-15: this CRM returns custom fields NESTED under
    customFields, and only for fields that have a value -- the top-level
    object carries no custom_* keys at all. The flat fallback stays because
    the house skill documents the opposite shape; until that's reconciled,
    read both rather than trust either.
    """
    cf = raw.get("customFields")
    if isinstance(cf, dict) and FIELD_KEY in cf:
        return cf[FIELD_KEY]
    return raw.get(FIELD_KEY, None)


def cmd_write(args: argparse.Namespace) -> int:
    """One-client write probe -- the only step a read can't prove.

    Deliberately does NOT consult LAST_CLOSE_IP_CRM_WRITE_ENABLED: that gate
    guards the scheduled job, and this probe is what you run to earn the right
    to flip it. Requires --confirm instead.
    """
    if not args.confirm:
        print("refusing to write without --confirm")
        return 1

    before_raw = crm_read_raw(args.client_id)
    before = current_field_value(before_raw)
    print(f"client {args.client_id}")
    print(f"  before: {FIELD_KEY} = {before!r}")
    print(f"  writing: {args.value!r}")

    s = get_settings()
    payload = {"user": int(args.client_id), "customFields": {FIELD_KEY: args.value}}
    resp = requests.post(
        s.CRM_LAST_CLOSE_IP_API_URL,
        json=payload,
        headers={
            "Authorization": f"Bearer {s.CRM_LAST_CLOSE_IP_API_TOKEN}",
            "Content-Type": "application/json",
        },
        timeout=(10, 30),
    )
    print(f"  HTTP {resp.status_code}")
    if resp.status_code != 200:
        print(f"  body: {resp.text[:500]}")
        return 1

    # 200 proves nothing -- a mistyped key returns 200 and writes nothing.
    after = current_field_value(crm_read_raw(args.client_id))
    print(f"  after:  {FIELD_KEY} = {after!r}")
    if after == args.value:
        print("\nVERIFIED: the value landed and reads back.")
        print(f"restore with: ... write {args.client_id} --value {before or ''!r} --confirm")
        return 0
    print("\nFAILED: 200 but the value did not land (silent no-op).")
    return 1


def cmd_fields(args: argparse.Namespace) -> int:
    """Which custom-field keys does the CRM actually carry, and is ours one?

    fxbackoffice.user_custom_fields is the k/v store behind custom_* fields.
    A key present here is proof the field exists and has been written at least
    once; a key absent is NOT proof it doesn't exist (an existing-but-never-
    written field has no rows), which is exactly the ambiguity we need the CRM
    admin to settle before the first write.
    """
    conn = connect_mysql("fxbackoffice")
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT k, COUNT(*) AS n
                FROM fxbackoffice.user_custom_fields
                GROUP BY k
                ORDER BY n DESC
                """
            )
            rows = cur.fetchall()
            print(f"distinct custom-field keys in user_custom_fields ({len(rows)}):")
            for r in rows:
                mark = "  <-- ours" if r["k"] == FIELD_KEY else ""
                print(f"  {r['k']:<40} {r['n']:>8} rows{mark}")

            if not any(r["k"] == FIELD_KEY for r in rows):
                print(f"\n{FIELD_KEY}: NO rows -- never written (field may still "
                      f"exist as a definition with no values yet).")

            # A user that definitely has custom fields, to learn the read shape.
            cur.execute(
                """
                SELECT userid, k FROM fxbackoffice.user_custom_fields
                WHERE k = %s LIMIT 1
                """,
                (args.sample_key,),
            )
            row = cur.fetchone()
    finally:
        conn.close()

    if not row:
        print(f"\nno user found carrying {args.sample_key!r} -- pass --sample-key "
              f"with a key from the list above to probe the read shape.")
        return 0

    print(f"\n--- read shape probe: user {row['userid']} carries {args.sample_key!r} ---")
    ns = argparse.Namespace(client_id=int(row["userid"]), shape=True)
    return cmd_read(ns)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("resolve", help="map last_trade_ip -> CRM client ids (read-only)")
    r.add_argument("date", nargs="?", help="YYYYMMDD (default: latest in table)")
    r.add_argument("--sample", type=int, default=0, help="print N sample winners")
    r.set_defaults(func=cmd_resolve)

    rd = sub.add_parser("read", help="CRM read probe for one client (read-only)")
    rd.add_argument("client_id", type=int)
    rd.add_argument("--shape", action="store_true",
                    help="dump response key names (no values) to diagnose read shape")
    rd.set_defaults(func=cmd_read)

    w = sub.add_parser("write", help="ONE-client write probe (MUTATES the CRM)")
    w.add_argument("client_id", type=int)
    w.add_argument("--value", required=True, help="value to write")
    w.add_argument("--confirm", action="store_true", help="required; without it, refuses")
    w.set_defaults(func=cmd_write)

    f = sub.add_parser("fields", help="list custom-field keys the CRM carries (read-only)")
    f.add_argument("--sample-key", default="custom_chinese_name",
                   help="probe the read shape via a user carrying this key")
    f.set_defaults(func=cmd_fields)

    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
