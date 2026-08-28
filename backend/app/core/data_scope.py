"""Row-level (country) data scope — the THIRD authorization axis.

``core/auth_deps.py`` answers two questions and neither of them is this one:

  * ``require_manager``       — a ROLE.        May you use /api/v1/admin?
  * ``enforce_module_access`` — a PAGE GROUP.  May you open the CS pages?

Both are gates on the REQUEST. They decide whether a handler runs at all, and
once it runs it returns the firm's whole book. This module adds the axis that
cuts the other way: given that you may open the page, WHICH ROWS are yours.

The row key is ``fxbackoffice.users.cid`` — the CRM company/country id.
Only two values exist today: 0 = CN, 1 = Global (confirmed with the desk,
2026-08-27, and the same mapping ``services/crm_risk_tag_client.py`` already
uses to pick a CRM tag string). Two CS colleagues work for the Global entity
and must never see CN client data; everybody else is unrestricted.

Why a hardcoded name list and not a ``users.allowed_cids`` column
----------------------------------------------------------------
For v1 there are exactly two people and the list changes roughly never. A code
constant produces a git diff and a review — somebody has to approve widening or
narrowing a colleague's data scope. The two obvious alternatives do not:

  * ``backend/.env`` is a SINGLE file shared by dev and prod (there is no
    separate prod .env), it is not in git, and an edit leaves no trace of who
    made it or when. Data scope is exactly the thing you want a trace of.
  * a DB column is the right answer at ~10 people with a management UI in front
    of it; at 2 people it is a schema migration, an admin screen and a fourth
    thing that can silently be NULL.

The dict is deliberately shaped ``email -> allowed cids``, not "the list of
Global-only people". Adding a CN-only colleague later is then ONE LINE and no
restructuring — the shape already says "these are the cids this person may
see" rather than encoding today's single policy into the variable name.

What lives here
---------------
  * ``DATA_SCOPE_OVERRIDES`` / ``caller_cids``  — who is restricted to what
  * ``require_cids_allowed``                    — the LOOKUP gate (403)
  * ``cid_for_crm_user_ids`` / ``cid_for_login``— the shared cid resolvers
  * ``scope_cache_suffix``                      — cache-key discriminator
  * ``ROUTE_SCOPE``                             — how each cs route is treated
  * ``SCOPED_MODULES`` / ``enforce_data_scope_coverage`` — the OUTER gate: a
    restricted caller who reaches a module this table does not cover is refused
    rather than served. Mounted on ``api_v1_router`` next to
    ``enforce_module_access``, and the reason widening somebody's modules
    cannot silently void their country restriction.

The resolvers are here rather than in each caller's service on purpose: two
copies of "how do I turn an id into a cid" drift, and the direction they drift
in is the one where the second copy forgets ``MAX_EXECUTION_TIME`` (see the
2026-08-09 / 08-15 replica incidents) or forgets to fail closed on a miss.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Iterable

import pymysql
import pymysql.cursors
from fastapi import HTTPException, Request, status

from app.core.auth_deps import classify_path, module_names
from app.core.auth_middleware import client_ip
from app.core.config import Settings, get_settings
from app.core.logging_config import get_logger
from app.core.users_db import get_users_db
from app.schemas.admin import MODULE_KEYS
from app.services.auth_service import SessionUser, record_auth_event

logger = get_logger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# The two cids that exist
# ─────────────────────────────────────────────────────────────────────────────

CID_CN = 0
CID_GLOBAL = 1

KNOWN_CIDS: frozenset[int] = frozenset({CID_CN, CID_GLOBAL})
"""Every cid the CRM actually issues today.

Spelled out so an UNEXPECTED value (a third entity, a NULL, a string that did
not cast) is distinguishable from a known one. It is treated as unresolvable,
and unresolvable means REFUSED for a restricted caller — see
``require_cids_allowed``. A new entity therefore presents as "these two people
cannot see the new company's clients", which is the safe direction, rather than
as "the filter quietly stopped matching".
"""

CID_LABELS: dict[int, str] = {CID_CN: "CN", CID_GLOBAL: "Global"}


# ─────────────────────────────────────────────────────────────────────────────
# The name list
# ─────────────────────────────────────────────────────────────────────────────

DATA_SCOPE_OVERRIDES: dict[str, frozenset[int]] = {
    # Both verified in the live backend/data/users.db on 2026-08-27:
    # role=user, allowed_modules=["cs"]. They work for the Global entity.
    "anson.zou@vn.kcmtrade.com": frozenset({CID_GLOBAL}),
    "rose.t@vn.kohlecapital.com": frozenset({CID_GLOBAL}),
}
"""LOWERCASE email -> the cids that person may see.

ABSENCE FROM THIS DICT MEANS UNRESTRICTED. That is the opposite default from
``allowed_modules`` (where the absence of a grant means "no"), and it is
deliberate: this is an exception list bolted onto a system where everybody has
always seen everything, not a permission system in its own right. Making the
default "restricted" would mean enumerating all ~30 colleagues here, and the
first person left off the list would silently lose half their data with no
error message to explain it.

