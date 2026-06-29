from __future__ import annotations

import threading
import time
from typing import Any
from concurrent.futures import ThreadPoolExecutor

import pymysql

from ..core.config import Settings
from ..core.logging_config import get_logger

logger = get_logger(__name__)

# Module-level TTL cache for the excluded (demo/test) groupsids. These change
# rarely, but the snapshot scheduler calls _get_excluded_groupsids every 60s,
# so re-querying each tick is wasteful. Cache the list for 1h; a simple
# time-checked global guarded by a Lock is enough for the scheduler thread.
_EXCLUDED_GROUPSIDS_TTL_SEC = 3600
_excluded_groupsids_cache: list[str] | None = None
_excluded_groupsids_cached_at: float = 0.0
_excluded_groupsids_lock = threading.Lock()


def _get_excluded_groupsids(settings: Settings) -> list[str]:
    """
    Fetch the groupsids that should be excluded (demo/test groups).

    Result is cached at module level for _EXCLUDED_GROUPSIDS_TTL_SEC (1h) so
    repeated scheduler ticks reuse it instead of re-querying MySQL every call;
    on cache miss/expiry it re-queries. On query failure returns [] (and does
    not poison the cache) so callers degrade gracefully.
    """
    global _excluded_groupsids_cache, _excluded_groupsids_cached_at

    now = time.monotonic()
    with _excluded_groupsids_lock:
        if (
            _excluded_groupsids_cache is not None
            and now - _excluded_groupsids_cached_at < _EXCLUDED_GROUPSIDS_TTL_SEC
        ):
            return _excluded_groupsids_cache

    sql = """
        SELECT groupsid
        FROM fxbackoffice.groups
        WHERE groupsid LIKE '%demo%' OR groupsid LIKE '%test%'
    """
    try:
        conn = pymysql.connect(
            host=settings.DB_HOST,
            user=settings.DB_USER,
            password=settings.DB_PASSWORD,
            database=settings.FXBACK_DB_NAME,
            port=int(settings.DB_PORT),
            charset=settings.DB_CHARSET,
            cursorclass=pymysql.cursors.DictCursor,
            connect_timeout=5,
            read_timeout=20,
        )
        with conn:
            with conn.cursor() as cur:
                cur.execute(sql)
                rows = cur.fetchall()
        result = [row["groupsid"] for row in rows]
        with _excluded_groupsids_lock:
            _excluded_groupsids_cache = result
            _excluded_groupsids_cached_at = time.monotonic()
        return result
    except Exception as e:
        logger.error(f"Failed to fetch excluded groupsids: {e}")
        return []


def compute_net_position(volume_buy: float, volume_sell: float) -> float:
    """Net position = total Buy lots − total Sell lots.

    Positive ⇒ the company is net long, negative ⇒ net short. Pure helper so
    the snapshot scheduler and the detail query share one definition (and it
    is trivially unit-testable).
    """
    return (volume_buy or 0.0) - (volume_sell or 0.0)


_SID_MAP = {"mt4_live": 1, "mt4_live2": 6, "mt5": 5}


