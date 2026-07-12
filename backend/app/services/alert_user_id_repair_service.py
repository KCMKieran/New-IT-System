"""
Scheduled repair for alert_events.user_id NULL rows (OPT-0047 deliverable 6,
absorbing OPT-0045 cold-review F2).

Why: scan-time user_id enrichment is fail-open — a MySQL outage writes NULL
and moves on. The standalone backfill script
(scripts/backfill_alert_events_user_id.py) fixes those rows, but nothing ran
it automatically, so outage-window alerts (exactly the ones that matter)
would age out of the 30-day window still invisible to GROUP BY user_id.
This module is the same two-phase repair as the script, packaged for
APScheduler (core/scheduler.py runs it daily before the case baseline).

Phase A — SQLite-only: gap rules carry the client id in their own detail
    tables (rule 71 → l_userid, rule 81 → client_userid); copy it across.
Phase B — one batched MySQL lookup for the remaining NULL (server, login)
    pairs via account_enrichment.get_user_id_map (chunked, fail-open).

Idempotent: every UPDATE is scoped `WHERE user_id IS NULL`.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from ..core.config import Settings, get_settings
from ..core.risk_monitor_db import get_risk_monitor_db
from ..core.sql_helpers import SID_MAP

logger = logging.getLogger(__name__)

# (rule_id, detail table, detail column carrying the client id) — mirrors
# scripts/backfill_alert_events_user_id.py _PHASE_A_SPECS.
_PHASE_A_SPECS: tuple[tuple[int, str, str], ...] = (
    (71, "alert_gap_so_detail", "l_userid"),
    (81, "alert_gap_profit_detail", "client_userid"),
)


def _apply_phase_a(conn) -> int:
    fixed = 0
    for rule_id, table, col in _PHASE_A_SPECS:
        cur = conn.execute(
            f"""
            UPDATE alert_events
            SET user_id = (
                SELECT d.{col} FROM {table} d WHERE d.id = alert_events.id
            )
            WHERE user_id IS NULL
              AND rule_id = ?
              AND EXISTS (
                  SELECT 1 FROM {table} d
                  WHERE d.id = alert_events.id AND d.{col} IS NOT NULL
              )
            """,
            (rule_id,),
        )
        fixed += cur.rowcount or 0
    return fixed


def repair_null_user_ids(settings: Optional[Settings] = None) -> Dict[str, Any]:
    """One repair pass. Returns counters; never raises (job-safe)."""
    settings = settings or get_settings()
    result: Dict[str, Any] = {
        "phase_a_fixed": 0,
        "phase_b_fixed": 0,
        "remaining_null": -1,
        "ok": True,
    }
    try:
        # Phase A: short SQLite-only transaction.
        with get_risk_monitor_db() as conn:
            result["phase_a_fixed"] = _apply_phase_a(conn)

        # Phase B planning: distinct unresolved (server, login) pairs.
        with get_risk_monitor_db() as conn:
            pairs = conn.execute(
                """
                SELECT DISTINCT server, login FROM alert_events
                WHERE user_id IS NULL AND server != '' AND login > 0
                """
            ).fetchall()

        if pairs:
            # Batched MySQL lookup — get_user_id_map is fail-open (returns {}
            # on error). Chunk HERE (it ships one IN-list per call): after an
            # outage the distinct set can be thousands of accounts and the
            # production replica should never see an unbounded IN-list.
            from .account_enrichment import get_user_id_map
            from .risk_monitor_service import _get_connection

            pseudo_alerts: List[Dict[str, Any]] = [
                {"server": s, "login": l} for s, l in pairs
            ]
            user_id_map: Dict[str, int] = {}
            mysql_conn = _get_connection(settings)
            try:
                for i in range(0, len(pseudo_alerts), 500):
                    user_id_map.update(
                        get_user_id_map(mysql_conn, pseudo_alerts[i : i + 500])
                    )
            finally:
                mysql_conn.close()

            if user_id_map:
                # SQLite write kept short and separate from the MySQL call.
                with get_risk_monitor_db() as conn:
                    for server, login in pairs:
                        sid = SID_MAP.get(server)
                        if sid is None:
                            continue
                        uid = user_id_map.get(f"{sid}-{login}")
                        if uid is None:
                            continue
                        cur = conn.execute(
                            """
                            UPDATE alert_events SET user_id = ?
                            WHERE user_id IS NULL AND server = ? AND login = ?
                            """,
                            (int(uid), server, login),
                        )
                        result["phase_b_fixed"] += cur.rowcount or 0

        with get_risk_monitor_db() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM alert_events WHERE user_id IS NULL"
            ).fetchone()
            result["remaining_null"] = int(row[0]) if row else -1

        logger.info(
            "user_id repair: phase A fixed %d, phase B fixed %d, "
            "%d NULL row(s) remain",
            result["phase_a_fixed"],
            result["phase_b_fixed"],
            result["remaining_null"],
        )
    except Exception:
        result["ok"] = False
        logger.error("user_id repair failed (will retry tomorrow)", exc_info=True)
    return result
