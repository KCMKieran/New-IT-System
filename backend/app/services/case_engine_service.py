"""
Case engine — signal → case upsert (OPT-0047, risk-V2 归集引擎 minimal).

Pipeline position: `append_scan_and_events` (SQLite, detection layer) →
THIS module → `risk_cases` (cloud PG, case layer). V2 scope = single source:
rebate-arbitrage signals (rule band 121-130, OPT-0046); every trigger
enrolls the client on the watchlist (conservative default, open question
2026-07-12).

Reliability model (fail-open, AC4):
    A SQLite-side cursor (`case_sync_cursor`) marks the highest alert id
    already condensed into PG. Each sync tick replays everything after the
    cursor and only advances it after the PG transaction COMMITS. PG being
    down therefore never blocks detection — alerts keep landing in SQLite
    and the next tick catches the case layer up (alert_events retains 30
    days, orders of magnitude more than any realistic outage).

Cold-condensation (skill §10 hard rule): alert_events purges at 30 days, so
each signal's key detail fields are copied INTO the case row
(`signal_timeline` JSONB, capped) — the case layer never stores bare FKs.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from ..core.config import Settings, get_settings
from ..core.risk_cases_pg import (
    RiskCasesUnavailable,
    SIGNAL_TIMELINE_MAX_ENTRIES,
    STATE_WATCHING,
    risk_cases_conn,
)
from ..core.risk_monitor_db import (
    count_null_user_id_events,
    fetch_rebate_arb_events_after,
    get_case_sync_cursor,
    set_case_sync_cursor,
)
from ..core.sql_helpers import SID_MAP

logger = logging.getLogger(__name__)

# Risk tag for the rebate-arbitrage rule family (multi-tag JSONB array —
# Phase B sources append their own tags).
REBATE_ARB_TAG = "rebate_arb"

# One batch per PG transaction; loop caps below bound a worst-case backlog
# replay (500 × 20 = 10k events per tick — far beyond a normal day).
SYNC_BATCH_LIMIT = 500
MAX_BATCHES_PER_SYNC = 20

_SID_TO_SERVER = {sid: label for label, sid in SID_MAP.items()}


def _condense_signal(event: Dict[str, Any]) -> Dict[str, Any]:
    """Shape one alert row into a durable timeline entry (CaseSignal shape)."""
    return {
        "event_id": event.get("id"),
        "scanned_at": event.get("scanned_at"),
        "rule_id": event.get("rule_id"),
        "rule_label": event.get("rule_label"),
        "window_date": event.get("window_date"),
        "rebate_30d": event.get("rebate_30d"),
        "total_pl_30d": event.get("total_pl_30d"),
        "combined_30d": event.get("combined_30d"),
        "ratio_5m": event.get("ratio_5m"),
        "ratio_10m": event.get("ratio_10m"),
        "hold_geo_mean_sec": event.get("hold_geo_mean_sec"),
        "trading_net_deposit": event.get("trading_net_deposit"),
        "ib_withdrawal": event.get("ib_withdrawal"),
        "contributing_login_sids": event.get("contributing_login_sids"),
        "contributing_account_count": event.get("contributing_account_count"),
        "wallet_login_sids": event.get("wallet_login_sids"),
    }


def _entities_from_login_sids(raw: Optional[str]) -> List[Dict[str, Any]]:
    """Parse 'contributing_login_sids' ("1-100,5-200") into entity dicts."""
    out: List[Dict[str, Any]] = []
    for token in (raw or "").split(","):
        token = token.strip()
        if not token or "-" not in token:
            continue
        sid_part, _, login_part = token.partition("-")
        try:
            sid = int(sid_part)
            login = int(login_part)
        except ValueError:
            continue
        out.append(
            {
                "server": _SID_TO_SERVER.get(sid, ""),
                "login": login,
                "login_sid": token,
                "family": "",  # V2 populates the account level of the cascade
            }
        )
    return out


# Keep-last-N while preserving chronological order: inner query takes the
# tail by ordinality, outer re-sorts ascending.
_TIMELINE_MERGE_SQL = f"""
    (
        SELECT COALESCE(jsonb_agg(entry ORDER BY ord), '[]'::jsonb)
        FROM (
            SELECT entry, ord
            FROM jsonb_array_elements(
                risk_cases.signal_timeline || EXCLUDED.signal_timeline
            ) WITH ORDINALITY AS x(entry, ord)
            ORDER BY ord DESC
            LIMIT {SIGNAL_TIMELINE_MAX_ENTRIES}
        ) tail
    )
"""

_CASE_UPSERT_SQL = f"""
    INSERT INTO risk_cases
        (user_id, state, tags, signal_count, first_signal_at,
         last_signal_at, signal_timeline, user_name)
    VALUES (%s, %s, %s::jsonb, %s, %s, %s, %s::jsonb, %s)
    ON CONFLICT (user_id) DO UPDATE SET
        signal_count    = risk_cases.signal_count + EXCLUDED.signal_count,
        first_signal_at = LEAST(
            COALESCE(risk_cases.first_signal_at, EXCLUDED.first_signal_at),
            EXCLUDED.first_signal_at
        ),
        last_signal_at  = GREATEST(
            COALESCE(risk_cases.last_signal_at, EXCLUDED.last_signal_at),
            EXCLUDED.last_signal_at
        ),
        tags = (
            SELECT COALESCE(jsonb_agg(DISTINCT t), '[]'::jsonb)
            FROM jsonb_array_elements(risk_cases.tags || EXCLUDED.tags) AS t
        ),
        signal_timeline = {_TIMELINE_MERGE_SQL},
        user_name  = COALESCE(EXCLUDED.user_name, risk_cases.user_name),
        updated_at = now()