def get_xauusd_position_detail(settings: Settings) -> dict[str, Any]:
    """Per (server, symbol) open-position detail for all XAUUSD* products.

    Runs across the 3 servers in parallel (same pattern as the cross-server
    summary) but, unlike that summary, keeps the **product dimension** —
    GROUP BY t.SYMBOL with a `SYMBOL LIKE 'XAUUSD%'` filter — so each
    (server, symbol) combination is its own row. Cent accounts (.kcmc/.cent)
    are scaled /100, and the standard demo/test exclusion is reused.

    Returns {"ok": bool, "items": [{server, symbol, volume_buy, volume_sell,
    net_position}, ...]}. Used by the minute-level snapshot scheduler.
    """
    sources = ["mt4_live", "mt4_live2", "mt5"]

    excluded_groupsids = _get_excluded_groupsids(settings)
    if excluded_groupsids:
        groupsid_placeholders = ", ".join(
            [f"%(excluded_g{i})s" for i in range(len(excluded_groupsids))]
        )
        groupsid_condition = f"OR u.groupsid IN ({groupsid_placeholders})"
        groupsid_params = {f"excluded_g{i}": g for i, g in enumerate(excluded_groupsids)}
    else:
        groupsid_condition = ""
        groupsid_params = {}

    sql = f"""
        SELECT
          t.SYMBOL AS symbol,
          SUM(CASE WHEN t.CMD = 0 THEN
            (CASE WHEN t.SYMBOL LIKE '%%.kcmc' OR t.SYMBOL LIKE '%%.cent' THEN t.lots / 100 ELSE t.lots END)
          ELSE 0 END) AS volume_buy,
          SUM(CASE WHEN t.CMD = 1 THEN
            (CASE WHEN t.SYMBOL LIKE '%%.kcmc' OR t.SYMBOL LIKE '%%.cent' THEN t.lots / 100 ELSE t.lots END)
          ELSE 0 END) AS volume_sell
        FROM mt4_trades t
        WHERE t.sid = %(sid)s
          AND t.SYMBOL LIKE 'XAUUSD%%'
          AND t.closeDate = '1970-01-01'
          AND t.CMD IN (0, 1)
          AND t.LOGIN NOT LIKE '7%%'
          AND NOT EXISTS (
            SELECT 1
            FROM mt4_users u
            WHERE u.LOGIN = t.LOGIN
              AND u.sid = t.sid
              AND (
                u.NAME LIKE %(like_test)s
                OR (
                    (u.`GROUP` LIKE %(like_test)s OR u.NAME LIKE %(like_test)s)
                    AND (u.`GROUP` LIKE %(like_kcm)s OR u.`GROUP` LIKE %(like_testkcm)s)
                )
                {groupsid_condition}
              )
          )
        GROUP BY t.SYMBOL
        ORDER BY t.SYMBOL
        """

    def fetch_source_data(source_name: str) -> list[dict[str, Any]]:
        sid = _SID_MAP.get(source_name)
        params = {
            "sid": sid,
            "like_test": "%test%",
            "like_kcm": "KCM%",
            "like_testkcm": "testKCM%",
            **groupsid_params,
        }
        conn = pymysql.connect(
            host=settings.DB_HOST,
            user=settings.DB_USER,
            password=settings.DB_PASSWORD,
            database=settings.FXBACK_DB_NAME,
            port=int(settings.DB_PORT),
            charset=settings.DB_CHARSET,
            cursorclass=pymysql.cursors.DictCursor,
            connect_timeout=5,
            read_timeout=20,
        )
        with conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                rows = cur.fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            volume_buy = float(row.get("volume_buy") or 0.0)
            volume_sell = float(row.get("volume_sell") or 0.0)
            out.append(
                {
                    "server": source_name,
                    "symbol": row.get("symbol") or "",
                    "volume_buy": volume_buy,
                    "volume_sell": volume_sell,
                    "net_position": compute_net_position(volume_buy, volume_sell),
                }
            )
        return out

    def fetch_source_data_safe(source_name: str) -> tuple[str, list[dict[str, Any]], str | None]:
        """Wrap one server's fetch so a single failure can't zero out the batch.

        Returns (source, rows, error). On failure logs a warning and treats the
        server as empty so the union of the successful servers is still written.
        """
        try:
            return source_name, fetch_source_data(source_name), None
        except Exception as exc:  # noqa: BLE001 - isolate per-server failure
            logger.warning(
                "XAUUSD detail fetch failed for %s; treating as empty: %s",
                source_name,
                exc,
            )
            return source_name, [], str(exc)

    try:
        with ThreadPoolExecutor(max_workers=3) as executor:
            results = list(executor.map(fetch_source_data_safe, sources))
    except Exception as exc:
        # Executor-level failure (not a per-server query error) — keep the old
        # all-or-nothing fallback so the caller still sees ok=False.
        logger.error(f"Error in get_xauusd_position_detail: {exc}")
        return {"ok": False, "items": [], "error": str(exc)}

    items = [row for _src, server_rows, _err in results for row in server_rows]
    failed_servers = [src for src, _rows, err in results if err is not None]
    out: dict[str, Any] = {"ok": True, "items": items}
    if failed_servers:
        out["failed_servers"] = failed_servers
    return out


