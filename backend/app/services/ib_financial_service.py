"""
Service layer for IB Financial Monitor.

Handles:
- Watchlist CRUD (SQLite)
- Financial data queries (MySQL fxbackoffice — ported from D08 script)
- Report config CRUD (SQLite)
- Audit logging (SQLite)
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timezone, timedelta
from typing import Dict, List, Optional

import pymysql

from ..core.config import Settings
from ..core.database import get_db

logger = logging.getLogger(__name__)


# ── MySQL connection (read-only, fxbackoffice) ────────────

def _connect_fxbackoffice(settings: Settings):
    """Create a MySQL connection to fxbackoffice (slave)."""
    return pymysql.connect(
        host=settings.DB_HOST,
        user=settings.DB_USER,
        password=settings.DB_PASSWORD,
        database=settings.MYSQL_DATABASE_FXBACKOFFICE,
        port=int(settings.DB_PORT),
        charset=settings.DB_CHARSET,
        cursorclass=pymysql.cursors.DictCursor,
    )


# ── Watchlist ─────────────────────────────────────────────

def get_watchlist() -> List[dict]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT ib_id, ib_name, added_by, added_at, is_active "
            "FROM watchlist WHERE is_active = 1 ORDER BY added_at"
        ).fetchall()
        return [dict(r) for r in rows]


def add_ib(ib_id: str, ib_name: Optional[str], operator: str) -> None:
    with get_db() as conn:
        # Reactivate if soft-deleted, otherwise insert
        existing = conn.execute(
            "SELECT id, is_active FROM watchlist WHERE ib_id = ?", (ib_id,)
        ).fetchone()
        if existing:
            if existing["is_active"] == 1:
                raise ValueError(f"IB {ib_id} already exists in watchlist")
            conn.execute(
                "UPDATE watchlist SET is_active = 1, ib_name = ?, added_by = ?, "
                "added_at = datetime('now', '+8 hours') WHERE ib_id = ?",
                (ib_name, operator, ib_id),
            )
        else:
            conn.execute(
                "INSERT INTO watchlist (ib_id, ib_name, added_by) VALUES (?, ?, ?)",
                (ib_id, ib_name, operator),
            )
        _write_audit(conn, "add_ib", {"ib_id": ib_id, "ib_name": ib_name}, operator)


def batch_add_ib(
    ibs: List[dict], operator: str
) -> tuple[int, List[str]]:
    """Add multiple IBs in one transaction.

    Returns (added_count, skipped_ib_ids).
    """
    added = 0
    skipped: List[str] = []
    with get_db() as conn:
        for item in ibs:
            ib_id = str(item.get("ib_id", "")).strip()
            ib_name = (item.get("ib_name") or ib_id).strip()
            if not ib_id:
                continue
            existing = conn.execute(
                "SELECT id, is_active FROM watchlist WHERE ib_id = ?", (ib_id,)
            ).fetchone()
            if existing:
                if existing["is_active"] == 1:
                    skipped.append(ib_id)
                    continue
                conn.execute(
                    "UPDATE watchlist SET is_active = 1, ib_name = ?, added_by = ?, "
                    "added_at = datetime('now', '+8 hours') WHERE ib_id = ?",
                    (ib_name, operator, ib_id),
                )
            else:
                conn.execute(
                    "INSERT INTO watchlist (ib_id, ib_name, added_by) VALUES (?, ?, ?)",
                    (ib_id, ib_name, operator),
                )
            added += 1
        _write_audit(
            conn,
            "batch_add_ib",
            {"count": added, "ibs": [i.get("ib_id") for i in ibs], "skipped": skipped},
            operator,
        )
    return added, skipped


def remove_ib(ib_id: str, operator: str) -> None:
    with get_db() as conn:
        result = conn.execute(
            "UPDATE watchlist SET is_active = 0 WHERE ib_id = ? AND is_active = 1",
            (ib_id,),
        )
        if result.rowcount == 0:
            raise ValueError(f"IB {ib_id} not found in active watchlist")
        _write_audit(conn, "remove_ib", {"ib_id": ib_id}, operator)


# ── Financial Query (ported from D08) ─────────────────────

# IB query: expand IB tree to include all downstream clients
_IB_FINANCIAL_QUERY = """
WITH Target_IB_List AS (
    SELECT
        tree.ibid AS root_ib_id,
        tree.referralId AS client_id
    FROM fxbackoffice.ib_tree_with_self tree
    WHERE tree.ibid IN ({ib_placeholders})
),
Transaction_Stats AS (
    SELECT
        map.root_ib_id,
        trans.currency,
        SUM(CASE WHEN trans.type = 'deposit' AND trans.date = %s
            THEN trans.amount ELSE 0 END) AS today_deposit,
        SUM(CASE WHEN trans.type IN ('withdrawal', 'ib withdrawal') AND trans.date = %s
            THEN trans.amount ELSE 0 END) AS today_withdrawal,
        SUM(CASE WHEN trans.type = 'deposit'
            THEN trans.amount ELSE 0 END) AS total_deposit,
        SUM(CASE WHEN trans.type IN ('withdrawal', 'ib withdrawal')
            THEN trans.amount ELSE 0 END) AS total_withdrawal
    FROM fxbackoffice.stats_transactions trans
    INNER JOIN Target_IB_List map ON trans.userid = map.client_id
    WHERE trans.type IN ('deposit', 'withdrawal', 'ib withdrawal')
    GROUP BY map.root_ib_id, trans.currency
),
Balance_Snapshot AS (
    SELECT
        map.root_ib_id,
        bal.currency,
        SUM(CASE WHEN bal.loginsid NOT LIKE '2-%%' THEN bal.endingEquity ELSE 0 END) AS mt4_equity,
        SUM(CASE WHEN bal.loginsid LIKE '2-%%' THEN bal.endingEquity ELSE 0 END) AS ib_wallet_equity
    FROM fxbackoffice.stats_balances bal
    INNER JOIN Target_IB_List map ON bal.userId = map.client_id
    WHERE bal.date = %s
    GROUP BY map.root_ib_id, bal.currency
),
All_Keys AS (
    SELECT root_ib_id, currency FROM Transaction_Stats
    UNION
    SELECT root_ib_id, currency FROM Balance_Snapshot
)
SELECT
    k.root_ib_id AS ib_id,
    k.currency,
    COALESCE(t.today_deposit, 0) AS today_deposit,
    COALESCE(t.today_withdrawal, 0) AS today_withdrawal,
    COALESCE(t.total_deposit, 0) AS total_deposit,
    COALESCE(t.total_withdrawal, 0) AS total_withdrawal,
    COALESCE(b.mt4_equity, 0) AS mt4_equity,
    COALESCE(b.ib_wallet_equity, 0) AS ib_wallet_equity,
    (COALESCE(t.total_deposit, 0) + COALESCE(t.total_withdrawal, 0))
        - (COALESCE(b.mt4_equity, 0) + COALESCE(b.ib_wallet_equity, 0)) AS difference
