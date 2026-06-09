"""One-off manual test for the OPT-0032 gap-trade CRM tag pipeline.

Runs the REAL pipeline against a single MT day window (default: today):
detect rule-81 gap-profit clients -> tag them in the CRM -> email a digest.

SAFETY
------
- Default is DRY: detect + print only, NO CRM write, NO email.
- Pass ``--live`` to actually write tags + send the digest. This freezes
  real clients' withdrawals — only run when you mean it.
- Recipient override: set ``CRM_RISK_MAIL_TO`` in the environment to send the
  digest somewhere other than the configured 3-recipient list (e.g. just
  yourself for a test). The script does NOT touch backend/.env, so the
  scheduled 05:55/07:20 runs keep emailing all 3 recipients.
- Window defaults to the current MT day 00:00-02:00; override with
  ``--date YYYY-MM-DD`` (interpreted in MT).

Usage (inside the prod container):
    python3 /tmp/crm_tag_manual_test.py            # dry preview
    python3 /tmp/crm_tag_manual_test.py --live      # real write + email
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone

from app.core.config import get_settings
from app.core.risk_monitor_db import load_gap_trade_config
from app.services.rule_gap_trade_gap_service import (
    detect_gap_trade_gap_profit,
    _get_connection,
)
from app.services.gap_trade_crm_tag_service import process_gap_trade_crm_tags
from app.services.crm_risk_tag_client import CID_TAG_MAP
from app.schemas.risk_monitor import GapTradeConfig


def _mt_now() -> datetime:
    return (datetime.now(timezone.utc) + timedelta(hours=3)).replace(
        tzinfo=None, microsecond=0
    )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true",
                    help="actually write tags + send email (default: dry preview)")
    ap.add_argument("--date", help="MT day YYYY-MM-DD (default: today MT)")
    args = ap.parse_args()

    settings = get_settings()
    cfg = GapTradeConfig(**load_gap_trade_config())

    if args.date:
        wday = datetime.strptime(args.date, "%Y-%m-%d")
    else:
        wday = _mt_now().replace(hour=0, minute=0, second=0, microsecond=0)
    start_mt = wday.replace(hour=cfg.window_start_hour_mt)
    end_mt = wday + timedelta(hours=cfg.window_end_hour_mt)

    print(f"window MT      : {start_mt} ~ {end_mt}")
    print(f"recipients     : {settings.CRM_RISK_MAIL_TO}")
    print(f"mode           : {'LIVE (write + email)' if args.live else 'DRY (preview only)'}")

    result = detect_gap_trade_gap_profit(
        settings,
        start_mt=start_mt,
        end_mt=end_mt,
        sid_list=list(cfg.sid_list),
        profit_ratio_min=cfg.gap_profit.profit_ratio_min,
        min_profit_usd=cfg.gap_profit.min_profit_usd,
        min_net_deposit_hist=cfg.gap_profit.min_net_deposit_hist,
        strict_deposit=True,
    )
    alerts = result["alerts"]
    print(f"\ndetected {len(alerts)} client(s):")
    conn = _get_connection(settings)
    try:
        for a in alerts:
            uid = a["client_userid"]
            with conn.cursor() as c:
                c.execute(
                    "SELECT CONCAT_WS(' ', firstName, lastName) AS name, email, cid "
                    "FROM fxbackoffice.users WHERE id=%s",
                    (uid,),
                )
                row = c.fetchone() or {}
            cid = row.get("cid")
            tag = CID_TAG_MAP.get(cid, "<SKIP: cid not in {0,1}>")
            print(f"  userid={uid} cid={cid} profit=${a.get('total_profit_usd')} "
                  f"name={row.get('name')!r} -> tag {tag!r}")
    finally:
        conn.close()

    if not args.live:
        print("\nDRY run — nothing written. Re-run with --live to write + email.")
        return

    print("\nrunning LIVE pipeline (write + email)...")
    process_gap_trade_crm_tags(
        settings,
        alerts=alerts,
        window_date=start_mt.date().isoformat(),
        scan_label="MANUAL TEST",
        crm_tag_config={"enabled": True, "write_enabled": True,
                        "max_tags_per_scan": 10},
        heartbeat=True,
    )
    print("done — check the audit table (gap_trade_crm_tag_log) and the inbox.")


if __name__ == "__main__":
    main()
