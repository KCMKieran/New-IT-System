"""
Login IP Monitor — MySQL enrichment service.

Ports the `get_account_details()` function from the legacy
`46-MT-Server-Login-Detect/search.py` to the new platform.

Purpose
-------
Given a list of MT account IDs (e.g. the "correlated" accounts surfaced by
the daily report or the manual search API), enrich each one with its
business-side metadata:

    account_id -> {client_id, chinese_name, first_name, last_name} (names may be null)

Daily report uses `chinese_name` for the email HTML.
Search API (Phase 6) uses `client_id` to filter out "same customer" noise.

Data source
-----------
Trading accounts (MT4 + MT5) are looked up in **CRM** ``fxbackoffice.mt4_users``
(not ``mt4_live.mt4_users``), then joined to ``user_custom_fields`` for
``custom_chinese_name``. Same read slave as the rest of the platform
(``DB_*`` + ``MYSQL_DATABASE_FXBACKOFFICE`` in ``backend/.env``).

See `ib_financial_service._connect_fxbackoffice()` for the identical connection
recipe already used by the IB Financial Monitor page.
"""

from __future__ import annotations

import logging
from typing import Iterable

import pymysql
import pymysql.cursors

from ..core.config import Settings, get_settings

logger = logging.getLogger(__name__)

# Chunk size for the IN-clause. MT accounts are small ints so this is more
# about MySQL statement length limits than performance. 1000 is comfortably
# under max_allowed_packet and matches the chunking used by other services.
_IN_CLAUSE_CHUNK = 1000


def _connect_fxbackoffice(settings: Settings) -> pymysql.connections.Connection:
    """Open a read-only MySQL connection to the fxbackoffice slave.

    Kept in sync with `ib_financial_service._connect_fxbackoffice` — if the
    platform ever switches credentials, both services update together.
    """
    return pymysql.connect(
        host=settings.DB_HOST,
        user=settings.DB_USER,
        password=settings.DB_PASSWORD,
        database=settings.MYSQL_DATABASE_FXBACKOFFICE,
        port=int(settings.DB_PORT),
        charset=settings.DB_CHARSET,
        cursorclass=pymysql.cursors.DictCursor,
    )


def get_account_details(
    account_ids: Iterable[int | str],
) -> tuple[dict[str, dict], bool]:
    """Batch-lookup `{account_id_str: {client_id, chinese_name}}`.

    Args:
        account_ids: Iterable of MT LOGINs. Ints and strs both accepted;
            keys in the returned dict are always strings (matches legacy
            behavior, and lets callers look up by the same str they had).

    Returns:
        ``(mapping, db_reachable)``:
        - **mapping** — keyed by str(LOGIN). Missing accounts (not in
          ``fxbackoffice.mt4_users``) are absent. Values:
          ``{"client_id": 123456, "chinese_name": "张三"}`` (either field may
          be None).
        - **db_reachable** — ``False`` only when we could not open a MySQL
          connection. ``True`` if we connected and ran the query(ies), even
          when mapping is empty (no matching LOGIN rows). This lets
          :func:`login_ip_search_service.perform_search` tell "slave down"
          from "LOGIN not in CRM mt4_users".

    Failure handling:
        On connection failure: ``({}, False)``. The daily report and other
        callers that only need best-effort names can ignore the second value
        and still render with an empty mapping.
    """
    ids_list = [str(x) for x in account_ids if str(x).isdigit()]
    ids_list = list(dict.fromkeys(ids_list))  # de-dupe, preserve order
    if not ids_list:
        return {}, True

    settings = get_settings()
    results: dict[str, dict] = {}

    try:
        conn = _connect_fxbackoffice(settings)
    except Exception as exc:
        # Network blip / credentials rotated / slave down — log loudly but
        # don't raise. Daily report should still go out without names.
        logger.warning("enrichment: MySQL connect failed: %s", exc)
        return {}, False

    try:
        with conn.cursor() as cursor:
            # Chunk the IN clause so we don't blow up max_allowed_packet on
            # huge correlated-account lists (observed a few hundred in prod).
            for start in range(0, len(ids_list), _IN_CLAUSE_CHUNK):
                chunk = ids_list[start : start + _IN_CLAUSE_CHUNK]
                placeholders = ", ".join(["%s"] * len(chunk))
                # CRM table holds MT4 + MT5; filter by trading LOGIN.
                sql = f"""
                    SELECT
                        u.LOGIN       AS account_id,
                        u.userId      AS client_id,
                        cf.v          AS chinese_name,
                        us.firstname  AS first_name,
                        us.lastname   AS last_name
                    FROM fxbackoffice.mt4_users u
                    LEFT JOIN fxbackoffice.user_custom_fields cf
                      ON u.userId = cf.userid AND cf.k = 'custom_chinese_name'
                    LEFT JOIN fxbackoffice.users us
                      ON us.id = u.userId
                    WHERE u.LOGIN IN ({placeholders})
                """
                cursor.execute(sql, tuple(chunk))
                for row in cursor.fetchall():
                    results[str(row["account_id"])] = {
                        "client_id": row["client_id"],
                        "chinese_name": row["chinese_name"],
                        "first_name": row.get("first_name"),
                        "last_name": row.get("last_name"),
                    }
    except Exception as exc:
        # Log, return partial results — some chunks may have succeeded.
        logger.warning("enrichment: MySQL query failed: %s", exc)
    finally:
        try:
            conn.close()
        except Exception:
            pass

    logger.info(
        "enrichment: looked up %d id(s), resolved %d (with chinese_name: %d)",
        len(ids_list),
        len(results),
        sum(1 for v in results.values() if v.get("chinese_name")),
    )
    return results, True