def get_open_positions_today(settings: Settings, source: str = "mt4_live") -> dict[str, Any]:
    """
    Query all open positions using the consolidated mt4_trades table.
    - sid 1: MT4 Live
    - sid 6: MT4 Live2
    - sid 5: MT5
    - Uses closeDate = '1970-01-01' for optimized index usage.
    - Automatically handles cent accounts (.kcmc/.cent): both volume and profit / 100.
    - Excludes test/demo accounts based on groups table.
    """

    # Map source to sid
    sid_map = {
        "mt4_live": 1,
        "mt4_live2": 6,
        "mt5": 5
    }
    sid = sid_map.get(source, 1)

    # Pre-fetch excluded groupsids for better performance
    excluded_groupsids = _get_excluded_groupsids(settings)

    # Build dynamic IN clause for excluded groupsids using named params
    if excluded_groupsids:
        groupsid_placeholders = ", ".join([f"%(excluded_g{i})s" for i in range(len(excluded_groupsids))])
        groupsid_condition = f"OR u.groupsid IN ({groupsid_placeholders})"
        groupsid_params = {f"excluded_g{i}": g for i, g in enumerate(excluded_groupsids)}
    else:
        groupsid_condition = ""
        groupsid_params = {}

    sql = f"""
        SELECT
          t.SYMBOL AS symbol,
          -- Use 'lots' virtual column for volume; cent accounts (.kcmc/.cent) are scaled by 100
          SUM(CASE WHEN t.CMD = 0 THEN
            (CASE WHEN t.SYMBOL LIKE '%%.kcmc' OR t.SYMBOL LIKE '%%.cent' THEN t.lots / 100 ELSE t.lots END)
          ELSE 0 END) AS volume_buy,
          SUM(CASE WHEN t.CMD = 1 THEN
            (CASE WHEN t.SYMBOL LIKE '%%.kcmc' OR t.SYMBOL LIKE '%%.cent' THEN t.lots / 100 ELSE t.lots END)
          ELSE 0 END) AS volume_sell,
          -- Use 'totalProfit' (Profit+Swap+Comm) and handle cent accounts
          SUM(CASE WHEN t.CMD = 0 THEN 
            (CASE WHEN t.SYMBOL LIKE '%%.kcmc' OR t.SYMBOL LIKE '%%.cent' THEN t.totalProfit / 100 ELSE t.totalProfit END)
          ELSE 0 END) AS profit_buy,
          SUM(CASE WHEN t.CMD = 1 THEN 
            (CASE WHEN t.SYMBOL LIKE '%%.kcmc' OR t.SYMBOL LIKE '%%.cent' THEN t.totalProfit / 100 ELSE t.totalProfit END)
          ELSE 0 END) AS profit_sell,
          SUM(CASE WHEN t.SYMBOL LIKE '%%.kcmc' OR t.SYMBOL LIKE '%%.cent' THEN t.totalProfit / 100 ELSE t.totalProfit END) AS profit_total
        FROM mt4_trades t
        WHERE t.sid = %(sid)s
          AND t.closeDate = '1970-01-01'
          AND t.CMD IN (0, 1)
          AND t.LOGIN NOT LIKE '7%%'
          AND NOT EXISTS (
            SELECT 1
            FROM mt4_users u
            WHERE u.LOGIN = t.LOGIN 
              AND u.sid = t.sid
              AND (
                u.NAME LIKE %(like_test)s
                OR (
                    (u.`GROUP` LIKE %(like_test)s OR u.NAME LIKE %(like_test)s)
                    AND (u.`GROUP` LIKE %(like_kcm)s OR u.`GROUP` LIKE %(like_testkcm)s)
                )
                {groupsid_condition}
              )
          )
        GROUP BY t.SYMBOL
        ORDER BY t.SYMBOL
        """

    params = {
        "sid": sid,
        "like_test": "%test%",
        "like_kcm": "KCM%",
        "like_testkcm": "testKCM%",
        **groupsid_params,
    }

    try:
        conn = pymysql.connect(
            host=settings.DB_HOST,
            user=settings.DB_USER,
            password=settings.DB_PASSWORD,
            database=settings.FXBACK_DB_NAME,
            port=int(settings.DB_PORT),
            charset=settings.DB_CHARSET,
            cursorclass=pymysql.cursors.DictCursor,
        )

        with conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                rows = cur.fetchall()

        return {"ok": True, "items": rows}
    except Exception as exc:
        logger.error(f"Error in get_open_positions_today: {exc}")
        return {"ok": False, "items": [], "error": str(exc)}


