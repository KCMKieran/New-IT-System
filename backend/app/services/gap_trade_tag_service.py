"""Gap Trade → CRM risk-tag adapter (OPT-0032).

Thin adapter over the generic `crm_tag_service`: it knows only the Gap Trade
specifics — which alerts carry a taggable client, how to build the dedup key,
and the cid→tag policy — then delegates the read-modify-write / idempotency /
dedup / audit / email to the generic engine.

Tagging a client flips their withdrawal from auto-approve to manual CS review.
"""
from __future__ import annotations

from typing import Any, Optional

from . import crm_tag_service
from .crm_tag_service import TagItem, TagRunSummary, build_tag_email  # re-export

# cid -> CRM tag string. MUST be byte-for-byte identical to the CRM tags
# (traditional 風 vs simplified 风, full-width vs half-width parens). Copied
# verbatim from the CRM — do NOT retype.
TAG_BY_CID: dict[int, str] = {
    0: "禁止出金(風控)",      # CN  (tagid 488374)
    1: "Withdrawal Notice",   # Global (tagid 263196)
}

GAP_TRADE_GAP_RULE_ID = 81
SOURCE = "gap_trade"

__all__ = ["tag_gap_profit_clients", "build_tag_email", "TAG_BY_CID", "TagRunSummary"]


def _cid_tag_resolver(user: dict[str, Any], item: TagItem) -> Optional[str]:
    """Pick the tag by the CRM user's cid; None (skip) for unmapped cids."""
    return TAG_BY_CID.get(user.get("cid"))


def tag_gap_profit_clients(
    alerts: list[dict[str, Any]],
    *,
    window_date: str,
    dry_run: bool,
) -> TagRunSummary:
    """Tag each rule-81 client in the CRM. Returns a run summary for email.

    dedup_key = "<window_date>:<client_userid>" so a client is tagged at most
    once per MT trading day across the 5-min intraday ticks + the 07:20 run.
    """
    items: list[TagItem] = []
    seen: set[int] = set()
    for a in alerts:
        if int(a.get("rule_id") or 0) != GAP_TRADE_GAP_RULE_ID:
            continue
        uid = int(a.get("client_userid") or 0)
        if uid <= 0 or uid in seen:
            continue
        seen.add(uid)
        items.append(TagItem(
            user_id=uid,
            dedup_key=f"{window_date}:{uid}",
            context={
                "window_date": window_date,
                "profit_usd": a.get("total_profit_usd"),
                "profit_ratio": a.get("profit_ratio"),
            },
        ))

    return crm_tag_service.apply_tags(
        items,
        source=SOURCE,
        label=f"Gap Trade 风控上 tag — {window_date}",
        dry_run=dry_run,
        tag_resolver=_cid_tag_resolver,
    )
