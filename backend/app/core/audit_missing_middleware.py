"""AUDIT_MISSING — the fallback alarm for write endpoints nobody wired up.

Design: docs/architecture/audit-log-design.md §D4.2 (last section) and §D6.4.

This is deliberately NOT an audit writer. A middleware cannot read the value a
field held *before* the business write, and "what did it change from" is the
part of an audit row that carries the meaning; a middleware-written trail would
also record the 74% of non-GET traffic that D2 rules out (autosaves, queries
shaped as POSTs, failed requests). So the middleware's whole job is the opposite
one: notice that a successful write produced NO audit row and say so in the
application log, where check_audit_health.sh greps for it.

    AUDIT_MISSING is a stable grep token — the third of the three the health
    check depends on, next to AUDIT_WRITE_FAILED and AUDIT_ANONYMOUS. Renaming
    it silently disarms that check.

Why a register of exempt paths rather than a heuristic: the list below IS the
record of "we looked at this endpoint and decided it needs no audit row". A new
route is therefore noisy by default, which is the intended failure direction —
the alternative (pattern-matching on `/query`-ish names) makes the next
forgotten write endpoint silent, which is the exact failure this exists for.
"""

from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.core.config import get_settings
from app.core.logging_config import get_logger

logger = get_logger(__name__)

_READ_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

# Route TEMPLATES (`/api/v1/x/{id}`), not concrete paths — matched against
# `scope["route"].path`, so one entry covers every id.
#
# Each line answers "why is a state-changing HTTP verb not worth an audit row".
AUDIT_EXEMPT_ROUTES = frozenset({
    # ── POSTs that are really queries: they read, they return, they change
    #    nothing. The verb is POST only because the filter payload is too big
    #    for a query string.
    "/api/v1/trade-summary/query",
    "/api/v1/audience/preview",
    "/api/v1/trading/analysis",
    "/api/v1/trading/hourly-details",
    "/api/v1/ib-data/query",
    "/api/v1/ib-data/region-query",
    "/api/v1/ib-report/search",
    "/api/v1/ibid-lots/query",
    "/api/v1/login-ip/search",
    "/api/v1/cs/fund-flow/query",
    "/api/v1/aggregate/to-json",
    # ── Cache / derived-data recomputation. No state semantics: the numbers are
    #    rebuilt from the same source rows either way (design §D6.2 "可不做").
    "/api/v1/aggregate/refresh",
    "/api/v1/etl/pnl-user-summary/refresh",
    "/api/v1/etl/client-pnl/refresh",
    "/api/v1/client-return-rate/roace/refresh",
    "/api/v1/client-return-rate/cache",
    "/api/v1/client-return-rate/export/tasks",
    # ── The high-frequency autosave path. 59% of all non-GET traffic measured
    #    over 30 days; auditing it buries every human action under it.
    #    force-release is the one that IS audited — it takes something away
    #    from a colleague.
    "/api/v1/view-profiles",
    "/api/v1/view-profiles/{name}/claim",
    "/api/v1/view-profiles/{name}/release",
    "/api/v1/view-profiles/{name}/state",
    # ── Auth's own endpoints keep their trail in auth_events, which records
    #    more about a login than audit_log has columns for.
    "/api/v1/auth/logout",
    "/api/v1/auth/dev-login",
    "/api/v1/auth/login",
    "/api/v1/auth/callback",
    # ── Browser error reporting: written by the frontend, not by a person.
    "/api/v1/log/client-error",
    # ── Emails a one-time code to an external IB. The code table is its trail,
    #    and the caller has no session yet by definition.
    "/api/v1/ib-financial/request-code",
})


class AuditMissingMiddleware(BaseHTTPMiddleware):
    """Warn when a successful write produced no audit row.

    Registered INNERMOST (closest to the routes) so it only ever sees requests
    that got past the API-key and session layers: a 401 from AuthMiddleware is
    not a missing audit row, it is a rejected request.
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        response = await call_next(request)
        try:
            self._check(request, response)
        except Exception:  # pragma: no cover — a monitor must not break traffic
            logger.debug("AuditMissingMiddleware check failed", exc_info=True)
        return response

    @staticmethod
    def _check(request: Request, response: Response) -> None:
        if not get_settings().AUDIT_MISSING_ALERT_ENABLED:
            return
        if request.method.upper() in _READ_METHODS:
            return
        if not request.url.path.startswith("/api/"):
            return
        # Only successes. A 4xx/5xx changed nothing (or already failed loudly),
        # and D2 rules failed requests out of the audit trail on purpose — so
        # their missing row is correct, not a gap.
        if not 200 <= response.status_code < 300:
            return

        route = request.scope.get("route")
        template = getattr(route, "path", None) or request.url.path
        if template in AUDIT_EXEMPT_ROUTES:
            return
        if getattr(request.state, "audit_records", 0):
            return

        logger.warning(
            "AUDIT_MISSING method=%s route=%s path=%s status=%s — a successful "
            "write left no audit row. Either add audit.record(...) to this "
            "endpoint or add it to AUDIT_EXEMPT_ROUTES with the reason.",
            request.method,
            template,
            request.url.path,
            response.status_code,
        )