def get_symbol_cross_server_summary(settings: Settings, symbol: str) -> dict[str, Any]:
    """
    Query a specific symbol's summary across all servers (mt4_live, mt4_live2, mt5).
    Excludes test/demo accounts based on groups table.
    """
    sources = ["mt4_live", "mt4_live2", "mt5"]
    sid_map = {
        "mt4_live": 1,
        "mt4_live2": 6,
        "mt5": 5
    }

    # Pre-fetch excluded groupsids once before parallel execution
    excluded_groupsids = _get_excluded_groupsids(settings)

    # Build dynamic IN clause for excluded groupsids
    if excluded_groupsids:
        groupsid_placeholders = ", ".join([f"%(excluded_g{i})s" for i in range(len(excluded_groupsids))])
        groupsid_condition = f"OR u.groupsid IN ({groupsid_placeholders})"
        groupsid_params = {f"excluded_g{i}": g for i, g in enumerate(excluded_groupsids)}
    else:
        groupsid_condition = ""
        groupsid_params = {}

    sql = f"""
        SELECT
          %(source)s AS source,
          -- Use GROUP_CONCAT to show which symbols were included
          GROUP_CONCAT(DISTINCT t.SYMBOL SEPARATOR ', ') AS symbol,
          -- cent accounts (.kcmc/.cent) are scaled by 100
          SUM(CASE WHEN t.CMD = 0 THEN
            (CASE WHEN t.SYMBOL LIKE '%%.kcmc' OR t.SYMBOL LIKE '%%.cent' THEN t.lots / 100 ELSE t.lots END)
          ELSE 0 END) AS volume_buy,
          SUM(CASE WHEN t.CMD = 1 THEN
            (CASE WHEN t.SYMBOL LIKE '%%.kcmc' OR t.SYMBOL LIKE '%%.cent' THEN t.lots / 100 ELSE t.lots END)
          ELSE 0 END) AS volume_sell,
          SUM(CASE WHEN t.CMD = 0 THEN 
            (CASE WHEN t.SYMBOL LIKE '%%.kcmc' OR t.SYMBOL LIKE '%%.cent' THEN t.totalProfit / 100 ELSE t.totalProfit END)
          ELSE 0 END) AS profit_buy,
          SUM(CASE WHEN t.CMD = 1 THEN 
            (CASE WHEN t.SYMBOL LIKE '%%.kcmc' OR t.SYMBOL LIKE '%%.cent' THEN t.totalProfit / 100 ELSE t.totalProfit END)
          ELSE 0 END) AS profit_sell,
          SUM(CASE WHEN t.SYMBOL LIKE '%%.kcmc' OR t.SYMBOL LIKE '%%.cent' THEN t.totalProfit / 100 ELSE t.totalProfit END) AS profit_total
        FROM mt4_trades t
        WHERE t.sid = %(sid)s
          AND (
            CASE 
              WHEN %(is_fuzzy)s = 1 THEN t.SYMBOL LIKE CONCAT(%(symbol)s, '%%')
              ELSE t.SYMBOL = %(symbol)s
            END
          )
          AND t.closeDate = '1970-01-01'
          AND t.CMD IN (0, 1)
          AND t.LOGIN NOT LIKE '7%%'
          AND NOT EXISTS (
            SELECT 1
            FROM mt4_users u
            WHERE u.LOGIN = t.LOGIN 
              AND u.sid = t.sid
              AND (
                u.NAME LIKE %(like_test)s
                OR (
                    (u.`GROUP` LIKE %(like_test)s OR u.NAME LIKE %(like_test)s)
                    AND (u.`GROUP` LIKE %(like_kcm)s OR u.`GROUP` LIKE %(like_testkcm)s)
                )
                {groupsid_condition}
              )
          )
        """

    def fetch_source_data(source_name: str):
        sid = sid_map.get(source_name)
        # Check if it's a fuzzy search (e.g., 'XAUUSD (Related)')
        is_fuzzy = 1 if " (Related)" in symbol else 0
        clean_symbol = symbol.replace(" (Related)", "")

        params = {
            "source": source_name,
            "sid": sid,
            "symbol": clean_symbol,
            "is_fuzzy": is_fuzzy,
            "like_test": "%test%",
            "like_kcm": "KCM%",
            "like_testkcm": "testKCM%",
            **groupsid_params,
        }
        
        try:
            conn = pymysql.connect(
                host=settings.DB_HOST,
                user=settings.DB_USER,
                password=settings.DB_PASSWORD,
                database=settings.FXBACK_DB_NAME,
                port=int(settings.DB_PORT),
                charset=settings.DB_CHARSET,
                cursorclass=pymysql.cursors.DictCursor,
            )
            with conn:
                with conn.cursor() as cur:
                    cur.execute(sql, params)
                    res = cur.fetchone()
                    if not res:
                        # Return empty record if no data for this server
                        return {
                            "source": source_name,
                            "symbol": symbol,
                            "volume_buy": 0.0,
                            "volume_sell": 0.0,
                            "profit_buy": 0.0,
                            "profit_sell": 0.0,
                            "profit_total": 0.0
                        }
                    # Replace None values with 0 to prevent frontend/calculation issues
                    return {
                        "source": res.get("source") or source_name,
                        "symbol": res.get("symbol") or symbol,
                        "volume_buy": res.get("volume_buy") or 0.0,
                        "volume_sell": res.get("volume_sell") or 0.0,
                        "profit_buy": res.get("profit_buy") or 0.0,
                        "profit_sell": res.get("profit_sell") or 0.0,
                        "profit_total": res.get("profit_total") or 0.0,
                    }
        except Exception as e:
            logger.error(f"Error fetching data for {source_name}: {e}")
            return {
                "source": source_name,
                "symbol": symbol,
                "volume_buy": 0.0,
                "volume_sell": 0.0,
                "profit_buy": 0.0,
                "profit_sell": 0.0,
                "profit_total": 0.0,
                "error": str(e)
            }

    try:
        with ThreadPoolExecutor(max_workers=3) as executor:
            items = list(executor.map(fetch_source_data, sources))

        # Calculate Grand Total
        total = {
            "source": "Total",
            "symbol": symbol,
            # Use `or 0` to handle None values (dict.get only returns default when key is missing, not when value is None)
            "volume_buy": sum(i.get("volume_buy") or 0 for i in items),
            "volume_sell": sum(i.get("volume_sell") or 0 for i in items),
            "profit_buy": sum(i.get("profit_buy") or 0 for i in items),
            "profit_sell": sum(i.get("profit_sell") or 0 for i in items),
            "profit_total": sum(i.get("profit_total") or 0 for i in items),
        }

        return {"ok": True, "items": items, "total": total}
    except Exception as exc:
        logger.error(f"Error in get_symbol_cross_server_summary: {exc}")
        return {"ok": False, "items": [], "error": str(exc)}



