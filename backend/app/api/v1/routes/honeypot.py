"""
Honeypot decoy endpoints (security honeytoken).

Two routes dressed up as ordinary business APIs:

    GET/POST /api/v1/client/data   -> honeytoken "client.data"
    GET/POST /api/v1/usdt/check     -> honeytoken "usdt.check"

They appear in Swagger and are callable like any other endpoint, on purpose:
an attacker who scrapes the JS bundle or enumerates routes should see them and
be tempted to try them. NO legitimate code path ever calls them, so ANY hit is
an intrusion signal — one trip == one real breach, zero false positives.

WHY THESE ARE REACHABLE WITHOUT A SESSION
-----------------------------------------
The whole point is to catch an outsider who holds the public API key (it is
compiled into the bundle — never a secret) but has NO session. So these two
paths are:
  * classified INFRA in core/auth_deps.MODULE_MAP  -> the module gate abstains,
  * listed in core/auth_middleware.EXEMPT_PATHS      -> the session check is skipped,
  * listed in core/audit_missing_middleware.AUDIT_EXEMPT_ROUTES -> the camouflage
    200 on a POST does not look like an un-audited write.
They still require the public API key (api_key_middleware is NOT exempted): the
realistic attacker has it, and requiring it keeps keyless internet noise out.

INERT BY CONSTRUCTION
---------------------
This handler touches NO database and reads NO business data. Its only side
effects are: log a WARNING, and hand off an alert (to the central collector if
configured, else a direct email) on a BackgroundTask so the HTTP response is
never blocked. A decoy that could reach a real DB would be a pivot for the very
attacker it is meant to catch — so it deliberately imports nothing that can.

The id gate: a request whose id is in settings.HONEYPOT_SAFE_IDS is treated as
our own camouflage heartbeat — no alert, a plausible fake 200. Everyone else
(wrong id, or no id) does not know the secret and trips.
"""

from __future__ import annotations

import html
import json
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, BackgroundTasks, Request
from fastapi.responses import JSONResponse

from app.core.config import get_settings
from app.core.logging_config import get_logger
from app.services.email_service import send_email  # SMTP only — no DB, safe to import here

logger = get_logger(__name__)

router = APIRouter()

# Per (token, ip) cooldown: collapse a burst of probes from one source into a
# single alert. The central collector dedups too; this also protects the
# direct-email fallback path when the collector is not yet deployed.
_COOLDOWN = timedelta(minutes=5)
_last_fired: dict[tuple[str, str], datetime] = {}

_MT_TZ = timezone(timedelta(hours=3))   # MT server clock
_HK_TZ = timezone(timedelta(hours=8))   # office / reporting clock


def _client_ip(request: Request) -> str:
    xff = request.headers.get("X-Forwarded-For", "")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _extract_id(request: Request, body: str) -> str | None:
    """id from ?id=... first, then a JSON body {"id": ...}."""
    qid = request.query_params.get("id")
    if qid is not None:
        return qid.strip()
    try:
        parsed = json.loads(body) if body else None
        if isinstance(parsed, dict) and "id" in parsed:
            return str(parsed["id"]).strip()
    except Exception:  # noqa: BLE001 — malformed body is not our problem to raise on
        pass
    return None


def _camouflage_payload(token: str, cid: str) -> dict:
    """A plausible 200 for our own heartbeat traffic. All fake, no DB."""
    if token == "usdt.check":
        return {"id": cid, "usdt_verified": True, "status": "ok"}
    return {"id": cid, "status": "active", "record": "ok"}


def _should_fire(token: str, ip: str, now: datetime) -> bool:
    key = (token, ip)
    last = _last_fired.get(key)
    if last is not None and now - last < _COOLDOWN:
        return False
    _last_fired[key] = now
    return True


