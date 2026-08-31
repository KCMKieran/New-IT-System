from __future__ import annotations

from typing import List, Optional, Tuple, Dict, Any
from datetime import datetime as _dt
import math
import os

import psycopg2
from psycopg2.extras import RealDictCursor
import mysql.connector

from app.core.pg_session import (
    LOCK_PNL_USER_SUMMARY_MT4LIVE2,
    LOCK_PNL_USER_SUMMARY_MT5,
    harden_dsn,
    pg_session,
)


def _pg_mt5_dsn() -> str:
    host = os.getenv("POSTGRES_HOST", "localhost")
    port = int(os.getenv("POSTGRES_PORT", "5432"))
    db = os.getenv("POSTGRES_DBNAME_MT5")
    user = os.getenv("POSTGRES_USER", "postgres")
    password = os.getenv("POSTGRES_PASSWORD", "")
    return harden_dsn(
        f"host={host} port={port} dbname={db} user={user} password={password}",
        application_name="etl_pnl",
    )


def resolve_table_and_dataset(server: str) -> Tuple[str, str]:
    """Map server to source table and watermark dataset.

    - MT5       -> public.pnl_user_summary,          dataset='pnl_user_summary'
    - MT4Live2  -> public.pnl_user_summary_mt4live2, dataset='pnl_user_summary_mt4live2'
    - default   -> MT5 mapping
    """
    srv = (server or "").upper()
    if srv == "MT5":
        return "public.pnl_user_summary", "pnl_user_summary"
    if srv == "MT4LIVE2":
        return "public.pnl_user_summary_mt4live2", "pnl_user_summary_mt4live2"
    raise ValueError(f"Unsupported server: {server}")