Keys must be lowercase — ``caller_cids`` lowercases before lookup, so an
uppercase key here would simply never match and the restriction would be
silently void. There is a test for that.
"""


def verify_data_scope_overrides() -> None:
    """Boot-time check: does every listed address still name an ACTIVE user?

    The whole restriction hangs on a string comparison against ``users.email``
    — and this codebase decided in auth P3.5 that email is NOT an identity key,
    precisely because "email 会被改名，离职者信箱会分配给新人".
    ``upsert_user()`` matches on ``entra_oid`` and updates ``email`` in place, so
    a rename rewrites the row this dict is trying to match and NOTHING else
    changes: no error, no log, no red test. The person keeps their session,
    keeps their pages, and silently starts seeing CN clients again. The two
    listed colleagues sit on two different ``vn.*`` domains, so one domain
    consolidation is all it takes. A typo'd key looks identical from the inside,
    and this catches that too.

    Keying the dict on ``entra_oid`` instead was considered and rejected for v1:
    an oid is an opaque GUID, so the constant would stop being reviewable by the
    person approving the diff ("is this the right colleague?" becomes
    unanswerable) — and that reviewability is the entire reason the list is a
    code constant rather than a DB column. So the email key stays, and this
    check is what turns "silently void" into "loud at the next deploy".

    NEVER raises, and never blocks startup. ``backend/data/users.db`` is a
    bind mount shared by dev and prod; a locked, missing or half-migrated file
    must not stop the API from serving. A restriction that cannot be VERIFIED is
    a reason to shout, not a reason to refuse to boot — refusing would turn a
    reporting problem into an outage.

    Runs in EVERY worker rather than behind the scheduler flock, deliberately:

      * the healthy case is ONE line per process START, not per tick, so the
        OPT-0058 volume rule does not apply — four INFO lines per deploy;
      * the flock elects one worker to own the WRITES (retention sweep,
        schedulers). Hanging a read-only alarm off it would couple "does anybody
        notice the restriction is void" to an unrelated lock whose failure mode
        is SILENCE — which is the exact failure this function exists to remove.

    Four identical CRITICAL lines on a broken boot is the cheaper mistake.
    """
    if not DATA_SCOPE_OVERRIDES:
        return

    try:
        with get_users_db() as conn:
            rows = conn.execute(
                "SELECT lower(email) AS email FROM users WHERE status = 'active'"
            ).fetchall()
        active = {row["email"] for row in rows}
    except Exception:
        # Includes a locked db, a missing file and a schema that has not been
        # migrated yet. Loud, because "unverified" and "void" are
        # indistinguishable from the outside and both need a human.
        logger.critical(
            "DATA_SCOPE_OVERRIDES could NOT be verified against users.db. The "
            "country restriction on %d listed address(es) (%s) is UNVERIFIED — "
            "it matches on email, and an email that no longer exists silently "
            "means UNRESTRICTED.",
            len(DATA_SCOPE_OVERRIDES),
            ", ".join(sorted(DATA_SCOPE_OVERRIDES)),
            exc_info=True,
        )
        return

    missing = sorted(email for email in DATA_SCOPE_OVERRIDES if email not in active)
    for email in missing:
        logger.critical(
            "DATA_SCOPE_OVERRIDES lists %s but users.db has no ACTIVE user with "
            "that address. That person is CURRENTLY UNRESTRICTED: the row filter "
            "matches on email, so a rename (upsert_user updates users.email by "
            "entra_oid, by design) or a typo here voids the restriction with no "
            "error anywhere. Fix the key in core/data_scope.py — or, if they "
            "left, delete the entry so the list stops claiming a restriction it "
            "is not applying.",
            email,
        )
    if not missing:
        # Says the check RAN. Without it, "no CRITICAL at boot" is ambiguous
        # between "verified" and "the check was quietly deleted".
        logger.info(
            "DATA_SCOPE_OVERRIDES verified: %d listed address(es) match an "
            "active user in users.db",
            len(DATA_SCOPE_OVERRIDES),
        )


# ─────────────────────────────────────────────────────────────────────────────
# Who is the caller, and what may they see
# ─────────────────────────────────────────────────────────────────────────────

def caller_cids(request: Request) -> frozenset[int] | None:
    """The cids this caller may see. ``None`` means UNRESTRICTED (everything).

    ``None`` and ``frozenset()`` are NOT the same answer and must never be
    collapsed: ``None`` is "no restriction applies", the empty set would be "may
    see nothing at all". Same trap as ``allowed_modules`` ``["*"]`` vs ``[]``,
    and the same rule follows from it — never test this return value for
    truthiness, always test it against ``None`` explicitly.

    Two rules here are load-bearing and both differ from the module gate:

    1. **Managers are NOT auto-exempt.** ``caller_has_module()`` returns True
       for any manager, because managers are the people who GRANT modules and
       needing to grant yourself one first is a footgun with no upside. That
       reasoning does not transfer: this is not a grant somebody forgot to
       tick, it is a statement about which company's clients a person may look
       at. So the name list is consulted FIRST, before ``user.role`` is read at
       all. If one of the two listed people were ever promoted to manager — by
       a mis-click on /cfg/managers, or because their job changed — a
       manager-exempt gate would silently void their restriction, with no
       refusal logged anywhere to notice it by. Removing somebody's restriction
       has to be an edit to the dict above, i.e. a git diff.

    2. **``AUTH_ENABLED=false`` returns ``None`` for everybody, and this is a
       REAL HOLE — not a design choice.** With the kill switch off,
       ``AuthMiddleware`` sets ``request.state.user = None`` on its first line
       and returns before resolving a session, so there is no identity to match
       against the name list. The gate is not "relaxed" in that window, it is
       physically unable to run: there is nothing to compare. Anyone who can
       reach the API during that window sees every cid, and the API key is
       compiled into the public JS bundle. Do not read this branch as a policy;
       read it as one more reason the kill switch is a last resort. Since
       2026-08-27 the IdP-outage case has its own answer that DOES keep the
       gate working — break-glass login issues a real session, so
       ``request.state.user`` is populated and the name list applies normally.

    A third case is not a hole. With auth enabled and no subject
    (``request.state.user is None``) this also returns ``None``, but such a
    request cannot reach a scoped handler in the first place: every cs route is
    classified ``cs`` in ``MODULE_MAP``, and ``enforce_module_access`` refuses a
    subject-less caller with a 403 before the handler body runs. The branch
    exists so that a unit test, a scheduler job or a future INFRA path calling
    a scoped service does not crash on ``None.email``.
    """
    if not get_settings().AUTH_ENABLED:
        return None

    user: SessionUser | None = getattr(request.state, "user", None)
    if user is None:
        return None

    # Entra does not guarantee the case of the email it hands back, and the
    # value has round-tripped through SQLite since. Normalize both sides.
    email = (user.email or "").strip().lower()

    # Rule 1: the list wins over the role. Looked up BEFORE user.is_manager is
    # read — there is no manager branch below on purpose.
    return DATA_SCOPE_OVERRIDES.get(email)


def scope_cache_suffix(request: Request) -> str:
    """A short, stable discriminator to append to any Redis / SingleFlight key.

    ``"all"`` when unrestricted, otherwise ``"cid-1"`` (sorted, so the same
    scope always produces the same string).

    **This exists because a shared cache silently defeats the whole gate, and
    it is the single highest-risk bug in this change.**
    ``routes/fund_flow_monitor.py:_query_cache_key()`` hashes ONLY the request
    payload:

        canonical = json.dumps(payload, sort_keys=True, default=str)
        return f"app:fund_flow:query:{md5(canonical).hexdigest()}"

    Two callers who send the same filters therefore share one cache entry. Add
    row filtering without touching that key and the first unrestricted colleague
    to run a query warms the cache with the FULL firm-wide result — CN rows
    included — and the next restricted user sending the same filters is served
    that entry verbatim, from cache, without the filtered query ever running.
    The gate would test green in every unit test and leak in production, on a
    schedule set by whoever queried first.

    So: every cache key on a ``"filter"`` route must include this suffix, and
    that includes SingleFlight keys — coalescing two in-flight requests from
    differently-scoped callers onto one result is the same bug with a shorter
    window.
    """
    cids = caller_cids(request)
    if cids is None:
        return "all"
    return "cid-" + "-".join(str(c) for c in sorted(cids))


# ─────────────────────────────────────────────────────────────────────────────
# The lookup gate
# ─────────────────────────────────────────────────────────────────────────────

def _normalize_cid(value: object) -> int | None:
    """Coerce a raw DB value to a KNOWN cid, or ``None`` if it is not one.

    ``None`` out means "unresolvable", which the gate below treats as refused.
    Everything that is not recognisably 0 or 1 lands here: SQL NULL, a third
    entity id nobody told us about, a string the driver did not cast.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        cid = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return cid if cid in KNOWN_CIDS else None


