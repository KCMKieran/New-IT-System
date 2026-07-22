from __future__ import annotations

import logging
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Iterable, List, Tuple

import fcntl
import pymysql

from ..core.config import Settings
from ..schemas.ib_data import RegionSummary, RegionTypeMetrics

logger = logging.getLogger(__name__)


IB_QUERY = """
WITH params AS (
    SELECT
        %s AS target_ib,
        %s AS start_time,
        %s AS end_time
),
tx_referrals AS (
    SELECT it.referralId
    FROM fxbackoffice.ib_tree_with_self it
    JOIN params p ON p.target_ib = it.ibid
),
tx_totals AS (
    SELECT
        SUM(CASE WHEN st.type = 'deposit'       THEN normalized_amount ELSE 0 END) AS deposit_usd,
        SUM(CASE WHEN st.type = 'withdrawal'    THEN normalized_amount ELSE 0 END) AS withdrawal_usd,
        SUM(CASE WHEN st.type = 'ib withdrawal' THEN normalized_amount ELSE 0 END) AS ib_withdrawal_usd
    FROM (
        SELECT
            st.type,
            CASE
                WHEN UPPER(st.currency) = 'CEN' THEN st.amount / 100.0
                ELSE st.amount
            END AS normalized_amount
        FROM fxbackoffice.stats_transactions st
        JOIN params p
        WHERE st.type IN ('deposit', 'withdrawal', 'ib withdrawal')
          AND st.date >= DATE(p.start_time)
          AND st.date <= DATE(p.end_time)
          AND st.userId IN (SELECT referralId FROM tx_referrals)
    ) st
),
wallet_referrals AS (
    SELECT it.referralId
    FROM fxbackoffice.ib_tree_with_self it
    JOIN params p ON p.target_ib = it.ibid
),
wallet_total AS (
    -- STOCK, not flow: the CURRENT balance sitting in the IB wallet accounts.
    -- Deliberately NOT constrained by the start/end window (mt4_users only
    -- holds a live balance snapshot, there is no per-day wallet history here),
    -- so this value is a lifetime-to-date figure and must never be mixed into
    -- the windowed net_deposit_usd arithmetic below. It is surfaced as its own
    -- column for the UI to render alongside the flow columns.
    SELECT IFNULL(SUM(mu.balance), 0) AS ib_wallet_balance
    FROM fxbackoffice.mt4_users mu
    WHERE mu.`GROUP` LIKE 'IB-WALLET%%'
      AND mu.userId IN (SELECT referralId FROM wallet_referrals)
)
SELECT
    p.target_ib AS ibid,
    IFNULL(tx.deposit_usd, 0) AS deposit_usd,
    IFNULL(tx.withdrawal_usd, 0) + IFNULL(tx.ib_withdrawal_usd, 0) AS total_withdrawal_usd,
    IFNULL(tx.ib_withdrawal_usd, 0) AS ib_withdrawal_usd,
    IFNULL(wt.ib_wallet_balance, 0) AS ib_wallet_balance,
    -- Net Deposit = deposit + withdrawal + 'ib withdrawal', an arithmetic sum:
    -- withdrawal / 'ib withdrawal' amounts are stored NEGATIVE in
    -- stats_transactions, so adding them subtracts the money that went out.
    -- INCLUDES 'ib withdrawal' (IB commission withdrawals), matching the
    -- business-confirmed formula in docs/archive/ib-net-deposit-reform.md and
    -- the IB Report (clickhouse_service.py net_deposit_range/month).
    -- The IB wallet balance is NOT subtracted here: it is a lifetime stock and
    -- these three legs are window-filtered flows (see wallet_total above).
    IFNULL(tx.deposit_usd, 0)
        + (IFNULL(tx.withdrawal_usd, 0) + IFNULL(tx.ib_withdrawal_usd, 0)) AS net_deposit_usd
FROM params p
LEFT JOIN tx_totals tx ON 1=1
LEFT JOIN wallet_total wt ON 1=1
"""

LAST_QUERY_FILENAME = "ib_data_last_query.txt"
LOCK_FILENAME = "ib_data_last_query.lock"