FROM All_Keys k
LEFT JOIN Transaction_Stats t ON k.root_ib_id = t.root_ib_id AND k.currency = t.currency
LEFT JOIN Balance_Snapshot b ON k.root_ib_id = b.root_ib_id AND k.currency = b.currency
ORDER BY k.root_ib_id, k.currency
"""

# Client query: no IB tree expansion, query the user's own data directly.
# Param order: date×2 (Transaction SELECT) → client_ids (Transaction WHERE)
#            → client_ids (Balance WHERE) → date (Balance WHERE)
_CLIENT_FINANCIAL_QUERY = """
WITH Transaction_Stats AS (
    SELECT
        trans.userid AS client_id,
        trans.currency,
        SUM(CASE WHEN trans.type = 'deposit' AND trans.date = %s
            THEN trans.amount ELSE 0 END) AS today_deposit,
        SUM(CASE WHEN trans.type IN ('withdrawal', 'ib withdrawal') AND trans.date = %s
            THEN trans.amount ELSE 0 END) AS today_withdrawal,
        SUM(CASE WHEN trans.type = 'deposit'
            THEN trans.amount ELSE 0 END) AS total_deposit,
        SUM(CASE WHEN trans.type IN ('withdrawal', 'ib withdrawal')
            THEN trans.amount ELSE 0 END) AS total_withdrawal
    FROM fxbackoffice.stats_transactions trans
    WHERE trans.userid IN ({client_placeholders})
      AND trans.type IN ('deposit', 'withdrawal', 'ib withdrawal')
    GROUP BY trans.userid, trans.currency
),
Balance_Snapshot AS (
    SELECT
        bal.userId AS client_id,
        bal.currency,
        SUM(bal.endingEquity) AS mt4_equity
    FROM fxbackoffice.stats_balances bal
    WHERE bal.userId IN ({client_placeholders})
      AND bal.date = %s
      AND bal.loginsid NOT LIKE '2-%%'
    GROUP BY bal.userId, bal.currency
),
All_Keys AS (
    SELECT client_id, currency FROM Transaction_Stats
    UNION
    SELECT client_id, currency FROM Balance_Snapshot
)
SELECT
    k.client_id AS ib_id,
    k.currency,
    COALESCE(t.today_deposit, 0) AS today_deposit,
    COALESCE(t.today_withdrawal, 0) AS today_withdrawal,
    COALESCE(t.total_deposit, 0) AS total_deposit,
    COALESCE(t.total_withdrawal, 0) AS total_withdrawal,
    COALESCE(b.mt4_equity, 0) AS mt4_equity,
    0 AS ib_wallet_equity,
    (COALESCE(t.total_deposit, 0) + COALESCE(t.total_withdrawal, 0))
        - COALESCE(b.mt4_equity, 0) AS difference