_LOG_SAFE_MAX_CHARS = 120


def _log_safe(value: object, limit: int = _LOG_SAFE_MAX_CHARS) -> str:
    """Render a caller-supplied value as ONE log field. Never trust it verbatim.

    ``what`` reaches this module straight from a request body — ``/ib-data/query``
    takes ``ib_ids`` from the client and ``IBAnalyticsRequest._normalize_ids``
    only ``.strip()``s, so an id of ``"1<newline>WARNING  [-] fake line"``
    survives intact and renders as TWO lines in backend.log. That log is what
    ``/opt/myproject/morning-digest`` greps and what incident work reads: a
    forged line there is a forged fact, and it is forged inside the audit trail
    of an authorization refusal, i.e. exactly where somebody would want it.

    Sanitising HERE rather than at each call site is the point. There are four
    scoped routes today and there will be more; "remember to clean the string" is a
    rule that gets forgotten once and then never noticed, while a helper on the
    single line that does the logging cannot be forgotten by a future caller at
    all.

    ``str.isprintable()`` is False for every C0/C1 control character including
    newline, carriage return, tab and NUL, and True for CJK — so a Chinese
    client note survives and a framing character does not. The truncation is the
    second half: an id list is unbounded in length, and a 40 KB "id" would push
    real lines out of a rotated log just as effectively as forging one.
    """
    text = str(value)
    cleaned = "".join(ch if ch.isprintable() else " " for ch in text)
    if len(cleaned) > limit:
        cleaned = cleaned[:limit] + "...[truncated]"
    return cleaned


# ── refusal-log throttle (OPT-0058 rules applied to a REFUSAL) ────────────────
#
# `record_auth_event("permission_denied", ...)` is already throttled per person
# (10/min, auth_service._refusal_event_allowed) because every row contends for
# the same SQLite write lock as every live request's resolve_session(). The
# WARNING line next to it was not throttled at all, and it is the cheaper thing
# to produce and the more voluminous: enumerating `/ib-tree/{id}` over a dense
# two-valued id space (39,005 CN / 34,086 Global, no NULLs) writes one WARNING
# per request, and a log nobody can scan is a log nobody reads.
#
# What this must NOT become is silence. OPT-0058's rule is "only quieten the
# NORMAL case" — and a run of scope refusals is not the normal case, it is the
# single thing in this file worth waking somebody up for. So the shape is:
#
#   * FIRST refusal per person per minute -> WARNING, in full, immediately;
#   * the rest of that minute             -> DEBUG, counted, not lost;
#   * the next window's first line carries "+N suppressed" so the count is
#     always attached to a line somebody actually sees;
#   * `_REFUSAL_SUSTAINED_WINDOWS` busy minutes in a row -> ERROR, because a
#     refusal rate that stays high for minutes is what enumeration looks like
#     and is a different event from one colleague clicking one wrong client.
#
# Same escalation shape as burst_open_scheduler's FAST_SKIP_STALL_THRESHOLD:
# single occurrence = expected, a RUN of them = the fault.
_REFUSAL_LOG_WINDOW_SECONDS = 60.0
_REFUSAL_SUSTAINED_WINDOWS = 3
_REFUSAL_STATE_MAX_KEYS = 64


@dataclass
class _RefusalWindow:
    """Per-caller throttle state. All times are ``time.monotonic()`` seconds."""

    started: float
    seen: int          # refusals inside the current window, the logged one included
    suppressed: int    # refusals swallowed since the last line that was EMITTED
    busy_windows: int  # consecutive windows that held more than one refusal


_refusal_log_lock = threading.Lock()
_refusal_log_state: dict[str, _RefusalWindow] = {}


