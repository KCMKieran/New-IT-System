import os
import decimal
import pandas as pd
import clickhouse_connect
import redis
import json
import hashlib
from typing import List, Dict, Any, Optional
from datetime import datetime, timedelta
import dotenv

dotenv.load_dotenv()

# Use centralized logging configuration
from app.core.logging_config import get_logger
from app.core.singleflight import SingleFlight

logger = get_logger(__name__)


def _json_serializer(obj):
    """
    Custom JSON serializer for types not supported by default json.dumps.
    
    Fresh grad note:
    - ClickHouse often returns Decimal for precise numeric values
    - Python's json module doesn't know how to serialize Decimal
    - This function converts Decimal to float for JSON compatibility
    """
    if isinstance(obj, decimal.Decimal):
        return float(obj)
    if isinstance(obj, datetime):
        return obj.isoformat()
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")

class ClickHouseService:
    def __init__(self):
        # 优先读取环境变量 (默认连接用于 PnL 分析等通用业务)
        self.host = os.getenv("CLICKHOUSE_HOST", "dwsz2tfd9y.ap-northeast-1.aws.clickhouse.cloud")
        self.port = int(os.getenv("CLICKHOUSE_PORT", "8443"))
        self.username = os.getenv("CLICKHOUSE_USER", "default")
        
        # 处理密码 (去除可能存在的首尾空格)
        raw_password = os.getenv("CLICKHOUSE_PASSWORD")
        self.password = raw_password.strip() if raw_password else None
        
        # Default connection: used when use_prod=False (e.g. client_return). KCM_fxbackoffice lives on prod cluster.
        self.database = os.getenv("CLICKHOUSE_DB", "Fxbo_Trades")
        self.secure = True  # Force TLS

        # Prod connection: CDC target cluster where KCM_fxbackoffice exists. Used by IB Report & Client PnL.
        self.prod_host = os.getenv("CLICKHOUSE_prod_HOST")
        self.prod_user = os.getenv("CLICKHOUSE_prod_USER")
        self.prod_pass = os.getenv("CLICKHOUSE_prod_PASSWORD")
        self.prod_db = "KCM_fxbackoffice"

        # --- 内存缓存 (用于 IB 组别列表，有效期 7 天，减少 ClickHouse 压力) ---
        self._group_cache = None
        self._cache_expiry = None

        # SingleFlight: coalesce concurrent identical ClickHouse queries
        # If 4 requests for the same PnL query arrive simultaneously,
        # only 1 actually hits ClickHouse; the others wait and share the result.
        self._singleflight = SingleFlight()

        # Redis 初始化 (用于业务数据缓存)
        try:
            self.redis_client = redis.Redis(
                host=os.getenv("REDIS_HOST", "localhost"),
                port=int(os.getenv("REDIS_PORT", 6379)),
                db=0,
                decode_responses=True 
            )
        except Exception as e:
            logger.error(f"Redis initialization failed: {e}")
            self.redis_client = None

        # [可选] 启动时尝试轻量连接测试
        try:
            with self.get_client() as client:
                client.command('SELECT 1')
            logger.info("ClickHouse default connection established")
        except Exception as e:
            logger.warning(f"ClickHouse connection warning: {e}")

    def get_client(self, use_prod: bool = False):
        """
        获取 ClickHouse 客户端连接。
        :param use_prod: 是否使用生产环境连接配置
        """
        if use_prod:
            # 这里的 host/user/password 必须从生产环境变量读取
            return clickhouse_connect.get_client(
                host=self.prod_host,
                username=self.prod_user,
                password=self.prod_pass,
                database=self.prod_db,
                secure=True
            )
        return clickhouse_connect.get_client(
            host=self.host,
            port=self.port,
            username=self.username,
            password=self.password,
            database=self.database,
            secure=self.secure
        )

    def get_ib_groups(self) -> Dict[str, Any]:
        """
        获取所有 IB 组别及其对应的用户数量。
        实现逻辑：
        1. 优先从内存缓存中读取数据。
        2. 如果缓存不存在或已超过 7 天，则从生产 ClickHouse 查询。
        3. 采用批量聚合查询方式提高性能。
        """
        now = datetime.now()
        
        # 1. 检查缓存是否有效 (7 天有效期)
        if self._group_cache and self._cache_expiry and now < self._cache_expiry:
            logger.info("Returning IB groups from memory cache")
            return self._group_cache

        # SingleFlight: if multiple requests for IB groups arrive at the same time
        # (e.g. StrictMode double-mount), only one thread queries ClickHouse.
        def _fetch_ib_groups():
            # Re-check memory cache inside singleflight (another thread may have populated it)
            if self._group_cache and self._cache_expiry and datetime.now() < self._cache_expiry:
                logger.info("Returning IB groups from memory cache (inside singleflight)")
                return self._group_cache

            logger.info("Fetching IB groups from ClickHouse (prod)")
            with self.get_client(use_prod=True) as client:
                # Step 1: get group basic info (categoryId=6 is the group category)
                tags_sql = 'SELECT id AS tag_id, tag AS tag_name FROM "fxbackoffice_tags" WHERE categoryId = 6'
                tags_result = client.query(tags_sql)
                tags_df = pd.DataFrame(tags_result.result_set, columns=tags_result.column_names)

                # Step 2: get user count per group (distinct count)
                count_sql = 'SELECT tagId, count(DISTINCT userId) AS user_count FROM "fxbackoffice_user_tags" GROUP BY tagId'
                counts_result = client.query(count_sql)
                counts_df = pd.DataFrame(counts_result.result_set, columns=counts_result.column_names)

                # Step 3: merge dataframes
                tags_df['tag_id'] = tags_df['tag_id'].astype(str)
                counts_df['tagId'] = counts_df['tagId'].astype(str)

                merged_df = pd.merge(tags_df, counts_df, left_on='tag_id', right_on='tagId', how='left')
                merged_df['user_count'] = merged_df['user_count'].fillna(0).astype(int)
                merged_df = merged_df.sort_values(by='user_count', ascending=False)
                
                group_list = merged_df[['tag_id', 'tag_name', 'user_count']].to_dict('records')
                
                previous_time = self._group_cache["last_update_time"] if self._group_cache else "N/A"
                
                result = {
                    "group_list": group_list,
                    "last_update_time": datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                    "previous_update_time": previous_time,
                    "total_groups": len(group_list)
                }

                # Update memory cache with 7-day expiry
                self._group_cache = result
                self._cache_expiry = datetime.now() + timedelta(days=7)
                
                logger.info(f"IB groups cache updated, found {len(group_list)} groups")
                return result

        try:
            return self._singleflight.do("ib_groups", _fetch_ib_groups)
        except Exception as e:
            logger.error(f"Error fetching IB groups from ClickHouse: {str(e)}", exc_info=True)
            # Fallback to expired cache if available
            if self._group_cache:
                logger.warning("Using expired cache as fallback due to query error.")
                return self._group_cache
            raise e

    def get_pnl_analysis(
        self, 
        start_date: datetime, 
        end_date: datetime, 
        search: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Query ClickHouse for client PnL analysis within a date range.
        Returns a dict containing 'data' (list of records) and 'statistics' (query metadata).
        Includes Redis caching logic.
        """
        logger.debug(f"PnL analysis request: start={start_date}, end={end_date}, search={search}")
        
        # 1. 生成缓存 Key (基于日期和搜索词进行 MD5)
        search_key = (search or "").strip()
        cache_params = f"pnl_v1_{start_date.date()}_{end_date.date()}_{search_key}"
        cache_key = f"app:pnl:cache:{hashlib.md5(cache_params.encode()).hexdigest()}"

        # 2. 尝试从 Redis 获取缓存
        try:
            cached_data = self.redis_client.get(cache_key)
            if cached_data:
                logger.info(f"Redis cache hit for PnL analysis: {cache_key[:50]}...")
                res = json.loads(cached_data)
                # 注入从缓存读取的标记
                if "statistics" in res:
                    res["statistics"]["from_cache"] = True
                return res
        except Exception as re:
            logger.warning(f"Redis read error: {re}")

        # 3. SingleFlight: coalesce concurrent identical queries into one execution.
        # If multiple threads request the same cache_key simultaneously (e.g. StrictMode
        # or multiple users), only the first actually queries ClickHouse.
        def _execute_pnl_query():
            # Re-check Redis inside singleflight (another thread may have populated it
            # while we were waiting for the singleflight lock)
            try:
                cached_data = self.redis_client.get(cache_key)
                if cached_data:
                    logger.info(f"Redis cache hit (inside singleflight): {cache_key[:50]}...")
                    res = json.loads(cached_data)
                    if "statistics" in res:
                        res["statistics"]["from_cache"] = True
                    return res
            except Exception:
                pass

            # Use prod connection: KCM_fxbackoffice lives there (CDC target).
            client = self.get_client(use_prod=True)

            # Optional: IB net deposit agg table. Not in KCM_fxbackoffice list; set env to table name or empty to skip (ib_net_deposit=0).
            raw_agg = (os.getenv("CLICKHOUSE_IB_NET_DEPOSIT_AGG_TABLE") or "ib_downline_net_deposit_agg").strip()
            agg_table = raw_agg if raw_agg and all(c.isalnum() or c == "_" for c in raw_agg) else ""

            # Format date strings
            start_str = start_date.strftime('%Y-%m-%d %H:%M:%S')
            end_str = end_date.strftime('%Y-%m-%d %H:%M:%S')

            if agg_table:
                ib_net_join = f"""
            LEFT JOIN (
                SELECT
                    ibId,
                    sumMerge(net_deposit) AS net_deposit_usd
                FROM {agg_table}
                GROUP BY ibId
            ) AS ib_sum ON toString(u.partnerId) = toString(ib_sum.ibId)"""
            else:
                # Dummy join so ib_net_deposit is 0 when table is not present in DB
                ib_net_join = """
            LEFT JOIN (SELECT NULL AS ibId, 0 AS net_deposit_usd) AS ib_sum ON 1 = 0"""

            sql = f"""
            WITH
                ib_costs AS (
                    SELECT
                        ticketSid,
                        sum(commission) AS total_ib_cost
                    FROM fxbackoffice_ib_processed_tickets
                    WHERE close_time >= %(start_date)s
                      AND close_time <= %(end_date)s
                    GROUP BY ticketSid
                )
            SELECT
                t.LOGIN AS account,
                m.userId AS client_id,
                any(m.NAME) AS client_name,
                any(m.GROUP) AS group,
                any(NULLIF(u.country, '')) AS country,
                any(m.ZIPCODE) AS zipcode,
                any(m.CURRENCY) AS currency,
                any(m.sid) AS sid,
                any(u.partnerId) AS partner_id,
                any(ib_sum.net_deposit_usd) AS ib_net_deposit,
                'MT4' AS server,

                countIf(t.CMD IN (0, 1)) AS total_trades,

                sumIf(t.lots, t.CMD IN (0, 1)) / if(any(m.CURRENCY) = 'CEN', 100, 1) AS total_volume_lots,

                sumIf(t.PROFIT, t.CMD IN (0, 1)) / if(any(m.CURRENCY) = 'CEN', 100, 1) AS trade_profit_usd,

                sumIf(t.SWAPS, t.CMD IN (0, 1)) / if(any(m.CURRENCY) = 'CEN', 100, 1) AS swap_usd,
                sumIf(t.COMMISSION, t.CMD IN (0, 1)) / if(any(m.CURRENCY) = 'CEN', 100, 1) AS commission_usd,

                COALESCE(sum(ib.total_ib_cost), 0) AS ib_commission_usd,

                ((sumIf(t.PROFIT + t.SWAPS + t.COMMISSION, t.CMD IN (0, 1)) * -1) / if(any(m.CURRENCY) = 'CEN', 100, 1)) - COALESCE(sum(ib.total_ib_cost), 0) AS broker_net_revenue,

                sumIf(t.PROFIT, t.CMD = 6) / if(any(m.CURRENCY) = 'CEN', 100, 1) AS period_net_deposit

            FROM fxbackoffice_mt4_trades AS t
            INNER JOIN fxbackoffice_mt4_users AS m ON t.LOGIN = m.LOGIN
            LEFT JOIN fxbackoffice_users AS u ON m.userId = u.id
            LEFT JOIN ib_costs AS ib ON t.ticketSid = ib.ticketSid
            {ib_net_join}
            WHERE
                t.CLOSE_TIME >= %(start_date)s
                AND t.CLOSE_TIME <= %(end_date)s
                AND t.CMD IN (0, 1, 6)

                AND m.userId > 0
                AND COALESCE(u.isEmployee, 0) != 1
            """
            
            parameters = {
                'start_date': start_str,
                'end_date': end_str
            }

            if search:
                # 性能优化：仅支持 ClientID 或 AccountID 搜索 (前缀匹配)
                clean_search = search.strip()
                if clean_search:
                    sql += " AND (toString(m.userId) LIKE %(search)s OR toString(t.LOGIN) LIKE %(search)s)"
                    parameters['search'] = f"{clean_search}%"

            sql += """
            GROUP BY t.LOGIN, m.userId
            -- 过滤掉既没有交易量也没有入金的记录
            HAVING total_volume_lots > 0 OR period_net_deposit != 0
            ORDER BY ib_commission_usd DESC
            """

            logger.debug(f"Executing PnL SQL with params: {parameters}")
            
            result = client.query(sql, parameters=parameters)
            columns = result.column_names
            data = [dict(zip(columns, row)) for row in result.result_set]
            
            # 安全获取 elapsed 时间 (优先使用纳秒)
            elapsed_ns = result.summary.get('elapsed_ns', 0)
            if elapsed_ns:
                elapsed_seconds = float(elapsed_ns) / 1_000_000_000
            else:
                elapsed_seconds = result.summary.get('elapsed', 0)

            if not data:
                return {
                    "data": [], 
                    "statistics": {
                        "elapsed": elapsed_seconds,
                        "rows_read": result.summary.get('read_rows', 0),
                        "bytes_read": result.summary.get('read_bytes', 0)
                    }
                }

            # 使用 Pandas 处理数据清洗 (fillna 等)
            df = pd.DataFrame(data)
            # IMPORTANT (fresh grad note):
            # Do NOT do `df.fillna(0)` on the whole DataFrame.
            # Reason: it will convert NULLs in TEXT columns into 0 (e.g. country/client_name/group),
            # which pollutes UI display and breaks "empty should stay NULL" requirements.
            #
            # Instead, we maintain a NUMERIC column whitelist and only fill NULLs for those metrics.
            # Maintenance rule:
            # - If you add/remove numeric metrics in the SELECT list (sum/count/amount fields),
            #   update this list accordingly.
            # - Keep dimension / identity fields (ids, names, group, zipcode, currency, server, country)
            #   OUT of this whitelist so they can remain NULL/empty naturally.
            #
            # Why a whitelist (instead of pandas dtype auto-detection)?
            # - In real datasets, numeric columns may arrive as strings/Decimal/object due to driver quirks,
            #   mixed types, or missing values. dtype-based detection can silently miss columns.
            # - A whitelist is explicit, stable, and aligned with business semantics.
            NUMERIC_FILLNA_ZERO_COLUMNS = [
                "total_trades",
                "total_volume_lots",
                "trade_profit_usd",
                "swap_usd",
                "commission_usd",
                "ib_commission_usd",
                "broker_net_revenue",
                "period_net_deposit",
                "ib_net_deposit",
            ]

            for col in NUMERIC_FILLNA_ZERO_COLUMNS:
                if col in df.columns:
                    df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)
            
            processed_data = df.to_dict('records')

            result_dict = {
                "data": processed_data,
                "statistics": {
                    "elapsed": elapsed_seconds,
                    "rows_read": result.summary.get('read_rows', 0),
                    "bytes_read": result.summary.get('read_bytes', 0),
                    "from_cache": False
                }
            }

            # Save to Redis cache (TTL 1800s = 30 minutes)
            try:
                self.redis_client.setex(
                    cache_key,
                    1800,
                    json.dumps(result_dict, default=_json_serializer)
                )
                logger.info(f"Redis cache saved: {cache_key[:50]}...")
            except Exception as se:
                logger.warning(f"Redis save error: {se}")

            return result_dict

        try:
            return self._singleflight.do(cache_key, _execute_pnl_query)
        except Exception as e:
            logger.exception(f"ClickHouse query error in get_pnl_analysis")
            raise e

    def get_ib_report_data(
        self,
        r_start: datetime,
        r_end: datetime,
        m_start: datetime,
        m_end: datetime,
        target_groups: List[str]
    ) -> List[Dict[str, Any]]:
        """
        获取 IB 报表数据。
        执行复杂的聚合查询，计算 Range 和 Month 维度的资金、交易量和佣金数据。
        
        优化：增加了 Redis 缓存机制，避免高并发下重复查询。
        """
        # 1. 生成 Cache Key (考虑所有影响 SQL 的变量)
        try:
            # 将组别列表排序，确保 ['A', 'B'] 和 ['B', 'A'] 生成相同的 key
            sorted_groups = sorted(target_groups or [])
            # 组合所有参数生成唯一字符串
            cache_params = f"ib_report_v1_{r_start}_{r_end}_{m_start}_{m_end}_{sorted_groups}"
            # 使用 MD5 生成短 key
            cache_key = f"app:ib_report:cache:{hashlib.md5(cache_params.encode()).hexdigest()}"
            
            # 2. 尝试从 Redis 读取
            if self.redis_client:
                cached_data = self.redis_client.get(cache_key)
                if cached_data:
                    logger.info(f"Redis cache hit for IB report: {cache_key[:50]}...")
                    return json.loads(cached_data)
        except Exception as e:
            logger.warning(f"Redis read error for IB report: {e}")

        try:
            logger.info(f"Fetching IB report data from ClickHouse: range={r_start}~{r_end}, month={m_start}~{m_end}, groups={len(target_groups)}")
            
            with self.get_client(use_prod=True) as client:
                sql = """
                WITH
                    -- [1] 参数定义 (由后端动态注入)
                    toDateTime64(%(r_start)s, 6) AS r_start,
                    toDateTime64(%(r_end)s, 6) AS r_end,
                    toDateTime64(%(m_start)s, 6) AS m_start,
                    toDateTime64(%(m_end)s, 6) AS m_end,
                    toDate32(%(r_start)s) AS r_date_start,
                    toDate32(%(r_end)s) AS r_date_end,
                    toDate32(%(m_start)s) AS m_date_start,
                    toDate32(%(m_end)s) AS m_date_end,
                    %(target_groups)s AS target_groups,

                    -- [2] 组别映射: 找到目标组别下的所有 User ID
                    group_mapping AS (
                        SELECT
                            t.tag AS group_name,
                            ut.userId AS user_id
                        FROM "fxbackoffice_tags" t
                        JOIN "fxbackoffice_user_tags" ut ON t.id = ut.tagId
                        WHERE t.categoryId = 6
                          AND (length(target_groups) = 0 OR has(arrayMap(x -> lower(x), target_groups), lower(t.tag)))
                    ),

                    -- [3] 资金统计: Transactions 表
                    money_stats AS (
                        SELECT
                            gm.group_name,
                            -- Range Stats
                            sumIf(if(upper(tr.processedCurrency) = 'CEN', tr.processedAmount / 100, tr.processedAmount), tr.type = 'deposit' AND tr.processedAt BETWEEN r_start AND r_end) AS deposit_range,
                            sumIf(if(upper(tr.processedCurrency) = 'CEN', tr.processedAmount / 100, tr.processedAmount), tr.type = 'withdrawal' AND tr.processedAt BETWEEN r_start AND r_end) AS withdrawal_range,
                            sumIf(if(upper(tr.processedCurrency) = 'CEN', tr.processedAmount / 100, tr.processedAmount), tr.type = 'ib withdrawal' AND tr.processedAt BETWEEN r_start AND r_end) AS ib_withdrawal_range,
                            -- Month Stats
                            sumIf(if(upper(tr.processedCurrency) = 'CEN', tr.processedAmount / 100, tr.processedAmount), tr.type = 'deposit' AND tr.processedAt BETWEEN m_start AND m_end) AS deposit_month,
                            sumIf(if(upper(tr.processedCurrency) = 'CEN', tr.processedAmount / 100, tr.processedAmount), tr.type = 'withdrawal' AND tr.processedAt BETWEEN m_start AND m_end) AS withdrawal_month,
                            sumIf(if(upper(tr.processedCurrency) = 'CEN', tr.processedAmount / 100, tr.processedAmount), tr.type = 'ib withdrawal' AND tr.processedAt BETWEEN m_start AND m_end) AS ib_withdrawal_month
                        FROM "fxbackoffice_transactions" tr
                        INNER JOIN group_mapping gm ON tr.fromUserId = gm.user_id
                        WHERE tr.status = 'approved' 
                          AND tr.type IN ('deposit', 'withdrawal', 'ib withdrawal')
                          AND tr.processedAt >= m_start
                        GROUP BY gm.group_name
                    ),

                    -- [4] 交易统计: MT4 Trades 表
                    trade_stats AS (
                        SELECT
                            gm.group_name,
                            -- Range Stats
                            sumIf(t.lots / if(mu.CURRENCY = 'CEN', 100, 1), t.CLOSE_TIME BETWEEN r_start AND r_end) AS volume_range,
                            sumIf((t.PROFIT + t.SWAPS + t.COMMISSION) / if(mu.CURRENCY = 'CEN', 100, 1), t.CLOSE_TIME BETWEEN r_start AND r_end) AS net_profit_range,
                            sumIf(t.COMMISSION / if(mu.CURRENCY = 'CEN', 100, 1), t.CLOSE_TIME BETWEEN r_start AND r_end) AS commission_range,
                            sumIf(t.SWAPS / if(mu.CURRENCY = 'CEN', 100, 1), t.CLOSE_TIME BETWEEN r_start AND r_end) AS swap_range,
                            -- Month Stats
                            sumIf(t.lots / if(mu.CURRENCY = 'CEN', 100, 1), t.CLOSE_TIME BETWEEN m_start AND m_end) AS volume_month,
                            sumIf((t.PROFIT + t.SWAPS + t.COMMISSION) / if(mu.CURRENCY = 'CEN', 100, 1), t.CLOSE_TIME BETWEEN m_start AND m_end) AS net_profit_month,
                            sumIf(t.COMMISSION / if(mu.CURRENCY = 'CEN', 100, 1), t.CLOSE_TIME BETWEEN m_start AND m_end) AS commission_month,
                            sumIf(t.SWAPS / if(mu.CURRENCY = 'CEN', 100, 1), t.CLOSE_TIME BETWEEN m_start AND m_end) AS swap_month
                        FROM "fxbackoffice_mt4_trades" t
                        INNER JOIN "fxbackoffice_mt4_users" mu ON t.LOGIN = mu.LOGIN
                        INNER JOIN group_mapping gm ON mu.userId = gm.user_id
                        WHERE t.CMD IN (0, 1) AND t.CLOSE_TIME >= m_start
                        GROUP BY gm.group_name
                    ),

                    -- [5] IB佣金统计: Stats 预聚合表
                    ib_commission_stats AS (
                        SELECT
                            gm.group_name,
                            -- Range Stats
                            sumIf(st.commission / if(upper(st.currency) = 'CEN', 100, 1), st.date BETWEEN r_date_start AND r_date_end) AS ib_commission_range,
                            -- Month Stats
                            sumIf(st.commission / if(upper(st.currency) = 'CEN', 100, 1), st.date BETWEEN m_date_start AND m_date_end) AS ib_commission_month
                        FROM "fxbackoffice_stats_ib_commissions_by_login_sid" st
                        -- Fix: Split SID-LOGIN format (e.g., '1-8522845') and match with mu.LOGIN
                        INNER JOIN "fxbackoffice_mt4_users" mu 
                            ON splitByChar('-', st.fromLoginSid)[2] = toString(mu.LOGIN)
                        INNER JOIN group_mapping gm ON mu.userId = gm.user_id
                        WHERE st.date >= m_date_start
                        GROUP BY gm.group_name
                    )

                -- [6] 最终输出 (Result Set)
                SELECT
                    coalesce(m.group_name, t.group_name, i.group_name) AS group,
                    
                    round(coalesce(m.deposit_range, 0), 2) AS deposit_range,
                    round(coalesce(m.deposit_month, 0), 2) AS deposit_month,
                    
                    round(coalesce(m.withdrawal_range, 0), 2) AS withdrawal_range,
                    round(coalesce(m.withdrawal_month, 0), 2) AS withdrawal_month,
                    
                    round(coalesce(m.ib_withdrawal_range, 0), 2) AS ib_withdrawal_range,
                    round(coalesce(m.ib_withdrawal_month, 0), 2) AS ib_withdrawal_month,
                    
                    -- Net Deposit = D + W + IBW (Arithmetic Sum)
                    round(coalesce(m.deposit_range, 0) + coalesce(m.withdrawal_range, 0) + coalesce(m.ib_withdrawal_range, 0), 2) AS net_deposit_range,
                    round(coalesce(m.deposit_month, 0) + coalesce(m.withdrawal_month, 0) + coalesce(m.ib_withdrawal_month, 0), 2) AS net_deposit_month,
                    
                    round(coalesce(t.volume_range, 0), 2) AS volume_range,
                    round(coalesce(t.volume_month, 0), 2) AS volume_month,
                    
                    round(coalesce(t.net_profit_range, 0), 2) AS profit_range,
                    round(coalesce(t.net_profit_month, 0), 2) AS profit_month,
                    
                    round(coalesce(t.commission_range, 0), 2) AS commission_range,
                    round(coalesce(t.commission_month, 0), 2) AS commission_month,

                    round(coalesce(t.swap_range, 0), 2) AS swap_range,
                    round(coalesce(t.swap_month, 0), 2) AS swap_month,
                    
                    round(coalesce(i.ib_commission_range, 0), 2) AS ib_commission_range,
                    round(coalesce(i.ib_commission_month, 0), 2) AS ib_commission_month

                FROM money_stats m
                FULL OUTER JOIN trade_stats t ON m.group_name = t.group_name
                FULL OUTER JOIN ib_commission_stats i ON coalesce(m.group_name, t.group_name) = i.group_name
                ORDER BY deposit_range DESC
                """
                
                params = {
                    'r_start': r_start.strftime('%Y-%m-%d %H:%M:%S'),
                    'r_end': r_end.strftime('%Y-%m-%d %H:%M:%S'),
                    'm_start': m_start.strftime('%Y-%m-%d %H:%M:%S'),
                    'm_end': m_end.strftime('%Y-%m-%d %H:%M:%S'),
                    'target_groups': target_groups
                }
                
                result = client.query(sql, parameters=params)
                
                # Convert result to list of dicts
                columns = result.column_names
                data = [dict(zip(columns, row)) for row in result.result_set]
                
                # 3. 将结果写入 Redis 缓存 (设置 10 分钟过期)
                try:
                    if self.redis_client and data:
                        self.redis_client.setex(
                            cache_key,
                            600, # TTL 10 分钟
                            json.dumps(data, default=_json_serializer)
                        )
                        logger.info(f"Redis cache saved for IB report: {cache_key[:50]}...")
                except Exception as e:
                    logger.warning(f"Redis save error for IB report: {e}")
                
                return data
                
        except Exception as e:
            logger.error(f"Error fetching IB report data: {str(e)}", exc_info=True)
            raise e

clickhouse_service = ClickHouseService()
