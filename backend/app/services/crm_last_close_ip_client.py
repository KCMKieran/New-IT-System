"""
CRM client for the last-close-position-IP custom field (login-ip §3.5).

Talks to the same external CRM ``POST /rest/users/update`` endpoint as
``crm_risk_tag_client`` (Bearer auth, IP-allowlisted), but writes a single
custom field instead of the tags list, using dedicated credentials
(``CRM_LAST_CLOSE_IP_API_*``) so either integration can be revoked without
breaking the other.

Safety model — deliberately lighter than the risk-tag client's:

- **No read-modify-write.** The tags field is full-replace and shared with
  CS, so writing it requires re-reading first or you erase someone's edit.
  This field is machine-owned: nothing but this job writes it, and a write
  carries the whole value. So a single POST is safe.
- **Post-write verify is kept, and is not optional.** A mistyped field key
  makes this CRM answer HTTP 200 and write nothing (verified 2026-07-15).
  A bare 200 is therefore not evidence of anything; every write reads back.
- **Retry only 429/5xx/network.** Other 4xx are deterministic (403 IP
  allowlist, 401 token, 422 payload) — retrying just multiplies the error.
- The Authorization header is never logged.

Read shape (verified 2026-07-15 against the live CRM): custom fields come
back **nested** under ``customFields``, and only those that have a value —
a client with no value has an empty ``customFields`` dict, and the top-level
object carries no ``custom_*`` keys at all. The house ``crm-api-update``
skill documents the opposite (flat at top level); until that's reconciled
``read_field`` checks both shapes rather than trusting either.

Storage note: ``fxbackoffice.user_custom_fields.v`` holds the value
JSON-encoded (``"1.2.3.4"``, quotes included). The API decodes it on read,
so this client sees a plain string — but SQL that reads the column directly
needs ``JSON_UNQUOTE``.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional

import requests

logger = logging.getLogger(__name__)

# The CRM custom-field key. Verified present in fxbackoffice.custom_fields
# (id=64, scope=user, type=text) on 2026-07-15.
#
# Byte-exactness matters more than usual here: a typo does NOT raise. The CRM
# returns 200 and silently drops the write, so a mistyped key looks like a
# clean run that pushes nothing. Never hand-retype this; the read-back verify
# in `write_field` is what actually catches it.
FIELD_KEY = "custom_last_close_position_ip"

_MAX_RETRIES = 2
_BACKOFF_BASE_SEC = 2.0
_CONNECT_TIMEOUT = 10
_READ_TIMEOUT = 30


class CrmError(Exception):
    """CRM call failed (network / HTTP / unparseable body)."""

    def __init__(self, message: str, http_status: Optional[int] = None,
                 body: Optional[str] = None):
        super().__init__(message)
        self.http_status = http_status
        # Truncated defensively — bodies land in the push-log `detail` column.
        self.body = (body or "")[:2000]


@dataclass
class IpWriteResult:
    """Outcome of one field write, shaped for the push-log row."""
    ok: bool
    client_id: int
    # `value`, not `ip_address`: since 2026-07-17 the field holds "1.2.3.4 (CN)".
    # This client is deliberately value-agnostic — it compares and verifies
    # opaque strings, and the CRM field is type=text. Don't reintroduce IP
    # parsing or validation here; the format is owned by login_ip_geo_service.
    value: str
    value_before: Optional[str]
    value_after: Optional[str]
    http_status: Optional[int]
    detail: str = ""
    # True when CRM already held this value and no POST was issued.
    no_op: bool = False
    # True when CRM answered 200 but the read-back didn't show the value —
    # distinct from a plain failure because it points at the field key, not
    # at the network.
    verify_failed: bool = False


class CrmLastCloseIpClient:
    """Wrapper around the CRM /rest/users/update endpoint for one custom field.

    ``session`` is injectable for tests (any object with ``.post``).
    """

    def __init__(self, api_url: str, api_token: str,
                 session: Optional[requests.Session] = None):
        if not api_url or not api_token:
            raise ValueError(
                "CRM_LAST_CLOSE_IP_API_URL / CRM_LAST_CLOSE_IP_API_TOKEN not configured"
            )
        self._url = api_url
        self._headers = {
            "Authorization": f"Bearer {api_token}",
            "Content-Type": "application/json",
        }
        self._session = session or requests.Session()

    # ── HTTP plumbing ────────────────────────────────────────────

    def _post(self, payload: Dict[str, Any]) -> requests.Response:
        """One POST, retrying only on 429 / 5xx / network errors."""
        attempt = 0
        while True:
            try:
                resp = self._session.post(
                    self._url,
                    json=payload,
                    headers=self._headers,
                    timeout=(_CONNECT_TIMEOUT, _READ_TIMEOUT),
                )
            except requests.RequestException as ex:
                if attempt < _MAX_RETRIES:
                    attempt += 1
                    time.sleep(_BACKOFF_BASE_SEC * (2 ** (attempt - 1)))
                    continue
                raise CrmError(f"CRM request failed: {type(ex).__name__}: {ex}") from ex
            if resp.status_code == 429 or 500 <= resp.status_code < 600:
                if attempt < _MAX_RETRIES:
                    attempt += 1
                    time.sleep(_BACKOFF_BASE_SEC * (2 ** (attempt - 1)))
                    continue
            return resp

    # ── Read ─────────────────────────────────────────────────────

    def read_field(self, client_id: int) -> Optional[str]:
        """Return the client's current field value, or None if unset.

        POSTs ``{"user": id}`` only — no writable key, so this cannot mutate.
        Raises CrmError on HTTP failure or a non-JSON / non-object body.
        """
        resp = self._post({"user": int(client_id)})
        if resp.status_code != 200:
            raise CrmError(
                f"CRM read for client {client_id} returned {resp.status_code}",
                http_status=resp.status_code, body=resp.text,
            )
        try:
            data = resp.json()
        except ValueError as ex:
            raise CrmError(
                f"CRM read for client {client_id}: body is not JSON",
                http_status=resp.status_code, body=resp.text,
            ) from ex
        if not isinstance(data, dict) or not data:
            raise CrmError(
                f"CRM read for client {client_id}: empty/non-object response",
                http_status=resp.status_code, body=resp.text,
            )
        return extract_field(data)

    # ── Write ────────────────────────────────────────────────────

    def write_field(self, client_id: int, value: str) -> IpWriteResult:
        """Set the client's field to ``value``, verifying the read-back.

        Reads first — not to avoid a race (nothing else writes this field) but
        because `value_before` is the only rollback path this job has, and the
        read doubles as a free no-op check when CRM already agrees.
        """
        try:
            before = self.read_field(client_id)
        except CrmError as ex:
            return IpWriteResult(
                ok=False, client_id=client_id, value=value,
                value_before=None, value_after=None,
                http_status=ex.http_status,
                detail=f"pre-write read failed: {ex}",
            )

        if before == value:
            # The local diff thought this needed a write but CRM already
            # agrees — e.g. the push-log row was pruned by retention, or a
            # previous run's write landed after its read-back failed. Recording
            # it settles the diff so the next run skips it outright.
            return IpWriteResult(
                ok=True, client_id=client_id, value=value,
                value_before=before, value_after=before,
                http_status=None, detail="CRM already holds this value",
                no_op=True,
            )

        resp = self._post({
            "user": int(client_id),
            "customFields": {FIELD_KEY: value},
        })
        if resp.status_code != 200:
            logger.error(
                "CRM last-close-IP write failed: client=%s status=%s body=%s",
                client_id, resp.status_code, resp.text[:500],
            )
            return IpWriteResult(
                ok=False, client_id=client_id, value=value,
                value_before=before, value_after=before,
                http_status=resp.status_code,
                detail=f"HTTP {resp.status_code}: {resp.text[:500]}",
            )

        # The whole reason this function exists: 200 does not mean written.
        try:
            after = self.read_field(client_id)
        except CrmError as ex:
            logger.error(
                "CRM last-close-IP read-back failed: client=%s: %s", client_id, ex
            )
            return IpWriteResult(
                ok=False, client_id=client_id, value=value,
                value_before=before, value_after=None,
                http_status=resp.status_code,
                detail=f"write 200 but read-back failed: {ex}",
            )
        if after != value:
            logger.error(
                "CRM last-close-IP verify FAILED: client=%s wanted=%s got=%r "
                "(200 but silent no-op — check the field key %r)",
                client_id, value, after, FIELD_KEY,
            )
            return IpWriteResult(
                ok=False, client_id=client_id, value=value,
                value_before=before, value_after=after,
                http_status=resp.status_code,
                detail=f"verify failed: wrote {value!r}, read back {after!r}",
                verify_failed=True,
            )
        return IpWriteResult(
            ok=True, client_id=client_id, value=value,
            value_before=before, value_after=after,
            http_status=resp.status_code,
        )


def extract_field(raw: Dict[str, Any]) -> Optional[str]:
    """Pull FIELD_KEY out of a CRM user object, tolerating both read shapes.

    Nested under ``customFields`` is what the live CRM actually returns;
    the flat top-level fallback covers the shape the house skill documents.
    Module-level (not a method) so the push service and the probe script can
    share one definition of "what does CRM currently hold".
    """
    cf = raw.get("customFields")
    if isinstance(cf, dict) and FIELD_KEY in cf:
        value = cf[FIELD_KEY]
    else:
        value = raw.get(FIELD_KEY)
    if value is None:
        return None
    # An unset field can come back as "" — normalise to None so callers don't
    # have to distinguish "absent" from "present but empty".
    value = str(value).strip()
    return value or None
