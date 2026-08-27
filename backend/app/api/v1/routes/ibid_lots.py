from __future__ import annotations

import logging
import time

from fastapi import APIRouter, Depends, HTTPException, Request, status

from ....core.config import Settings, get_settings
from ....core.data_scope import (
    caller_cids,
    cid_for_crm_user_ids,
    cid_for_login,
    require_cids_allowed,
)
from ....schemas.ibid_lots import IbidLotsQueryRequest, IbidLotsQueryResponse
from ....services.ibid_lots_service import query_tobe_global_lots

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/ibid-lots")


def _target_cid(settings: Settings, payload: IbidLotsQueryRequest) -> int | None:
    """Resolve this payload's ONE target to a cid. ``None`` = unresolvable.

    `target_id` means two different things depending on `query_type`, and the
    two live in different tables — so the resolver has to branch here rather
    than in data_scope. Getting the branch wrong is silent: an MT login number
    handed to `cid_for_crm_user_ids` resolves against `users.id`, which is a
    DIFFERENT person's row that happens to share the number, and the gate then
    answers about them instead.

      * "login"      -> an MT trading account, keyed by loginSid "{sid}-{login}"
      * everything else ("ibid" / "ibid_direct" / "ibid_direct_client" / "id")
                     -> a CRM user id (fxbackoffice.users.id)

    Returns ``None`` for anything it cannot resolve, which
    ``require_cids_allowed`` turns into a 403 for a restricted caller and
    ignores for an unrestricted one. Both bad-input branches below are already
    422s from the schema's own validators (`_digits_only`,
    `_check_server_and_range`) and so should be unreachable — they are here
    because "unreachable" is a property of today's schema, and the failure mode
    if a validator is ever relaxed is a 500 (or, worse, a skipped check), not a
    refusal. Fail closed instead.
    """
    if payload.query_type == "login":
        if not payload.server_sid:
            # No server means no loginSid means no owner. Unresolvable, not
            # "unrestricted".
            return None
        return cid_for_login(settings, payload.server_sid, payload.target_id)

    try:
        crm_id = int(payload.target_id)
    except (TypeError, ValueError):
        return None
    return cid_for_crm_user_ids(settings, [crm_id]).get(crm_id)


# Deliberately sync (`def`, not `async def`): pymysql blocks, and a large IB
# spends tens of seconds across its mt4_trades batches. FastAPI runs sync
# handlers in the threadpool, so a slow slave query cannot freeze the event
# loop for every other endpoint.
@router.post("/query", response_model=IbidLotsQueryResponse, status_code=status.HTTP_200_OK)
def query_ibid_lots(
    request: Request,
    payload: IbidLotsQueryRequest,
    settings: Settings = Depends(get_settings),
):
    """Return traded lots (total / >=10s / <10s) for a "For Tobe Global" target.

    An empty result is a 200 with zeroed totals, not a 404 — the UI shows its
    own "no trades found" state.
    """
    try:
        # Row-level (country) data scope. LOOKUP in data_scope.ROUTE_SCOPE:
        # despite the name, the payload always addresses exactly ONE target the
        # caller typed in, so the decision has to be made on the way in.
        #
        # Short-circuit first: unrestricted callers must not pay a resolver
        # round-trip. Restricted ones are refused BEFORE
        # query_tobe_global_lots(), which batches over mt4_trades (48M rows)
        # for tens of seconds on a large IB.
        scope = caller_cids(request)
        if scope is not None:
            require_cids_allowed(
                request,
                _target_cid(settings, payload),
                what=f"{payload.query_type} {payload.target_id}",
            )

        started = time.perf_counter()
        # The OUTPUT half. Gating the input is necessary and not sufficient:
        # the three "ibid*" modes answer with ONE ROW PER DOWNLINE CLIENT of the
        # IB the caller named, and 11 Global IBs have CN clients under them. So
        # a caller cleared for the IB still gets CN clients' CRM ids and lots
        # back unless the downline itself is narrowed. The service filters it in
        # SQL at step 1, which also means every total is recomputed from the
        # rows that survived rather than left as a firm-wide figure sitting
        # above a filtered list. `scope is None` for the unrestricted 99% and
        # the service then runs its original statements untouched.
        result = query_tobe_global_lots(settings, payload, allowed_cids=scope)
        result.query_time_ms = round((time.perf_counter() - started) * 1000, 2)
        return result
    except HTTPException:
        raise
    except Exception as exc:
        # Details go to the log only — never echo DB internals to the client.
        logger.error(f"ibid-lots query failed: {type(exc).__name__}: {exc}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="查询失败，请稍后重试",
        ) from exc
