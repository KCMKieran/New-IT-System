"""Alert mail source registry (OPT-0043).

Detection layer and notification layer are decoupled: every detection module
already writes its alerts to the shared SQLite `alert_events` table (rule_id
banded per module). The mail center is a pure CONSUMER of that table — giving
a new module mail capability means registering ONE entry here; the detection
code never changes.

Iron rule (docs/features/alert-mail-center.md): detection rules decide what
exists on the PAGE; subscription conditions decide what lands in the MAILBOX
— the latter is always a subset of the former.

Registry entry contract (every key required unless noted):

  label             str   — human-readable (zh) module label for the UI
  rule_id_range     (lo, hi) — inclusive alert_events.rule_id band
  rules_loader      () -> [{"id", "name", "enabled", "params"}] — the module's
                    current detection rules for the UI multi-select; "id" is
                    the value as it appears in alert_events.rule_id
  filterable_fields {field: (type, zh label)} — fields a subscription
                    condition may reference (type in "float"|"int"|"str")
  field_getters     {field: (alert dict) -> value|None} — condition-time
                    evaluators; None means "not derivable for this alert"
                    and the condition never matches
  match_context     (alert) -> dict|None — per-alert context merged into the
                    match dict handed to template_builder; None marks the
                    alert as non-renderable (e.g. detail row missing) and it
                    never matches
  empty_context     dict — template context used when match_context returns
                    None on the pinned test-send fallback alert
  fetch_after       (last_alert_id, limit=...) -> [alert] — cursor pull,
                    band-filtered, detail JOINed, ascending id
  fetch_by_ids      ([id]) -> [alert]
  fetch_for_day     (YYYY-MM-DD) -> [alert] — sibling-account lookup
  fetch_recent      (limit=...) -> [alert] — newest first (test-send scan)
  template_builder  (hits, *, subscription, sibling_map, test) ->
                    (subject, body_html)
  fallback_sample   dict — frozen in-code sample alert (aliased row shape,
                    same keys as the fetchers return) rendered by test-send
                    when nothing recent matches. Deliberately NOT a DB row
                    id: the 30-day retention purge would delete a pinned
                    row and break test-send forever

v2 registered only `hedge_open`; OPT-0046 added `rebate_arb` (rule 121-130).
The structure is ready for the remaining risk-monitor tabs + fund-flow (one
entry each, follow-up).
"""

from __future__ import annotations

import logging
import math
from typing import Any, Callable, Dict, List, Optional, Tuple

from ...core.risk_monitor_db import (
    fetch_hedge_alerts_after,
    fetch_hedge_alerts_by_ids,
    fetch_hedge_alerts_for_day,
    fetch_recent_hedge_alerts,
    load_hedge_open_config,
)
from ..rule_hedge_open_service import (
    HEDGE_OPEN_RULE_ID_BASE,
    HEDGE_OPEN_RULE_ID_MAX,
)

# NOTE: alert_mail_dispatcher imports this module lazily (inside functions);
# this top-level import is therefore cycle-safe in both import orders.
from ..alert_mail_dispatcher import (
    TEST_SEND_SAMPLE_ALERT,
    build_hedge_digest_email,
    matched_lots_std,
)

# OPT-0046: rebate-arbitrage source pieces live in their own module (this
# file stays the registry, not a template dump). Safe import order: the
# module only pulls from core.risk_monitor_db + rule_rebate_arb_service.
from .rebate_arb import SOURCE as _REBATE_ARB_SOURCE

logger = logging.getLogger(__name__)


# ── hedge_open field getters ──────────────────────────────────────────────────

def _opt_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _get_orders_per_side(alert: Dict[str, Any]) -> Optional[int]:
    """min(buy_count, sell_count); None when the detail row is missing."""
    buy, sell = alert.get("buy_count"), alert.get("sell_count")
    if buy is None or sell is None:
        return None
    return min(int(buy), int(sell))