FROM All_Keys k
LEFT JOIN Transaction_Stats t ON k.client_id = t.client_id AND k.currency = t.currency
LEFT JOIN Balance_Snapshot b ON k.client_id = b.client_id AND k.currency = b.currency
ORDER BY k.client_id, k.currency
"""


def _classify_ids(
    cursor, all_ids: List[str],
) -> tuple[List[str], List[str]]:
    """Split watchlist IDs into IB IDs and plain client IDs."""
    if not all_ids:
        return [], []
    placeholders = ", ".join(["%s"] * len(all_ids))
    cursor.execute(
        f"SELECT DISTINCT ibid FROM fxbackoffice.ib_tree_with_self "
        f"WHERE ibid IN ({placeholders})",
        tuple(all_ids),
    )
    ib_set = {str(r["ibid"]) for r in cursor.fetchall()}
    ib_ids = [i for i in all_ids if i in ib_set]
    client_ids = [i for i in all_ids if i not in ib_set]
    return ib_ids, client_ids


def _enrich_rows(
    rows: List[dict], name_map: Dict[str, str],
) -> List[dict]:
    """Convert numeric types and attach ib_name from watchlist."""
    records = []
    for row in rows:
        row["ib_id"] = str(row["ib_id"])
        row["ib_name"] = name_map.get(row["ib_id"], row["ib_id"])
        for key in ("today_deposit", "today_withdrawal", "total_deposit",
                     "total_withdrawal", "mt4_equity", "ib_wallet_equity", "difference"):
            if key in row and row[key] is not None:
                row[key] = float(row[key])
        records.append(row)
    return records


def query_financial_data(
    settings: Settings,
    target_date: Optional[date] = None,
) -> tuple[str, List[dict]]:
    """Query financial data for all active watchlist IDs.

    Automatically detects which IDs are IBs (expand downstream via ib_tree)
    and which are plain clients (query their own data directly).
    """
    watchlist = get_watchlist()
    if not watchlist:
        return str(target_date or _yesterday_hkt()), []

    name_map: Dict[str, str] = {
        w["ib_id"]: w["ib_name"] or w["ib_id"] for w in watchlist
    }
    all_ids = list(name_map.keys())

    if target_date is None:
        target_date = _yesterday_hkt()
    date_str = str(target_date)

    conn = _connect_fxbackoffice(settings)
    try:
        with conn.cursor() as cursor:
            ib_ids, client_ids = _classify_ids(cursor, all_ids)
            logger.info(
                "IB Financial query date=%s, ibs=%s, clients=%s",
                date_str, ib_ids, client_ids,
            )

            records: List[dict] = []

            # Query IB data (with downstream expansion)
            if ib_ids:
                ph = ", ".join(["%s"] * len(ib_ids))
                query = _IB_FINANCIAL_QUERY.format(ib_placeholders=ph)
                params = tuple(ib_ids) + (date_str, date_str, date_str)
                cursor.execute(query, params)
                records.extend(_enrich_rows(cursor.fetchall(), name_map))

            # Query client data (own data only, no tree expansion)
            if client_ids:
                ph = ", ".join(["%s"] * len(client_ids))
                query = _CLIENT_FINANCIAL_QUERY.format(client_placeholders=ph)
                # Param order: date×2 (Trans SELECT) → client_ids (Trans WHERE)
                #            → client_ids (Bal WHERE) → date (Bal WHERE)
                params = (
                    (date_str, date_str) + tuple(client_ids)
                    + tuple(client_ids) + (date_str,)
                )
                cursor.execute(query, params)
                records.extend(_enrich_rows(cursor.fetchall(), name_map))
    finally:
        conn.close()

    return date_str, records


# ── Report Config ─────────────────────────────────────────

def get_report_config() -> dict:
    with get_db() as conn:
        row = conn.execute("SELECT * FROM report_config WHERE id = 1").fetchone()
        return dict(row) if row else {}


def update_report_config(
    updates: dict,
    operator: str,
) -> dict:
    with get_db() as conn:
        set_parts = []
        values = []
        for key in ("mail_to", "mail_cc", "schedule_time", "is_enabled"):
            if key in updates and updates[key] is not None:
                set_parts.append(f"{key} = ?")
                values.append(updates[key])
        if not set_parts:
            return get_report_config()

        set_parts.append("updated_by = ?")
        values.append(operator)
        set_parts.append("updated_at = datetime('now', '+8 hours')")

        sql = f"UPDATE report_config SET {', '.join(set_parts)} WHERE id = 1"
        conn.execute(sql, values)
        _write_audit(conn, "update_config", updates, operator)

    return get_report_config()


# ── Admin Whitelist ───────────────────────────────────────

def get_admin_whitelist() -> List[str]:
    with get_db() as conn:
        rows = conn.execute("SELECT email FROM admin_whitelist").fetchall()
        return [r["email"] for r in rows]


def is_whitelisted(email: str) -> bool:
    with get_db() as conn:
        row = conn.execute(
            "SELECT 1 FROM admin_whitelist WHERE email = ?", (email,)
        ).fetchone()
        return row is not None


# ── Audit Log ─────────────────────────────────────────────

def get_audit_log(limit: int = 50) -> List[dict]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, action, detail, operator, created_at "
            "FROM audit_log ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


def _write_audit(conn, action: str, detail, operator: str) -> None:
    """Write an entry to the audit log within an existing connection/transaction."""
    detail_str = json.dumps(detail, ensure_ascii=False) if not isinstance(detail, str) else detail
    conn.execute(
        "INSERT INTO audit_log (action, detail, operator) VALUES (?, ?, ?)",
        (action, detail_str, operator),
    )


# ── Helpers ───────────────────────────────────────────────

def _yesterday_hkt() -> date:
    """Return yesterday's date in HKT (UTC+8)."""
    now_hkt = datetime.now(timezone.utc) + timedelta(hours=8)
    return (now_hkt - timedelta(days=1)).date()