def _refusal_log_decision(key: str, now: float) -> tuple[int | None, int, int]:
    """Decide how to log one refusal. Returns ``(level, suppressed, busy_windows)``.

    ``level`` is ``None`` when this refusal must not produce a line of its own —
    the caller logs it at DEBUG instead, so full detail is still recoverable by
    turning LOG_LEVEL down without costing anything in prod.

    ``suppressed`` is how many refusals were swallowed since the last emitted
    line, and it is reported ON that next line rather than by a timer. A burst
    that stops dead therefore leaves its final few uncounted until the person
    refuses again — accepted, and the same property `_refusal_event_allowed`
    already has: the alternative is a background thread whose only job is to
    flush a counter.

    Keyed per PERSON, never globally: one colleague enumerating ids would
    otherwise consume the whole budget and suppress the FIRST refusal of the
    other, which is the one line nobody has seen before. Same reasoning, and the
    same key, as ``auth_service._throttle_key`` for ``permission_denied``.
    """
    with _refusal_log_lock:
        # Bounded by construction — only a caller whose email is in
        # DATA_SCOPE_OVERRIDES ever reaches here, so the key space is the size
        # of that dict. The cap is for the future caller who wires this to
        # something wider; recounting is cheaper than unbounded memory.
        if len(_refusal_log_state) > _REFUSAL_STATE_MAX_KEYS:
            _refusal_log_state.clear()

        state = _refusal_log_state.get(key)
        if state is not None and now - state.started < _REFUSAL_LOG_WINDOW_SECONDS:
            state.seen += 1
            state.suppressed += 1
            return None, 0, state.busy_windows

        suppressed = state.suppressed if state is not None else 0
        # "Consecutive" has to mean consecutive in TIME, not merely the previous
        # recorded window: a busy minute yesterday must not make today's first
        # refusal look like minute two of a run. Anything after a full idle
        # window starts the count over.
        if (
            state is None
            or state.seen <= 1
            or now - state.started >= _REFUSAL_LOG_WINDOW_SECONDS * 2
        ):
            busy = 0
        else:
            busy = state.busy_windows + 1
        _refusal_log_state[key] = _RefusalWindow(
            started=now, seen=1, suppressed=0, busy_windows=busy
        )
        level = (
            logging.ERROR if busy >= _REFUSAL_SUSTAINED_WINDOWS else logging.WARNING
        )
        return level, suppressed, busy


def require_cids_allowed(
    request: Request,
    cids: int | None | Iterable[int | None],
    *,
    what: str,
) -> None:
    """Refuse (403) if anything the caller asked for by id is outside their scope.

    This is the INPUT half of the design. A ``"filter"`` route narrows what it
    returns; a ``"lookup"`` route is handed an id and would otherwise answer
    about it regardless of whose client it is, so the check has to happen on the
    way IN. Call it after resolving the id(s) to cid(s) and BEFORE running the
    real query.

    ⚠ Passing this is not the whole answer when the response reaches beyond the
    id that was named — see the LOOKUP docstring below. The three IB routes are
    gated here AND filtered on the way out.

    Args:
        request: the live request, for the caller's scope and the refusal log.
        cids: one cid or an iterable of them, already resolved. ``None``
            entries mean "could not resolve" and are REFUSED for a restricted
            caller (see below).
        what: what the caller addressed, for the LOG only — e.g.
            ``"client 136017"`` or ``"login 1-8522845"``. Never put this in the
            response; see the leak note below.

    Fail closed on an unresolvable cid. ``None`` arrives when the id is not in
    the CRM at all, when the join found no row, or when the cid is neither 0 nor
    1. For an unrestricted caller that is simply not this function's business
    and it passes — the handler's own 404/empty-result path deals with it. For a
    RESTRICTED caller it is a refusal, because "I could not determine whose this
    is" must never resolve to "show it". A third entity appearing in the CRM
    would otherwise be visible to precisely the two people who are supposed to
    see the least.

    **403, never 401.** ``frontend/src/lib/fetch.ts`` reacts to 401 by calling
    ``notifyUnauthorized()``, which drops the client to anonymous and redirects
    to /login. Answering 401 for "you are logged in, this row is not yours"
    turns a permission error into an infinite bounce — click, get logged out,
    log back in, click, repeat. Same rule the module gate already follows, and
    the same rule nginx follows for /docs/ (401 -> login page, 403 -> a static
    page), for the same reason.

    The 403 message says what happened without saying whether the id EXISTS.
    "not in your data scope" is returned identically for a real CN client, a
    typo, and an id that was never issued — otherwise the refusal itself is an
    oracle: enumerate ids, keep the ones that come back 403-with-a-different-
    message, and you have reconstructed the CN client list you were not allowed
    to see.
    """
    allowed = caller_cids(request)
    if allowed is None:
        return  # Unrestricted: nothing to check, including unresolvable ids.

    values: list[int | None]
    # str/bytes are Iterable, and iterating one yields CHARACTERS. A caller that
    # hands over a stringly-typed id would otherwise be checked one character at
    # a time — which happens to refuse here, but for the wrong reason and only
    # by luck. Treat any scalar as a scalar.
    if cids is None or isinstance(cids, (int, str, bytes)):
        values = [cids]  # type: ignore[list-item]
    else:
        values = list(cids)

    refused = [v for v in values if _normalize_cid(v) not in allowed]
    if not refused:
        return

    user: SessionUser | None = getattr(request.state, "user", None)
    labels = "/".join(sorted(CID_LABELS[c] for c in allowed))

    # Throttled per person, first-of-window at WARNING, a run of busy windows at
    # ERROR — see _refusal_log_decision. `refused` is a list of ints/None built
    # by _normalize_cid, so it is interpolated as-is; everything a caller can
    # write goes through _log_safe. That includes the path: uvicorn percent-
    # DECODES it into the ASGI scope, and while urlsplit() strips CR/LF/TAB out
    # of `request.url.path`, it strips NOTHING else — an ANSI escape (`%1B[2J`)
    # or a NUL survives into the log file intact.
    level, suppressed, busy_windows = _refusal_log_decision(
        (user.email if user else None) or client_ip(request) or "-",
        time.monotonic(),
    )
    message = (
        "Data scope refused: %s %s email=%s scope=%s asked_for=%s cids=%s "
        "client=%s%s%s"
    )
    args = (
        request.method,
        _log_safe(request.url.path),
        _log_safe(user.email if user else "-"),
        sorted(allowed),
        _log_safe(what),
        refused,
        client_ip(request),
        f" (+{suppressed} suppressed since the previous line)" if suppressed else "",
        (
            f" — SUSTAINED: {busy_windows} consecutive minutes of refusals from "
            "this caller. One wrong client id is a mis-click; a rate that holds "
            "for minutes is somebody walking the id space."
        )
        if level == logging.ERROR
        else "",
    )
    if level is None:
        # Not lost, just not in the prod log: the count reaches the next emitted
        # WARNING/ERROR, and LOG_LEVEL=DEBUG recovers every individual line.
        logger.debug(message, *args)
    else:
        logger.log(level, message, *args)
    record_auth_event(
        "permission_denied",
        email=user.email if user else None,
        detail=f"data_scope:{sorted(allowed)}:{request.url.path}"[:200],
        ip=client_ip(request),
        ua=request.headers.get("User-Agent"),
    )
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail=f"该查询对象不在你的数据范围内（仅 {labels}）",
    )