def _hedge_match_context(alert: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Template context for the v1 hedge digest builder.

    `matched_lots_std` is required by _account_section; None (detail row
    missing) marks the alert non-renderable — same never-match semantics as
    the v1 evaluate_conditions.
    """
    matched = matched_lots_std(alert)
    if matched is None:
        return None
    return {"matched_lots_std": matched}


def _hedge_rules() -> List[Dict[str, Any]]:
    """Hedge detection rules with their EFFECTIVE alert_events.rule_id.

    Mirrors rule_hedge_open_service.rule_hedge_open_detect: the stored row id
    is used when it already sits inside the 91-100 band, otherwise the rule
    is band-clamped to BASE + index — so the ids shown in the subscription UI
    are exactly the ids the scan stamps onto alert_events.
    """
    cfg = load_hedge_open_config()
    rules: List[Dict[str, Any]] = []
    for idx, r in enumerate(cfg.get("rules") or []):
        rule_id = int(r.get("id") or (HEDGE_OPEN_RULE_ID_BASE + idx))
        if rule_id < HEDGE_OPEN_RULE_ID_BASE or rule_id > HEDGE_OPEN_RULE_ID_MAX:
            rule_id = HEDGE_OPEN_RULE_ID_BASE + idx
        rules.append({
            "id": rule_id,
            "name": str(r.get("name") or f"Rule {idx + 1}"),
            "enabled": bool(r.get("enabled", True)),
            "params": {
                "window_sec": r.get("window_sec"),
                "min_orders_per_side": r.get("min_orders_per_side"),
                "min_total_lots": r.get("min_total_lots"),
            },
        })
    return rules


# ── The registry ──────────────────────────────────────────────────────────────

MAIL_SOURCES: Dict[str, Dict[str, Any]] = {
    "hedge_open": {
        "label": "对冲刷单 Hedge Open",
        "rule_id_range": (HEDGE_OPEN_RULE_ID_BASE, HEDGE_OPEN_RULE_ID_MAX),
        "rules_loader": _hedge_rules,
        "filterable_fields": {
            "matched_lots_std": ("float", "匹配手数(标准手等值,cent已折算)"),
            "orders_per_side": ("int", "单边笔数"),
            "total_lots": ("float", "总手数"),
            "equity": ("float", "当前净值"),
            "net_deposit_hist": ("float", "历史净入金"),
        },
        "field_getters": {
            "matched_lots_std": matched_lots_std,
            "orders_per_side": _get_orders_per_side,
            "total_lots": lambda a: _opt_float(a.get("total_lots")),
            "equity": lambda a: _opt_float(a.get("equity")),
            "net_deposit_hist": lambda a: _opt_float(a.get("net_deposit_hist")),
        },
        "match_context": _hedge_match_context,
        "empty_context": {"matched_lots_std": 0.0},
        "fetch_after": fetch_hedge_alerts_after,
        "fetch_by_ids": fetch_hedge_alerts_by_ids,
        "fetch_for_day": fetch_hedge_alerts_for_day,
        "fetch_recent": fetch_recent_hedge_alerts,
        "template_builder": build_hedge_digest_email,
        "fallback_sample": TEST_SEND_SAMPLE_ALERT,
    },
    # OPT-0046: 返佣套利 rule band 121-130 (see services/alert_mail/rebate_arb.py)
    "rebate_arb": _REBATE_ARB_SOURCE,
}


# ── Lookup / validation helpers ───────────────────────────────────────────────

def get_source(module: str) -> Optional[Dict[str, Any]]:
    """Registry entry for `module`, or None when unregistered."""
    return MAIL_SOURCES.get(module)


def list_source_descriptors() -> List[Dict[str, Any]]:
    """API-shaped descriptors for GET /alert-mail/sources.

    A failing rules_loader degrades to an empty rules list (with a warning)
    instead of breaking the whole sources endpoint.
    """
    out: List[Dict[str, Any]] = []
    for module, src in MAIL_SOURCES.items():
        try:
            rules = src["rules_loader"]()
        except Exception:
            logger.warning("rules_loader failed for module %r", module, exc_info=True)
            rules = []
        lo, hi = src["rule_id_range"]
        out.append({
            "module": module,
            "label": src["label"],
            "rule_id_range": [int(lo), int(hi)],
            "filterable_fields": [
                {"field": field, "type": ftype, "label": label}
                for field, (ftype, label) in src["filterable_fields"].items()
            ],
            "rules": rules,
        })
    return out


def validate_module(module: str) -> Dict[str, Any]:
    """Return the registry entry or raise ValueError (route maps to 422)."""
    src = get_source(module)
    if src is None:
        raise ValueError(
            f"unknown module {module!r} — not registered in MAIL_SOURCES"
        )
    return src


def validate_rule_ids(module: str, rule_ids: Optional[List[int]]) -> None:
    """Every narrowed rule id must sit inside the module's rule band."""
    if not rule_ids:
        return
    lo, hi = validate_module(module)["rule_id_range"]
    bad = [r for r in rule_ids if not (int(lo) <= int(r) <= int(hi))]
    if bad:
        raise ValueError(
            f"rule_ids {bad} outside module {module!r} band [{lo}, {hi}]"
        )


def _coerce_int(value: Any) -> int:
    """Strict int coercion: '5' / 5.0 → 5; '5.5' / 5.5 / 'abc' raise."""
    if isinstance(value, bool):
        raise ValueError("boolean is not a valid int condition value")
    f = float(value)
    i = int(f)
    if i != f:
        raise ValueError("value is not an integer")
    return i


def _coerce_float(value: Any) -> float:
    if isinstance(value, bool):
        raise ValueError("boolean is not a valid float condition value")
    f = float(value)
    if not math.isfinite(f):
        raise ValueError("value is not a finite number")
    return f


_FIELD_TYPE_COERCERS: Dict[str, Callable[[Any], Any]] = {
    "float": _coerce_float,
    "int": _coerce_int,
    "str": str,
}


def coerce_condition_values(module: str, tree: Optional[Dict[str, Any]]) -> None:
    """Coerce every condition value IN PLACE to the field's declared type.

    filterable_fields declares each field as float/int/str; a raw API client
    may send {"field": "equity", "op": "==", "value": "100"} — without
    coercion the stored string never ==-matches the alert's 100.0 in the
    dispatcher (_compare falls into string comparison). Coercing at
    create/update time keeps the evaluator numeric for numeric fields.
    Uncoercible values raise ValueError (route maps to 422). Call AFTER
    validate_condition_fields — unknown fields must already be rejected.
    """
    if not tree:
        return
    fields = validate_module(module)["filterable_fields"]
    for cond in tree.get("conditions") or []:
        field = str(cond.get("field") or "")
        ftype = fields[field][0]
        coerce = _FIELD_TYPE_COERCERS[ftype]
        value = cond.get("value")
        try:
            cond["value"] = coerce(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"condition value {value!r} for field {field!r} is not a "
                f"valid {ftype}: {exc}"
            ) from exc


def validate_condition_fields(module: str, tree: Optional[Dict[str, Any]]) -> None:
    """Every condition field must be a filterable field of the module.

    Only validates the NEW {"logic","conditions"} shape — the legacy v1
    {"any": [...]} trees never arrive through the API (read-only there).
    """
    if not tree:
        return
    allowed = set(validate_module(module)["filterable_fields"].keys())
    for cond in tree.get("conditions") or []:
        field = str(cond.get("field") or "")
        if field not in allowed:
            raise ValueError(
                f"condition field {field!r} is not filterable for module "
                f"{module!r} (allowed: {sorted(allowed)})"
            )
