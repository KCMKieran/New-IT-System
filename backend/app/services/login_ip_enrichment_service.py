"""
Login IP Monitor — MySQL enrichment service.

Ports the `get_account_details()` function from the legacy
`46-MT-Server-Login-Detect/search.py` to the new platform.

Purpose
-------
Given a list of MT account IDs (e.g. the "correlated" accounts surfaced by
the daily report or the manual search API), enrich each one with its
business-side metadata:

    account_id -> {client_id: int | None, chinese_name: str | None}

Daily report uses `chinese_name` for the email HTML.
Search API (Phase 6) uses `client_id` to filter out "same customer" noise.

Why the legacy SQL goes unchanged
---------------------------------
The query does a cross-schema JOIN:

    mt4_live.mt4_users              u
    LEFT JOIN fxbackoffice.user_custom_fields cf
      ON u.ID = cf.userid AND cf.k = 'custom_chinese_name'

Only the fxbackoffice READ slave credentials (`DB_*` in backend/.env) have
SELECT rights across both `mt4_live` and `fxbackoffice` on the same server,
which is exactly what this JOIN requires. The main `MYSQL_*` credentials are
for the ETL source — using those here would be a layering violation.

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


def get_account_details(account_ids: Iterable[int | str]) -> dict[str, dict]:
    """Batch-lookup `{account_id_str: {client_id, chinese_name}}`.

    Args:
        account_ids: Iterable of MT LOGINs. Ints and strs both accepted;
            keys in the returned dict are always strings (matches legacy
            behavior, and lets callers look up by the same str they had).

    Returns:
        Dict keyed by str(account_id). Missing accounts (not in mt4_users)
        are simply absent from the result — callers must `.get()` with a
        default. Values look like:
            {"client_id": 123456, "chinese_name": "张三"}
        Either field may be None: `client_id` is None only if the MT user
        row itself is missing (shouldn't happen in practice); `chinese_name`
        is None when the customer hasn't filled in a Chinese name yet.

    Failure handling:
        Returns an **empty dict** (not exception) on DB connection failure,
        matching legacy behavior. Callers should not rely on enrichment
        being available — the report service must still work (but without
        Chinese names) if the DB is down.
    """
    ids_list = [str(x) for x in account_ids if str(x).isdigit()]
    ids_list = list(dict.fromkeys(ids_list))  # de-dupe, preserve order
    if not ids_list:
        return {}

    settings = get_settings()
    results: dict[str, dict] = {}

    try:
        conn = _connect_fxbackoffice(settings)
    except Exception as exc:
        # Network blip / credentials rotated / slave down — log loudly but
        # don't raise. Daily report should still go out without names.
        logger.warning("enrichment: MySQL connect failed: %s", exc)
        return {}

    try:
        with conn.cursor() as cursor:
            # Chunk the IN clause so we don't blow up max_allowed_packet on
            # huge correlated-account lists (observed a few hundred in prod).
            for start in range(0, len(ids_list), _IN_CLAUSE_CHUNK):
                chunk = ids_list[start : start + _IN_CLAUSE_CHUNK]
                placeholders = ", ".join(["%s"] * len(chunk))
                # IMPORTANT: leave fully-qualified schema names in the SQL.
                # `database=fxbackoffice` just sets the default; the JOIN
                # below needs to reach mt4_live, which is a separate schema
                # on the same slave instance.
                sql = f"""
                    SELECT
                        u.LOGIN AS account_id,
                        u.ID    AS client_id,
                        cf.v    AS chinese_name
                    FROM mt4_live.mt4_users u
                    LEFT JOIN fxbackoffice.user_custom_fields cf
                      ON u.ID = cf.userid AND cf.k = 'custom_chinese_name'
                    WHERE u.LOGIN IN ({placeholders})
                """
                cursor.execute(sql, tuple(chunk))
                for row in cursor.fetchall():
                    results[str(row["account_id"])] = {
                        "client_id": row["client_id"],
                        "chinese_name": row["chinese_name"],
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
    return results
