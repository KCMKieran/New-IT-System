"""Audit context — one line in a business route writes one trustworthy audit row.

Design goals
------------
1. The actor comes from ``request.state.user`` and NOWHERE else. That subject is
   resolved server-side from the session cookie by ``AuthMiddleware``, so a
   client cannot forge it. Anything a caller can type — ``X-Device-ID``, an
   ``author`` field in the body, a hardcoded ``"WebUser"`` — is attribution
   theatre, not identity, and must never reach ``actor_email``.
2. ip / trace_id fill themselves in. Callers never pass them.
3. An audit write must never affect the business write. Neither ``record()``
   nor ``record_diff()`` can raise — including the comparison ``record_diff``
   does, which runs on caller-supplied values after the business write has
   already committed.
4. ``AUTH_ENABLED=false`` degrades gracefully: the row is still written with a
   NULL actor, and a WARNING carrying the ``AUDIT_ANONYMOUS`` grep token says so.

When to record (the rule of thumb, from docs/architecture/audit-log-design.md §D2):

    A human did it + it changed state + it succeeded = record. Miss one, skip it.

So: no GETs, no query-shaped POSTs (/search /query /preview), no scheduler ticks,
no high-frequency autosaves (view-profiles), no failed requests, no fields whose
value did not actually move. The one exception is an action that already reached
the outside world (a test email that timed out) — record those even on failure,
distinguished through ``new_value``.

Usage
-----
    from app.core.audit import Auditor, get_auditor

    @router.delete("/subscriptions/{subscription_id}")
    def delete_subscription(
        subscription_id: int,
        audit: Auditor = Depends(get_auditor),
    ):
        sub = svc.get_subscription(subscription_id)   # read the old value FIRST
        svc.delete_subscription(subscription_id)      # raises -> no audit row
        audit.record(
            "alert_mail.subscription.delete",
            target=f"subscription:{subscription_id}:{sub['name']}",
            old_value=sub,
        )
"""

from __future__ import annotations

import functools
import hashlib
import inspect
import json
from typing import Any, Callable, Optional

from fastapi import Request

from app.core.auth_middleware import client_ip
from app.core.logging_config import get_logger
from app.services import auth_service

logger = get_logger(__name__)

# Upper bound on a single old_value / new_value. The audit table is a record of
# WHAT CHANGED, not a backup of the row: a 40 KB JSON blob makes the admin page
# unreadable and blows up the capacity estimate the retention window is based on.
MAX_VALUE_LEN = 2000


def _render(value: Any) -> Optional[str]:
    """Render any value into a string, at FULL length. Comparison uses this.

    Kept separate from ``_stringify`` on purpose: comparing the STORED (i.e.
    truncated) renderings silently loses every edit that happens past
    MAX_VALUE_LEN — two values that share their first 2000 characters compare
    equal, so record_diff() would decide "nothing moved" and write no row at
    all. That failure is invisible: no error, no log line, just an audit trail
    that quietly stops covering long values. So: compare in full, truncate only
    on the way into the table.

    ⚠ ``None`` is returned as ``None``, never as ``"None"`` or ``""``. SQL NULL
    carries meaning in this project — a legacy ``users.allowed_modules`` NULL
    (pre-2026-08-27, before the ``'["*"]'`` sentinel replaced it) means "every
    module" while ``'[]'`` means "no modules at all", two opposite grants.
    Losing that distinction inside the audit trail would make them read
    identically a year from now, which is precisely when someone needs to tell
    them apart — and rows written before the migration keep NULL on their
    "before" side forever.
    """
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, (dict, list)):
        # sort_keys makes the same content serialise identically every time,
        # which is what lets record_diff() compare two renderings safely.
        return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
    return str(value)


def _stringify(value: Any) -> Optional[str]:
    """Render a value the way the audit table STORES it — i.e. capped in length.

    Storage-only. Anything deciding whether a value moved must use ``_render``.

    The marker carries a digest of the FULL text as well as its length, because
    two long values that differ only past the cut-off would otherwise be stored
    as byte-identical strings — a row that says "changed" while showing old and
    new as the same thing. The digest is what lets a reader tell them apart.
    """
    text = _render(value)
    if text is not None and len(text) > MAX_VALUE_LEN:
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]
        return text[:MAX_VALUE_LEN] + f"…(truncated, {len(text)} chars, sha256:{digest})"
    return text