def _connect(settings: Settings):
    """Create a MySQL connection using shared FX backoffice credentials."""
    if not settings.DB_HOST:
        raise RuntimeError("DB_HOST is not configured")

    return pymysql.connect(
        host=settings.DB_HOST,
        user=settings.DB_USER,
        password=settings.DB_PASSWORD,
        database=settings.DB_NAME,
        port=int(settings.DB_PORT),
        charset=settings.DB_CHARSET,
        cursorclass=pymysql.cursors.DictCursor,
    )


def _last_query_path(settings: Settings) -> Path:
    return settings.parquet_dir / LAST_QUERY_FILENAME


def _lock_path(settings: Settings) -> Path:
    return settings.parquet_dir / LOCK_FILENAME


@contextmanager
def _file_lock(path: Path):
    """Advisory lock to block concurrent heavy queries."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w+") as fp:
        fcntl.flock(fp, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fp, fcntl.LOCK_UN)


def _to_float(value) -> float:
    if value is None:
        return 0.0
    if isinstance(value, Decimal):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _normalize_range(dt: datetime) -> str:
    """Format datetime for SQL layer."""
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _query_single_ib(conn, ibid: str, start_str: str, end_str: str) -> dict:
    """Execute SQL query for a single IB ID and return normalized metrics."""
    try:
        with conn.cursor() as cur:
            cur.execute(IB_QUERY, (ibid, start_str, end_str))
            row = cur.fetchone() or {}
        return {
            "ibid": str(row.get("ibid", ibid)),
            "deposit_usd": _to_float(row.get("deposit_usd")),
            "total_withdrawal_usd": _to_float(row.get("total_withdrawal_usd")),
            "ib_withdrawal_usd": _to_float(row.get("ib_withdrawal_usd")),
            "ib_wallet_balance": _to_float(row.get("ib_wallet_balance")),
            "net_deposit_usd": _to_float(row.get("net_deposit_usd")),
        }
    except Exception as e:
        logger.error(f"Query failed for IB ID {ibid}: {type(e).__name__}: {e}")
        raise RuntimeError(f"查询 IB {ibid} 时发生错误: {str(e)}") from e


def _sum_rows(rows: Iterable[dict]) -> dict:
    totals = {
        "deposit_usd": 0.0,
        "total_withdrawal_usd": 0.0,
        "ib_withdrawal_usd": 0.0,
        "ib_wallet_balance": 0.0,
        "net_deposit_usd": 0.0,
    }
    for row in rows:
        for key in totals.keys():
            totals[key] += float(row.get(key, 0.0) or 0.0)
    return totals


def read_last_query_time(settings: Settings) -> datetime | None:
    path = _last_query_path(settings)
    try:
        raw = path.read_text().strip()
        if not raw:
            return None
        return datetime.fromisoformat(raw)
    except FileNotFoundError:
        return None
    except ValueError:
        return None


def _write_last_query_time(settings: Settings, ts: datetime) -> None:
    path = _last_query_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(ts.isoformat())


def aggregate_ib_data(
    settings: Settings,
    ib_ids: List[str],
    start: datetime,
    end: datetime,
) -> Tuple[list[dict], dict, datetime]:
    """Aggregate IB data for given IDs and time range. Returns (rows, totals, timestamp)."""
    if not ib_ids:
        raise ValueError("ib_ids cannot be empty")
    if end < start:
        raise ValueError("end must be greater than or equal to start")

    start_str = _normalize_range(start)
    end_str = _normalize_range(end)

    logger.info(f"Starting IB data aggregation: ib_ids={ib_ids}, start={start_str}, end={end_str}")

    try:
        with _file_lock(_lock_path(settings)):
            try:
                conn = _connect(settings)
            except Exception as e:
                logger.error(f"Database connection failed: {type(e).__name__}: {e}")
                raise RuntimeError(f"数据库连接失败: {str(e)}") from e

            try:
                rows = []
                for ibid in ib_ids:
                    try:
                        row = _query_single_ib(conn, ibid, start_str, end_str)
                        rows.append(row)
                    except Exception as e:
                        logger.error(f"Failed to query IB {ibid}: {e}")
                        # Continue with other IB IDs instead of failing completely
                        rows.append({
                            "ibid": ibid,
                            "deposit_usd": 0.0,
                            "total_withdrawal_usd": 0.0,
                            "ib_withdrawal_usd": 0.0,
                            "ib_wallet_balance": 0.0,
                            "net_deposit_usd": 0.0,
                        })
            finally:
                try:
                    conn.close()
                except Exception:
                    pass

            totals = _sum_rows(rows)
            timestamp = datetime.now(timezone.utc)
            try:
                _write_last_query_time(settings, timestamp)
            except Exception as e:
                logger.warning(f"Failed to write last query time: {e}")

            logger.info(f"IB data aggregation completed: {len(rows)} rows")
            return rows, totals, timestamp
    except Exception as e:
        logger.error(f"IB data aggregation failed: {type(e).__name__}: {e}", exc_info=True)
        raise


# ============ Region Analytics (地区出入金查询) ============

REGION_QUERY = """
SELECT
    u.cid,
    st.type,
    SUM(st.countTransactions) AS tx_count,
    SUM(
        CASE
            WHEN UPPER(st.currency) = 'CEN' THEN st.amount / 100.0
            ELSE st.amount
        END
    ) AS amount_usd
