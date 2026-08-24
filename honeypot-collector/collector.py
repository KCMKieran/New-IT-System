"""
Honeypot Collector Service
==========================

A standalone, deliberately ISOLATED FastAPI service that acts as the central
sink for intrusion signals ("honeytoken trips"). Decoy endpoints planted in the
company's real systems (an analysis site, an App, a payment site) report here,
and this service does three things and ONLY three things:

    1. receive a signal,
    2. dedup it (collapse bursts from the same source into one alert),
    3. email an alert to a human.

The isolation is the whole point. This process holds NO database credentials and
NO internal-network access. If an attacker pokes it, there is nothing here to
pivot into. The only outbound thing it needs is SMTP, to send the alert email.

Two intake patterns are supported:

  Pattern B — embedded decoy webhook (e.g. the analysis site):
      the real system already knows how to detect a trip inside its own backend,
      so it just POSTs the captured context to  POST /collect  with a shared
      secret header. This collector authenticates the webhook, dedups, and mails.

  Pattern A — hosted decoy (e.g. the App and the payment site):
      those systems cannot easily change their own backend, so instead they embed
      a distinct FAKE api-key plus a URL pointing straight at a decoy hosted HERE:
          GET/POST /api/v1/client/data      (fake "client data export")
          GET/POST /api/v1/usdt/check        (fake "USDT deposit check")
      No legitimate flow ever calls these with a wrong id, so any such request is
      an intrusion signal. They use the SAME id-gate as the reference demo: a
      request carrying a known-safe id is our own camouflage traffic (fake 200,
      no alert); anything else trips (dedup + email) and gets a realistic fake 401
      so the probe cannot tell it hit a trap.

Design rules honoured throughout:
  * A decoy must NEVER return 500 — every handler is wrapped defensively.
  * Email is always sent in a BackgroundTask so the HTTP response never blocks.
  * Swagger is OFF (docs_url=None): a real collector must not document itself.
  * No imports from app.* — this module is fully self-contained and portable, so
    it can be shipped as its own image to Azure with nothing internal attached.

Run:
    uvicorn collector:app --host 0.0.0.0 --port 8010
"""

from __future__ import annotations

import html
import json
import logging
import os
import smtplib
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr, formatdate

import hmac

from fastapi import BackgroundTasks, FastAPI, Request
from fastapi.responses import JSONResponse

# --- Configuration (all from environment; no secrets baked into the image) ---


def _env(name: str, default: str = "") -> str:
    """Read an env var, trimming whitespace. Empty string means 'unset'."""
    return os.environ.get(name, default).strip()


# The webhook shared secret. Decoys embedded in real systems must present this in
# the X-Honeypot-Secret header. A wrong/missing secret is rejected with 401 and
# sends NO email, so random internet noise cannot spam the alert inbox.
SHARED_SECRET = _env("HONEYPOT_SHARED_SECRET")

# Alert recipient. Defaults to Kieran; override per deployment.
# Recipient comes from the environment (.env), not hardcoded here.
ALERT_TO = _env("HONEYPOT_ALERT_TO") or ""

# The "safe" ids for the hosted (Pattern A) decoys. A request carrying an id in
# this set is treated as our own camouflage traffic: it does NOT alert and gets a
# plausible-looking 200 back, so the endpoint appears to be a live, legitimately
# used API. ANY other id — or no id at all — is someone who does not know the
# secret, and trips the alert. Comma-separated env, default "136017".
SAFE_IDS: frozenset[str] = frozenset(
    part.strip() for part in (_env("HONEYPOT_SAFE_IDS") or "136017").split(",") if part.strip()
)

# Per (source, token, ip) cooldown: collapse a burst of probes from the same
# source into a single alert instead of one email per request. Same lesson as
# auth_events flooding — a scanner hitting a decoy 500 times must not send 500
# emails. Configurable via COOLDOWN_MINUTES, default 5.
try:
    _cooldown_minutes = int(_env("COOLDOWN_MINUTES") or "5")
except ValueError:
    _cooldown_minutes = 5
COOLDOWN = timedelta(minutes=max(0, _cooldown_minutes))

