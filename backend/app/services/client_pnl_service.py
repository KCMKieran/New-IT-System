from __future__ import annotations

from typing import List, Optional, Tuple, Dict, Any
from datetime import datetime as _dt
import math
import os
import time
import re

import psycopg2
from psycopg2.extras import RealDictCursor
import psycopg2.extras
import pymysql

from app.core.pg_session import (
    DEFAULT_CONNECT_TIMEOUT_SEC,
    DEFAULT_STATEMENT_TIMEOUT_MS,
    LOCK_CLIENT_PNL_REFRESH,
    harden_dsn,
    pg_session,
)


def _pg_dsn() -> str:
    """获取 PostgreSQL 连接字符串"""
    host = os.getenv("POSTGRES_HOST", "localhost")
    port = int(os.getenv("POSTGRES_PORT", "5432"))
    db = os.getenv("POSTGRES_DBNAME_MT5", "MT5_ETL")
    user = os.getenv("POSTGRES_USER", "postgres")
    password = os.getenv("POSTGRES_PASSWORD", "")
    return harden_dsn(
        f"host={host} port={port} dbname={db} user={user} password={password}",
        application_name="client_pnl",
    )


def get_client_pnl_summary_paginated(
    page: int = 1,
    page_size: int = 50,
    sort_by: Optional[str] = None,
    sort_order: str = "asc",
    search: Optional[str] = None,
    filters: Optional[Dict[str, Any]] = None,
) -> Tuple[List[dict], int, int, Optional[_dt]]:
    """分页查询 ClientID 盈亏汇总表
    
    Args:
        page: 页码（从1开始）
        page_size: 每页记录数
        sort_by: 排序字段
        sort_order: 排序方向（asc/desc）
        search: 搜索关键词（支持 client_id 或 account_id 精确匹配）
    
    Returns:
        (rows, total_count, total_pages, last_updated)
    """
    
    # 排序白名单（防注入）
    allowed_sort_fields = {
        "client_id",
        "client_name",
        "zipcode",
        "is_enabled",
        "account_count",
        "total_balance_usd",
        "total_floating_pnl_usd",
        "total_equity_usd",
        "total_closed_profit_usd",
        "total_commission_usd",
        "total_ib_commission_income_usd",
        "total_net_profit_with_ib_usd",
        "total_deposit_usd",
        "total_withdrawal_usd",
        "net_deposit_usd",
        "total_volume_lots",
        "total_overnight_volume_lots",
        "auto_swap_free_status",
        "last_updated",
    }
    
    where_conditions: List[str] = []
    params: List[object] = []
    
    # 搜索：仅支持 client_id 或 account_id（login）的精确匹配
    if search is not None:
        s = str(search).strip()
        if s:
            try:
                search_int = int(s)
                # 精确匹配 client_id 或 account_id（在 pnl_client_accounts 表中查找）
                # 使用 EXISTS 子查询检查 account_id 是否存在
                where_conditions.append(
                    "(client_id = %s OR EXISTS (SELECT 1 FROM public.pnl_client_accounts WHERE client_id = public.pnl_client_summary.client_id AND login = %s))"
                )
                params.append(search_int)
                params.append(search_int)
            except ValueError:
                # 非数字输入，忽略搜索（不添加任何条件）
                # 这样可以避免无效的文本搜索导致性能问题
                pass
    # filters_json 解析结果（filters）转 WHERE
    if filters and isinstance(filters.get("rules"), list) and filters.get("rules"):
        joiner = " OR " if str(filters.get("join")).upper() == "OR" else " AND "

        # 字段白名单映射（前端字段 -> 数据库列）
        field_map: Dict[str, str] = {
            "client_id": "client_id",
            "client_name": "client_name",
            "zipcode": "zipcode",
            "is_enabled": "is_enabled",
            "account_count": "account_count",
            "total_balance_usd": "total_balance_usd",
            "total_floating_pnl_usd": "total_floating_pnl_usd",
            "total_equity_usd": "total_equity_usd",
            "total_closed_profit_usd": "total_closed_profit_usd",
            "total_commission_usd": "total_commission_usd",
            "total_ib_commission_income_usd": "total_ib_commission_income_usd",
            "total_net_profit_with_ib_usd": "total_net_profit_with_ib_usd",
            "total_deposit_usd": "total_deposit_usd",
            "total_withdrawal_usd": "total_withdrawal_usd",
            "net_deposit_usd": "net_deposit_usd",
            "total_volume_lots": "total_volume_lots",
            "auto_swap_free_status": "auto_swap_free_status",
            "last_updated": "last_updated",
        }

        # 运算符到 SQL 的映射工具
        def _text_clause(col: str, op: str, val: Any) -> Tuple[str, List[Any]]:
            if op == "blank":
                return (f"({col} IS NULL OR {col} = '')", [])
            if op == "not_blank":
                return (f"({col} IS NOT NULL AND {col} <> '')", [])
            if op == "contains":
                return (f"{col} ILIKE %s", [f"%{val}%"])
            if op == "not_contains":
                return (f"{col} NOT ILIKE %s", [f"%{val}%"])
            if op == "starts_with":
                return (f"{col} ILIKE %s", [f"{val}%"])
            if op == "ends_with":
                return (f"{col} ILIKE %s", [f"%{val}"])
            if op == "equals":
                return (f"{col} = %s", [val])
            if op == "not_equals":
                return (f"{col} <> %s", [val])
            return ("1=1", [])

        def _num_clause(col: str, op: str, v1: Any, v2: Any) -> Tuple[str, List[Any]]:
            if op in ("blank",): return (f"{col} IS NULL", [])
            if op in ("not_blank",): return (f"{col} IS NOT NULL", [])
            if op in ("=", "eq", "equals"): return (f"{col} = %s", [v1])
            if op in ("!=", "ne", "not_equals"): return (f"{col} <> %s", [v1])
            if op in (">", "gt"): return (f"{col} > %s", [v1])
            if op in (">=", "ge", "gte"): return (f"{col} >= %s", [v1])
            if op in ("<", "lt"): return (f"{col} < %s", [v1])
            if op in ("<=", "le", "lte"): return (f"{col} <= %s", [v1])
            if op in ("between",): return (f"{col} BETWEEN %s AND %s", [v1, v2])
            return ("1=1", [])

        def _date_clause(col: str, op: str, v1: Any, v2: Any) -> Tuple[str, List[Any]]:
            # 接受 'YYYY-MM-DD' 或完整 ISO 字符串，由 PG 端转换
            if op in ("blank",): return (f"{col} IS NULL", [])
            if op in ("not_blank",): return (f"{col} IS NOT NULL", [])
            if op in ("on", "eq", "equals"): return (f"DATE({col}) = %s::date", [v1])
            if op in ("before", "lt", "<"): return (f"{col} < %s::timestamp", [v1])
            if op in ("after", "gt", ">"): return (f"{col} > %s::timestamp", [v1])
            if op in (">=", "ge", "gte"): return (f"{col} >= %s::timestamp", [v1])
            if op in ("<=", "le", "lte"): return (f"{col} <= %s::timestamp", [v1])
            if op in ("between",): return (f"{col} BETWEEN %s::timestamp AND %s::timestamp", [v1, v2])
            return ("1=1", [])

        sub_clauses: List[str] = []
        sub_params: List[Any] = []
        for rule in filters.get("rules", []):
            try:
                field = str(rule.get("field"))
                op = str(rule.get("op"))
                val = rule.get("value")
                val2 = rule.get("value2")

                # 特殊处理：account_last_updated（查询 pnl_client_accounts）
                if field == "account_last_updated" and op in ("after", "gt", ">"):
                    sub_clauses.append(
                        """
                        EXISTS (
                            SELECT 1 FROM public.pnl_client_accounts a 
                            WHERE a.client_id = public.pnl_client_summary.client_id 
                              AND a.last_updated > %s::timestamp
                        )
                        """
                    )
                    sub_params.append(val)
                    continue

                col = field_map.get(field)
                if not col:
                    continue

                # 类型选择：简单根据列推断；文本列
                if field in ("client_name", "zipcode"):
                    clause, ps = _text_clause(col, op, val)
                # 日期列
                elif field in ("last_updated",):
                    clause, ps = _date_clause(col, op, val, val2)
                # 其他数值/布尔视为数值
                else:
                    clause, ps = _num_clause(col, op, val, val2)

                sub_clauses.append(f"({clause})")
                sub_params.extend(ps)
            except Exception:
                # 忽略单条错误规则
                continue

        if sub_clauses:
            where_conditions.append(f"(" + joiner.join(sub_clauses) + ")")
            params.extend(sub_params)

    where_clause = " WHERE " + " AND ".join(where_conditions) if where_conditions else ""
    
    base_select = (
        "SELECT "
        "  client_id, "
        "  client_name, "
        "  NULL::text AS primary_server, "
        "  zipcode, "
        "  is_enabled, "
        "  NULL::text[] AS countries, "
        "  NULL::text[] AS currencies, "
        "  account_count, "
        "  NULL::bigint[] AS account_list, "
        "  COALESCE(total_balance_usd, 0)::numeric AS total_balance_usd, "
        "  0.0::numeric AS total_credit_usd, "
        "  COALESCE(total_floating_pnl_usd, 0)::numeric AS total_floating_pnl_usd, "
        "  COALESCE(total_equity_usd, 0)::numeric AS total_equity_usd, "
        "  COALESCE(total_closed_profit_usd, 0)::numeric AS total_closed_profit_usd, "
        "  COALESCE(total_commission_usd, 0)::numeric AS total_commission_usd, "
        "  COALESCE(total_ib_commission_income_usd, 0)::numeric AS total_ib_commission_income_usd, "
        "  COALESCE(total_net_profit_with_ib_usd, 0)::numeric AS total_net_profit_with_ib_usd, "
        "  COALESCE(total_deposit_usd, 0)::numeric AS total_deposit_usd, "
        "  COALESCE(total_withdrawal_usd, 0)::numeric AS total_withdrawal_usd, "
        "  (COALESCE(total_deposit_usd, 0) - COALESCE(total_withdrawal_usd, 0))::numeric AS net_deposit_usd, "
        "  COALESCE(total_volume_lots, 0)::numeric AS total_volume_lots, "
        "  COALESCE(total_overnight_volume_lots, 0)::numeric AS total_overnight_volume_lots, "
        "  COALESCE(auto_swap_free_status, -1)::numeric AS auto_swap_free_status, "
        "  0::bigint AS total_closed_count, "
        "  0::bigint AS total_overnight_count, "
        "  0.0::numeric AS closed_sell_volume_lots, "
        "  0::bigint AS closed_sell_count, "
        "  0.0::numeric AS closed_sell_profit_usd, "
        "  0.0::numeric AS closed_sell_swap_usd, "
        "  0.0::numeric AS closed_buy_volume_lots, "
        "  0::bigint AS closed_buy_count, "
        "  0.0::numeric AS closed_buy_profit_usd, "
        "  0.0::numeric AS closed_buy_swap_usd, "
        "  last_updated "
        "FROM public.pnl_client_summary" + where_clause
    )
    
    # 排序
    order_clause = ""
    if sort_by and sort_by in allowed_sort_fields:
        direction = "DESC" if sort_order.lower() == "desc" else "ASC"
        # 对可能为 NULL 的字段添加 NULLS LAST
        order_clause = f" ORDER BY {sort_by} {direction} NULLS LAST, client_id ASC"
    else:
        # 默认按 client_id 升序
        order_clause = " ORDER BY client_id ASC"
    
    offset = (page - 1) * page_size
    paginated_sql = base_select + order_clause + " LIMIT %s OFFSET %s"
    
    count_sql = f"SELECT COUNT(*) FROM public.pnl_client_summary" + where_clause
    
    dsn = _pg_dsn()
    with pg_session(dsn) as conn:
        # 查询总数
        with conn.cursor() as cur:
            cur.execute(count_sql, params)
            total_count = cur.fetchone()[0]
        
        total_pages = math.ceil(total_count / page_size) if total_count > 0 else 0
        
        # 查询分页数据
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(paginated_sql, params + [page_size, offset])
            rows = [dict(r) for r in cur.fetchall()]
        
        # 获取最后更新时间（取汇总表中的最大 last_updated）
        with conn.cursor() as cur:
            cur.execute("SELECT MAX(last_updated) FROM public.pnl_client_summary")
            row = cur.fetchone()
            last_updated = row[0] if row else None
        
        return rows, total_count, total_pages, last_updated