FROM fxbackoffice.stats_transactions st
INNER JOIN fxbackoffice.users u ON st.userId = u.id
WHERE st.type IN ('deposit', 'withdrawal', 'ib withdrawal')
  AND st.date >= DATE(%s)
  AND st.date < DATE(%s)
GROUP BY u.cid, st.type
ORDER BY u.cid, st.type
"""


def _get_company_name(cid: int) -> str:
    """Convert cid to human-readable company name."""
    if cid == 0:
        return "CN"
    elif cid == 1:
        return "Global"
    else:
        return f"Unknown({cid})"


def query_region_analytics(
    settings: Settings,
    start: datetime,
    end: datetime,
) -> List[RegionSummary]:
    """
    Query deposit/withdrawal analytics grouped by region (company).
    
    Args:
        settings: Application settings with DB credentials
        start: Inclusive start time
        end: Exclusive end time
        
    Returns:
        List of RegionSummary objects, one per region (cid)
    """
    if end < start:
        raise ValueError("end must be greater than or equal to start")

    start_str = _normalize_range(start)
    end_str = _normalize_range(end)

    logger.info(f"Region analytics query: start={start_str}, end={end_str}")

    try:
        conn = _connect(settings)
    except Exception as e:
        logger.error(f"Database connection failed: {type(e).__name__}: {e}")
        raise RuntimeError(f"数据库连接失败: {str(e)}") from e

    try:
        with conn.cursor() as cur:
            cur.execute(REGION_QUERY, (start_str, end_str))
            rows = cur.fetchall()
    finally:
        try:
            conn.close()
        except Exception:
            pass

    # Group results by cid
    # Expected rows: [{'cid': 0, 'type': 'deposit', 'tx_count': 100, 'amount_usd': Decimal('...')}, ...]
    region_map: dict[int, RegionSummary] = {}

    for row in rows:
        cid = int(row.get("cid", -1))
        tx_type = str(row.get("type", "")).lower()
        tx_count = int(row.get("tx_count", 0))
        amount_usd = _to_float(row.get("amount_usd"))

        # Initialize region if not exists
        if cid not in region_map:
            region_map[cid] = RegionSummary(
                cid=cid,
                company_name=_get_company_name(cid),
            )

        region = region_map[cid]
        metrics = RegionTypeMetrics(tx_count=tx_count, amount_usd=amount_usd)

        # Assign metrics to the appropriate type
        if tx_type == "deposit":
            region.deposit = metrics
        elif tx_type == "withdrawal":
            region.withdrawal = metrics
        elif tx_type == "ib withdrawal":
            region.ib_withdrawal = metrics

    # Calculate derived fields for each region
    for region in region_map.values():
        region.total_deposit_usd = region.deposit.amount_usd
        region.total_withdrawal_usd = abs(region.withdrawal.amount_usd) + abs(region.ib_withdrawal.amount_usd)
        region.net_deposit_usd = region.total_deposit_usd - region.total_withdrawal_usd

    # Sort by cid and return as list
    result = sorted(region_map.values(), key=lambda r: r.cid)
    logger.info(f"Region analytics completed: {len(result)} regions")
    return result