# SMTP settings — the SAME env names the main application uses, so the same
# mailbox can be reused. This is the ONLY external dependency of this service.
SMTP_SERVER = _env("SMTP_SERVER") or "smtp.office365.com"
try:
    SMTP_PORT = int(_env("SMTP_PORT") or "587")
except ValueError:
    SMTP_PORT = 587
SMTP_USERNAME = _env("SMTP_USERNAME")
SMTP_PASSWORD = _env("SMTP_PASSWORD")

# MT server clock is UTC+3, the office/reporting clock is Asia/Hong_Kong (UTC+8).
MT_TZ = timezone(timedelta(hours=3))
HK_TZ = timezone(timedelta(hours=8))

logger = logging.getLogger("honeypot.collector")
logging.basicConfig(level=logging.INFO)

# Swagger OFF: a real decoy/collector must not advertise or document itself.
app = FastAPI(title="Honeypot Collector", docs_url=None, redoc_url=None, openapi_url=None)

# In-memory last-fired clock, keyed by (source, token, ip). A restart resets it —
# fine for a collector: a fresh process erring on the side of "send the alert" is
# the safe direction.
_last_fired: dict[tuple[str, str, str], datetime] = {}


# --- Small helpers ----------------------------------------------------------


def _client_ip(request: Request) -> str:
    """First hop of X-Forwarded-For if present, else the socket peer."""
    xff = request.headers.get("X-Forwarded-For", "")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _secret_ok(request: Request) -> bool:
    """Constant-time compare of the webhook secret. Missing secret -> False.

    If the server itself has no secret configured we fail closed (reject), so a
    misconfigured deployment cannot silently accept anonymous webhooks.
    """
    if not SHARED_SECRET:
        logger.error("HONEYPOT_NO_SHARED_SECRET configured; rejecting webhook")
        return False
    supplied = request.headers.get("X-Honeypot-Secret", "")
    return hmac.compare_digest(supplied, SHARED_SECRET)


def _should_fire(source: str, token: str, ip: str, now: datetime) -> bool:
    """Dedup gate. Same (source, token, ip) within COOLDOWN -> suppress email."""
    key = (source, token, ip)
    last = _last_fired.get(key)
    if last is not None and now - last < COOLDOWN:
        return False
    _last_fired[key] = now
    return True


# --- Email ------------------------------------------------------------------


def _build_email(token: str, source: str, ctx: dict) -> str:
    """Plain, English, no-emoji alert body. Bilingual title line, dual MT/HK
    time, attacker IP called out, all captured context in an aligned table."""
    rows = "".join(
        f"<tr><td style='padding:2px 14px 2px 0;color:#555;white-space:nowrap'>{html.escape(k)}</td>"
        f"<td style='padding:2px 0;font-family:monospace;word-break:break-all'>{html.escape(str(v))}</td></tr>"
        for k, v in ctx.items()
    )
    return f"""\
<div style="font-family:-apple-system,Segoe UI,Roboto,sans-serif;font-size:14px;color:#222;line-height:1.5">
  <p style="font-size:16px;font-weight:600;margin:0 0 12px">
    诱饵API Honeypot API — 被触发 Tripped
  </p>
  <table style="border-collapse:collapse;font-size:13px">
    <tr><td style="padding:2px 14px 2px 0;color:#555">Honeytoken</td>
        <td style="padding:2px 0;font-family:monospace;font-weight:600">{html.escape(token)}</td></tr>
    <tr><td style="padding:2px 14px 2px 0;color:#555">Source system</td>
        <td style="padding:2px 0;font-family:monospace;font-weight:600">{html.escape(source)}</td></tr>
    {rows}
  </table>
  <p style="margin:14px 0 0;color:#888;font-size:12px">
    This is an automated alert from the Honeypot Collector. No legitimate flow
    trips this decoy; treat any occurrence as a genuine intrusion signal, and use
    the source system above to identify WHICH system was breached.
  </p>
</div>"""


