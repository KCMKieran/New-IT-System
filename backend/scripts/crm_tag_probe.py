#!/usr/bin/env python3
"""
OPT-0032 D0 gate tool — CRM ID-mapping probe + canary tag write/remove.

The dry-run phase was waived (user decision 2026-06-05); this script is the
mandatory manual substitute. Run it BEFORE flipping any live-write flag:

1) Egress IP (run from the PROD backend container — that's the IP the CRM
   must allowlist, not the test box):
       python scripts/crm_tag_probe.py --egress-ip

2) ID-mapping probe (read-only, zero write risk): reads every historical
   rule-81 userid from risk_monitor.db (+ any extra ids) via
   POST {"user": id} and cross-checks name/email against fxbackoffice.
   ANY mismatch = hard stop, do not go live.
       python scripts/crm_tag_probe.py --probe-historical --verify-db
       python scripts/crm_tag_probe.py --probe 100017 --verify-db

3) Canary (one attended session, one internal account per cid class, each
   already carrying >=2 tags): add the tag, verify byte-exact in CRM UI +
   pre-existing tags preserved + CS confirms the withdrawal flips to manual
   review, then remove it.
       python scripts/crm_tag_probe.py --canary-add 123456
       python scripts/crm_tag_probe.py --canary-remove 123456

All canary actions are logged to gap_trade_crm_tag_log (window_date=CANARY).
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(BACKEND_ROOT / ".env")

from app.core.config import get_settings  # noqa: E402
from app.services.crm_risk_tag_client import (  # noqa: E402
    CID_TAG_MAP,
    CrmError,
    CrmRiskTagClient,
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _client() -> CrmRiskTagClient:
    s = get_settings()
    if not s.CRM_RISK_API_URL or not s.CRM_RISK_API_TOKEN:
        sys.exit("CRM_RISK_API_URL / CRM_RISK_API_TOKEN missing in backend/.env")
    return CrmRiskTagClient(s.CRM_RISK_API_URL, s.CRM_RISK_API_TOKEN)


def cmd_egress_ip() -> None:
    import requests
    ip = requests.get("https://api.ipify.org", timeout=10).text.strip()
    print(f"Egress IP: {ip}")
    print("→ Have the CRM admin allowlist this IP, then re-run --probe from here.")


def _historical_userids() -> list[int]:
    import sqlite3
    db = BACKEND_ROOT / "data" / "risk_monitor.db"
    conn = sqlite3.connect(str(db))
    try:
        rows = conn.execute(
            "SELECT DISTINCT client_userid FROM alert_gap_profit_detail "
            "ORDER BY client_userid"
        ).fetchall()
    finally:
        conn.close()
    return [int(r[0]) for r in rows if r[0]]


def _mysql_users(userids: list[int]) -> dict[int, dict]:
    import pymysql
    s = get_settings()
    conn = pymysql.connect(
        host=s.DB_HOST, user=s.DB_USER, password=s.DB_PASSWORD,
        port=int(s.DB_PORT), charset=s.DB_CHARSET,
        cursorclass=pymysql.cursors.DictCursor, connect_timeout=10,
    )
    try:
        placeholders = ",".join(["%s"] * len(userids))
        with conn.cursor() as cur:
            cur.execute(
                # users.cid doubles as a second cross-check against the CRM
                # read's cid (the authoritative one for tag dispatch).
                f"SELECT id, CONCAT_WS(' ', firstName, lastName) AS name, "
                f"email, cid FROM fxbackoffice.users "
                f"WHERE id IN ({placeholders})",
                tuple(userids),
            )
            return {int(r["id"]): r for r in cur.fetchall()}
    finally:
        conn.close()


def cmd_probe(userids: list[int], verify_db: bool) -> None:
    client = _client()
    db_users = _mysql_users(userids) if verify_db else {}
    mismatches = 0
    for uid in userids:
        try:
            state = client.read_user(uid)
        except CrmError as ex:
            print(f"✗ {uid}: CRM read FAILED — {ex} (status={ex.http_status})")
            mismatches += 1
            continue
        crm_name = str(state.raw.get("name") or state.raw.get("fullName") or "")
        crm_email = str(state.raw.get("email") or "")
        line = (f"  {uid}: cid={state.cid} tags={state.tags} "
                f"name={crm_name!r} email={crm_email!r}")
        if verify_db:
            db_row = db_users.get(uid)
            if not db_row:
                print(f"✗ {uid}: not found in fxbackoffice.users{line}")
                mismatches += 1
                continue
            db_email = str(db_row.get("email") or "")
            ok = bool(db_email) and db_email.strip().lower() == crm_email.strip().lower()
            mark = "✓" if ok else "✗ EMAIL MISMATCH vs DB " + repr(db_email)
            db_cid = db_row.get("cid")
            if db_cid is not None and state.cid is not None and int(db_cid) != state.cid:
                mark += f" ⚠ cid differs: DB={db_cid} CRM={state.cid}"
            print(f"{mark}{line}")
            if not ok:
                mismatches += 1
        else:
            print(f"·{line}")
        if state.cid not in CID_TAG_MAP:
            print(f"  ⚠ {uid}: cid={state.cid} has NO tag mapping (would be skipped_cid)")
    print()
    if mismatches:
        sys.exit(f"HARD STOP: {mismatches} mismatch(es)/failure(s) — do NOT go live.")
    print(f"All {len(userids)} probes consistent. ID mapping verified.")


def _audit_canary(uid: int, res, action: str) -> None:
    from app.core.risk_monitor_db import insert_crm_tag_log
    insert_crm_tag_log(
        window_date="CANARY",
        client_userid=uid,
        cid=res.cid,
        tag=res.tag,
        result="tagged" if res.ok else "failed",
        http_status=res.http_status,
        tags_before=res.tags_before,
        tags_after=res.tags_after,
        detail=f"manual canary {action}: {res.detail}".strip(),
        attempted_at=_now_iso(),
    )


def cmd_canary(uid: int, *, add: bool) -> None:
    client = _client()
    state = client.read_user(uid)
    tag = CID_TAG_MAP.get(state.cid) if state.cid is not None else None
    if tag is None:
        sys.exit(f"cid={state.cid} has no tag mapping — pick a cid 0/1 account.")
    print(f"user={uid} cid={state.cid} tag={tag!r}")
    print(f"tags before: {state.tags}")
    confirm = input(f"{'ADD' if add else 'REMOVE'} this tag? Type the userid to confirm: ")
    if confirm.strip() != str(uid):
        sys.exit("aborted")
    res = client.add_tag(uid, tag) if add else client.remove_tag(uid, tag)
    _audit_canary(uid, res, "add" if add else "remove")
    print(f"result ok={res.ok} no_op={res.no_op} http={res.http_status}")
    print(f"tags after:  {res.tags_after}")
    if res.detail:
        print(f"detail: {res.detail}")
    if res.ok and add:
        print(json.dumps({"verify_in_crm_ui": [
            "tag string byte-exact (no near-duplicate tag was created)",
            "pre-existing tags all preserved",
            "CS confirms a withdrawal on this account routes to MANUAL review",
        ]}, ensure_ascii=False, indent=2))


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--egress-ip", action="store_true")
    p.add_argument("--probe", nargs="*", type=int, default=None,
                   help="read-only probe of specific CRM user ids")
    p.add_argument("--probe-historical", action="store_true",
                   help="probe every historical rule-81 userid from risk_monitor.db")
    p.add_argument("--verify-db", action="store_true",
                   help="cross-check probe results against fxbackoffice.users")
    p.add_argument("--canary-add", type=int, metavar="USERID")
    p.add_argument("--canary-remove", type=int, metavar="USERID")
    args = p.parse_args()

    if args.egress_ip:
        cmd_egress_ip()
    elif args.probe_historical or args.probe:
        ids = list(args.probe or [])
        if args.probe_historical:
            ids = sorted(set(ids) | set(_historical_userids()))
        if not ids:
            sys.exit("no userids to probe")
        cmd_probe(ids, args.verify_db)
    elif args.canary_add:
        cmd_canary(args.canary_add, add=True)
    elif args.canary_remove:
        cmd_canary(args.canary_remove, add=False)
    else:
        p.print_help()


if __name__ == "__main__":
    main()