class Auditor:
    """Request-scoped audit writer. Produced by ``get_auditor`` as a dependency."""

    __slots__ = ("_request",)

    def __init__(self, request: Request) -> None:
        self._request = request

    # ── the primary API ──────────────────────────────────────────────────────

    @property
    def actor_email(self) -> Optional[str]:
        """The session subject's email, or None when no session resolved.

        Exposed so a route can stamp the SAME verified identity onto a business
        history table it also writes (account_remarks_history /
        client_remarks_history, whose ``author`` column used to be whatever the
        client typed). Read-only and derived from ``request.state.user``, so
        there is still exactly one place identity can come from.
        """
        user = getattr(self._request.state, "user", None)
        return getattr(user, "email", None)

    def record(
        self,
        action: str,
        *,
        target: Optional[str] = None,
        old_value: Any = None,
        new_value: Any = None,
    ) -> None:
        """Write one audit row. Never raises.

        Call it AFTER the business write has succeeded. Called before, an audit
        row survives a business write that then failed, and the table grows
        entries for changes that never happened — worse than no entry at all,
        because a reader has no way to tell the two apart.

        ``action`` is ``<module>.<object>.<verb>``, lowercase, dot-separated in
        exactly three segments (the shape is what makes ``LIKE 'risk_monitor.%'``
        a usable filter). ``target`` is ``<type>:<key>:<human-readable label>`` —
        the third segment is redundant ON PURPOSE, because the business row it
        points at may well be deleted by the time anyone reads this.
        """
        user = getattr(self._request.state, "user", None)
        ip = self._client_ip()
        self._mark_recorded()

        if user is None:
            # Normally unreachable: AuthMiddleware 401s anonymous callers. The
            # one way here is AUTH_ENABLED=false, where nothing resolves a
            # session at all. Still write the row — the IP is then the only
            # identity clue that exists — but say so loudly in the app log.
            # AUDIT_ANONYMOUS is a stable grep token; the health check greps it.
            logger.warning(
                "AUDIT_ANONYMOUS action=%s target=%s ip=%s — no session subject "
                "(AUTH_ENABLED is probably false). Row written with a NULL actor.",
                action,
                target,
                ip,
            )

        try:
            auth_service.record_audit(
                action,
                actor_email=getattr(user, "email", None),
                actor_user_id=getattr(user, "user_id", None),
                target=target,
                old_value=_stringify(old_value),
                new_value=_stringify(new_value),
                ip=ip,
            )
        except Exception:
            # record_audit() already swallows sqlite3.Error itself. This catches
            # everything above it — a value whose __str__ raises, a JSON encoder
            # surprise. The business write is already committed and must not be
            # disturbed by an accounting problem.
            logger.critical(
                "AUDIT_WRITE_FAILED action=%r target=%r — raised inside the auditor",
                action,
                target,
                exc_info=True,
            )

    def record_diff(
        self,
        action: str,
        *,
        target: str,
        old: dict,
        new: dict,
        ignore: frozenset[str] = frozenset(),
    ) -> int:
        """Write one row per field that ACTUALLY moved. Returns how many.

        This is where "avoid useless log entries" lives. A twelve-field config
        form posts all twelve values back on every save; without a diff that is
        twelve rows for one changed threshold, and the signal drowns.

        Each row's target becomes ``{target}.{field}``, so the audit trail says
        which knob moved rather than merely "the config was saved".

        Never raises, exactly like ``record()``. The comparison itself runs on
        caller-supplied values — a stray ``__eq__``, an object whose ``__str__``
        blows up, a key set that will not sort — and this method is called AFTER
        the business write committed. Turning that into a 500 would report a
        failure for a save that actually succeeded. A failure here gives up on
        the remaining fields, keeps the rows already written, and says so under
        the AUDIT_WRITE_FAILED grep token.
        """
        written = 0
        try:
            for key in sorted(set(old) | set(new)):
                if key in ignore:
                    continue
                before, after = old.get(key), new.get(key)
                # Full renderings, not the stored (truncated) ones: comparing
                # truncated text would drop any change past MAX_VALUE_LEN.
                if _render(before) == _render(after):
                    continue  # unchanged — the whole point of this method
                self.record(action, target=f"{target}.{key}", old_value=before, new_value=after)
                written += 1
        except Exception:
            logger.critical(
                "AUDIT_WRITE_FAILED action=%r target=%r — raised while diffing "
                "(%d row(s) already written)",
                action,
                target,
                written,
                exc_info=True,
            )
            return written
        if written == 0:
            # Someone pressed Save and changed nothing. No audit row (it records
            # no change), but keep an app-log line: "did they even click it?" is
            # sometimes the question being asked.
            logger.info("No-op save: action=%s target=%s (nothing changed)", action, target)
        return written

    # ── internals ────────────────────────────────────────────────────────────

    def _mark_recorded(self) -> None:
        """Count this row on the request, for AuditMissingMiddleware to read.

        ⚠ On ``request.state`` (i.e. the ASGI ``scope``), NOT a ContextVar.
        BaseHTTPMiddleware runs the app below it inside its own anyio task, so a
        ContextVar set in a route does not propagate back UP to the middleware —
        the "no audit row" check would then be true for every request and the
        AUDIT_MISSING token would mean nothing. ``scope["state"]`` is one dict
        shared by every layer, which is the same reason ``request.state.user``
        set by AuthMiddleware is readable in routes.

        Counted, not a bool, because record_diff() writes one row per changed
        field and "how many did this request produce" is the more useful fact.

        Defensive like the rest of this class: bookkeeping must never be the
        reason a business request fails.
        """
        try:
            state = self._request.state
            state.audit_records = getattr(state, "audit_records", 0) + 1
        except Exception:  # pragma: no cover — defensive only
            pass

    def _client_ip(self) -> Optional[str]:
        """The caller's IP, or None if even that cannot be determined.

        client_ip() itself is defensive, but this runs on a path that has
        promised never to raise, so it gets its own belt.
        """
        try:
            return client_ip(self._request)
        except Exception:  # pragma: no cover — defensive only
            return None