def _send_email(subject: str, html_body: str, to: str) -> None:
    """Self-contained smtplib sender. Raises on failure (caller logs + swallows).

    Deliberately does NOT import app.services — this service ships alone.
    """
    if not (SMTP_USERNAME and SMTP_PASSWORD):
        # Never crash: log loudly and return so the request path stays 200.
        raise RuntimeError("SMTP credentials missing (SMTP_USERNAME/SMTP_PASSWORD)")

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = formataddr(("Honeypot Collector", SMTP_USERNAME))
    msg["To"] = to
    msg["Date"] = formatdate(localtime=True)
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    with smtplib.SMTP(SMTP_SERVER, SMTP_PORT, timeout=30) as server:
        server.ehlo()
        server.starttls()
        server.ehlo()
        server.login(SMTP_USERNAME, SMTP_PASSWORD)
        server.sendmail(SMTP_USERNAME, [to], msg.as_string())


def _send_alert(token: str, source: str, ctx: dict) -> None:
    """Runs in a BackgroundTask so the HTTP response is never blocked. A decoy
    must never crash on alert failure — so every path here only logs."""
    ip = ctx.get("IP", "unknown")
    try:
        _send_email(
            subject=f"[Honeypot] Decoy tripped: {token} from {ip}",
            html_body=_build_email(token, source, ctx),
            to=ALERT_TO,
        )
        logger.warning("HONEYPOT_ALERT_SENT source=%s token=%s ip=%s", source, token, ip)
    except Exception:  # noqa: BLE001 — a decoy must never crash on alert failure
        logger.exception("HONEYPOT_ALERT_FAILED source=%s token=%s", source, token)


def _fire(source: str, token: str, ip: str, ctx: dict, background: BackgroundTasks) -> None:
    """Shared dedup + email fire path used by BOTH intake patterns."""
    now = datetime.now(timezone.utc)
    logger.warning("HONEYPOT_TRIP source=%s token=%s ip=%s", source, token, ip)
    if _should_fire(source, token, ip, now):
        background.add_task(_send_alert, token, source, ctx)
    else:
        logger.info("HONEYPOT_TRIP_SUPPRESSED (cooldown) source=%s token=%s ip=%s", source, token, ip)


# --- Pattern B: embedded-decoy webhook --------------------------------------


@app.post("/collect")
async def collect(request: Request, background: BackgroundTasks):
    """Receive a trip webhook from a decoy embedded in a real system.

    Auth: X-Honeypot-Secret header, constant-time compared against the shared
    secret. Wrong/missing secret -> 401 and NO email (randoms can't spam it).

    On a valid secret: dedup per (source, token, ip) and, if it fires, send the
    alert email in a BackgroundTask. Always return 200 {"ok": true} so a caller
    (real or hostile) learns nothing from the response, and so a malformed body
    can never turn into a 500.
    """
    if not _secret_ok(request):
        # Same shape a normal auth failure would return; no alert, no leak.
        return JSONResponse(status_code=401, content={"detail": "Unauthorized"})

    # Best-effort JSON parse. A bad body must not 500 — treat it as an empty dict.
    try:
        raw = await request.body()
        payload = json.loads(raw.decode("utf-8", "replace")) if raw else {}
        if not isinstance(payload, dict):
            payload = {"body": str(payload)[:2000]}
    except Exception:  # noqa: BLE001
        payload = {}

    def _s(key: str) -> str:
        """Coerce a payload field to a short display string, '-' when absent."""
        val = payload.get(key)
        if val is None or val == "":
            return "-"
        return str(val)[:2000]

    token = _s("token") if payload.get("token") else "unknown"
    source = _s("source") if payload.get("source") else "unknown"
    # Prefer the IP the decoy captured about the ATTACKER; fall back to the hop we
    # see (which is the reporting system, not the attacker).
    ip = _s("ip") if payload.get("ip") else _client_ip(request)

    ctx = {
        "IP": ip,
        "Method": _s("method"),
        "Path": _s("path"),
        "Query": _s("query"),
        "User-Agent": _s("user_agent"),
        "Referer": _s("referer"),
        "Supplied id": _s("supplied_id"),
        "X-API-Key seen": _s("api_key_seen"),
        "Authorization seen": _s("authorization_seen"),
        "Body": _s("body"),
        "Reported at (UTC)": _s("ts_utc"),
        "Received (MT UTC+3)": datetime.now(MT_TZ).strftime("%Y-%m-%d %H:%M:%S"),
        "Received (HK UTC+8)": datetime.now(HK_TZ).strftime("%Y-%m-%d %H:%M:%S"),
    }

    try:
        _fire(source, token, ip, ctx, background)
    except Exception:  # noqa: BLE001 — never 500 on the fire path
        logger.exception("HONEYPOT_COLLECT_FIRE_FAILED source=%s token=%s", source, token)

    return JSONResponse(status_code=200, content={"ok": True})


