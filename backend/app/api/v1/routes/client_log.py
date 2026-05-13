"""
Client-side error log endpoint.

Receives unhandled errors caught by the frontend's LazyErrorBoundary
(chunk-load failures and render errors). Logs them to backend.log with
the request's trace_id so we can correlate with Nginx access logs and
diagnose recurring "失联" reports without relying on user-reported
screenshots.
"""

from typing import Literal

from fastapi import APIRouter, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field

from app.core.logging_config import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/log")


class ClientErrorPayload(BaseModel):
    """Bounded shape — caps each field so we cannot be DoS'd into bloating the log."""

    kind: Literal["chunk_load", "render", "fetch", "unknown"] = "unknown"
    message: str = Field(default="", max_length=1000)
    stack: str = Field(default="", max_length=2000)
    url: str = Field(default="", max_length=500)
    ua: str = Field(default="", max_length=300)


@router.post("/client-error", status_code=204)
async def report_client_error(payload: ClientErrorPayload, request: Request) -> Response:
    """
    Accept a client-side error report. X-API-Key is enforced by the global
    APIKeyMiddleware — anonymous calls are blocked at the edge.
    """
    client_ip = request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
    if not client_ip:
        client_ip = request.client.host if request.client else "unknown"

    logger.warning(
        "Client error: kind=%s msg=%s url=%s ua=%s ip=%s",
        payload.kind,
        payload.message.replace("\n", " ")[:500],
        payload.url,
        payload.ua,
        client_ip,
    )

    # Stack trace as a separate (longer) line so log scanners can grep just the
    # warning summary lines first when triaging.
    if payload.stack:
        logger.info(
            "Client error stack (kind=%s): %s",
            payload.kind,
            payload.stack.replace("\n", " | ")[:2000],
        )

    return Response(status_code=204)