"""
# NOTE (state semantics): the upsert never touches `state`. New cases start
# 'watching'; a whitelisted/disposed case receiving new signals keeps its
# state (skill §3 — whitelist prevents bounce-back; disposition review is
# driven by the Δ columns, not by state resets).

_ENTITY_UPSERT_SQL = """
    INSERT INTO case_entities (user_id, server, login, family, login_sid)
    VALUES (%s, %s, %s, %s, %s)
    ON CONFLICT ON CONSTRAINT uq_case_entities
    DO UPDATE SET last_seen_at = now()
"""


def upsert_cases(conn, events: List[Dict[str, Any]]) -> int:
    """Upsert one batch of alert rows into risk_cases/case_entities.

    Caller owns the transaction (risk_cases_conn commits/rolls back).
    Events missing user_id are skipped with a warning — the daily user_id
    NULL repair reconciles them and the cursor replay picks them up only if
    they precede it (rebate-arb resolves user_id at detection, so this is a
    guard, not a path).
    """
    # Group chronologically per client so first/last aggregation is exact
    # even when one batch carries several days of backlog.
    by_user: Dict[int, List[Dict[str, Any]]] = {}
    skipped = 0
    for ev in events:
        uid = ev.get("user_id")
        if uid is None:
            skipped += 1
            continue
        by_user.setdefault(int(uid), []).append(ev)
    if skipped:
        logger.warning("case sync: %d event(s) without user_id skipped", skipped)

    with conn.cursor() as cur:
        for uid, evs in by_user.items():
            evs.sort(key=lambda e: (e.get("scanned_at") or "", e.get("id") or 0))
            # Cap client-side too: the SQL merge only trims on UPDATE, so a
            # giant first batch must not seed an over-cap timeline.
            timeline = [
                _condense_signal(e) for e in evs[-SIGNAL_TIMELINE_MAX_ENTRIES:]
            ]
            first_at = evs[0].get("scanned_at")
            last_at = evs[-1].get("scanned_at")
            user_name = next(
                (e.get("client_name") for e in reversed(evs) if e.get("client_name")),
                None,
            )
            cur.execute(
                _CASE_UPSERT_SQL,
                (
                    uid,
                    STATE_WATCHING,
                    json.dumps([REBATE_ARB_TAG]),
                    len(evs),
                    first_at,
                    last_at,
                    json.dumps(timeline),
                    user_name,
                ),
            )
            # Entities from the newest event (full account list every alert).
            for ent in _entities_from_login_sids(
                evs[-1].get("contributing_login_sids")
            ):
                cur.execute(
                    _ENTITY_UPSERT_SQL,
                    (uid, ent["server"], ent["login"], ent["family"], ent["login_sid"]),
                )
    return len(by_user)


def sync_cases_from_alert_events(
    settings: Optional[Settings] = None,
) -> Dict[str, Any]:
    """One sync tick: replay un-synced rule 121-130 alerts into the case DB.

    Called after every rebate-arb scan tick (and by the daily baseline job
    as a catch-up). Returns a summary dict; never raises for PG problems —
    the cursor simply stays put and the next tick retries (AC4).
    """
    settings = settings or get_settings()
    total_events = 0
    total_cases = 0
    batches = 0

    while batches < MAX_BATCHES_PER_SYNC:
        cursor = get_case_sync_cursor()
        events = fetch_rebate_arb_events_after(cursor, SYNC_BATCH_LIMIT)
        if not events:
            break
        try:
            with risk_cases_conn(settings) as conn:
                total_cases += upsert_cases(conn, events)
        except RiskCasesUnavailable as exc:
            # Fail-open: alerts are already durable in SQLite; retry next tick.
            logger.warning(
                "case sync: PG unavailable, cursor stays at %d "
                "(%d event(s) pending): %s",
                cursor, len(events), exc,
            )
            return {
                "pg_ok": False,
                "events_synced": total_events,
                "cases_upserted": total_cases,
                "pending": len(events),
            }
        except Exception:
            # Unexpected bug — same posture (don't advance, don't crash the
            # scan tick), but log at error for visibility.
            logger.error("case sync: upsert batch failed (cursor kept)", exc_info=True)
            return {
                "pg_ok": False,
                "events_synced": total_events,
                "cases_upserted": total_cases,
                "pending": len(events),
            }
        # COMMIT succeeded → now (and only now) advance the cursor.
        set_case_sync_cursor(max(int(e["id"]) for e in events))
        total_events += len(events)
        batches += 1
        if len(events) < SYNC_BATCH_LIMIT:
            break

    if total_events:
        logger.info(
            "case sync: %d event(s) → %d case upsert(s) in %d batch(es)",
            total_events, total_cases, batches,
        )
    return {
        "pg_ok": True,
        "events_synced": total_events,
        "cases_upserted": total_cases,
        "pending": 0,
    }


def log_null_user_id_count() -> int:
    """Observability hook (OPT-0047 deliverable 6): count+log recent NULL
    user_id alert rows. NULLs mean the MySQL enrichment fail-opened — those
    alerts are invisible to GROUP BY user_id until the daily repair runs."""
    try:
        n = count_null_user_id_events(since_hours=24)
    except Exception:
        logger.error("null user_id count failed", exc_info=True)
        return -1
    if n > 0:
        logger.warning(
            "alert_events has %d NULL user_id row(s) in the last 24h "
            "(daily repair job will reconcile)", n,
        )
    else:
        logger.info("alert_events NULL user_id (24h): 0")
    return n