# --- Pattern A: hosted decoys (look like real business APIs) -----------------


def _extract_id(request: Request, body: str) -> str | None:
    """Pull the id from ?id=... first, then a JSON body {"id": ...}."""
    qid = request.query_params.get("id")
    if qid is not None:
        return qid.strip()
    try:
        parsed = json.loads(body) if body else None
        if isinstance(parsed, dict) and "id" in parsed:
            return str(parsed["id"]).strip()
    except Exception:  # noqa: BLE001
        pass
    return None


def _camouflage_payload(token: str, cid: str) -> dict:
    """A plausible-looking 200 for our own heartbeat traffic. All fake, no DB."""
    if token == "usdt.check":
        return {"id": cid, "usdt_verified": True, "status": "ok"}
    return {"id": cid, "status": "active", "record": "ok"}


async def _hosted_trip(token: str, request: Request, background: BackgroundTasks) -> JSONResponse:
    """Id-gated hosted decoy. Same behaviour as the reference demo, but routes the
    trip through the shared dedup+email path with source='hosted'."""
    ip = _client_ip(request)

    # Best-effort body capture; attackers often POST probe payloads.
    try:
        raw = await request.body()
        body = raw.decode("utf-8", "replace")[:2000]
    except Exception:  # noqa: BLE001
        body = "<unreadable>"

    # id gate: the safe id is our own camouflage traffic — no alert, fake 200.
    # Everyone else (wrong id, or no id) does not know the secret and trips.
    supplied_id = _extract_id(request, body)
    if supplied_id in SAFE_IDS:
        logger.info("HONEYPOT_CAMOUFLAGE token=%s ip=%s id=%s", token, ip, supplied_id)
        return JSONResponse(status_code=200, content=_camouflage_payload(token, supplied_id))

    now = datetime.now(timezone.utc)
    ctx = {
        "Supplied id": supplied_id if supplied_id is not None else "<none>",
        "IP": ip,
        "Method": request.method,
        "Path": request.url.path,
        "Query": str(request.url.query or "-"),
        "User-Agent": request.headers.get("User-Agent", "-"),
        "Referer": request.headers.get("Referer", "-"),
        "X-API-Key seen": request.headers.get("X-API-Key", "-"),
        "Authorization seen": request.headers.get("Authorization", "-"),
        "Body": body or "-",
        "Time (MT UTC+3)": now.astimezone(MT_TZ).strftime("%Y-%m-%d %H:%M:%S"),
        "Time (HK UTC+8)": now.astimezone(HK_TZ).strftime("%Y-%m-%d %H:%M:%S"),
    }

    try:
        _fire("hosted", token, ip, ctx, background)
    except Exception:  # noqa: BLE001 — never 500 on the fire path
        logger.exception("HONEYPOT_HOSTED_FIRE_FAILED token=%s", token)

    # Realistic fake 401 — mimic a normal auth failure so the probe learns nothing.
    return JSONResponse(status_code=401, content={"detail": "Unauthorized"})


@app.api_route("/api/v1/client/data", methods=["GET", "POST"])
async def client_data(request: Request, background: BackgroundTasks):
    return await _hosted_trip("client.data", request, background)


@app.api_route("/api/v1/usdt/check", methods=["GET", "POST"])
async def usdt_check(request: Request, background: BackgroundTasks):
    return await _hosted_trip("usdt.check", request, background)


# --- Liveness (not a decoy) --------------------------------------------------


@app.get("/healthz")
async def healthz():
    """Liveness only — used by the container/orchestrator. Not a decoy."""
    return {"status": "ok"}
