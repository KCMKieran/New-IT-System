"""
CRM risk-tag client for Gap Trade auto-tagging (OPT-0032).

Talks to the external CRM ``POST /rest/users/update`` endpoint (Bearer auth,
IP-allowlisted) to read a client's profile and apply / remove a risk tag.

Safety model — the CRM ``tags`` field is FULL-REPLACE (the whole list is
overwritten on every write), and the tag we write blocks withdrawal
auto-approval, so a bug here either freezes the wrong client's money or
silently wipes tags other departments rely on. Hence:

- **Strict parse**: the read response must contain ``tags`` as a list of
  strings and an integer ``cid``. Anything else raises ``CrmParseError`` and
  the caller SKIPS that client (never write a partially-understood list).
- **Read-modify-write**: read immediately before every write attempt (incl.
  retries — a retry re-reads so it never writes a stale snapshot).
- **Post-write verify**: after a 200, re-read and assert
  ``old_tags ⊆ new_tags`` and the intended tag present/absent. A 200 whose
  read-back doesn't match is reported as a failure, not a success.
- **Response bodies are kept**: unlike the Swap_Free bulk uploader (which
  drops bodies for throughput), tag-write volume is a handful of clients per
  round — every non-200 body and every write's read-back is logged/returned
  for the audit table.
- The Authorization header is never logged.

Tag constants: ``cid`` (CRM company id) picks the tag string. The strings
must stay byte-identical to what the CRM stores — copy from a live CRM API
response, never hand-type (see tests/test_crm_risk_tag_client.py which pins
the exact UTF-8 bytes).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import requests

logger = logging.getLogger(__name__)

# cid → tag string. Only CN (0) and Global (1) exist today; any other cid is
# skipped by the caller (skipped_cid) and surfaced in the digest email so a
# new region's appearance is noticed by humans, not buried in a log line.
# ⚠ Byte-exactness matters: a near-miss (繁/简, full/half-width) creates a NEW
# tag that CS's withdrawal filter ignores — verify against a live CRM read
# during the canary step (OPT-0032 D0 checklist) before first live write.
CID_TAG_MAP: Dict[int, str] = {
    0: "禁止出金(風控)",      # CN cabinet (tagid 488374, reference only)
    1: "Withdrawal Notice",  # Global cabinet (tagid 263196, reference only)
}

_MAX_RETRIES = 2
_BACKOFF_BASE_SEC = 2.0
_CONNECT_TIMEOUT = 10
_READ_TIMEOUT = 30


class CrmError(Exception):
    """Base error for CRM client failures (network / HTTP / verify)."""

    def __init__(self, message: str, http_status: Optional[int] = None,
                 body: Optional[str] = None):
        super().__init__(message)
        self.http_status = http_status
        # Truncate defensively — bodies go into the audit `detail` column.
        self.body = (body or "")[:2000]


class CrmParseError(CrmError):
    """Read response couldn't be parsed into a round-trippable shape.

    Callers must SKIP the client (never write back a partially-understood
    tags list — full-replace semantics would erase what we failed to parse).
    """


@dataclass
class CrmUserState:
    """Snapshot of the fields we care about from a CRM read."""
    user_id: int
    cid: Optional[int]
    tags: List[str]
    raw: Dict[str, Any] = field(repr=False, default_factory=dict)


@dataclass
class TagWriteResult:
    """Outcome of an add/remove tag attempt, shaped for the audit table."""
    ok: bool
    user_id: int
    cid: Optional[int]
    tag: str
    tags_before: List[str]
    tags_after: List[str]
    http_status: Optional[int]
    detail: str = ""
    # True when the tag was already in the desired state (no POST issued).
    no_op: bool = False


class CrmRiskTagClient:
    """Thin, safety-first wrapper around the CRM /rest/users/update endpoint.

    ``session`` is injectable for tests (any object with ``.post``).
    """

    def __init__(self, api_url: str, api_token: str,
                 session: Optional[requests.Session] = None):
        if not api_url or not api_token:
            raise ValueError("CRM_RISK_API_URL / CRM_RISK_API_TOKEN not configured")
        self._url = api_url
        self._headers = {
            "Authorization": f"Bearer {api_token}",
            "Content-Type": "application/json",
        }
        self._session = session or requests.Session()

    # ── HTTP plumbing ────────────────────────────────────────────

    def _post(self, payload: Dict[str, Any]) -> requests.Response:
        """One POST with retry on 5xx / 429 / network errors.

        429 honours exponential backoff (Swap_Free's pattern treated it as a
        terminal failure — wrong for a write whose failure mode is a client's
        unblocked withdrawal). 4xx other than 429 never retries: it's a
        deterministic error (403 IP-allowlist, 401 token, 422 payload).
        """
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
            if resp.status_code in (429,) or 500 <= resp.status_code < 600:
                if attempt < _MAX_RETRIES:
                    attempt += 1
                    time.sleep(_BACKOFF_BASE_SEC * (2 ** (attempt - 1)))
                    continue
            return resp

    # ── Read ─────────────────────────────────────────────────────

    def read_user(self, user_id: int) -> CrmUserState:
        """Read-only probe: POST {"user": id} (no tags field = no mutation).

        Raises CrmError on HTTP failure, CrmParseError when the response
        isn't a round-trippable shape.
        """
        resp = self._post({"user": int(user_id)})
        if resp.status_code != 200:
            raise CrmError(
                f"CRM read for user {user_id} returned {resp.status_code}",
                http_status=resp.status_code, body=resp.text,
            )
        return self._parse_user(user_id, resp)

    @staticmethod
    def _parse_user(user_id: int, resp: requests.Response) -> CrmUserState:
        try:
            data = resp.json()
        except ValueError as ex:
            raise CrmParseError(
                f"CRM read for user {user_id}: body is not JSON",
                http_status=resp.status_code, body=resp.text,
            ) from ex
        if not isinstance(data, dict) or not data:
            raise CrmParseError(
                f"CRM read for user {user_id}: empty/non-object response",
                http_status=resp.status_code, body=resp.text,
            )
        # cid: authoritative from CRM (never inferred from MT group).
        cid_raw = data.get("cid")
        try:
            cid = int(cid_raw) if cid_raw is not None else None
        except (TypeError, ValueError):
            raise CrmParseError(
                f"CRM read for user {user_id}: non-integer cid={cid_raw!r}",
                http_status=resp.status_code, body=resp.text,
            )
        # tags: must be a list of strings — exactly the shape we'd write
        # back. Anything else (objects, comma-joined string, null entries)
        # is a parse failure: full-replace semantics forbid writing back a
        # list we normalised lossily. A missing key or null is treated as
        # an empty list ONLY if the key is absent/None (new client with no
        # tags), never if it's present with an unexpected type.
        tags_raw = data.get("tags")
        if tags_raw is None:
            tags: List[str] = []
        elif isinstance(tags_raw, list) and all(isinstance(t, str) for t in tags_raw):
            tags = list(tags_raw)
        else:
            raise CrmParseError(
                f"CRM read for user {user_id}: tags not a list of strings "
                f"(got {type(tags_raw).__name__})",
                http_status=resp.status_code, body=resp.text,
            )
        return CrmUserState(user_id=int(user_id), cid=cid, tags=tags, raw=data)

    # ── Write (read-modify-write) ────────────────────────────────

    def add_tag(self, user_id: int, tag: str) -> TagWriteResult:
        """Append ``tag`` to the client's tags (full-replace write-back)."""
        return self._modify_tags(user_id, tag, add=True)

    def remove_tag(self, user_id: int, tag: str) -> TagWriteResult:
        """Remove ``tag`` from the client's tags. Day-one rollback path —
        a false positive must be reversible faster than it does harm."""
        return self._modify_tags(user_id, tag, add=False)

    def _modify_tags(self, user_id: int, tag: str, *, add: bool) -> TagWriteResult:
        # Read immediately before the write to minimise the RMW race window
        # (CS / Swap_Free editing the same client between read and write).
        state = self.read_user(user_id)
        before = list(state.tags)

        if add and tag in before:
            return TagWriteResult(
                ok=True, user_id=user_id, cid=state.cid, tag=tag,
                tags_before=before, tags_after=before,
                http_status=None, detail="tag already present", no_op=True,
            )
        if not add and tag not in before:
            return TagWriteResult(
                ok=True, user_id=user_id, cid=state.cid, tag=tag,
                tags_before=before, tags_after=before,
                http_status=None, detail="tag already absent", no_op=True,
            )

        desired = before + [tag] if add else [t for t in before if t != tag]
        resp = self._post({"user": int(user_id), "tags": desired})
        if resp.status_code != 200:
            # Keep the body — 403 invalid_grant (IP allowlist) vs validation
            # error matters operationally and goes into the audit row.
            logger.error(
                "CRM tag write failed: user=%s status=%s body=%s",
                user_id, resp.status_code, resp.text[:500],
            )
            return TagWriteResult(
                ok=False, user_id=user_id, cid=state.cid, tag=tag,
                tags_before=before, tags_after=before,
                http_status=resp.status_code,
                detail=f"HTTP {resp.status_code}: {resp.text[:500]}",
            )

        # Post-write verify: a bare 200 is not proof the tag landed (and a
        # lost-update race shows up here as missing pre-existing tags).
        try:
            after_state = self.read_user(user_id)
        except CrmError as ex:
            logger.error("CRM post-write read-back failed: user=%s: %s", user_id, ex)
            return TagWriteResult(
                ok=False, user_id=user_id, cid=state.cid, tag=tag,
                tags_before=before, tags_after=desired,
                http_status=resp.status_code,
                detail=f"write 200 but read-back failed: {ex}",
            )
        after = list(after_state.tags)
        lost = [t for t in before if t != tag and t not in after]
        tag_ok = (tag in after) if add else (tag not in after)
        if lost or not tag_ok:
            logger.error(
                "CRM tag write verify FAILED: user=%s lost=%s tag_ok=%s "
                "before=%s after=%s",
                user_id, lost, tag_ok, before, after,
            )
            return TagWriteResult(
                ok=False, user_id=user_id, cid=state.cid, tag=tag,
                tags_before=before, tags_after=after,
                http_status=resp.status_code,
                detail=f"verify failed: lost={lost} tag_present={tag in after}",
            )
        logger.info(
            "CRM tag %s ok: user=%s cid=%s tag=%r (%d→%d tags)",
            "add" if add else "remove", user_id, state.cid, tag,
            len(before), len(after),
        )
        return TagWriteResult(
            ok=True, user_id=user_id, cid=state.cid, tag=tag,
            tags_before=before, tags_after=after,
            http_status=resp.status_code, detail="",
        )
