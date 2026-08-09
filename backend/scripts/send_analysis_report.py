#!/usr/bin/env python3
"""Send an [Analysis AI Report] email (CID-inline figures, CN/EN mixed body).

SSOT for the report format: docs/features/analysis-ai-report.md
Skill: .cursor/skills/analysis-ai-report/SKILL.md

Safety protocol (standing rule, enforced here rather than trusted to memory):
    default  -> TEST mode: recipient is REVIEWER, subject gets a "[TEST] " prefix
    --prod   -> PROD mode: recipient is risk@kcmtrade.com, REVIEWER is CC'd, no prefix

Never send prod without the reviewer having seen and approved a test copy first.

Usage:
    python3 backend/scripts/send_analysis_report.py \
        --html "docs/Analysis AI Report/no001-hold-duration.html" \
        --subject "[Analysis AI Report] No.001 Hold Duration Analysis - ..." \
        --image fig1="docs/Analysis AI Report/hodingtimeP1.png" \
        --image fig2="docs/Analysis AI Report/MTTime15-16.png"

    # after review:
    ... same command ... --prod
"""

from __future__ import annotations

import argparse
import os
import smtplib
import sys
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formatdate

# Recipients (SSOT — do not inline these anywhere else).
REVIEWER = "kieran.xiang@kohleservices.com"
PROD_TO = "risk@kcmtrade.com"
# The reviewer is always CC'd on prod sends, so he keeps a copy of exactly what
# the risk team received. Enforced here rather than left to whoever runs it.
PROD_CC = [REVIEWER]

# SMTP creds live in a sibling project's .env (Office365 service account).
ENV_PATH = "/opt/myproject/sales-belong-autofill/.env"

SUBJECT_PREFIX = "[Analysis AI Report]"


def load_env(path: str) -> dict:
    env = {}
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                env[key.strip()] = value.strip()
    return env


def parse_image(spec: str):
    """--image fig1=path/to.png -> ("fig1", "path/to.png")."""
    if "=" not in spec:
        raise argparse.ArgumentTypeError(
            f"--image needs cid=path form, got {spec!r}"
        )
    cid, path = spec.split("=", 1)
    return cid.strip(), path.strip()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--html", required=True, help="report body HTML file")
    ap.add_argument("--subject", required=True)
    ap.add_argument(
        "--image",
        action="append",
        default=[],
        type=parse_image,
        metavar="CID=PATH",
        help="inline figure; repeat once per figure. CID must match src=\"cid:CID\"",
    )
    ap.add_argument(
        "--prod",
        action="store_true",
        help=f"send to {PROD_TO} (CC {', '.join(PROD_CC)}) instead of the "
        "reviewer only; requires prior approval of a test copy",
    )
    ap.add_argument("--cc", default="", help="comma-separated CC list")
    args = ap.parse_args()

    if not args.subject.startswith(SUBJECT_PREFIX):
        print(
            f"refusing: subject must start with {SUBJECT_PREFIX!r}", file=sys.stderr
        )
        return 2

    html = open(args.html, encoding="utf-8").read()
    if "cid:" in html:
        declared = {cid for cid, _ in args.image}
        for cid in declared:
            if f"cid:{cid}" not in html:
                print(f"warning: --image {cid} is not referenced in the HTML")

    env = load_env(ENV_PATH)
    sender = env["SMTP_USERNAME"]

    if args.prod:
        to = [PROD_TO]
        subject = args.subject
    else:
        to = [REVIEWER]
        subject = f"[TEST] {args.subject}"

    cc = [a.strip() for a in args.cc.split(",") if a.strip()]
    if args.prod:
        # Always CC the reviewer on prod, on top of any --cc given.
        cc += [a for a in PROD_CC if a not in cc and a not in to]

    root = MIMEMultipart("related")
    root["Subject"] = subject
    root["From"] = sender
    root["To"] = ", ".join(to)
    if cc:
        root["Cc"] = ", ".join(cc)
    root["Date"] = formatdate(localtime=True)

    alt = MIMEMultipart("alternative")
    alt.attach(MIMEText(html, "html", "utf-8"))
    root.attach(alt)

    for cid, path in args.image:
        with open(path, "rb") as fh:
            img = MIMEImage(fh.read(), _subtype="png")
        img.add_header("Content-ID", f"<{cid}>")
        img.add_header(
            "Content-Disposition", "inline", filename=os.path.basename(path)
        )
        root.attach(img)

    raw = root.as_string()
    size_kb = len(raw) / 1024
    if size_kb > 8000:
        print(
            f"refusing: message is {size_kb:.0f} KB (>8 MB); shrink the PNGs",
            file=sys.stderr,
        )
        return 3

    smtp = smtplib.SMTP(env["SMTP_SERVER"], int(env["SMTP_PORT"]), timeout=60)
    smtp.starttls()
    smtp.login(sender, env["SMTP_PASSWORD"])
    smtp.sendmail(sender, to + cc, raw)
    smtp.quit()

    mode = "PROD" if args.prod else "TEST"
    print(f"SENT [{mode}] to {', '.join(to + cc)} | {size_kb:.0f} KB | {subject}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