# ─────────────────────────────────────────────────────────────────────────────
# cid resolvers
# ─────────────────────────────────────────────────────────────────────────────
#
# One connection path for every query in this module, built from the golden
# template in the `db-timeout-guard` skill. It is NOT
# `login_ip_enrichment_service._connect_fxbackoffice()`, and that is a
# deliberate departure from "reuse the existing helper": that one carries none
# of the three defences below — no connect_timeout, no read_timeout, no
# MAX_EXECUTION_TIME. Importing it would make this module the fourth place in
# the company to re-learn the 2026-08-09 lesson (2,637 zombie threads on the
# replica in 14.5h) and the second place in a month (sales-belong-autofill,
# 08-15). The credentials and database name are read from exactly the same
# Settings fields, so the two are interchangeable in every respect that is not
# a timeout.

_MAX_EXECUTION_TIME_MS = 5000
"""Server-side statement kill switch, strictly BELOW read_timeout.

Deliberately much lower than open_positions_service's 15s: both queries here
are point lookups on indexed columns for a handful of ids and run INSIDE a
request the user is waiting on. If one takes 5 seconds the replica is already
in trouble and the right answer is to give up, not to hold an MDL slot open.

Client-side read_timeout is only the backstop. A read_timeout that fires
abandons the SOCKET, not the query: the server thread keeps waiting (typically
queued behind a metadata lock) as a permanent zombie holding that same lock.
Only MAX_EXECUTION_TIME makes the SERVER stop.
"""


def _connect(settings: Settings) -> pymysql.connections.Connection:
    """Open a read-only connection to the fxbackoffice replica.

    autocommit=True is not decoration: with the DB-API default of False the
    first SELECT opens a transaction that holds metadata locks until COMMIT,
    while showing up in PROCESSLIST as a harmless-looking ``Sleep``. That is
    verbatim the kcm-risk-pipeline failure of 2026-08-09.
    """
    conn = pymysql.connect(
        host=settings.DB_HOST,
        user=settings.DB_USER,
        password=settings.DB_PASSWORD,
        database=settings.FXBACK_DB_NAME,
        port=int(settings.DB_PORT),
        charset=settings.DB_CHARSET,
        cursorclass=pymysql.cursors.DictCursor,
        connect_timeout=5,
        read_timeout=20,
        autocommit=True,
    )
    try:
        with conn.cursor() as cur:
            cur.execute(f"SET SESSION MAX_EXECUTION_TIME = {_MAX_EXECUTION_TIME_MS}")
    except Exception:
        conn.close()
        raise
    return conn


# Same reason as login_ip_enrichment_service: keep the IN-clause well under
# max_allowed_packet. Nothing here is expected to approach it, but a caller
# that hands over a whole page of ids should not have to know that.
_IN_CLAUSE_CHUNK = 1000


def _result_key(raw: object) -> int | str:
    """The key ``cid_for_crm_user_ids`` files an input id under.

    ``int(raw)`` when it parses, so ``123`` and ``"123"`` collapse onto ONE key
    and a caller holding either spelling can look the answer up. Otherwise the
    raw value itself, so an id that is not a number at all still APPEARS in the
    result (mapped to ``None``) rather than vanishing from it.

    The ``repr`` fallback is for an unhashable input — a nested list arriving
    from a JSON body. It cannot be a dict key, and silently dropping it is the
    exact bug this function exists to not have, so it becomes a key that is at
    least present and at least traceable in a log line.
    """
    try:
        return int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        pass
    try:
        hash(raw)
    except TypeError:
        return repr(raw)
    return raw  # type: ignore[return-value]


def cid_for_crm_user_ids(
    settings: Settings, ids: Iterable[int | str]
) -> dict[int | str, int | None]:
    """Resolve CRM user ids -> cid. Ids with no usable answer map to ``None``.

    **EVERY id passed in comes back as a key**, so a caller may iterate
    ``resolved.values()`` and be sure it has seen an answer for each of its own
    inputs. That property is the whole safety argument for this function and it
    was broken until 2026-08-27: unparseable ids were ``continue``d over, so
    they never became keys, so ``values()`` never mentioned them, so a gate fed
    from ``values()`` passed an id it had never actually checked. That is not
    theoretical — ``IBAnalyticsRequest.ib_ids`` is ``List[str]`` and its
    validator only strips blanks, so ``"abc"`` reaches ``/ib-data/query``
    intact. The absent key was the fail-OPEN direction, which is why the code
    now matches the safe reading rather than the docstring being softened to
    match the code.

    ``None`` covers every miss at once: id not in the CRM, cid NULL, cid not in
    ``KNOWN_CIDS``, and now "not a number". ``require_cids_allowed`` refuses all
    of them for a restricted caller, so one fail-closed rule covers the lot.

    Keys are ints for anything int-parseable (``123`` and ``"123"`` land on the
    same key), and the raw value otherwise — see ``_result_key``. A caller that
    normalises its own input to int and does ``resolved.get(my_int)`` keeps
    working unchanged; the extra keys can only ADD refusals, never remove one.

    Parameterised placeholders, never string interpolation. The ids reaching
    here come from request bodies (``/ib-data/query`` takes ``ib_ids`` straight
    from the client), and this is an authorization check — an injection here
    does not just leak data, it edits the question that decides who may see it.
    """
    # Ordered de-dupe over the KEYS, so "123" and 123 in the same payload are
    # one entry and one placeholder rather than two.
    resolved: dict[int | str, int | None] = {}
    for raw in ids:
        resolved.setdefault(_result_key(raw), None)

    # Only int keys can be looked up in the CRM; the rest are already answered
    # (None = unresolvable = refused for a restricted caller).
    wanted = [k for k in resolved if isinstance(k, int)]
    if not wanted:
        return resolved

    conn = _connect(settings)
    try:
        with conn.cursor() as cur:
            for start in range(0, len(wanted), _IN_CLAUSE_CHUNK):
                chunk = wanted[start : start + _IN_CLAUSE_CHUNK]
                placeholders = ", ".join(["%s"] * len(chunk))
                cur.execute(
                    f"SELECT id, cid FROM fxbackoffice.users "
                    f"WHERE id IN ({placeholders})",
                    tuple(chunk),
                )
                for row in cur.fetchall():
                    resolved[int(row["id"])] = _normalize_cid(row["cid"])
    finally:
        try:
            conn.close()
        except Exception:
            pass

    return resolved


