from __future__ import annotations

import logging
import time

from fastapi import APIRouter, Depends, HTTPException, Path, Request, status

from ....core.config import Settings, get_settings
from ....core.data_scope import caller_cids, cid_for_crm_user_ids, require_cids_allowed
from ....schemas.ib_tree import IBTreeResponse
from ....services.ib_tree_service import query_ib_tree

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/ib-tree")


# Deliberately sync (`def`, not `async def`): pymysql blocks, and FastAPI
# runs sync handlers in the threadpool so a hung slave connection cannot
# freeze the event loop for every other endpoint.
@router.get("/{client_id}", response_model=IBTreeResponse, status_code=status.HTTP_200_OK)
def get_ib_tree(
    request: Request,
    client_id: int = Path(ge=1),
    settings: Settings = Depends(get_settings),
):
    """Return the IB hierarchy chain (sales > upper IBs > direct IB > client) for a client."""
    try:
        # Row-level (country) data scope. A LOOKUP route in
        # data_scope.ROUTE_SCOPE: the caller named the client, and the response
        # is that client's whole upline/downline chain, so there is nothing to
        # narrow on the way out. Gate the INPUT or not at all.
        #
        # The `caller_cids(...) is not None` guard is not a micro-optimisation:
        # without it every ib-tree lookup in the system pays an extra MySQL
        # round-trip to the replica so that two people can be restricted. An
        # unrestricted caller must reach query_ib_tree() byte-identically to
        # before this gate existed.
        scope = caller_cids(request)
        if scope is not None:
            # Runs BEFORE query_ib_tree(), which walks the whole IB chain on the
            # replica. Refusing after paying for the answer would still leak it
            # into the query log and cost the same seconds.
            #
            # `.get()` rather than `[...]`: an unresolvable id (not in the CRM,
            # NULL cid, an entity nobody told us about) yields None, and
            # require_cids_allowed refuses None for a restricted caller. That is
            # deliberate — a restricted caller must get 403 here, never the 404
            # below, or the status code itself distinguishes "exists but is CN"
            # from "does not exist" and becomes an enumeration oracle.
            resolved = cid_for_crm_user_ids(settings, [client_id])
            require_cids_allowed(
                request, resolved.get(client_id), what=f"client {client_id}"
            )

        started = time.perf_counter()
        # ...and the OUTPUT half, which the input gate above cannot cover. The
        # gate checked the client the caller named; the response is that
        # client's UPLINE, and 11 tree edges (3 distinct clients) put a Global
        # client under a CN IB. Passing the
        # scope down lets the service MASK those nodes (identity only — the
        # chain keeps its shape, because a chain with a hole in it is worse
        # than useless for CS work). `scope is None` for everyone else and the
        # service then behaves exactly as before.
        result = query_ib_tree(settings, client_id, allowed_cids=scope)
        if result is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"未找到客户 {client_id}",
            )
        result.query_time_ms = round((time.perf_counter() - started) * 1000, 2)
        return result
    except HTTPException:
        raise
    except Exception as exc:
        # Full details go to the log only — this page's output gets pasted
        # to external parties, so never echo internals to the client.
        logger.error(f"ib-tree query failed: {type(exc).__name__}: {exc}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="查询失败，请稍后重试",
        ) from exc
