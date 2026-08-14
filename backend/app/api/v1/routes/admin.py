"""Manager-only user administration (auth P4a) — /api/v1/admin.

Thin routing over ``services/admin_service`` (HTTP only, no business logic and
no SQL here). Contract: docs/architecture/auth-p4-process.md §2.

⚠ These endpoints live under ``/api/v1/admin`` and NOT in ``routes/auth.py``,
even though "change a role" feels like an auth concern. Until P4.0 the session
middleware exempted the whole ``/api/v1/auth/`` prefix, so any route added to
that file was born unauthenticated — and these are the most privileged routes
in the system. The prefix is now an exact-path set and ``test_app_assembly.py``
asserts both halves: nothing under ``/api/v1/admin`` may be exempt, and no
seventh route may appear under ``/api/v1/auth``. Moving anything here into that
file would turn both assertions red, which is the intent.

The manager check is declared on the router itself rather than per handler, so
a new endpoint added to this file is guarded by default instead of by memory.
Handlers that write also take the dependency in their signature to get the
acting subject for the audit trail; FastAPI caches the dependency per request,
so it still runs exactly once.

Handlers are ``def``, not ``async def`` (CLAUDE.md): the service layer does
synchronous SQLite, which on the event loop would serialise every concurrent
request behind it.
"""

from __future__ import annotations

import time

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.core.auth_deps import require_manager
from app.schemas.admin import (
    MODULE_CATALOGUE,
    AdminSession,
    AdminUser,
    AuditEntry,
    AuthEvent,
    UpdateUserRequest,
)
from app.services import admin_service as svc
from app.services.auth_service import SessionUser

router = APIRouter(prefix="/admin", dependencies=[Depends(require_manager)])


def _ms(t0: float) -> int:
    return int((time.time() - t0) * 1000)


def _envelope(data: list, t0: float, *, page: int = 1, page_size: int | None = None,
              total: int | None = None) -> dict:
    """The project response envelope: {data, total, page, page_size, ...}."""
    resolved_total = len(data) if total is None else total
    resolved_size = page_size if page_size is not None else max(len(data), 1)
    return {
        "data": data,
        "total": resolved_total,
        "page": page,
        "page_size": resolved_size,
        "total_pages": svc.total_pages(resolved_total, resolved_size),
        "statistics": {"query_time_ms": _ms(t0)},
    }


def _http(exc: svc.AdminError) -> HTTPException:
    """Map a service refusal onto a status code.

    Never 401 for any of these. ``lib/fetch.ts`` treats 401 as "the session
    died" and redirects to /login, so a business refusal answered with 401
    logs the manager out instead of telling them why the edit was rejected.
    """
    if isinstance(exc, svc.UserNotFound):
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    if isinstance(exc, svc.SelfModification):
        return HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc))
    # LastActiveManager / OtpRoleForbidden: the request is well-formed, it is
    # the current state of the world that forbids it. 409 rather than 422 so
    # the frontend can tell "you typed something invalid" (fix the form) from
    # "this is not allowed right now" (show the reason).
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))


# ── users ────────────────────────────────────────────────────────────────────

@router.get("/users")
def get_users() -> dict:
    t0 = time.time()
    data = [AdminUser(**u).model_dump() for u in svc.list_users()]
    return _envelope(data, t0)


@router.patch("/users/{user_id}")
def patch_user(
    user_id: int,
    payload: UpdateUserRequest,
    actor: SessionUser | None = Depends(require_manager),
) -> AdminUser:
    """Change role / status / allowed_modules. Guardrails live in the service.

    Returns the bare updated AdminUser, not the list envelope — §2.2 of the
    contract, which the frontend is written against.

    Absent fields are passed as ``UNSET`` rather than None. This is the seam
    where "clear this person's modules" (``[]``) and "give this person every
    module" (``null``) would otherwise collapse into the same call — Pydantic
    represents both as None, and only ``model_fields_set`` still knows which
    one the manager clicked.
    """
    try:
        data = svc.update_user(
            user_id,
            role=payload.role if payload.touches("role") else svc.UNSET,
            status=payload.status if payload.touches("status") else svc.UNSET,
            allowed_modules=(
                payload.allowed_modules if payload.touches("allowed_modules") else svc.UNSET
            ),
            actor=actor,
        )
    except svc.AdminError as exc:
        raise _http(exc) from exc
    return AdminUser(**data)


# ── sessions ─────────────────────────────────────────────────────────────────

@router.get("/users/{user_id}/sessions")
def get_user_sessions(user_id: int) -> dict:
    t0 = time.time()
    try:
        rows = svc.list_user_sessions(user_id)
    except svc.AdminError as exc:
        raise _http(exc) from exc
    return _envelope([AdminSession(**r).model_dump() for r in rows], t0)


@router.delete("/users/{user_id}/sessions")
def delete_user_sessions(
    user_id: int, actor: SessionUser | None = Depends(require_manager)
) -> dict:
    """Sign one user out of every device."""
    try:
        return {"revoked": svc.revoke_user_sessions(user_id, actor=actor)}
    except svc.AdminError as exc:
        raise _http(exc) from exc


@router.delete("/sessions/{sid_hash}")
def delete_session(
    sid_hash: str, actor: SessionUser | None = Depends(require_manager)
) -> dict:
    """Sign one device out. ``sid_hash`` is the sha256 the list endpoint gave."""
    return {"revoked": svc.revoke_session(sid_hash, actor=actor)}


# ── catalogues and logs ──────────────────────────────────────────────────────

@router.get("/modules")
def get_modules() -> dict:
    """The module catalogue the permission checkboxes are rendered from.

    Served rather than hard-coded in the SPA so there is exactly one list to
    change when a module is added — the frontend keeps its own copy for sidebar
    filtering (P4b), and this endpoint is what keeps that copy honest.
    """
    return {"data": [m.model_dump() for m in MODULE_CATALOGUE]}


@router.get("/auth-events")
def get_auth_events(
    page: int = Query(1, ge=1),
    page_size: int = Query(svc.DEFAULT_PAGE_SIZE, ge=1, le=svc.MAX_PAGE_SIZE),
    email: str | None = None,
    event: str | None = None,
) -> dict:
    t0 = time.time()
    rows, total = svc.list_auth_events(
        page=page, page_size=page_size, email=email, event=event
    )
    data = [AuthEvent(**r).model_dump() for r in rows]
    return _envelope(data, t0, page=page, page_size=page_size, total=total)


@router.get("/audit-log")
def get_audit_log(
    page: int = Query(1, ge=1),
    page_size: int = Query(svc.DEFAULT_PAGE_SIZE, ge=1, le=svc.MAX_PAGE_SIZE),
) -> dict:
    t0 = time.time()
    rows, total = svc.list_audit_log(page=page, page_size=page_size)
    data = [AuditEntry(**r).model_dump() for r in rows]
    return _envelope(data, t0, page=page, page_size=page_size, total=total)