def cid_for_login(settings: Settings, sid: str, login: str) -> int | None:
    """Resolve an MT account -> its owner's cid. ``None`` when unresolvable.

    ``sid`` + ``login`` rather than a single argument because that is how the
    callers hold it: ``/ibid-lots/query`` takes ``server_sid`` and ``target_id``
    as separate fields. They are joined here into the house compound key
    ``loginSid = '{SID}-{LOGIN}'`` (e.g. ``1-8522845``) so no caller has to
    remember the format — ``mt4_users.loginSid`` is UNIQUE, which is what makes
    the LIMIT 1 below honest rather than arbitrary.

    Errors are NOT swallowed into ``None``. A replica hiccup would then read as
    "this account belongs to nobody", which for a restricted caller is a 403
    that looks like a permission bug and for an unrestricted one is a silent
    wrong answer. Let it raise; the route's own error handler turns it into a
    500, which is the truth.
    """
    login_sid = f"{str(sid).strip()}-{str(login).strip()}"

    conn = _connect(settings)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT u.cid AS cid "
                "FROM fxbackoffice.mt4_users mu "
                "INNER JOIN fxbackoffice.users u ON u.id = mu.userId "
                "WHERE mu.loginSid = %s "
                "LIMIT 1",
                (login_sid,),
            )
            row = cur.fetchone()
    finally:
        try:
            conn.close()
        except Exception:
            pass

    return _normalize_cid(row["cid"]) if row else None


# ─────────────────────────────────────────────────────────────────────────────
# Route classification
# ─────────────────────────────────────────────────────────────────────────────

FILTER = "filter"
"""Returns a LIST of client rows -> the rows must be narrowed to the caller's cids.

Also: every cache key on such a route must carry ``scope_cache_suffix()``, or
the filtering is defeated by the first unrestricted caller to warm the cache.
"""

LOOKUP = "lookup"
"""Addresses ONE object by id -> gate the INPUT with ``require_cids_allowed``.

The caller already named the thing, so the decision about THAT object can only
be made before the query runs — answering afterwards means having paid for the
answer, and on these routes the query is the expensive part.

⚠ "lookup" describes the INPUT, and does NOT imply there is nothing to filter on
the way out. It said exactly that until 2026-08-27, and it was wrong for all
three IB routes: the id names one object but the RESPONSE fans out to the other
side of the relationship — ``/ibid-lots/query`` and ``/ib-data/query`` return
(or aggregate) the target's whole DOWNLINE, ``/ib-tree/{client_id}`` returns the
client's UPLINE chain. A cold review confirmed on the replica that 11 Global IBs
have at least one CN downline client, and 11 tree EDGES put a Global client
under a CN IB (3 distinct clients — the row count is 11 in BOTH directions,
which is exactly how an earlier draft of this comment reported "11 clients";
count DISTINCT, not rows, when you re-measure), so
the input gate passed and CN data came back with a 200. Those three now filter
on the way out as well (see the notes on their ROUTE_SCOPE entries).

The general rule this leaves: an id gate is sufficient only when the response is
about the named object ALONE. Before classifying a new route LOOKUP, look at
what its response actually contains, not at what its parameter is.
"""

OPEN = "open"
"""Carries no client-level data, or is deliberately left unrestricted.

"Deliberately" is doing real work in that sentence — see the /login-ip note
below. An OPEN entry is a decision that was made, not a route nobody got to.
"""