def get_pnl_user_summary_paginated(
    page: int = 1,
    page_size: int = 100,
    sort_by: Optional[str] = None,
    sort_order: str = "asc",
    user_groups: Optional[List[str]] = None,
    search: Optional[str] = None,
    source_table: str = "public.pnl_user_summary",
    filters: Optional[Dict[str, Any]] = None,
) -> Tuple[List[dict], int, int]:
    """分页查询 public.pnl_user_summary

    Args:
        filters: 筛选条件字典，格式：{join:'AND'|'OR', rules:[{field,op,value,value2?}]}

    Returns: rows, total_count, total_pages
    """

    # 排序白名单（防注入）
    allowed_sort_fields = {
        "login", "symbol", "user_name", "user_group", "country", "zipcode", "currency", "user_id",
        "user_balance", "user_credit", "positions_floating_pnl", "equity",
        "closed_sell_volume_lots", "closed_sell_count", "closed_sell_profit", "closed_sell_swap",
        "closed_sell_overnight_count", "closed_sell_overnight_volume_lots",
        "closed_buy_volume_lots", "closed_buy_count", "closed_buy_profit", "closed_buy_swap",
        "closed_buy_overnight_count", "closed_buy_overnight_volume_lots",
        "total_commission", "deposit_count", "deposit_amount", "withdrawal_count",
        "withdrawal_amount", "net_deposit", "overnight_volume_ratio", "last_updated",
        # 平仓总盈亏（含 swap）：从数据库字段 closed_total_profit_with_swap 映射
        "closed_total_profit",
        # 计算列（前端聚合列）的排序别名
        "overnight_volume_all", "total_volume_all", "overnight_order_all", "total_order_all",
    }

    where_conditions: List[str] = []
    params: List[object] = []

    # 组别筛选 + 特殊选项（与旧 /pnl 行为保持一致）
    if user_groups:
        cleaned = [g.strip() for g in user_groups if g and g.strip()]
        if cleaned:
            if "__ALL__" in cleaned:
                # 全部组别：不加组别条件，但允许处理排除客户名 test 的条件
                if "__EXCLUDE_USER_NAME_TEST__" in cleaned:
                    where_conditions.append("user_name NOT ILIKE %s")
                    params.append("%test%")
                if "__EXCLUDE_GROUP_NAME_TEST__" in cleaned:
                    where_conditions.append("user_group NOT ILIKE %s")
                    params.append("%test%")
            elif "__NONE__" in cleaned:
                # 显式要求返回 0 行
                where_conditions.append("1 = 0")
            else:
                # 分离常规组别与特殊筛选
                regular_groups = [g for g in cleaned if g not in ["__USER_NAME_TEST__", "__EXCLUDE_USER_NAME_TEST__", "__EXCLUDE_GROUP_NAME_TEST__"]]
                has_user_name_test = "__USER_NAME_TEST__" in cleaned
                has_exclude_user_name_test = "__EXCLUDE_USER_NAME_TEST__" in cleaned
                has_exclude_group_name_test = "__EXCLUDE_GROUP_NAME_TEST__" in cleaned

                group_conditions: List[str] = []

                # 支持 'manager' 虚拟组别：匹配以 'managers\' 开头的真实组别
                include_manager = any(g.lower() == 'manager' for g in regular_groups)
                exact_groups = [g for g in regular_groups if g.lower() != 'manager']

                if exact_groups:
                    if len(exact_groups) == 1:
                        group_conditions.append("user_group = %s")
                        params.append(exact_groups[0])
                    else:
                        placeholders = ",".join(["%s"] * len(exact_groups))
                        group_conditions.append(f"user_group IN ({placeholders})")
                        params.extend(exact_groups)

                if include_manager:
                    # ILIKE 前缀匹配 'managers\%'; 指定 ESCAPE 字符避免 \
                    # 对 % 产生转义，确保 % 作为通配符生效
                    group_conditions.append("user_group ILIKE %s ESCAPE '|'")
                    params.append("managers\\%")

                if has_user_name_test:
                    group_conditions.append("user_name ILIKE %s")
                    params.append("%test%")

                if group_conditions:
                    combined = "(" + " OR ".join(group_conditions) + ")"
                    where_conditions.append(combined)

                if has_exclude_user_name_test:
                    where_conditions.append("user_name NOT ILIKE %s")
                    params.append("%test%")
                if has_exclude_group_name_test:
                    where_conditions.append("user_group NOT ILIKE %s")
                    params.append("%test%")

    # 统一搜索（login/user_id 精确 或 user_name 模糊）
    if search is not None:
        s = str(search).strip()
        if s:
            sub = []
            try:
                login_int = int(s)
                sub.append("login = %s")
                params.append(login_int)
                # 数值输入时，额外匹配 user_id 精确等于
                sub.append("user_id = %s")
                params.append(login_int)
            except ValueError:
                pass
            sub.append("user_name ILIKE %s")
            params.append(f"%{s}%")
            where_conditions.append("(" + " OR ".join(sub) + ")")

    # 解析筛选条件（filters）
    if filters and isinstance(filters, dict):
        join_type = filters.get("join", "AND")
        rules = filters.get("rules", [])
        
        if rules:
            filter_conditions: List[str] = []
            for rule in rules:
                field = rule.get("field")
                op = rule.get("op")
                value = rule.get("value")
                value2 = rule.get("value2")
                
                # 字段与操作符白名单校验（防注入）
                allowed_filter_fields = {
                    "login", "symbol", "user_name", "user_group", "country", "zipcode", "currency", "user_id",
                    "user_balance", "user_credit", "positions_floating_pnl", "equity",
                    "closed_sell_volume_lots", "closed_sell_count", "closed_sell_profit", "closed_sell_swap",
                    "closed_sell_overnight_count", "closed_sell_overnight_volume_lots",
                    "closed_buy_volume_lots", "closed_buy_count", "closed_buy_profit", "closed_buy_swap",
                    "closed_buy_overnight_count", "closed_buy_overnight_volume_lots",
                    "total_commission", "deposit_count", "deposit_amount", "withdrawal_count",
                    "withdrawal_amount", "net_deposit", "closed_total_profit", "overnight_volume_ratio", "last_updated",
                }
                allowed_operators = {
                    # 文本操作符
                    "contains", "not_contains", "equals", "not_equals", "starts_with", "ends_with", "blank", "not_blank",
                    # 数字/日期操作符
                    "=", "!=", ">", ">=", "<", "<=", "between", "on", "before", "after",
                }
                
                if field not in allowed_filter_fields:
                    continue  # 跳过非法字段
                if op not in allowed_operators:
                    continue  # 跳过非法操作符
                
                # 字段映射：closed_total_profit -> closed_total_profit_with_swap
                db_field = "closed_total_profit_with_swap" if field == "closed_total_profit" else field
                
                # 生成 SQL 条件
                condition = _build_filter_condition(db_field, op, value, value2, params)
                if condition:
                    filter_conditions.append(condition)
            
            # 组合所有筛选条件
            if filter_conditions:
                combined = f"({f' {join_type} '.join(filter_conditions)})"
                where_conditions.append(combined)

    where_clause = " WHERE " + " AND ".join(where_conditions) if where_conditions else ""

    base_select = (
        "SELECT login, symbol, user_name, user_group, country, zipcode, currency, user_id, "
        "user_balance, user_credit, positions_floating_pnl, equity, "
        "closed_sell_volume_lots, closed_sell_count, closed_sell_profit, closed_sell_swap, "
        "closed_sell_overnight_count, closed_sell_overnight_volume_lots, "
        "closed_buy_volume_lots, closed_buy_count, closed_buy_profit, closed_buy_swap, "
        "closed_buy_overnight_count, closed_buy_overnight_volume_lots, "
        "total_commission, deposit_count, deposit_amount, withdrawal_count, withdrawal_amount, net_deposit, "
        # 平仓总盈亏（含 swap）：从数据库字段 closed_total_profit_with_swap 映射为 closed_total_profit
        "closed_total_profit_with_swap AS closed_total_profit, "
        "overnight_volume_ratio, last_updated "
        f"FROM {source_table}" + where_clause
    )

    # 排序
    order_clause = ""
    # 计算列映射到 SQL 可排序表达式（只使用白名单内的安全表达式）
    alias_sort_expressions = {
        "overnight_volume_all": "(closed_buy_overnight_volume_lots + closed_sell_overnight_volume_lots)",
        "total_volume_all": "(closed_buy_volume_lots + closed_sell_volume_lots)",
        "overnight_order_all": "(closed_buy_overnight_count + closed_sell_overnight_count)",
        "total_order_all": "(closed_buy_count + closed_sell_count)",
        # closed_total_profit 现在直接映射到数据库字段 closed_total_profit_with_swap，无需额外映射
    }

    if sort_by and sort_by in allowed_sort_fields:
        direction = "DESC" if sort_order.lower() == "desc" else "ASC"
        sort_expression = alias_sort_expressions.get(sort_by, sort_by)
        # 加入 login 作为二级排序，保证稳定分页顺序；对可能为 NULL 的字段添加 NULLS LAST
        order_clause = f" ORDER BY {sort_expression} {direction} NULLS LAST, login ASC"
    else:
        order_clause = " ORDER BY login ASC"

    offset = (page - 1) * page_size
    paginated_sql = base_select + order_clause + " LIMIT %s OFFSET %s"

    count_sql = f"SELECT COUNT(*) FROM {source_table}" + where_clause

    dsn = _pg_mt5_dsn()
    with pg_session(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(count_sql, params)
            total_count = cur.fetchone()[0]

        total_pages = math.ceil(total_count / page_size) if total_count > 0 else 0

        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(paginated_sql, params + [page_size, offset])
            rows = cur.fetchall()
            return [dict(r) for r in rows], total_count, total_pages



def get_etl_watermark_last_updated(dataset: str = "pnl_user_summary") -> Optional[_dt]:
    """查询 public.etl_watermarks 中指定 dataset 的 last_updated（UTC+0）

    返回 None 表示不存在记录。
    """
    dsn = _pg_mt5_dsn()
    with pg_session(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT last_updated FROM public.etl_watermarks WHERE dataset = %s LIMIT 1",
                (dataset,),
            )
            row = cur.fetchone()
            return row[0] if row else None


def get_user_groups_from_user_summary(source_table: str = "public.pnl_user_summary") -> List[str]:
    """从 public.pnl_user_summary 去重获取组别，并将以 'managers\' 开头的归并为 'manager'。

    Returns:
        List[str]: 归一化后的组别列表，按字母排序（不区分大小写）。
    """
    dsn = _pg_mt5_dsn()
    with pg_session(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT DISTINCT user_group
                FROM {source_table}
                WHERE user_group IS NOT NULL
                  AND TRIM(user_group) != ''
                """
            )
            rows = cur.fetchall()

    normalized: List[str] = []
    for r in rows:
        g = str(r[0]).strip()
        if g.lower().startswith('managers\\'):
            normalized.append('manager')
        else:
            normalized.append(g)

    # 去重并按不区分大小写排序
    dedup = sorted(set(normalized), key=lambda s: s.lower())
    return dedup


# ------------------- Incremental refresh (MT5) -------------------

def _pg_mt5_dsn_forced_db(dbname: str) -> str:
    host = os.getenv("POSTGRES_HOST", "localhost")
    port = int(os.getenv("POSTGRES_PORT", "5432"))
    user = os.getenv("POSTGRES_USER", "postgres")
    password = os.getenv("POSTGRES_PASSWORD", "")
    return harden_dsn(
        f"host={host} port={port} dbname={dbname} user={user} password={password}",
        application_name="etl_pnl",
    )


def _mysql_mt5_cfg() -> Dict[str, Any]:
    return {
        "host": os.getenv("MYSQL_HOST"),
        "user": os.getenv("MYSQL_USER"),
        "password": os.getenv("MYSQL_PASSWORD"),
        "database": os.getenv("MYSQL_DATABASE"),
        "ssl_ca": os.getenv("MYSQL_SSL_CA"),
    }


# A client-side timeout only closes our own socket -- MySQL keeps executing and
# keeps holding metadata locks. That is exactly how the 2026-08-09 slave
# incident put 2,637 zombie threads on the replica. MAX_EXECUTION_TIME is the
# only guard that actually stops the server, so it is set below the client
# socket timeout, never above it.
_MYSQL_CONNECT_TIMEOUT_SEC = 10
_MYSQL_MAX_EXECUTION_TIME_MS = 15 * 60 * 1000  # matches the PG statement_timeout


def _connect_mysql_guarded(cfg: Dict[str, Any]):
    """Open a MySQL connection with the server-side execution cap armed."""
    conn = mysql.connector.connect(
        connection_timeout=_MYSQL_CONNECT_TIMEOUT_SEC, **cfg
    )
    try:
        with conn.cursor() as cur:
            cur.execute(f"SET SESSION MAX_EXECUTION_TIME={_MYSQL_MAX_EXECUTION_TIME_MS}")
    except Exception:
        conn.close()
        raise
    return conn


def mt5_incremental_refresh() -> Dict[str, Any]:
    """Run incremental ETL per provided reference design.

    - Force Postgres dbname = MT5_ETL
    - Use advisory lock to prevent concurrent runs
    - Ensure etl_watermarks, read last_deal_id
    - Insert new logins; aggregate deals deltas; upsert; update floating pnl; update watermarks
    - Maintain currency based on fxbackoffice.groups mapping (fallback to 'USD')
    """
    import time
    import os
    import logging

    logger = logging.getLogger(__name__)

    pg_dsn = _pg_mt5_dsn_forced_db("MT5_ETL")
    mysql_cfg = _mysql_mt5_cfg()

    result: Dict[str, Any] = {
        "success": False,
        "processed_rows": 0,
        "duration_seconds": 0.0,
        "new_max_deal_id": None,
        "new_trades_count": 0,
        "floating_only_count": 0,
        "message": None,
        "skipped": False,
    }

    # cache for groupName -> currency mapping
    group_currency_map: Dict[str, str] = {}

    def _load_group_currency_map() -> Dict[str, str]:
        """Load groupName->currency from fxbackoffice.groups once and cache."""
        nonlocal group_currency_map
        if group_currency_map:
            return group_currency_map

        fx_db_name = os.getenv("MYSQL_DATABASE_FXBO", "fxbackoffice")
        fx_mysql_cfg = {
            "host": mysql_cfg.get("host"),
            "user": mysql_cfg.get("user"),
            "password": mysql_cfg.get("password"),
            "database": fx_db_name,
            "ssl_ca": mysql_cfg.get("ssl_ca"),
        }

        mapping: Dict[str, str] = {}
        try:
            fx_conn = _connect_mysql_guarded(fx_mysql_cfg)
            try:
                with fx_conn.cursor(dictionary=True) as cur:
                    cur.execute("SELECT groupName, currency FROM `groups`")
                    for r in cur.fetchall():
                        group_name = r.get("groupName")
                        currency = r.get("currency")
                        if group_name and currency:
                            mapping[str(group_name)] = str(currency)
            finally:
                try:
                    fx_conn.close()
                except Exception:
                    pass
            logger.info(
                "Loaded %s group-to-currency mappings from fxbackoffice.groups",
                len(mapping),
            )
        except Exception as e:
            logger.warning(
                "Failed to load group-currency mapping from fxbackoffice.groups: %s. Using empty mapping.",
                e,
            )

        group_currency_map = mapping
        return mapping

    start_ts = time.time()

    # Basic env validation
    if not all(
        [
            mysql_cfg.get("host"),
            mysql_cfg.get("user"),
            mysql_cfg.get("password"),
            mysql_cfg.get("database"),
        ]
    ):
        raise RuntimeError("Missing required MySQL env vars for MT5 incremental refresh")

    with pg_session(pg_dsn) as pg_conn:
        pg_conn.autocommit = False

        with pg_conn.cursor() as cur:
            # advisory lock
            cur.execute("SELECT pg_try_advisory_lock(%s)", (LOCK_PNL_USER_SUMMARY_MT5,))
            locked = bool(cur.fetchone()[0])
            pg_conn.commit()

        if not locked:
            # another run is in progress
            # A rejected lock is not a failure, but it is emphatically not a
            # refresh either -- the caller must be able to tell the difference.
            result["success"] = True
            result["skipped"] = True
            result["message"] = "Another incremental run is in progress"
            return result

        try:
            # ensure watermarks table
            with pg_conn.cursor() as cur:
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS public.etl_watermarks (
                      dataset       text        NOT NULL,
                      partition_key text        NOT NULL DEFAULT 'ALL',
                      last_deal_id  bigint,
                      last_time     timestamptz,
                      last_login    bigint,
                      last_updated  timestamptz NOT NULL DEFAULT now(),
                      CONSTRAINT pk_etl_watermarks PRIMARY KEY (dataset, partition_key)
                    );
                    """
                )
                pg_conn.commit()

            # read last_deal_id
            last_deal_id: int = 0
            with pg_conn.cursor() as cur:
                cur.execute(
                    "SELECT last_deal_id FROM public.etl_watermarks WHERE dataset=%s AND partition_key=%s",
                    ("pnl_user_summary", "ALL"),
                )
                row = cur.fetchone()
                if row and row[0] is not None:
                    last_deal_id = int(row[0])
                else:
                    cur.execute(
                        "INSERT INTO public.etl_watermarks (dataset, partition_key, last_deal_id, last_updated) "
                        "VALUES (%s,%s,%s, now()) ON CONFLICT DO NOTHING",
                        ("pnl_user_summary", "ALL", 0),
                    )
                    pg_conn.commit()
                    last_deal_id = 0

            # connect to MySQL
            mysql_conn = _connect_mysql_guarded(mysql_cfg)

            try:
                # ---------- Step 0: insert new logins skeleton rows (with currency) ----------
                to_insert: List[Tuple] = []
                group_currency_map_local = _load_group_currency_map()

                with mysql_conn.cursor(dictionary=True) as cur:
                    cur.execute(
                        "SELECT u.Login, u.Name, u.`Group`, u.Country, u.ZipCode, u.ID, u.Balance, u.Credit "
                        "FROM mt5_live.mt5_users u"
                    )
                    for r in cur.fetchall():
                        user_group = r.get("Group")
                        currency = group_currency_map_local.get(
                            str(user_group) if user_group else "", "USD"
                        )
                        to_insert.append(
                            (
                                int(r["Login"]),
                                "ALL",
                                r.get("Name"),
                                user_group,
                                r.get("Country"),
                                r.get("ZipCode"),
                                r.get("ID") or None,
                                r.get("Balance") or 0,
                                r.get("Credit") or 0,
                                0,  # positions_floating_pnl placeholder
                                currency,
                            )
                        )

                if to_insert:
                    from psycopg2.extras import execute_values

                    with pg_conn.cursor() as cur:
                        execute_values(
                            cur,
                            "INSERT INTO public.pnl_user_summary ("
                            "  login, symbol, "
                            "  user_name, user_group, country, zipcode, user_id, "
                            "  user_balance, user_credit, positions_floating_pnl, "
                            "  currency"
                            ") VALUES %s "
                            "ON CONFLICT (login, symbol) DO UPDATE SET "
                            "  user_name=EXCLUDED.user_name, "
                            "  user_group=EXCLUDED.user_group, "
                            "  country=EXCLUDED.country, "
                            "  zipcode=EXCLUDED.zipcode, "
                            "  user_id=EXCLUDED.user_id, "
                            "  user_balance=EXCLUDED.user_balance, "
                            "  user_credit=EXCLUDED.user_credit, "
                            "  currency=EXCLUDED.currency",
                            to_insert,
                            page_size=5000,
                        )

                # ---------- Step 1: aggregate deltas since last_deal_id ----------
                sql = """
                WITH
                deals_window AS (
                  SELECT * FROM mt5_live.mt5_deals WHERE Deal > %s
                ),
                closed_deals AS (
                  SELECT
                    d.Login,
                    d.Action,
                    d.Entry,
                    d.Time AS close_time,
                    d.VolumeClosed AS volume_closed,
                    d.Profit,
                    d.Storage,
                    (
                      SELECT MIN(d2.Time)
                      FROM mt5_live.mt5_deals d2
                      WHERE d2.Login = d.Login AND d2.PositionID = d.PositionID AND d2.Entry = 0 AND d2.Time <= d.Time
                    ) AS open_time_for_close
                  FROM deals_window d
                  WHERE d.Entry IN (1, 3)
                ),
                closed_agg AS (
                  SELECT
                    Login,
                    CASE WHEN Action = 0 THEN 'BUY' WHEN Action = 1 THEN 'SELL' ELSE 'OTHER' END AS side,
                    SUM(volume_closed) / 10000.0 AS volume_lots,
                    COUNT(*) AS trade_count,
                    SUM(Profit) AS profit_sum,
                    SUM(Storage) AS swap_sum,
                    SUM(CASE WHEN DATE(close_time) <> DATE(open_time_for_close) THEN 1 ELSE 0 END) AS overnight_count,
                    SUM(CASE WHEN DATE(close_time) <> DATE(open_time_for_close) THEN volume_closed/10000.0 ELSE 0 END) AS overnight_volume_lots
                  FROM closed_deals GROUP BY Login, side
                ),
                commission_agg AS (
                  SELECT Login, SUM(Commission) AS total_commission
                  FROM deals_window
                  WHERE Entry IN (0, 2) AND Action IN (0, 1)
                  GROUP BY Login
                ),
                balance_agg AS (
                  SELECT
                    Login,
                    SUM(CASE WHEN Action = 2 AND Profit > 0 THEN 1 ELSE 0 END) AS deposit_count,
                    SUM(CASE WHEN Action = 2 AND Profit > 0 THEN Profit ELSE 0 END) AS deposit_amount,
                    SUM(CASE WHEN Action = 2 AND Profit < 0 THEN 1 ELSE 0 END) AS withdrawal_count,
                    SUM(CASE WHEN Action = 2 AND Profit < 0 THEN -Profit ELSE 0 END) AS withdrawal_amount
                  FROM deals_window GROUP BY Login
                ),
                max_deal AS (
                  SELECT COALESCE(MAX(Deal), %s) AS mx FROM deals_window
                )
                SELECT
                  u.Login,
                  u.Name          AS user_name,
                  u.`Group`       AS user_group,
                  u.Country,
                  u.ZipCode       AS zipcode,
                  u.ID            AS user_id,
                  u.Balance       AS user_balance,
                  u.Credit        AS user_credit,
                  COALESCE(MAX(CASE WHEN ca.side='SELL' THEN ca.volume_lots END), 0) AS closed_sell_volume_lots,
                  COALESCE(MAX(CASE WHEN ca.side='SELL' THEN ca.trade_count END), 0) AS closed_sell_count,
                  COALESCE(MAX(CASE WHEN ca.side='SELL' THEN ca.profit_sum END), 0) AS closed_sell_profit,
                  COALESCE(MAX(CASE WHEN ca.side='SELL' THEN ca.swap_sum END), 0) AS closed_sell_swap,
                  COALESCE(MAX(CASE WHEN ca.side='SELL' THEN ca.overnight_count END), 0) AS closed_sell_overnight_count,
                  COALESCE(MAX(CASE WHEN ca.side='SELL' THEN ca.overnight_volume_lots END), 0) AS closed_sell_overnight_volume_lots,
                  COALESCE(MAX(CASE WHEN ca.side='BUY' THEN ca.volume_lots END), 0) AS closed_buy_volume_lots,
                  COALESCE(MAX(CASE WHEN ca.side='BUY' THEN ca.trade_count END), 0) AS closed_buy_count,
                  COALESCE(MAX(CASE WHEN ca.side='BUY' THEN ca.profit_sum END), 0) AS closed_buy_profit,
                  COALESCE(MAX(CASE WHEN ca.side='BUY' THEN ca.swap_sum END), 0) AS closed_buy_swap,
                  COALESCE(MAX(CASE WHEN ca.side='BUY' THEN ca.overnight_count END), 0) AS closed_buy_overnight_count,
                  COALESCE(MAX(CASE WHEN ca.side='BUY' THEN ca.overnight_volume_lots END), 0) AS closed_buy_overnight_volume_lots,
                  COALESCE(cm.total_commission, 0) AS total_commission,
                  COALESCE(b.deposit_count, 0)     AS deposit_count,
                  COALESCE(b.deposit_amount, 0)    AS deposit_amount,
                  COALESCE(b.withdrawal_count, 0)  AS withdrawal_count,
                  COALESCE(b.withdrawal_amount, 0) AS withdrawal_amount,
                  (SELECT mx FROM max_deal) AS max_deal_id
                FROM mt5_live.mt5_users u
                LEFT JOIN closed_agg ca ON ca.Login = u.Login
                LEFT JOIN commission_agg cm ON cm.Login = u.Login
                LEFT JOIN balance_agg b ON b.Login = u.Login
                WHERE u.Login IN (
                  SELECT DISTINCT Login FROM deals_window
                )
                GROUP BY u.Login, u.Name, u.`Group`, u.Country, u.ZipCode, u.ID, u.Balance, u.Credit,
                         cm.total_commission, b.deposit_count, b.deposit_amount, b.withdrawal_count, b.withdrawal_amount
                """

                rows: List[Tuple] = []
                max_deal_id = last_deal_id
                new_trades_count = 0

                with mysql_conn.cursor(dictionary=True) as cur:
                    cur.execute(sql, (last_deal_id, last_deal_id))
                    fetched = cur.fetchall()
                    new_trades_count = len(fetched)
                    group_currency_map_local = _load_group_currency_map()
                    for r in fetched:
                        max_deal_id = max(max_deal_id, int(r["max_deal_id"] or last_deal_id))
                        user_group = r.get("user_group")
                        currency = group_currency_map_local.get(
                            str(user_group) if user_group else "", "USD"
                        )
                        rows.append(
                            (
                                int(r["Login"]),
                                "ALL",
                                r.get("user_name"),
                                user_group,
                                r.get("Country"),
                                r.get("ZipCode"),
                                r.get("user_id") or None,
                                r.get("user_balance") or 0,
                                r.get("user_credit") or 0,
                                r.get("closed_sell_volume_lots") or 0,
                                r.get("closed_sell_count") or 0,
                                r.get("closed_sell_profit") or 0,
                                r.get("closed_sell_swap") or 0,
                                r.get("closed_sell_overnight_count") or 0,
                                r.get("closed_sell_overnight_volume_lots") or 0,
                                r.get("closed_buy_volume_lots") or 0,
                                r.get("closed_buy_count") or 0,
                                r.get("closed_buy_profit") or 0,
                                r.get("closed_buy_swap") or 0,
                                r.get("closed_buy_overnight_count") or 0,
                                r.get("closed_buy_overnight_volume_lots") or 0,
                                r.get("total_commission") or 0,
                                r.get("deposit_count") or 0,
                                r.get("deposit_amount") or 0,
                                r.get("withdrawal_count") or 0,
                                r.get("withdrawal_amount") or 0,
                                currency,
                            )
                        )

                affected = 0
                if rows:
                    from psycopg2.extras import execute_values

                    upsert_sql = """
                    INSERT INTO public.pnl_user_summary (
                      login, symbol,
                      user_name, user_group, country, zipcode, user_id,
                      user_balance, user_credit,
                      closed_sell_volume_lots, closed_sell_count, closed_sell_profit, closed_sell_swap,
                      closed_sell_overnight_count, closed_sell_overnight_volume_lots,
                      closed_buy_volume_lots,  closed_buy_count,  closed_buy_profit,  closed_buy_swap,
                      closed_buy_overnight_count,  closed_buy_overnight_volume_lots,
                      total_commission,
                      deposit_count, deposit_amount, withdrawal_count, withdrawal_amount,
                      currency
                    ) VALUES %s
                    ON CONFLICT (login, symbol) DO UPDATE SET
                      user_name = EXCLUDED.user_name,
                      user_group = EXCLUDED.user_group,
                      country = EXCLUDED.country,
                      zipcode = EXCLUDED.zipcode,
                      user_id = EXCLUDED.user_id,
                      user_balance = EXCLUDED.user_balance,
                      user_credit = EXCLUDED.user_credit,
                      currency = EXCLUDED.currency,
                      closed_sell_volume_lots = public.pnl_user_summary.closed_sell_volume_lots + EXCLUDED.closed_sell_volume_lots,
                      closed_sell_count       = public.pnl_user_summary.closed_sell_count       + EXCLUDED.closed_sell_count,
                      closed_sell_profit      = public.pnl_user_summary.closed_sell_profit      + EXCLUDED.closed_sell_profit,
                      closed_sell_swap        = public.pnl_user_summary.closed_sell_swap        + EXCLUDED.closed_sell_swap,
                      closed_sell_overnight_count = public.pnl_user_summary.closed_sell_overnight_count + EXCLUDED.closed_sell_overnight_count,
                      closed_sell_overnight_volume_lots = public.pnl_user_summary.closed_sell_overnight_volume_lots + EXCLUDED.closed_sell_overnight_volume_lots,
                      closed_buy_volume_lots  = public.pnl_user_summary.closed_buy_volume_lots  + EXCLUDED.closed_buy_volume_lots,
                      closed_buy_count        = public.pnl_user_summary.closed_buy_count        + EXCLUDED.closed_buy_count,
                      closed_buy_profit       = public.pnl_user_summary.closed_buy_profit       + EXCLUDED.closed_buy_profit,
                      closed_buy_swap         = public.pnl_user_summary.closed_buy_swap         + EXCLUDED.closed_buy_swap,
                      closed_buy_overnight_count = public.pnl_user_summary.closed_buy_overnight_count + EXCLUDED.closed_buy_overnight_count,
                      closed_buy_overnight_volume_lots = public.pnl_user_summary.closed_buy_overnight_volume_lots + EXCLUDED.closed_buy_overnight_volume_lots,
                      total_commission        = public.pnl_user_summary.total_commission        + EXCLUDED.total_commission,
                      deposit_count           = public.pnl_user_summary.deposit_count           + EXCLUDED.deposit_count,
                      deposit_amount          = public.pnl_user_summary.deposit_amount          + EXCLUDED.deposit_amount,
                      withdrawal_count        = public.pnl_user_summary.withdrawal_count        + EXCLUDED.withdrawal_count,
                      withdrawal_amount       = public.pnl_user_summary.withdrawal_amount       + EXCLUDED.withdrawal_amount,
                      last_updated = now()
                    """
                    with pg_conn.cursor() as cur:
                        execute_values(cur, upsert_sql, rows, page_size=5000)
                        affected = len(rows)

                # ---------- Step 2: update floating pnl ----------
                floating_pairs: List[Tuple[int, float]] = []
                with mysql_conn.cursor() as cur:
                    cur.execute(
                        "SELECT Login, SUM(COALESCE(Profit,0) + COALESCE(Storage,0)) AS floating_pnl_total "
                        "FROM mt5_live.mt5_positions GROUP BY Login"
                    )
                    for r in cur.fetchall():
                        floating_pairs.append((int(r[0]), float(r[1] or 0.0)))

                if floating_pairs:
                    from psycopg2.extras import execute_values

                    with pg_conn.cursor() as cur:
                        cur.execute(
                            "CREATE TEMP TABLE tmp_floating (login bigint, symbol text, floating_pnl numeric) ON COMMIT DROP"
                        )
                        execute_values(
                            cur,
                            "INSERT INTO tmp_floating (login, symbol, floating_pnl) VALUES %s",
                            [(login, "ALL", pnl) for login, pnl in floating_pairs],
                            page_size=5000,
                        )
                        cur.execute(
                            "UPDATE public.pnl_user_summary s "
                            "SET positions_floating_pnl = t.floating_pnl, "
                            "    last_updated = CASE WHEN s.positions_floating_pnl IS DISTINCT FROM t.floating_pnl "
                            "                         THEN now() ELSE s.last_updated END "
                            "FROM tmp_floating t "
                            "WHERE s.login = t.login AND s.symbol = t.symbol "
                            "  AND s.positions_floating_pnl IS DISTINCT FROM t.floating_pnl"
                        )
                        # watermark last_time for floating
                        cur.execute(
                            "INSERT INTO public.etl_watermarks (dataset, partition_key, last_time, last_updated) "
                            "VALUES (%s,%s, now(), now()) "
                            "ON CONFLICT (dataset, partition_key) DO UPDATE SET last_time=now(), last_updated=now()",
                            ("pnl_user_summary", "ALL"),
                        )

                # ---------- Step 3: update watermark last_deal_id ----------
                with pg_conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO public.etl_watermarks (dataset, partition_key, last_deal_id, last_updated) "
                        "VALUES (%s,%s,%s, now()) "
                        "ON CONFLICT (dataset, partition_key) DO UPDATE SET "
                        "  last_deal_id=EXCLUDED.last_deal_id, last_updated=now()",
                        ("pnl_user_summary", "ALL", max_deal_id),
                    )

                pg_conn.commit()

                result["success"] = True
                result["processed_rows"] = affected
                result["new_max_deal_id"] = max_deal_id
                result["new_trades_count"] = new_trades_count
                # floating_only_count is approximate for UI
                result["floating_only_count"] = len(floating_pairs)
            finally:
                try:
                    if mysql_conn and mysql_conn.is_connected():
                        mysql_conn.close()
                except Exception:
                    pass
        except Exception:
            pg_conn.rollback()
            raise
        finally:
            with pg_conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_unlock(%s)", (LOCK_PNL_USER_SUMMARY_MT5,))
                pg_conn.commit()

    result["duration_seconds"] = round(time.time() - start_ts, 3)
    return result