def get_client_accounts(client_id: int) -> List[dict]:
    """获取某个客户的所有账户明细
    
    Args:
        client_id: 客户ID
    
    Returns:
        账户列表
    """
    dsn = _pg_dsn()
    with pg_session(dsn) as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT 
                    client_id,
                    login,
                    server,
                    currency,
                    user_name,
                    user_group,
                    country,
                    balance_usd,
                    floating_pnl_usd,
                    equity_usd,
                    closed_profit_usd,
                    commission_usd,
                    ib_commission_income_usd,
                    deposit_usd,
                    withdrawal_usd,
                    volume_lots,
                    auto_swap_free_status,
                    last_updated
                FROM public.pnl_client_accounts
                WHERE client_id = %s
                ORDER BY server, login
                """,
                (client_id,)
            )
            rows = cur.fetchall()
            # Add credit_usd field with default value 0.0 since it doesn't exist in the table
            # This maintains compatibility with the schema while the field is not in the database
            return [{**dict(r), 'credit_usd': 0.0} for r in rows]


def initialize_client_summary(force: bool = False) -> dict:
    """初始化客户聚合表
    
    Args:
        force: 是否强制重新初始化（清空现有数据）
    
    Returns:
        统计信息字典
    """
    dsn = _pg_dsn()
    with pg_session(dsn) as conn:
        # 如果 force=True，先清空数据
        if force:
            with conn.cursor() as cur:
                cur.execute("TRUNCATE TABLE public.pnl_client_summary CASCADE")
                cur.execute("TRUNCATE TABLE public.pnl_client_accounts CASCADE")
                conn.commit()
        
        # 调用初始化函数
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM public.initialize_client_summary()")
            row = cur.fetchone()
            conn.commit()
            
            if row:
                return {
                    "total_clients": int(row[0]),
                    "total_accounts": int(row[1]),
                    "duration_seconds": float(row[2]),
                }
            return {
                "total_clients": 0,
                "total_accounts": 0,
                "duration_seconds": 0.0,
            }


def compare_client_summary(auto_fix: bool = False) -> List[dict]:
    """对比客户聚合表与源表的差异
    
    Args:
        auto_fix: 是否自动修复差异
    
    Returns:
        差异列表
    """
    dsn = _pg_dsn()
    with pg_session(dsn) as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT * FROM public.compare_client_summary(%s)",
                (auto_fix,)
            )
            rows = cur.fetchall()
            conn.commit()
            return [dict(r) for r in rows]


def get_refresh_status() -> dict:
    """获取数据刷新状态
    
    Returns:
        状态信息字典
    """
    dsn = _pg_dsn()
    with pg_session(dsn) as conn:
        with conn.cursor() as cur:
            # 获取最后更新时间和统计信息
            cur.execute(
                """
                SELECT 
                    MAX(last_updated) AS last_updated,
                    COUNT(*) AS total_clients
                FROM public.pnl_client_summary
                """
            )
            row = cur.fetchone()
            
            # 获取账户总数
            cur.execute("SELECT COUNT(*) FROM public.pnl_client_accounts")
            total_accounts = cur.fetchone()[0]
            
            return {
                "last_updated": row[0] if row else None,
                "total_clients": int(row[1]) if row and row[1] else 0,
                "total_accounts": int(total_accounts),
            }


def run_client_pnl_incremental_refresh() -> dict:
    """执行 Client PnL 增量刷新（直接在服务层执行 SQL），返回结构化步骤给前端。

    返回字段包括：success, message, duration_seconds, steps[], max_last_updated, raw_log。
    """
    start_ts = time.perf_counter()

    # 读取环境变量（兼容 .env）
    def _get_env(name: str, default: Optional[str] = None) -> str:
        v = os.getenv(name, default)
        if v is None:
            raise RuntimeError(f"Missing required environment variable: {name}")
        return v

    def _connect_postgres() -> psycopg2.extensions.connection:
        host = _get_env("POSTGRES_HOST")
        user = _get_env("POSTGRES_USER")
        password = _get_env("POSTGRES_PASSWORD")
        port = int(_get_env("POSTGRES_PORT", "5432"))
        dbname = _get_env("POSTGRES_DBNAME_MT5", "MT5_ETL")
        conn = psycopg2.connect(
            host=host,
            user=user,
            password=password,
            port=port,
            dbname=dbname,
            connect_timeout=DEFAULT_CONNECT_TIMEOUT_SEC,
            sslmode="require",
            application_name="client_pnl_refresh",
            options=f"-c statement_timeout={DEFAULT_STATEMENT_TIMEOUT_MS}",
        )
        conn.autocommit = False
        return conn

    def _connect_mysql_fx(dbname: str) -> pymysql.connections.Connection:
        host = _get_env("MYSQL_HOST")
        user = _get_env("MYSQL_USER")
        password = _get_env("MYSQL_PASSWORD")
        port = int(_get_env("MYSQL_PORT", "3306"))
        return pymysql.connect(
            host=host,
            user=user,
            password=password,
            port=port,
            database=dbname,
            charset="utf8mb4",
            cursorclass=pymysql.cursors.DictCursor,
            autocommit=True,
        )

    def _chunked(seq: List[int], size: int) -> List[List[int]]:
        return [seq[i : i + size] for i in range(0, len(seq), size)]

    pg = None
    mysql_fx = None
    lock_held = False
    try:
        pg = _connect_postgres()

        # This refresh rolls pnl_client_accounts up into pnl_client_summary. Two
        # overlapping runs would have the summary aggregated from a half-written
        # accounts table, so serialise them the same way the two pnl_user_summary
        # refreshes already do.
        with pg.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_lock(%s)", (LOCK_CLIENT_PNL_REFRESH,))
            lock_held = bool(cur.fetchone()[0])
            pg.commit()
        if not lock_held:
            return {
                "success": True,
                "skipped": True,
                "message": "Another client PnL refresh is in progress",
                "duration_seconds": time.perf_counter() - start_ts,
                "steps": [],
                "max_last_updated": None,
                "raw_log": None,
            }

        mysql_fx = _connect_mysql_fx(os.getenv("MYSQL_DATABASE_FXBACKOFFICE", "fxbackoffice"))

        # 1) 计算 watermark（供步骤展示，逻辑保持与原脚本一致：从 etl_watermarks 取多个 dataset 的 MAX(last_updated)）
        t0 = time.perf_counter()
        datasets_csv = os.getenv("INCR_SOURCE_DATASETS", "pnl_user_summary,pnl_user_summary_mt4live2")
        datasets = [d.strip() for d in datasets_csv.split(",") if d.strip()]
        watermark_text: Optional[str] = None
        if datasets:
            with pg.cursor() as cur:
                cur.execute(
                    """
                    SELECT TO_CHAR(MAX(last_updated), 'YYYY-MM-DD HH24:MI:SSOF')
                    FROM etl_watermarks
                    WHERE dataset = ANY(%s)
                    """,
                    (datasets,),
                )
                row = cur.fetchone()
                watermark_text = row[0] if row else None
        t1 = time.perf_counter()

        # 2) 候选 client_id（missing + lag）
        cand_t0 = time.perf_counter()
        with pg.cursor() as cur:
            cur.execute("DROP TABLE IF EXISTS temp_candidates;")
            cur.execute(
                """
                CREATE TEMP TABLE temp_candidates (
                  client_id BIGINT PRIMARY KEY,
                  reason    TEXT
                ) ON COMMIT DROP;
                """
            )
            # missing
            cur.execute(
                """
                INSERT INTO temp_candidates (client_id, reason)
                SELECT s.user_id AS client_id, 'missing' AS reason
                FROM (
                  SELECT DISTINCT user_id FROM public.pnl_user_summary WHERE user_id IS NOT NULL
                  UNION
                  SELECT DISTINCT user_id FROM public.pnl_user_summary_mt4live2 WHERE user_id IS NOT NULL
                ) s
                LEFT JOIN public.pnl_client_summary cs ON cs.client_id = s.user_id
                WHERE cs.client_id IS NULL
                ON CONFLICT (client_id) DO NOTHING;
                """
            )
            # lag
            cur.execute(
                """
                WITH src_max AS (
                  SELECT user_id, MAX(last_updated) AS src_lu
                  FROM (
                    SELECT user_id, last_updated FROM public.pnl_user_summary WHERE user_id IS NOT NULL
                    UNION ALL
                    SELECT user_id, last_updated FROM public.pnl_user_summary_mt4live2 WHERE user_id IS NOT NULL
                  ) z
                  GROUP BY user_id
                )
                INSERT INTO temp_candidates (client_id, reason)
                SELECT sm.user_id AS client_id, 'lag'::text AS reason
                FROM src_max sm
                JOIN public.pnl_client_summary cs ON cs.client_id = sm.user_id
                WHERE sm.src_lu > cs.last_updated
                ON CONFLICT (client_id) DO NOTHING;
                """
            )
            # count
            cur.execute(
                """
                SELECT COUNT(*),
                       COALESCE(SUM(CASE WHEN reason='missing' THEN 1 ELSE 0 END),0),
                       COALESCE(SUM(CASE WHEN reason='lag' THEN 1 ELSE 0 END),0)
                FROM temp_candidates;
                """
            )
            cand_total, missing_cnt, lag_cnt = cur.fetchone()
        cand_t1 = time.perf_counter()

        if int(cand_total or 0) == 0:
            pg.rollback()
            elapsed = time.perf_counter() - start_ts
            return {
                "success": True,
                "message": "No candidates. Nothing to refresh.",
                "duration_seconds": elapsed,
                "steps": [
                    {"name": "watermark", "duration_seconds": t1 - t0},
                    {"name": "candidates", "duration_seconds": cand_t1 - cand_t0, "total": 0, "missing": 0, "lag": 0},
                ],
                "max_last_updated": None,
                "raw_log": None,
            }

        # 3) Accounts UPSERT for candidates
        acc_t0 = time.perf_counter()
        accounts_sql = r"""
        WITH src AS (
          SELECT s.user_id, s.login, 'MT5' AS server, s.currency, s.user_name, s.user_group, s.country,
                 s.user_balance, s.equity, s.positions_floating_pnl, s.closed_total_profit_with_swap,
                 s.total_commission, s.deposit_amount, s.withdrawal_amount,
                 s.closed_sell_volume_lots, s.closed_buy_volume_lots,
                 s.closed_sell_overnight_volume_lots, s.closed_buy_overnight_volume_lots,
                 s.last_updated
          FROM public.pnl_user_summary s
          JOIN temp_candidates c ON c.client_id = s.user_id
          WHERE s.user_id IS NOT NULL
          UNION ALL
          SELECT s.user_id, s.login, 'MT4Live2' AS server, s.currency, s.user_name, s.user_group, s.country,
                 s.user_balance, s.equity, s.positions_floating_pnl, s.closed_total_profit_with_swap,
                 s.total_commission, s.deposit_amount, s.withdrawal_amount,
                 s.closed_sell_volume_lots, s.closed_buy_volume_lots,
                 s.closed_sell_overnight_volume_lots, s.closed_buy_overnight_volume_lots,
                 s.last_updated
          FROM public.pnl_user_summary_mt4live2 s
          JOIN temp_candidates c ON c.client_id = s.user_id
          WHERE s.user_id IS NOT NULL
        ),
        combined AS (
          SELECT 
            s.user_id AS client_id,
            s.login,
            s.server,
            s.currency,
            s.user_name,
            s.user_group,
            s.country,
            CASE WHEN s.currency = 'CEN' THEN ROUND(COALESCE(s.user_balance, 0) / 100.0, 4) ELSE ROUND(COALESCE(s.user_balance, 0), 4) END AS balance_usd,
            CASE WHEN s.currency = 'CEN' THEN ROUND(COALESCE(s.equity, 0) / 100.0, 4) ELSE ROUND(COALESCE(s.equity, 0), 4) END AS equity_usd,
            CASE WHEN s.currency = 'CEN' THEN ROUND(COALESCE(s.positions_floating_pnl, 0) / 100.0, 4) ELSE ROUND(COALESCE(s.positions_floating_pnl, 0), 4) END AS floating_pnl_usd,
            CASE WHEN s.currency = 'CEN' THEN ROUND(COALESCE(s.closed_total_profit_with_swap, 0) / 100.0, 4) ELSE ROUND(COALESCE(s.closed_total_profit_with_swap, 0), 4) END AS closed_profit_usd,
            CASE WHEN s.currency = 'CEN' THEN ROUND(COALESCE(s.total_commission, 0) / 100.0, 4) ELSE ROUND(COALESCE(s.total_commission, 0), 4) END AS commission_usd,
            CASE WHEN s.currency = 'CEN' THEN ROUND(COALESCE(s.deposit_amount, 0) / 100.0, 4) ELSE ROUND(COALESCE(s.deposit_amount, 0), 4) END AS deposit_usd,
            CASE WHEN s.currency = 'CEN' THEN ROUND(COALESCE(s.withdrawal_amount, 0) / 100.0, 4) ELSE ROUND(COALESCE(s.withdrawal_amount, 0), 4) END AS withdrawal_usd,
            CASE WHEN s.currency = 'CEN' THEN ROUND((COALESCE(s.closed_sell_volume_lots, 0) + COALESCE(s.closed_buy_volume_lots, 0)) / 100.0, 4)
                 ELSE ROUND(COALESCE(s.closed_sell_volume_lots, 0) + COALESCE(s.closed_buy_volume_lots, 0), 4)
            END AS volume_lots,
            CASE WHEN s.currency = 'CEN' THEN ROUND((COALESCE(s.closed_sell_overnight_volume_lots, 0) + COALESCE(s.closed_buy_overnight_volume_lots, 0)) / 100.0, 4)
                 ELSE ROUND(COALESCE(s.closed_sell_overnight_volume_lots, 0) + COALESCE(s.closed_buy_overnight_volume_lots, 0), 4)
            END AS overnight_volume_lots,
            s.last_updated
          FROM src s
        ),
        rolled AS (
          SELECT
            client_id,
            login,
            CASE WHEN MIN(server) = MAX(server) THEN MIN(server) ELSE MIN(server) END AS server,
            MAX(currency) AS currency,
            (array_agg(user_name ORDER BY user_name NULLS LAST))[1] AS user_name,
            (array_agg(user_group ORDER BY user_group NULLS LAST))[1] AS user_group,
            (array_agg(country ORDER BY country NULLS LAST))[1] AS country,
            ROUND(SUM(balance_usd), 4) AS balance_usd,
            ROUND(SUM(equity_usd), 4) AS equity_usd,
            ROUND(SUM(floating_pnl_usd), 4) AS floating_pnl_usd,
            ROUND(SUM(closed_profit_usd), 4) AS closed_profit_usd,
            ROUND(SUM(commission_usd), 4) AS commission_usd,
            ROUND(SUM(deposit_usd), 4) AS deposit_usd,
            ROUND(SUM(withdrawal_usd), 4) AS withdrawal_usd,
            ROUND(SUM(volume_lots), 4) AS volume_lots,
            ROUND(SUM(overnight_volume_lots), 4) AS overnight_volume_lots,
            MAX(last_updated) AS last_updated
          FROM combined
          GROUP BY client_id, login
        )
        INSERT INTO public.pnl_client_accounts (
          client_id, login, server, currency, user_name, user_group, country,
          balance_usd, equity_usd, floating_pnl_usd, closed_profit_usd, commission_usd,
          deposit_usd, withdrawal_usd, volume_lots, overnight_volume_lots,
          auto_swap_free_status, last_updated
        )
        SELECT
          r.client_id, r.login, r.server, r.currency, r.user_name, r.user_group, r.country,
          r.balance_usd, r.equity_usd, r.floating_pnl_usd, r.closed_profit_usd, r.commission_usd,
          r.deposit_usd, r.withdrawal_usd, r.volume_lots, r.overnight_volume_lots,
          CASE WHEN r.volume_lots = 0 THEN -1.0000 ELSE ROUND(1 - (r.overnight_volume_lots / NULLIF(r.volume_lots, 0)), 4) END,
          r.last_updated
        FROM rolled r
        ON CONFLICT (client_id, login, server) DO UPDATE SET
          currency = EXCLUDED.currency,
          user_name = EXCLUDED.user_name,
          user_group = EXCLUDED.user_group,
          country = EXCLUDED.country,
          balance_usd = EXCLUDED.balance_usd,
          equity_usd = EXCLUDED.equity_usd,
          floating_pnl_usd = EXCLUDED.floating_pnl_usd,
          closed_profit_usd = EXCLUDED.closed_profit_usd,
          commission_usd = EXCLUDED.commission_usd,
          deposit_usd = EXCLUDED.deposit_usd,
          withdrawal_usd = EXCLUDED.withdrawal_usd,
          volume_lots = EXCLUDED.volume_lots,
          overnight_volume_lots = EXCLUDED.overnight_volume_lots,
          auto_swap_free_status = EXCLUDED.auto_swap_free_status,
          last_updated = EXCLUDED.last_updated
        ;
        """
        with pg.cursor() as cur:
            cur.execute(accounts_sql)
            accounts_affected = cur.rowcount if cur.rowcount is not None else 0
        acc_t1 = time.perf_counter()

        # 4) 删除孤儿账户（仅候选集）
        del_t0 = time.perf_counter()
        with pg.cursor() as cur:
            cur.execute(
                r"""
                DELETE FROM public.pnl_client_accounts ca
                USING temp_candidates c
                WHERE ca.client_id = c.client_id
                  AND NOT EXISTS (
                    SELECT 1 FROM public.pnl_user_summary s WHERE s.user_id = ca.client_id AND s.login = ca.login
                    UNION ALL
                    SELECT 1 FROM public.pnl_user_summary_mt4live2 s WHERE s.user_id = ca.client_id AND s.login = ca.login
                  );
                """
            )
            orphan_deleted = cur.rowcount if cur.rowcount is not None else 0
        del_t1 = time.perf_counter()

        # 5) 读取 FX BackOffice 映射并加载到临时表
        with pg.cursor() as cur:
            cur.execute("SELECT client_id FROM temp_candidates;")
            client_ids = [int(r[0]) for r in cur.fetchall()]
        map_t0 = time.perf_counter()
        mapping_rows: List[Tuple[int, Optional[str], Optional[int]]] = []
        if client_ids:
            sql = "SELECT id AS client_id, isEnabled, zipcode FROM users WHERE id IN ({ph})"
            with mysql_fx.cursor() as mcur:
                for batch in _chunked(client_ids, 1000):
                    ph = ",".join(["%s"] * len(batch))
                    mcur.execute(sql.format(ph=ph), batch)
                    for r in mcur.fetchall():
                        cid = int(r.get("client_id")) if r.get("client_id") is not None else None
                        zipcode = r.get("zipcode")
                        is_enabled = r.get("isEnabled")
                        mapped_enabled: Optional[int] = None
                        if is_enabled is not None:
                            try:
                                mapped_enabled = 1 if int(is_enabled) == 1 else 0
                            except Exception:
                                mapped_enabled = 0
                        if cid is not None:
                            mapping_rows.append((cid, zipcode, mapped_enabled))

        with pg.cursor() as cur:
            cur.execute("DROP TABLE IF EXISTS temp_fx_user_map;")
            cur.execute(
                """
                CREATE TEMP TABLE temp_fx_user_map (
                  client_id   BIGINT PRIMARY KEY,
                  zipcode     TEXT,
                  is_enabled  SMALLINT
                ) ON COMMIT DROP;
                """
            )
            if mapping_rows:
                psycopg2.extras.execute_values(
                    cur,
                    "INSERT INTO temp_fx_user_map (client_id, zipcode, is_enabled) VALUES %s",
                    mapping_rows,
                )

            # zipcode 变更数量
            cur.execute(
                """
                SELECT COUNT(*)
                FROM temp_candidates c
                LEFT JOIN temp_fx_user_map m ON m.client_id = c.client_id
                LEFT JOIN public.pnl_client_summary s ON s.client_id = c.client_id
                WHERE m.zipcode IS NOT NULL AND (m.zipcode IS DISTINCT FROM s.zipcode);
                """
            )
            zipcode_changes = int(cur.fetchone()[0])
        loaded_mapping = len(mapping_rows)

        # 额外收集 zipcode 变化详情（最多 100 条，避免响应过大）
        zipcode_details: List[Dict[str, Any]] = []
        with pg.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT c.client_id AS clientid,
                       s.zipcode    AS before,
                       m.zipcode    AS after
                FROM temp_candidates c
                LEFT JOIN temp_fx_user_map m ON m.client_id = c.client_id
                LEFT JOIN public.pnl_client_summary s ON s.client_id = c.client_id
                WHERE m.zipcode IS NOT NULL AND (m.zipcode IS DISTINCT FROM s.zipcode)
                ORDER BY c.client_id
                LIMIT 100
                """
            )
            rows = cur.fetchall()
            zipcode_details = [dict(r) for r in rows]
        map_t1 = time.perf_counter()

        # 6) 汇总 UPSERT（仅候选集）
        sum_t0 = time.perf_counter()
        summary_sql = r"""
        WITH agg AS (
          SELECT
            a.client_id,
            (array_agg(a.user_name ORDER BY a.user_name NULLS LAST))[1] AS client_name,
            COUNT(DISTINCT (a.login, a.server)) AS account_count,
            ROUND(SUM(a.balance_usd), 4)                AS total_balance_usd,
            ROUND(SUM(a.equity_usd), 4)                 AS total_equity_usd,
            ROUND(SUM(a.floating_pnl_usd), 4)           AS total_floating_pnl_usd,
            ROUND(SUM(a.closed_profit_usd), 4)          AS total_closed_profit_usd,
            ROUND(SUM(a.commission_usd), 4)             AS total_commission_usd,
            ROUND(SUM(a.deposit_usd), 4)                AS total_deposit_usd,
            ROUND(SUM(a.withdrawal_usd), 4)             AS total_withdrawal_usd,
            ROUND(SUM(a.volume_lots), 4)                AS total_volume_lots,
            ROUND(SUM(a.overnight_volume_lots), 4)      AS total_overnight_volume_lots,
            MAX(a.last_updated)                          AS last_updated
          FROM public.pnl_client_accounts a
          JOIN temp_candidates c ON c.client_id = a.client_id
          GROUP BY a.client_id
        )
        INSERT INTO public.pnl_client_summary (
          client_id, client_name, zipcode, is_enabled,
          total_balance_usd, total_equity_usd, total_floating_pnl_usd, total_closed_profit_usd,
          total_commission_usd, total_deposit_usd, total_withdrawal_usd,
          total_volume_lots, total_overnight_volume_lots, auto_swap_free_status,
          account_count, last_updated
        )
        SELECT
          g.client_id, g.client_name,
          m.zipcode,
          COALESCE(m.is_enabled, 1) AS is_enabled,
          g.total_balance_usd, g.total_equity_usd, g.total_floating_pnl_usd, g.total_closed_profit_usd,
          g.total_commission_usd, g.total_deposit_usd, g.total_withdrawal_usd,
          g.total_volume_lots, g.total_overnight_volume_lots,
          CASE WHEN g.total_volume_lots = 0 THEN -1.0000 ELSE ROUND(1 - (g.total_overnight_volume_lots / NULLIF(g.total_volume_lots, 0)), 4) END,
          g.account_count, g.last_updated
        FROM agg g
        LEFT JOIN temp_fx_user_map m ON m.client_id = g.client_id
        ON CONFLICT (client_id) DO UPDATE SET
          client_name = EXCLUDED.client_name,
          zipcode = EXCLUDED.zipcode,
          is_enabled = EXCLUDED.is_enabled,
          total_balance_usd = EXCLUDED.total_balance_usd,
          total_equity_usd = EXCLUDED.total_equity_usd,
          total_floating_pnl_usd = EXCLUDED.total_floating_pnl_usd,
          total_closed_profit_usd = EXCLUDED.total_closed_profit_usd,
          total_commission_usd = EXCLUDED.total_commission_usd,
          total_deposit_usd = EXCLUDED.total_deposit_usd,
          total_withdrawal_usd = EXCLUDED.total_withdrawal_usd,
          total_volume_lots = EXCLUDED.total_volume_lots,
          total_overnight_volume_lots = EXCLUDED.total_overnight_volume_lots,
          auto_swap_free_status = EXCLUDED.auto_swap_free_status,
          account_count = EXCLUDED.account_count,
          last_updated = EXCLUDED.last_updated
        ;
        """
        with pg.cursor() as cur:
            cur.execute(summary_sql)
            summary_affected = cur.rowcount if cur.rowcount is not None else 0
        sum_t1 = time.perf_counter()

        # 7) 统计 + 提交
        stats_t0 = time.perf_counter()
        with pg.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM temp_candidates;")
            candidates = int(cur.fetchone()[0])
            cur.execute(
                """
                SELECT COUNT(*) FROM public.pnl_client_accounts a JOIN temp_candidates c ON c.client_id = a.client_id
                """
            )
            accounts = int(cur.fetchone()[0])
            cur.execute(
                """
                SELECT TO_CHAR(MAX(last_updated), 'YYYY-MM-DD HH24:MI:SSOF')
                FROM public.pnl_client_accounts a JOIN temp_candidates c ON c.client_id = a.client_id
                """
            )
            max_lu = cur.fetchone()[0]
        stats_t1 = time.perf_counter()
        # 7.1) 更新 pnl_client 的 ETL watermark（UTC+0）
        with pg.cursor() as cur:
            # 确保公共水位表存在（与账户级ETL保持一致的结构）
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
            # 使用 UPSERT 保证幂等：相同(dataset,partition_key)多次写入将执行更新而非重复插入
            cur.execute(
                """
                INSERT INTO public.etl_watermarks (dataset, partition_key, last_time, last_updated)
                VALUES (%s, %s, now(), now())
                ON CONFLICT (dataset, partition_key)
                DO UPDATE SET last_time = now(), last_updated = now()
                """,
                ("pnl_client", "ALL"),
            )
        pg.commit()

        elapsed = time.perf_counter() - start_ts
        steps = [
            {"name": "watermark", "duration_seconds": t1 - t0},
            {"name": "candidates", "duration_seconds": cand_t1 - cand_t0, "total": cand_total, "missing": missing_cnt, "lag": lag_cnt},
            {"name": "accounts_upsert", "duration_seconds": acc_t1 - acc_t0, "affected_rows": accounts_affected},
            {"name": "delete_orphans", "duration_seconds": del_t1 - del_t0, "affected_rows": orphan_deleted},
            {"name": "mapping", "duration_seconds": map_t1 - map_t0, "loaded_mapping": loaded_mapping, "zipcode_changes": zipcode_changes, "zipcode_details": zipcode_details},
            {"name": "summary_upsert", "duration_seconds": sum_t1 - sum_t0, "affected_rows": summary_affected},
            {"name": "stats", "duration_seconds": stats_t1 - stats_t0},
        ]

        return {
            "success": True,
            "message": "Incremental refresh completed",
            "duration_seconds": elapsed,
            "steps": steps,
            "max_last_updated": max_lu,
            "raw_log": None,
        }
    except Exception as e:
        if pg is not None:
            try:
                pg.rollback()
            except Exception:
                pass
        return {
            "success": False,
            "message": f"Incremental refresh failed: {e}",
            "duration_seconds": 0.0,
            "steps": [],
            "raw_log": str(e),
        }
    finally:
        try:
            if mysql_fx is not None:
                mysql_fx.close()
        except Exception:
            pass
        try:
            if pg is not None and lock_held:
                # An exception on the way out leaves the transaction aborted,
                # which would make the unlock statement itself fail.
                pg.rollback()
                with pg.cursor() as cur:
                    cur.execute("SELECT pg_advisory_unlock(%s)", (LOCK_CLIENT_PNL_REFRESH,))
                    pg.commit()
        except Exception:
            pass
        try:
            if pg is not None:
                pg.close()
        except Exception:
            pass