ROUTE_SCOPE: dict[str, str] = {
    # ── /cs/fund-flow ────────────────────────────────────────────────────────
    "/cs/fund-flow/query": FILTER,
    "/cs/fund-flow/snapshot/latest": FILTER,
    # FILTER, not OPEN, and this one is easy to get wrong by reading the name:
    # scan-now RETURNS the resulting snapshot (`_snapshot_to_model(result)`), it
    # is not a trigger that answers with an ack. The SCAN itself stays
    # firm-wide — a restricted user kicking off a scan must not produce a
    # snapshot that is missing CN alerts for everyone else — so it is only the
    # RESPONSE that gets scoped.
    "/cs/fund-flow/scan-now": FILTER,
    "/cs/fund-flow/export": FILTER,
    # FILTER because of an AGGREGATE, not because of the rows:
    # `FundFlowScanBatch.total_alerts` is a firm-wide count. Leave it whole and
    # a restricted user subtracts the rows they can see from it and recovers
    # exactly the CN count — the number they were not supposed to have. A
    # filtered list next to an unfiltered total is a subtraction away from
    # being no filter at all.
    "/cs/fund-flow/scans": FILTER,
    # LOOKUP: one client addressed by id in the path.
    "/cs/fund-flow/detail/{user_id}": LOOKUP,
    # OPEN: detection thresholds, not client data. Note the module gate is the
    # only thing in front of this and it is a WRITE — that is the pre-existing
    # "any signed-in user may change risk configuration" position (audited, not
    # authorized, per the user's 2026-08-14 decision), unchanged here.
    "/cs/fund-flow/config": OPEN,

    # ── /ib-tree ─────────────────────────────────────────────────────────────
    # LOOKUP: one client id in the path. The response is that client's UPLINE
    # chain (not downline — /ibid-lots and /ib-data are the downline ones), so
    # the gate goes on the way in AND the service masks out-of-scope ancestors
    # on the way out.
    "/ib-tree/{client_id}": LOOKUP,

    # ── /ibid-lots ───────────────────────────────────────────────────────────
    # LOOKUP even though it is spelled "query": the payload always names ONE
    # target (`target_id`). Which resolver applies depends on `query_type` —
    # "login" needs `cid_for_login(settings, server_sid, target_id)`, the other
    # four modes address a CRM user id and need `cid_for_crm_user_ids`.
    "/ibid-lots/query": LOOKUP,

    # ── /ib-data ─────────────────────────────────────────────────────────────
    # LOOKUP: `ib_ids` is a caller-supplied list of CRM ids. A list, but every
    # element is still named by the caller, so it is the input that gets
    # gated — pass the whole resolved set to `require_cids_allowed` at once.
    # ⚠ Classified {cs, data} in MODULE_MAP: the same endpoint backs
    # /warehouse/ib-data too. The scope check keys off the PERSON, not the
    # page, so a Data-module colleague is unaffected.
    "/ib-data/query": LOOKUP,
    # OPEN: a timestamp of the last warehouse refresh. No client data at all.
    "/ib-data/last-run": OPEN,

    # ── /login-ip — ALL OPEN, by an explicit decision (2026-08-27) ────────────
    # Not an oversight and not "we will get to it". The whole value of this
    # module is CROSS-ACCOUNT IP CORRELATION: it answers "which other accounts
    # signed in from this address". Filter the rows by cid and the correlated
    # PEER silently disappears from the answer — the report does not get
    # smaller, it gets WRONG. A shared-IP cluster of one CN and one Global
    # account would render as a Global account with no peers, which is the
    # exact opposite of what the page is for, and it would look like a clean
    # result rather than a redacted one.
    #
    # A misleading security report is worse than a permissive one, so the user
    # accepted the trade: these two colleagues can see CN clients' login-IP
    # data. Revisit only with a way to say "there are N more matches you may
    # not see" — a redaction the reader can SEE is a different thing from a
    # filter they cannot.
    #
    # 13 paths / 16 route objects (watchlist and mail/recipients each carry
    # several verbs); ROUTE_SCOPE is keyed by path, so the count differs from
    # the route count on purpose.
    "/login-ip/available-dates": OPEN,
    "/login-ip/report": OPEN,
    "/login-ip/last-trade-ip": OPEN,
    "/login-ip/watchlist": OPEN,
    "/login-ip/watchlist/{row_id}": OPEN,
    "/login-ip/scheduler/runs": OPEN,
    "/login-ip/scheduler/run-now": OPEN,
    "/login-ip/search": OPEN,
    "/login-ip/mail/recipients": OPEN,
    "/login-ip/mail/recipients/{recipient_id}": OPEN,
    "/login-ip/export/tasks": OPEN,
    "/login-ip/export/tasks/{task_id}": OPEN,
    "/login-ip/export/tasks/{task_id}/download": OPEN,
}

SCOPE_VALUES: frozenset[str] = frozenset({FILTER, LOOKUP, OPEN})

# Import-time guard, same shape as the MODULE_MAP assertions in auth_deps.py: a
# typo'd value ("lookups") would otherwise sit in the table looking correct and
# simply never match either branch of whatever wires this in.
assert set(ROUTE_SCOPE.values()) <= SCOPE_VALUES, (
    f"ROUTE_SCOPE holds values that are not one of {sorted(SCOPE_VALUES)}: "
    f"{sorted(set(ROUTE_SCOPE.values()) - SCOPE_VALUES)}"
)


SCOPED_MODULES: frozenset[str] = frozenset({"cs"})
"""The modules ROUTE_SCOPE actually covers — i.e. where this gate is IMPLEMENTED.

Today: `cs` only, because that is where the two restricted colleagues work and
that is the module whose 24 routes have been classified one by one.

⚠ **Adding a key here is the LAST step, not the first.** It is a claim that
every live route of that module has a ROUTE_SCOPE entry and that every "filter"
route among them really filters (cache keys included). Adding `data` here
before doing that work would not fail — it would just re-open the exact hole
`enforce_data_scope_coverage` below exists to close, and re-open it silently.
An anti-drift test asserts that EVERY live route of each key here is classified
in ROUTE_SCOPE — not merely that one of them is, which would let somebody add
"data" on the strength of the single shared /ib-data/query entry while
region-query stayed unclassified. So
the constant cannot claim coverage the table does not have; it cannot check the
filtering is CORRECT, which is why this comment is here.
"""

assert SCOPED_MODULES <= set(MODULE_KEYS), (
    f"SCOPED_MODULES names things that are not grantable modules: "
    f"{sorted(SCOPED_MODULES - set(MODULE_KEYS))}"
)


def path_is_scope_covered(path: str) -> bool:
    """Does ROUTE_SCOPE speak for this path?

    True when the path's module policy INTERSECTS ``SCOPED_MODULES``, not when
    it is a subset of it. The difference is `/ib-data/query`, classified
    ``{cs, data}`` because the same endpoint backs a CS page and a Data page: it
    IS in ROUTE_SCOPE and it IS wired, so a subset test would refuse a path that
    is fully covered.

    Intersection is also exactly the selector the anti-drift test uses to decide
    which routes ROUTE_SCOPE must contain (`"cs" in module_names(policy)`). That
    is deliberate and worth keeping: the runtime gate and the test then agree by
    construction, so a future `{cs, risk}` endpoint that nobody classified turns
    the suite red instead of being waved through here.

    Pseudo-modules (INFRA / COMMON / MANAGER) yield an empty ``module_names()``
    and are covered — see the gate's docstring for why they must pass.
    """
    policy = classify_path(path)
    if policy is None:
        return False
    modules = module_names(policy)
    if not modules:
        return True
    return bool(SCOPED_MODULES.intersection(modules))