# ------------------- Incremental refresh (MT4Live2) -------------------
def mt4live2_incremental_refresh() -> Dict[str, Any]:
    """Run MT4Live2 incremental ETL into public.pnl_user_summary_mt4live2.

    Design goals (for junior engineers):
    - Use advisory lock (937000002) to avoid concurrent runs
    - Maintain shared watermarks table (dataset='pnl_user_summary_mt4live2')
    - Ensure target summary table and open orders state table
    - Incremental window by MT4 TICKET (> last_deal_id watermark)
    - Upsert per-login deltas and refresh floating PnL from mt4_users snapshot
    - Return concise metrics for UI
    """
    pg_dsn = _pg_mt5_dsn_forced_db("MT5_ETL")
    mysql_cfg: Dict[str, Any] = {
        "host": os.getenv("MYSQL_HOST"),
        "user": os.getenv("MYSQL_USER"),
        "password": os.getenv("MYSQL_PASSWORD"),
        "database": os.getenv("MYSQL_DATABASE_MT4LIVE2"),
    }

    result: Dict[str, Any] = {
        "success": False,
        "processed_rows": 0,
        "duration_seconds": 0.0,
        "new_max_deal_id": None,
        "new_trades_count": 0,
        "floating_only_count": 0,
        "message": None,
        "skipped": False,
    }

    import time
    start_ts = time.time()

    # Basic env validation
    if not all([mysql_cfg.get("host"), mysql_cfg.get("user"), mysql_cfg.get("password"), mysql_cfg.get("database")]):
        raise RuntimeError("Missing required MySQL env vars for MT4Live2 incremental refresh")

    TARGET_TABLE = "public.pnl_user_summary_mt4live2"
    DATASET = "pnl_user_summary_mt4live2"
    PARTITION = "ALL"
    LOCK_KEY = LOCK_PNL_USER_SUMMARY_MT4LIVE2

    with pg_session(pg_dsn) as pg_conn:
        pg_conn.autocommit = False

        # Acquire advisory lock
        with pg_conn.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_lock(%s)", (LOCK_KEY,))
            locked = bool(cur.fetchone()[0])
            pg_conn.commit()
        if not locked:
            # A rejected lock is not a failure, but it is emphatically not a
            # refresh either -- the caller must be able to tell the difference.
            result["success"] = True
            result["skipped"] = True
            result["message"] = "Another MT4Live2 incremental run is in progress"
            return result

        try:
            # Ensure common watermarks table
            with pg_conn.cursor() as cur:
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS public.etl_watermarks (
                      dataset       text        NOT NULL,
                      partition_key text        NOT NULL DEFAULT 'ALL',
                      last_deal_id  bigint,
                      last_time     timestamptz,
                      last_login    bigint,
                      last_updated  timestamptz NOT NULL DEFAULT now(),
                      CONSTRAINT pk_etl_watermarks PRIMARY KEY (dataset, partition_key)
                    );
                    """
                )
                pg_conn.commit()

            # Ensure target summary table (mirror of MT5 table)
            ddl_main = f"""
            CREATE TABLE IF NOT EXISTS {TARGET_TABLE} (
              login                           bigint        NOT NULL,
              symbol                          text          NOT NULL DEFAULT 'ALL',
              user_name                       text,
              user_group                      text,
              country                         text,
              zipcode                         text,
              user_id                         bigint,
              user_balance                    numeric(20,2) NOT NULL DEFAULT 0,
              user_credit                     numeric(20,2) NOT NULL DEFAULT 0,
              positions_floating_pnl          numeric(20,2) NOT NULL DEFAULT 0,
              equity                          numeric(20,2) GENERATED ALWAYS AS (user_balance + user_credit + positions_floating_pnl) STORED,
              closed_sell_volume_lots         numeric(20,6) NOT NULL DEFAULT 0,
              closed_sell_count               integer       NOT NULL DEFAULT 0,
              closed_sell_profit              numeric(20,2) NOT NULL DEFAULT 0,
              closed_sell_swap                numeric(20,2) NOT NULL DEFAULT 0,
              closed_sell_overnight_count     integer       NOT NULL DEFAULT 0,
              closed_sell_overnight_volume_lots numeric(20,6) NOT NULL DEFAULT 0,
              closed_buy_volume_lots          numeric(20,6) NOT NULL DEFAULT 0,
              closed_buy_count                integer       NOT NULL DEFAULT 0,
              closed_buy_profit               numeric(20,2) NOT NULL DEFAULT 0,
              closed_buy_swap                 numeric(20,2) NOT NULL DEFAULT 0,
              closed_buy_overnight_count      integer       NOT NULL DEFAULT 0,
              closed_buy_overnight_volume_lots numeric(20,6) NOT NULL DEFAULT 0,
              total_commission                numeric(20,2) NOT NULL DEFAULT 0,
              deposit_count                   integer       NOT NULL DEFAULT 0,
              deposit_amount                  numeric(20,2) NOT NULL DEFAULT 0,
              withdrawal_count                integer       NOT NULL DEFAULT 0,
              withdrawal_amount               numeric(20,2) NOT NULL DEFAULT 0,
              net_deposit                     numeric(20,2) GENERATED ALWAYS AS (deposit_amount - withdrawal_amount) STORED,
              last_updated                    timestamptz   NOT NULL DEFAULT now(),
              CONSTRAINT pk_pnl_user_summary_mt4live2 PRIMARY KEY (login, symbol)
            );
            """
            ddl_ratio = f"""
            ALTER TABLE {TARGET_TABLE}
            ADD COLUMN IF NOT EXISTS overnight_volume_ratio numeric(6,3)
            GENERATED ALWAYS AS (
              CASE
                WHEN (COALESCE(closed_sell_volume_lots, 0) + COALESCE(closed_buy_volume_lots, 0)) > 0 THEN
                  ROUND(
                    (
                      COALESCE(closed_sell_overnight_volume_lots, 0) +
                      COALESCE(closed_buy_overnight_volume_lots, 0)
                    ) / (
                      COALESCE(closed_sell_volume_lots, 0) +
                      COALESCE(closed_buy_volume_lots, 0)
                    ),
                    3
                  )
                ELSE -1
              END
            ) STORED;
            """
            with pg_conn.cursor() as cur:
                cur.execute(ddl_main)
                # Ensure equity generated column uses balance + credit + floating_pnl even if table existed earlier
                try:
                    cur.execute(f"ALTER TABLE {TARGET_TABLE} DROP COLUMN IF EXISTS equity")
                    cur.execute(
                        f"ALTER TABLE {TARGET_TABLE} ADD COLUMN equity numeric(20,2) GENERATED ALWAYS AS (user_balance + user_credit + positions_floating_pnl) STORED"
                    )
                except Exception:
                    pass
                cur.execute(ddl_ratio)
                pg_conn.commit()

            # Ensure open orders table (state for incremental window)
            ddl_open = """
            CREATE TABLE IF NOT EXISTS public.mt4_open_orders (
              ticket             bigint        PRIMARY KEY,
              login              bigint        NOT NULL,
              symbol             text          NOT NULL,
              cmd                smallint      NOT NULL,
              volume_lots        numeric(20,6) NOT NULL,
              open_time          timestamptz   NOT NULL,
              open_price         numeric(20,6) NOT NULL,
              digits             integer       NOT NULL,
              swaps              numeric(20,2) NOT NULL DEFAULT 0,
              comment            text,
              magic              integer,
              source_modify_time timestamptz,
              last_seen          timestamptz   NOT NULL DEFAULT now()
            );
            CREATE INDEX IF NOT EXISTS idx_mt4_open_orders_login  ON public.mt4_open_orders(login);
            CREATE INDEX IF NOT EXISTS idx_mt4_open_orders_symbol ON public.mt4_open_orders(symbol);
            """
            with pg_conn.cursor() as cur:
                cur.execute(ddl_open)
                pg_conn.commit()

            # Read last watermark (MT4 ticket semantics)
            since_ticket = 0
            with pg_conn.cursor() as cur:
                cur.execute(
                    "SELECT last_deal_id FROM public.etl_watermarks WHERE dataset=%s AND partition_key=%s",
                    (DATASET, PARTITION),
                )
                row = cur.fetchone()
                if row and row[0] is not None:
                    since_ticket = int(row[0])
                else:
                    cur.execute(
                        "INSERT INTO public.etl_watermarks (dataset, partition_key, last_deal_id, last_updated) VALUES (%s,%s,%s, now()) ON CONFLICT DO NOTHING",
                        (DATASET, PARTITION, 0),
                    )
                    pg_conn.commit()
                    since_ticket = 0

            # Connect to MySQL
            mysql_conn = _connect_mysql_guarded(mysql_cfg)
            try:
                # Ensure skeleton rows exist in target
                users_sql = (
                    "SELECT u.Login, u.`Name` AS user_name, u.`Group` AS user_group, u.Country, u.ZipCode AS zipcode, "
                    "CAST(NULLIF(u.ID,'') AS SIGNED) AS user_id, COALESCE(u.balance,0) AS balance, COALESCE(u.credit,0) AS credit "
                    "FROM mt4_live2.mt4_users u"
                )
                to_insert = []
                with mysql_conn.cursor(dictionary=True) as cur:
                    cur.execute(users_sql)
                    for r in cur.fetchall():
                        to_insert.append((
                            int(r['Login']), 'ALL',
                            r.get('user_name'), r.get('user_group'), r.get('Country'), r.get('zipcode'), r.get('user_id'),
                            float(r.get('balance') or 0), float(r.get('credit') or 0),
                            0.0,
                        ))
                if to_insert:
                    from psycopg2.extras import execute_values
                    with pg_conn.cursor() as cur:
                        execute_values(cur,
                            f"INSERT INTO {TARGET_TABLE} (login, symbol, user_name, user_group, country, zipcode, user_id, user_balance, user_credit, positions_floating_pnl) VALUES %s "
                            f"ON CONFLICT (login, symbol) DO UPDATE SET "
                            f"  user_name = EXCLUDED.user_name, user_group = EXCLUDED.user_group, country = EXCLUDED.country, zipcode = EXCLUDED.zipcode, user_id = EXCLUDED.user_id, "
                            f"  user_balance = EXCLUDED.user_balance, user_credit = EXCLUDED.user_credit",
                            to_insert, page_size=2000)

                # Helper: group closed rows per login
                def _group_closed_rows_per_login(rows: List[tuple], volume_divisor: float) -> Dict[int, Dict[str, Any]]:
                    per_login: Dict[int, Dict[str, Any]] = {}
                    for r in rows:
                        # (TICKET, LOGIN, SYMBOL, CMD, VOLUME, OPEN_TIME, OPEN_PRICE, CLOSE_TIME, PROFIT, SWAPS, COMMISSION)
                        _, login, _symbol, cmd, volume, open_time, _open_price, close_time, profit, swaps, commission = r
                        side = 'BUY' if cmd == 0 else 'SELL'
                        lots = (volume or 0) / 100.0  # MT4 volume to lots; default 100 divisor
                        overnight = 1 if (open_time.date() != close_time.date()) else 0
                        if login not in per_login:
                            per_login[login] = {
                                'closed_sell_volume_lots': 0.0,
                                'closed_sell_count': 0,
                                'closed_sell_profit': 0.0,
                                'closed_sell_swap': 0.0,
                                'closed_sell_overnight_count': 0,
                                'closed_sell_overnight_volume_lots': 0.0,
                                'closed_buy_volume_lots': 0.0,
                                'closed_buy_count': 0,
                                'closed_buy_profit': 0.0,
                                'closed_buy_swap': 0.0,
                                'closed_buy_overnight_count': 0,
                                'closed_buy_overnight_volume_lots': 0.0,
                                'total_commission': 0.0,
                            }
                        agg = per_login[login]
                        if side == 'SELL':
                            agg['closed_sell_volume_lots'] += lots
                            agg['closed_sell_count'] += 1
                            agg['closed_sell_profit'] += (profit or 0)
                            agg['closed_sell_swap'] += (swaps or 0)
                            agg['closed_sell_overnight_count'] += overnight
                            agg['closed_sell_overnight_volume_lots'] += (lots if overnight else 0)
                        else:
                            agg['closed_buy_volume_lots'] += lots
                            agg['closed_buy_count'] += 1
                            agg['closed_buy_profit'] += (profit or 0)
                            agg['closed_buy_swap'] += (swaps or 0)
                            agg['closed_buy_overnight_count'] += overnight
                            agg['closed_buy_overnight_volume_lots'] += (lots if overnight else 0)
                        agg['total_commission'] += (commission or 0)
                    return per_login

                # Fetch current open orders tickets
                open_ticket_ids: List[int] = []
                with pg_conn.cursor() as cur:
                    cur.execute("SELECT ticket FROM public.mt4_open_orders")
                    open_ticket_ids = [int(r[0]) for r in cur.fetchall()]

                # Closures among current open orders
                closures: List[tuple] = []
                if open_ticket_ids:
                    # chunked IN
                    sql = (
                        "SELECT TICKET, LOGIN, SYMBOL, CMD, VOLUME, OPEN_TIME, OPEN_PRICE, CLOSE_TIME, PROFIT, SWAPS, COMMISSION "
                        "FROM mt4_live2.mt4_trades WHERE TICKET IN ({ph}) AND CLOSE_TIME <> '1970-01-01 00:00:00'"
                    )
                    chunk = 1000
                    with mysql_conn.cursor() as cur:
                        for i in range(0, len(open_ticket_ids), chunk):
                            part = open_ticket_ids[i:i+chunk]
                            placeholders = ",".join(["%s"] * len(part))
                            cur.execute(sql.format(ph=placeholders), part)
                            closures.extend(cur.fetchall())
                closed_ids = [int(r[0]) for r in closures]
                per_login_a = _group_closed_rows_per_login(closures, 100.0) if closures else {}

                # New opens after since_ticket
                with mysql_conn.cursor() as cur:
                    cur.execute(
                        "SELECT TICKET, LOGIN, SYMBOL, CMD, VOLUME/ %s AS volume_lots, OPEN_TIME, OPEN_PRICE, DIGITS, COALESCE(SWAPS,0) AS swaps, COMMENT, MAGIC, MODIFY_TIME "
                        "FROM mt4_live2.mt4_trades WHERE TICKET > %s AND CMD IN (0,1) AND CLOSE_TIME = '1970-01-01 00:00:00'",
                        (100.0, since_ticket),
                    )
                    new_opens = [tuple(r) for r in cur.fetchall()]

                # New closed after since_ticket excluding those already closed above
                params: List[Any] = [since_ticket]
                base = (
                    "SELECT TICKET, LOGIN, SYMBOL, CMD, VOLUME, OPEN_TIME, OPEN_PRICE, CLOSE_TIME, PROFIT, SWAPS, COMMISSION "
                    "FROM mt4_live2.mt4_trades WHERE TICKET > %s AND CMD IN (0,1) AND CLOSE_TIME <> '1970-01-01 00:00:00'"
                )
                if closed_ids:
                    placeholders = ",".join(["%s"] * len(closed_ids))
                    sql = base + f" AND TICKET NOT IN ({placeholders})"
                    params.extend(closed_ids)
                else:
                    sql = base
                with mysql_conn.cursor() as cur:
                    cur.execute(sql, params)
                    new_closed = [tuple(r) for r in cur.fetchall()]

                per_login_b = _group_closed_rows_per_login(new_closed, 100.0) if new_closed else {}

                # Merge per-login deltas
                per_login: Dict[int, Dict[str, Any]] = {}
                for d in (per_login_a, per_login_b):
                    for login, agg in d.items():
                        if login not in per_login:
                            per_login[login] = {k: (0.0 if isinstance(v, float) else 0) for k, v in agg.items()}
                        for k, v in agg.items():
                            per_login[login][k] += v

                # Balance deltas
                with mysql_conn.cursor() as cur:
                    cur.execute(
                        "SELECT LOGIN, "
                        "SUM(CASE WHEN PROFIT > 0 THEN 1 ELSE 0 END) AS deposit_count, "
                        "SUM(CASE WHEN PROFIT > 0 THEN PROFIT ELSE 0 END) AS deposit_amount, "
                        "SUM(CASE WHEN PROFIT < 0 THEN 1 ELSE 0 END) AS withdrawal_count, "
                        "SUM(CASE WHEN PROFIT < 0 THEN -PROFIT ELSE 0 END) AS withdrawal_amount "
                        "FROM mt4_live2.mt4_trades WHERE CMD=6 AND TICKET > %s GROUP BY LOGIN",
                        (since_ticket,),
                    )
                    balance_rows = [tuple(r) for r in cur.fetchall()]

                # Fetch user snapshots for involved logins
                login_list = sorted(set(list(per_login.keys()) + [int(r[0]) for r in balance_rows]))
                snapshots: Dict[int, Dict[str, Any]] = {}
                if login_list:
                    placeholders = ",".join(["%s"] * len(login_list))
                    with mysql_conn.cursor(dictionary=True) as cur:
                        cur.execute(
                            f"SELECT Login, `Name` AS name, `Group` AS `group`, Country, ZipCode, CAST(NULLIF(ID,'') AS SIGNED) AS user_id, COALESCE(balance,0) AS balance, COALESCE(credit,0) AS credit FROM mt4_live2.mt4_users WHERE Login IN ({placeholders})",
                            login_list,
                        )
                        for r in cur.fetchall():
                            snapshots[int(r['Login'])] = r

                # Apply changes: delete closed from open_orders; insert new opens; upsert deltas
                deleted_from_open = 0
                if closed_ids:
                    with pg_conn.cursor() as cur:
                        # chunk delete
                        chunk = 1000
                        for i in range(0, len(closed_ids), chunk):
                            part = closed_ids[i:i+chunk]
                            placeholders = ",".join(["%s"] * len(part))
                            cur.execute(f"DELETE FROM public.mt4_open_orders WHERE ticket IN ({placeholders})", part)
                        deleted_from_open = len(closed_ids)

                added_to_open = 0
                if new_opens:
                    from psycopg2.extras import execute_values
                    with pg_conn.cursor() as cur:
                        cur.execute(
                            "CREATE TABLE IF NOT EXISTS public.mt4_open_orders (ticket bigint PRIMARY KEY, login bigint NOT NULL, symbol text NOT NULL, cmd smallint NOT NULL, volume_lots numeric(20,6) NOT NULL, open_time timestamptz NOT NULL, open_price numeric(20,6) NOT NULL, digits integer NOT NULL, swaps numeric(20,2) NOT NULL DEFAULT 0, comment text, magic integer, source_modify_time timestamptz, last_seen timestamptz NOT NULL DEFAULT now());"
                        )
                        execute_values(cur,
                            "INSERT INTO public.mt4_open_orders (ticket, login, symbol, cmd, volume_lots, open_time, open_price, digits, swaps, comment, magic, source_modify_time) VALUES %s ON CONFLICT (ticket) DO UPDATE SET "
                            "login=EXCLUDED.login, symbol=EXCLUDED.symbol, cmd=EXCLUDED.cmd, volume_lots=EXCLUDED.volume_lots, open_time=EXCLUDED.open_time, open_price=EXCLUDED.open_price, digits=EXCLUDED.digits, swaps=EXCLUDED.swaps, comment=EXCLUDED.comment, magic=EXCLUDED.magic, source_modify_time=EXCLUDED.source_modify_time, last_seen=now()",
                            new_opens, page_size=2000)
                        added_to_open = len(new_opens)

                # Build upsert rows per login
                zero_agg = {
                    'closed_sell_volume_lots': 0.0,
                    'closed_sell_count': 0,
                    'closed_sell_profit': 0.0,
                    'closed_sell_swap': 0.0,
                    'closed_sell_overnight_count': 0,
                    'closed_sell_overnight_volume_lots': 0.0,
                    'closed_buy_volume_lots': 0.0,
                    'closed_buy_count': 0,
                    'closed_buy_profit': 0.0,
                    'closed_buy_swap': 0.0,
                    'closed_buy_overnight_count': 0,
                    'closed_buy_overnight_volume_lots': 0.0,
                    'total_commission': 0.0,
                }
                balance_map = {int(login): (int(dc or 0), float(da or 0.0), int(wc or 0), float(wa or 0.0)) for login, dc, da, wc, wa in balance_rows}
                all_logins = sorted(set([int(l) for l in per_login.keys()]) | set(balance_map.keys()))
                upsert_rows: List[tuple] = []
                for login in all_logins:
                    agg = per_login.get(login, zero_agg)
                    snap = snapshots.get(int(login), {})
                    dc, da, wc, wa = balance_map.get(int(login), (0, 0.0, 0, 0.0))
                    upsert_rows.append((
                        int(login), 'ALL',
                        snap.get('name'), snap.get('group'), snap.get('Country'), snap.get('ZipCode'), snap.get('user_id'),
                        float(snap.get('balance', 0) or 0), float(snap.get('credit', 0) or 0),
                        0.0,
                        float(agg['closed_sell_volume_lots']), int(agg['closed_sell_count']), float(agg['closed_sell_profit']), float(agg['closed_sell_swap']), int(agg['closed_sell_overnight_count']), float(agg['closed_sell_overnight_volume_lots']),
                        float(agg['closed_buy_volume_lots']), int(agg['closed_buy_count']), float(agg['closed_buy_profit']), float(agg['closed_buy_swap']), int(agg['closed_buy_overnight_count']), float(agg['closed_buy_overnight_volume_lots']),
                        float(agg['total_commission']),
                        int(dc), float(da), int(wc), float(wa)
                    ))

                affected = 0
                if upsert_rows:
                    from psycopg2.extras import execute_values
                    sql = f"""
                    INSERT INTO {TARGET_TABLE} (
                      login, symbol,
                      user_name, user_group, country, zipcode, user_id,
                      user_balance, user_credit, positions_floating_pnl,
                      closed_sell_volume_lots, closed_sell_count, closed_sell_profit, closed_sell_swap, closed_sell_overnight_count, closed_sell_overnight_volume_lots,
                      closed_buy_volume_lots,  closed_buy_count,  closed_buy_profit,  closed_buy_swap,  closed_buy_overnight_count,  closed_buy_overnight_volume_lots,
                      total_commission,
                      deposit_count, deposit_amount, withdrawal_count, withdrawal_amount
                    ) VALUES %s
                    ON CONFLICT (login, symbol) DO UPDATE SET
                      user_name = EXCLUDED.user_name,
                      user_group = EXCLUDED.user_group,
                      country = EXCLUDED.country,
                      zipcode = EXCLUDED.zipcode,
                      user_id = EXCLUDED.user_id,
                      user_balance = EXCLUDED.user_balance,
                      user_credit = EXCLUDED.user_credit,
                      positions_floating_pnl = {TARGET_TABLE}.positions_floating_pnl,
                      closed_sell_volume_lots = {TARGET_TABLE}.closed_sell_volume_lots + EXCLUDED.closed_sell_volume_lots,
                      closed_sell_count       = {TARGET_TABLE}.closed_sell_count       + EXCLUDED.closed_sell_count,
                      closed_sell_profit      = {TARGET_TABLE}.closed_sell_profit      + EXCLUDED.closed_sell_profit,
                      closed_sell_swap        = {TARGET_TABLE}.closed_sell_swap        + EXCLUDED.closed_sell_swap,
                      closed_sell_overnight_count = {TARGET_TABLE}.closed_sell_overnight_count + EXCLUDED.closed_sell_overnight_count,
                      closed_sell_overnight_volume_lots = {TARGET_TABLE}.closed_sell_overnight_volume_lots + EXCLUDED.closed_sell_overnight_volume_lots,
                      closed_buy_volume_lots  = {TARGET_TABLE}.closed_buy_volume_lots  + EXCLUDED.closed_buy_volume_lots,
                      closed_buy_count        = {TARGET_TABLE}.closed_buy_count        + EXCLUDED.closed_buy_count,
                      closed_buy_profit       = {TARGET_TABLE}.closed_buy_profit       + EXCLUDED.closed_buy_profit,
                      closed_buy_swap         = {TARGET_TABLE}.closed_buy_swap         + EXCLUDED.closed_buy_swap,
                      closed_buy_overnight_count = {TARGET_TABLE}.closed_buy_overnight_count + EXCLUDED.closed_buy_overnight_count,
                      closed_buy_overnight_volume_lots = {TARGET_TABLE}.closed_buy_overnight_volume_lots + EXCLUDED.closed_buy_overnight_volume_lots,
                      total_commission        = {TARGET_TABLE}.total_commission        + EXCLUDED.total_commission,
                      deposit_count           = {TARGET_TABLE}.deposit_count           + EXCLUDED.deposit_count,
                      deposit_amount          = {TARGET_TABLE}.deposit_amount          + EXCLUDED.deposit_amount,
                      withdrawal_count        = {TARGET_TABLE}.withdrawal_count        + EXCLUDED.withdrawal_count,
                      withdrawal_amount       = {TARGET_TABLE}.withdrawal_amount       + EXCLUDED.withdrawal_amount,
                      last_updated = now()
                    """
                    with pg_conn.cursor() as cur:
                        execute_values(cur, sql, upsert_rows, page_size=2000)
                        affected = len(upsert_rows)

                # Refresh floating pnl using mt4_users snapshot: equity - balance
                pairs: List[tuple] = []
                with mysql_conn.cursor() as cur:
                    cur.execute("SELECT Login, COALESCE(equity,0) - COALESCE(balance,0) - COALESCE(credit,0) AS floating_pnl FROM mt4_live2.mt4_users")
                    for r in cur.fetchall():
                        pairs.append((int(r[0]), float(r[1] or 0.0)))
                if pairs:
                    from psycopg2.extras import execute_values
                    with pg_conn.cursor() as cur:
                        cur.execute("CREATE TEMP TABLE tmp_mt4_floating (login bigint, symbol text, floating_pnl numeric) ON COMMIT DROP")
                        execute_values(cur, "INSERT INTO tmp_mt4_floating (login, symbol, floating_pnl) VALUES %s", [(login, 'ALL', pnl) for login, pnl in pairs], page_size=2000)
                        cur.execute(
                            f"UPDATE {TARGET_TABLE} s SET positions_floating_pnl = t.floating_pnl, "
                            "    last_updated = CASE WHEN s.positions_floating_pnl IS DISTINCT FROM t.floating_pnl THEN now() ELSE s.last_updated END "
                            "FROM tmp_mt4_floating t WHERE s.login = t.login AND s.symbol = t.symbol AND s.positions_floating_pnl IS DISTINCT FROM t.floating_pnl"
                        )
                        # watermark last_time for floating
                        cur.execute(
                            "INSERT INTO public.etl_watermarks (dataset, partition_key, last_time, last_updated) VALUES (%s,%s, now(), now()) "
                            "ON CONFLICT (dataset, partition_key) DO UPDATE SET last_time=now(), last_updated=now()",
                            (DATASET, PARTITION),
                        )

                # Advance watermark last_deal_id
                window_max = since_ticket
                if new_opens:
                    window_max = max(window_max, max(int(r[0]) for r in new_opens))
                if new_closed:
                    window_max = max(window_max, max(int(r[0]) for r in new_closed))
                if balance_rows:
                    with mysql_conn.cursor() as cur:
                        cur.execute("SELECT COALESCE(MAX(TICKET), %s) FROM mt4_live2.mt4_trades WHERE CMD=6 AND TICKET > %s", (since_ticket, since_ticket))
                        row = cur.fetchone()
                        window_max = max(window_max, int(row[0] or since_ticket))
                with pg_conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO public.etl_watermarks (dataset, partition_key, last_deal_id, last_updated) VALUES (%s,%s,%s, now()) "
                        "ON CONFLICT (dataset, partition_key) DO UPDATE SET last_deal_id=EXCLUDED.last_deal_id, last_updated=now()",
                        (DATASET, PARTITION, window_max),
                    )

                pg_conn.commit()

                result["success"] = True
                result["processed_rows"] = affected
                result["new_max_deal_id"] = window_max
                result["new_trades_count"] = len(closures) + len(new_closed)
                result["floating_only_count"] = len(pairs)
            finally:
                try:
                    if mysql_conn and mysql_conn.is_connected():
                        mysql_conn.close()
                except Exception:
                    pass
        except Exception:
            pg_conn.rollback()
            raise
        finally:
            with pg_conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_unlock(%s)", (LOCK_KEY,))
                pg_conn.commit()

    result["duration_seconds"] = round(time.time() - start_ts, 3)
    return result


def _build_filter_condition(field: str, op: str, value: Any, value2: Any, params: List) -> Optional[str]:
    """根据操作符构建 SQL WHERE 条件片段（用于筛选功能）
    
    Args:
        field: 列名（已通过白名单校验）
        op: 操作符（已通过白名单校验）
        value: 主值
        value2: 副值（between 使用）
        params: 参数列表（用于 psycopg2 的 %s 占位符）
    
    Returns:
        SQL 条件字符串，如 "user_name ILIKE %s"；返回 None 表示跳过该条件
    """
    # 定义数字字段（不能使用空字符串比较）
    numeric_fields = {
        "login", "user_id", "user_balance", "user_credit", "positions_floating_pnl", "equity",
        "closed_sell_volume_lots", "closed_sell_count", "closed_sell_profit", "closed_sell_swap",
        "closed_sell_overnight_count", "closed_sell_overnight_volume_lots",
        "closed_buy_volume_lots", "closed_buy_count", "closed_buy_profit", "closed_buy_swap",
        "closed_buy_overnight_count", "closed_buy_overnight_volume_lots",
        "total_commission", "deposit_count", "deposit_amount", "withdrawal_count",
        "withdrawal_amount", "net_deposit", "closed_total_profit_with_swap", "overnight_volume_ratio",
    }
    
    # 文本操作符
    if op == "contains":
        params.append(f"%{value}%")
        return f"{field} ILIKE %s"
    elif op == "not_contains":
        params.append(f"%{value}%")
        return f"{field} NOT ILIKE %s"
    elif op == "equals":
        params.append(value)
        return f"{field} = %s"
    elif op == "not_equals":
        params.append(value)
        return f"{field} != %s"
    elif op == "starts_with":
        params.append(f"{value}%")
        return f"{field} ILIKE %s"
    elif op == "ends_with":
        params.append(f"%{value}")
        return f"{field} ILIKE %s"
    elif op == "blank":
        # 数字字段：只检查 NULL；文本字段：检查 NULL 或空字符串
        if field in numeric_fields:
            return f"{field} IS NULL"
        else:
            return f"({field} IS NULL OR {field} = '')"
    elif op == "not_blank":
        # 数字字段：只检查 NOT NULL；文本字段：检查非 NULL 且非空字符串
        if field in numeric_fields:
            return f"{field} IS NOT NULL"
        else:
            return f"({field} IS NOT NULL AND {field} != '')"
    
    # 数字/日期操作符
    elif op == "=":
        params.append(value)
        return f"{field} = %s"
    elif op == "!=":
        params.append(value)
        return f"{field} != %s"
    elif op == ">":
        params.append(value)
        return f"{field} > %s"
    elif op == ">=":
        params.append(value)
        return f"{field} >= %s"
    elif op == "<":
        params.append(value)
        return f"{field} < %s"
    elif op == "<=":
        params.append(value)
        return f"{field} <= %s"
    elif op == "between":
        if value is None or value2 is None:
            return None  # 跳过无效区间
        params.append(value)
        params.append(value2)
        return f"{field} BETWEEN %s AND %s"
    
    # 日期特殊操作符
    elif op == "on":
        # 匹配整个日期（DATE(field) = value）
        params.append(value)
        return f"DATE({field}) = %s"
    elif op == "before":
        params.append(value)
        return f"DATE({field}) < %s"
    elif op == "after":
        params.append(value)
        return f"DATE({field}) > %s"
    
    return None