def _alert_email_body(token: str, ctx: dict) -> str:
    rows = "".join(
        f"<tr><td style='padding:2px 14px 2px 0;color:#555'>{html.escape(k)}</td>"
        f"<td style='padding:2px 0;font-family:monospace'>{html.escape(str(v))}</td></tr>"
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
    {rows}
  </table>
  <p style="margin:14px 0 0;color:#888;font-size:12px">
    Automated alert from the analysis backend honeypot. No legitimate flow calls
    this endpoint; treat any occurrence as suspicious.
  </p>
</div>"""


def _dispatch_alert(token: str, ctx: dict) -> None:
    """Runs in a threadpool BackgroundTask (never blocks the response).

    Prefers the central collector; falls back to a direct email so the trap is
    never silent. A decoy must never crash on an alerting failure.
    """
    settings = get_settings()
    payload = {
        "token": token,
        "source": "analysis",
        "ip": ctx.get("IP"),
        "method": ctx.get("Method"),
        "path": ctx.get("Path"),
        "query": ctx.get("Query"),
        "user_agent": ctx.get("User-Agent"),
        "referer": ctx.get("Referer"),
        "supplied_id": ctx.get("Supplied id"),
        "api_key_seen": ctx.get("X-API-Key seen"),
        "authorization_seen": ctx.get("Authorization seen"),
        "body": ctx.get("Body"),
        "ts_utc": ctx.get("Time (UTC)"),
    }

    if settings.HONEYPOT_COLLECTOR_URL:
        try:
            import httpx

            resp = httpx.post(
                settings.HONEYPOT_COLLECTOR_URL.rstrip("/") + "/collect",
                json=payload,
                headers={"X-Honeypot-Secret": settings.HONEYPOT_SHARED_SECRET},
                timeout=5.0,
            )
            if resp.status_code < 400:
                logger.warning("HONEYPOT_FORWARDED token=%s ip=%s", token, ctx.get("IP"))
                return
            logger.error(
                "HONEYPOT_FORWARD_REJECTED token=%s status=%s — falling back to email",
                token, resp.status_code,
            )
        except Exception:  # noqa: BLE001
            logger.exception("HONEYPOT_FORWARD_FAILED token=%s — falling back to email", token)

    # Fallback (or no collector configured yet): email directly.
    try:
        send_email(
            subject=f"[Honeypot] Decoy tripped: {token} from {ctx.get('IP')}",
            body=_alert_email_body(token, ctx),
            to=settings.HONEYPOT_ALERT_TO,
        )
        logger.warning("HONEYPOT_ALERT_SENT token=%s ip=%s", token, ctx.get("IP"))
    except Exception:  # noqa: BLE001 — a decoy must never crash on alert failure
        logger.exception("HONEYPOT_ALERT_FAILED token=%s", token)


async def _trip(token: str, request: Request, background: BackgroundTasks) -> JSONResponse:
    now = datetime.now(timezone.utc)
    ip = _client_ip(request)

    try:
        raw = await request.body()
        body = raw.decode("utf-8", "replace")[:2000]
    except Exception:  # noqa: BLE001
        body = "<unreadable>"

    # id gate: our own secret id is camouflage traffic — no alert, fake 200.
    supplied_id = _extract_id(request, body)
    if supplied_id in get_settings().HONEYPOT_SAFE_IDS:
        logger.info("HONEYPOT_CAMOUFLAGE token=%s ip=%s id=%s", token, ip, supplied_id)
        return JSONResponse(status_code=200, content=_camouflage_payload(token, supplied_id))

    ctx = {
        "IP": ip,
        "Method": request.method,
        "Path": request.url.path,
        "Query": str(request.url.query or "-"),
        "User-Agent": request.headers.get("User-Agent", "-"),
        "Referer": request.headers.get("Referer", "-"),
        "Supplied id": supplied_id if supplied_id is not None else "<none>",
        "X-API-Key seen": request.headers.get("X-API-Key", "-"),
        "Authorization seen": request.headers.get("Authorization", "-"),
        "Body": body or "-",
        "Time (UTC)": now.isoformat(),
        "Time (MT UTC+3)": now.astimezone(_MT_TZ).strftime("%Y-%m-%d %H:%M:%S"),
        "Time (HK UTC+8)": now.astimezone(_HK_TZ).strftime("%Y-%m-%d %H:%M:%S"),
    }

    logger.warning("HONEYPOT_TRIP token=%s ip=%s method=%s ua=%r",
                   token, ip, request.method, ctx["User-Agent"])

    if _should_fire(token, ip, now):
        background.add_task(_dispatch_alert, token, ctx)
    else:
        logger.info("HONEYPOT_TRIP_SUPPRESSED (cooldown) token=%s ip=%s", token, ip)

    # Realistic fake 401 — mimic a normal auth failure so the probe learns nothing.
    return JSONResponse(status_code=401, content={"detail": "Unauthorized"})


@router.api_route("/client/data", methods=["GET", "POST"], include_in_schema=True)
async def client_data(request: Request, background: BackgroundTasks):
    return await _trip("client.data", request, background)


@router.api_route("/usdt/check", methods=["GET", "POST"], include_in_schema=True)
async def usdt_check(request: Request, background: BackgroundTasks):
    return await _trip("usdt.check", request, background)