def enforce_data_scope_coverage(request: Request) -> None:
    """Refuse a RESTRICTED caller on any path this gate does not cover yet.

    Fail closed on the axis, not just within it. ROUTE_SCOPE covers `cs`, and
    both restricted colleagues hold ``allowed_modules == ["cs"]``, so today the
    two agree. Nothing keeps them agreeing: the day a manager ticks `data` for
    one of them in /cfg/managers, they reach `/ib-data/region-query` — the
    firm-wide CN/Global roll-up, i.e. the single most direct answer to the one
    question their restriction exists to prevent — plus every other data and
    risk endpoint, entirely unscoped. Nothing would error, nothing would log,
    the name list would still be there and would still look like it was working.
    The restriction would simply have stopped meaning anything, silently, as a
    side effect of a checkbox ticked by somebody who has never heard of this
    file.

    That is the failure mode this dependency removes: widening a restricted
    person's modules can now only ever produce a LOUD 403, never a quiet leak.

    Mounted once on ``api_v1_router``, immediately AFTER
    ``enforce_module_access`` in the same dependency list. The order is
    load-bearing in one direction only: FastAPI runs them in sequence, so a
    caller who was never granted `data` is refused by the module gate first and
    never produces the ERROR log below. Reaching this gate therefore means
    something worth an ERROR actually happened — a grant exists that this axis
    cannot honour — rather than "somebody clicked a page they do not have".

    The order of the checks:

    1. **``caller_cids(request) is None`` -> return, immediately.** The
       overwhelming majority of requests, and the answer is one dict lookup on
       an email — no DB, no path parsing. This early return also carries the
       kill switch: ``caller_cids`` returns ``None`` for everybody when
       ``AUTH_ENABLED`` is false, because ``AuthMiddleware`` never resolved a
       subject, so there is nothing here to judge. Deliberately NOT a second
       ``get_settings().AUTH_ENABLED`` test — one place decides what "no
       restriction applies" means, and a second copy would be free to drift
       into disagreeing with the gate it is supposed to mirror. The kill switch
       must pass everything for the same reason ``enforce_module_access`` says
       so: refusing during that window would make the switch lock people out
       harder than whatever it was thrown to undo.
    2. **Pseudo-modules pass** (handled inside ``path_is_scope_covered``). A
       restricted person still has to be able to log in (``/auth`` is INFRA) and
       still needs ``/view-profiles`` (COMMON) or DashboardLayout's
       unconditional ``useProfileAutoSave()`` breaks the app SHELL — every page,
       including the one telling them they have no modules. Refusing these would
       present as "the app is down for those two people".
    3. **Covered -> pass.** ROUTE_SCOPE speaks for the path; the route's own
       filter/lookup wiring is what narrows it.
    4. **Everything else -> 403 + ERROR.** Includes an unclassified path, which
       ``enforce_module_access`` should already have refused; keeping the branch
       means this gate does not depend on the ordering above being right.

    403, never 401 — a 401 sends ``frontend/src/lib/fetch.ts`` into
    ``notifyUnauthorized()`` and an infinite login bounce. Same rule as both
    older gates.

    The message names the SITUATION rather than the rule, because the person who
    hits it is a colleague who was just granted a module and finds it does not
    work. "Contact IT" is the correct action for them; the ERROR log is what
    tells IT which of the two fixes is needed — classify that module's routes,
    or lift the restriction.
    """
    cids = caller_cids(request)
    if cids is None:
        return

    if path_is_scope_covered(request.url.path):
        return

    user: SessionUser | None = getattr(request.state, "user", None)
    email = user.email if user else "-"

    # Throttled per person on the SAME mechanism as require_cids_allowed, and
    # for the same reason: a restricted colleague who was granted an uncovered
    # module hits this on EVERY request, including the SPA's background polls,
    # so an unthrottled line here is the OPT-0058 firehose with a rarer trigger.
    #
    # Two deliberate differences from the refusal log:
    #   * The key is namespaced ("coverage:"), so a burst of ordinary id
    #     refusals cannot consume the budget and swallow the first line of a
    #     genuine misconfiguration. They are different events with different
    #     fixes and must not share a window.
    #   * The first line of a window is ERROR, not WARNING. This is never a
    #     normal state — reaching here means a module grant and this gate have
    #     gotten out of sync — so there is no "expected" tier to demote it to.
    level, suppressed, _busy = _refusal_log_decision(
        f"coverage:{email}", time.monotonic()
    )
    logger.log(
        logging.ERROR if level is not None else logging.DEBUG,
        "Data scope has no coverage for %s %s (email=%s scope=%s). This caller is "
        "restricted by DATA_SCOPE_OVERRIDES but the path's module is outside "
        "SCOPED_MODULES=%s, so the country filter cannot be applied — refusing "
        "(fail closed). A module grant and this gate are out of sync: either "
        "classify that module's routes in ROUTE_SCOPE (core/data_scope.py) and "
        "add it to SCOPED_MODULES, or take the module back off this person in "
        "/cfg/managers.%s",
        request.method,
        # Caller-controlled text like any other: percent-decoded by uvicorn, and
        # urlsplit() removes only CR/LF/TAB from it — see _log_safe.
        _log_safe(request.url.path),
        _log_safe(email),
        sorted(cids),
        sorted(SCOPED_MODULES),
        f" (+{suppressed} suppressed since the previous line)" if suppressed else "",
    )
    record_auth_event(
        "permission_denied",
        email=user.email if user else None,
        detail=f"data_scope_uncovered:{request.url.path}"[:200],
        ip=client_ip(request),
        ua=request.headers.get("User-Agent"),
    )
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="该模块尚未支持数据范围限制，请联系 IT",
    )
