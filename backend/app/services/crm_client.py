"""Low-level CRM REST API client for the risk-tag writer (OPT-0032).

Talks to the company CRM's `POST /rest/users/update` endpoint with a
dedicated API account (isolated from the Swap_Free integration). The
endpoint takes the FULL desired user state for array fields, so updating
`tags` REPLACES the whole array — callers must read-modify-write.

Endpoint contract (verified 2026-06-02):
    POST {CRM_RISK_API_URL}
    Authorization: Bearer {CRM_RISK_API_TOKEN}
    Content-Type: application/json
    body: {"user": <id>, ...fields...}      # omitted fields are left unchanged
    -> 200 with the updated user object (id / cid / tags / ...)

Notes:
- The CRM enforces an IP allowlist; a non-allowlisted caller gets HTTP 403
  `{"error":"invalid_grant","error_description":"Client IP is not allowed."}`.
- Retry/rate-limit/timeout mirror the proven Swap_Free `run_crm_upload.py`
  shape (exponential backoff on 5xx / network errors).

This module is deliberately thin and PII-agnostic: it just moves JSON. The
dedup / cid->tag / audit / email orchestration lives in
`gap_trade_tag_service`.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Optional, Tuple

import requests

from ..core.config import get_settings

logger = logging.getLogger(__name__)


class CrmConfigError(RuntimeError):
    """Raised when CRM credentials are not configured."""


class _RateLimiter:
    """Simple per-second rate limiter (ported from Swap_Free)."""

    def __init__(self, max_per_second: float) -> None:
        self.max_per_second = max_per_second
        self._window_start = 0.0
        self._sent_in_window = 0
        self._lock = threading.Lock()

    def acquire(self) -> None:
        if self.max_per_second <= 0:
            return
        with self._lock:
            now = time.time()
            window = 1.0
            if self._window_start == 0.0 or (now - self._window_start) >= window:
                self._window_start = now
                self._sent_in_window = 0
            if self._sent_in_window >= self.max_per_second:
                sleep_seconds = window - (now - self._window_start)
                if sleep_seconds > 0:
                    time.sleep(sleep_seconds)
                self._window_start = time.time()
                self._sent_in_window = 0
            self._sent_in_window += 1


# Module-level singletons so the rate limit is shared across calls in a process.
_session: Optional[requests.Session] = None
_limiter: Optional[_RateLimiter] = None
_init_lock = threading.Lock()


def _get_session_and_limiter() -> Tuple[requests.Session, _RateLimiter]:
    global _session, _limiter
    if _session is None or _limiter is None:
        with _init_lock:
            if _session is None:
                _session = requests.Session()
            if _limiter is None:
                s = get_settings()
                _limiter = _RateLimiter(s.CRM_RISK_MAX_REQ_PER_SEC)
    return _session, _limiter


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _extract_user(body: Any) -> Optional[dict]:
    """The endpoint returns the user object (sometimes wrapped in a list)."""
    if isinstance(body, list) and body and isinstance(body[0], dict):
        return body[0]
    if isinstance(body, dict) and "id" in body:
        return body
    return None


def _post(payload: dict) -> Tuple[int, Any]:
    """POST `payload` to the CRM with retry + rate limit.

    Returns (http_status, parsed_body). http_status is -1 on a network
    error that exhausted retries. parsed_body is the decoded JSON (or raw
    text when not JSON, or None on network failure).
    """
    s = get_settings()
    token = s.CRM_RISK_API_TOKEN
    if not token:
        raise CrmConfigError("CRM_RISK_API_TOKEN is not set")

    session, limiter = _get_session_and_limiter()
    url = s.CRM_RISK_API_URL
    headers = _headers(token)
    timeout = (s.CRM_RISK_CONNECT_TIMEOUT, s.CRM_RISK_READ_TIMEOUT)
    max_retries = s.CRM_RISK_MAX_RETRIES
    backoff = s.CRM_RISK_RETRY_BACKOFF_SEC

    attempt = 0
    while True:
        limiter.acquire()
        try:
            resp = session.post(url, json=payload, headers=headers, timeout=timeout)
            if 500 <= resp.status_code < 600 and attempt < max_retries:
                attempt += 1
                time.sleep(backoff * (2 ** (attempt - 1)))
                continue
            try:
                body: Any = resp.json()
            except ValueError:
                body = resp.text
            return resp.status_code, body
        except requests.RequestException:
            if attempt < max_retries:
                attempt += 1
                time.sleep(backoff * (2 ** (attempt - 1)))
                continue
            logger.error("CRM POST failed after %d retries", max_retries, exc_info=True)
            return -1, None


def read_user(user_id: int) -> Tuple[int, Optional[dict]]:
    """Read a CRM user by id (safe: posts only `user`, mutates nothing).

    Returns (http_status, user_dict_or_None). The user dict carries `cid`
    and `tags` among other fields.
    """
    status, body = _post({"user": int(user_id)})
    return status, _extract_user(body)


def update_user_tags(user_id: int, tags: list[str]) -> Tuple[int, Optional[dict]]:
    """Write the FULL `tags` array for a CRM user (REPLACE semantics).

    Caller is responsible for read-modify-write (preserve existing tags).
    Returns (http_status, updated_user_dict_or_None).
    """
    status, body = _post({"user": int(user_id), "tags": list(tags)})
    return status, _extract_user(body)