def get_auditor(request: Request) -> Auditor:
    """FastAPI dependency. Use as ``audit: Auditor = Depends(get_auditor)``."""
    return Auditor(request)


def history_author(audit: Auditor, claimed: str | None) -> str:
    """The ``author`` to write into a business history table (remarks).

    ``account_remarks_history`` / ``client_remarks_history` are good tables —
    append-only, old and new value, trace id — with one defect: ``author`` was
    whatever the caller put in the request body, so `curl` could file a note
    under a colleague's name. Design §D2.4 does not rebuild those tables, it
    swaps the identity SOURCE, which is what this does: the session subject
    wins whenever one exists.

    The claimed name survives ONLY as the fallback for a request with no
    session at all — i.e. AUTH_ENABLED=false, where the server has no identity
    to offer and a locally-typed name is better than an empty column. It never
    reaches ``audit_log.actor_email``: that row is written by ``record()``,
    which reads the session and nothing else, and logs AUDIT_ANONYMOUS when
    there isn't one.
    """
    return audit.actor_email or (claimed or "").strip()


# ── decorator flavour ────────────────────────────────────────────────────────
#
# The decorator needs a Request to resolve the actor and the IP, but the route it
# wraps usually has no Request in its signature. The fix is to APPEND a
# keyword-only Request parameter to the wrapper's advertised signature — FastAPI
# reads that signature and injects the Request for us.
#
# Two things have to happen together for this to work:
#   1. functools.wraps, so __name__/__doc__ survive for OpenAPI generation;
#   2. wrapper.__signature__ = new_sig, because inspect.signature() prefers
#      __signature__ and functools.wraps would otherwise hand it the ORIGINAL
#      signature — the one without the Request — and FastAPI would never inject.

_AUDIT_REQ_PARAM = inspect.Parameter(
    "__audit_request", inspect.Parameter.KEYWORD_ONLY, annotation=Request
)


def audited(
    action: str,
    *,
    target: Optional[Callable[[dict], str]] = None,
    new_value: Optional[Callable[[dict, Any], Any]] = None,
):
    """Route decorator: audit on a normal return, stay silent on an exception.

    Sugar for the simple case — "a human did this thing" with no old value to
    capture. Anything that needs the previous value, or a per-field diff, wants
    ``Depends(get_auditor)`` instead: the old value only exists BEFORE the write.

    ``target`` and ``new_value`` are callables so they can read the route's own
    arguments. Both receive the route's keyword arguments as a dict; ``new_value``
    also receives whatever the route returned.

    ⚠ It MUST sit BELOW ``@router.xxx`` (closer to the function). Above it,
      FastAPI registers the undecorated function, and the decorator silently
      does nothing at all — no error, no audit row:

          @router.post("/x")            # outer
          @audited("mod.obj.verb")      # inner — correct
          def handler(): ...

    ⚠ Positional-only call sites are not supported: ``target``/``new_value``
      read the keyword arguments, which is how FastAPI always calls routes.
    """

    def decorator(fn):
        sig = inspect.signature(fn)
        if "__audit_request" in sig.parameters:
            raise RuntimeError(f"{fn.__name__}: '__audit_request' is a reserved parameter name")
        new_sig = sig.replace(parameters=list(sig.parameters.values()) + [_AUDIT_REQ_PARAM])

        def _emit(request: Request, kwargs: dict, result: Any) -> None:
            # The target/new_value callables are caller-supplied and run AFTER
            # the business write committed, so they get the same "must not
            # raise" treatment record() gives itself.
            try:
                resolved_target = target(kwargs) if target else None
                resolved_new = new_value(kwargs, result) if new_value else None
            except Exception:
                logger.critical(
                    "AUDIT_WRITE_FAILED action=%r — target/new_value callable raised",
                    action,
                    exc_info=True,
                )
                return
            Auditor(request).record(action, target=resolved_target, new_value=resolved_new)

        if inspect.iscoroutinefunction(fn):

            @functools.wraps(fn)
            async def wrapper(*args, __audit_request: Request, **kwargs):
                result = await fn(*args, **kwargs)  # raises -> the next line never runs
                _emit(__audit_request, kwargs, result)
                return result

        else:

            @functools.wraps(fn)
            def wrapper(*args, __audit_request: Request, **kwargs):
                result = fn(*args, **kwargs)
                _emit(__audit_request, kwargs, result)
                return result

        wrapper.__signature__ = new_sig  # type: ignore[attr-defined]
        return wrapper

    return decorator
