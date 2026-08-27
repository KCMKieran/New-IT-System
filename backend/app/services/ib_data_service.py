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
    {referral_scope}
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
    {referral_scope}
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

# ── Row-level (country) data scope, applied to the RELATED side ──────────────
#
# The route gates the INPUT (the caller's `ib_ids` are checked against their
# cids, all-or-nothing). That is not enough here, and the reason is the same
# one as in ibid_lots_service: the two CTEs above fan OUT to the named IB's
# whole downline, and every figure this endpoint returns — deposits,
# withdrawals, IB-wallet balance, net deposit — is a SUM over that downline.
# 11 Global IBs have at least one CN client under them, so an in-scope IB's
# totals silently include CN clients' money.
#
# Injected into BOTH CTEs, in SQL, so the narrowing happens before any amount
# is summed rather than after. There is nothing to post-filter here even in
# principle: by the time the rows reach Python they are already one aggregated
# number per IB, with the out-of-scope money folded in and unrecoverable.
#
# The JOIN is the filter: a referral whose cid is NULL or is some entity nobody
# told us about has no matching users row and drops out. Fail closed, with no
# extra branch that could be forgotten. Note _get_company_name() further down
# renders an unrecognised cid as the visible string "Unknown(2)" — that is the
# shape of mistake this join must not make.
REFERRAL_SCOPE_JOIN = """JOIN fxbackoffice.users u
      ON u.id = it.referralId
     AND u.cid IN ({cid_placeholders})"""

# "Was anything actually removed?", asked ONCE for the whole request rather
# than per IB id. Existence only — it returns a literal 1 and never a client id
# or an amount, so the flag is obtained without reading a single out-of-scope
# row into the process.
#
# Over-reporting (a notice on a request that happened to be fully in scope) is
# harmless; UNDER-reporting means a restricted colleague sees smaller totals
# than their neighbour with nothing on the page saying why. So an unresolvable
# cid counts as filtered, same direction as everywhere else in this change.
SCOPE_PROBE_QUERY = """
SELECT 1 AS hit
FROM fxbackoffice.ib_tree_with_self it
LEFT JOIN fxbackoffice.users u ON u.id = it.referralId
WHERE it.ibid IN ({ib_placeholders})
  AND (u.cid IS NULL OR u.cid NOT IN ({cid_placeholders}))
LIMIT 1
"""

# Server-side statement kill switch. Set as a SESSION variable rather than as a
# `/*+ MAX_EXECUTION_TIME(...) */` hint because IB_QUERY is a `WITH ... SELECT`
# and MySQL only honours that hint on the outermost query block — a hint that
# lands in the wrong place is accepted with a warning and silently does
# nothing, which is the worst possible outcome for a guard.
#
# Applied ONLY on the scoped path. Not because the unrestricted query deserves
# less protection, but because this change owns the scoped statements and an
# extra `SET` round-trip on every colleague's IB query is exactly the cost the
# short-circuit exists to avoid. Guarding the unscoped query too is a separate,
# larger change (this connection carries no read_timeout either).
#
# A client-side timeout would not substitute: it abandons the SOCKET while the
# server thread keeps running, queued behind whatever lock it wanted, as a
# zombie — 2,637 of them on the replica in 14.5h on 2026-08-09.
_SCOPE_MAX_EXECUTION_TIME_MS = 60000

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


# The unscoped statement, materialised once: identical to what this module ran
# before the data scope existed (bar one blank line where the JOIN goes).
IB_QUERY_UNSCOPED = IB_QUERY.format(referral_scope="")


def _scoped_ib_query(cid_count: int) -> str:
    """IB_QUERY with the cid JOIN spliced into both referral CTEs.

    Placeholders are BUILT from the count, never interpolated from the values:
    this is an authorization filter, and an injection here does not merely leak
    a row, it edits the question that decides who may see it.
    """
    placeholders = ",".join(["%s"] * cid_count)
    return IB_QUERY.format(
        referral_scope=REFERRAL_SCOPE_JOIN.format(cid_placeholders=placeholders)
    )


def _downline_has_out_of_scope(
    conn, ib_ids: List[str], allowed_cids: frozenset
) -> bool:
    """Did the scope JOIN above actually remove anybody? One query, whole request."""
    cid_params = tuple(sorted(allowed_cids))
    sql = SCOPE_PROBE_QUERY.format(
        ib_placeholders=",".join(["%s"] * len(ib_ids)),
        cid_placeholders=",".join(["%s"] * len(cid_params)),
    )
    with conn.cursor() as cur:
        cur.execute(sql, tuple(ib_ids) + cid_params)
        return cur.fetchone() is not None


def _query_single_ib(
    conn,
    ibid: str,
    start_str: str,
    end_str: str,
    sql: str = IB_QUERY_UNSCOPED,
    scope_params: tuple = (),
) -> dict:
    """Execute SQL query for a single IB ID and return normalized metrics.

    ``scope_params`` carries the caller's cids once per referral CTE, in
    statement order (params CTE first, then tx_referrals, then
    wallet_referrals). Order matters and is not checkable at runtime — pymysql
    fills %s positionally — so the tuple is assembled in exactly one place,
    ``aggregate_ib_data`` below.
    """
    try:
        with conn.cursor() as cur:
            cur.execute(sql, (ibid, start_str, end_str) + scope_params)
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
    allowed_cids: frozenset | None = None,
) -> Tuple[list[dict], dict, datetime, bool]:
    """Aggregate IB data for given IDs and time range.

    Returns (rows, totals, timestamp, data_scope_filtered).

    ``allowed_cids`` is the caller's country data scope. ``None`` means
    UNRESTRICTED and runs the original statement unchanged — no extra
    predicate, no extra query, no extra round-trip. It is never tested for
    truthiness: ``None`` (no restriction) and an empty set (may see nothing)
    are opposite answers, the ``["*"]`` vs ``[]`` trap again.
    """
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
                data_scope_filtered = False
                sql = IB_QUERY_UNSCOPED
                scope_params: tuple = ()
                if allowed_cids is not None:
                    # Restricted caller only: everything in this block is work
                    # the other ~30 colleagues never pay for.
                    with conn.cursor() as cur:
                        cur.execute(
                            f"SET SESSION MAX_EXECUTION_TIME = {_SCOPE_MAX_EXECUTION_TIME_MS}"
                        )
                    # The empty set means "may see NOTHING" (never None, which
                    # would have short-circuited above). It is unreachable
                    # today, but `IN ()` is a syntax error, so it is spelled
                    # `IN (NULL)` — which matches no row, and is the fail-closed
                    # answer. The probe is skipped for it because `NOT IN
                    # (NULL)` is NULL, i.e. it would answer "nothing was
                    # filtered" about a response that filtered everything.
                    cid_params = tuple(sorted(allowed_cids)) or (None,)
                    data_scope_filtered = (
                        True
                        if not allowed_cids
                        else _downline_has_out_of_scope(conn, ib_ids, allowed_cids)
                    )
                    sql = _scoped_ib_query(len(cid_params))
                    # Once per referral CTE, tx_referrals before
                    # wallet_referrals — statement order, see _query_single_ib.
                    scope_params = cid_params + cid_params

                rows = []
                for ibid in ib_ids:
                    try:
                        row = _query_single_ib(
                            conn, ibid, start_str, end_str, sql, scope_params
                        )
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

            logger.info(
                f"IB data aggregation completed: {len(rows)} rows, "
                f"data_scope_filtered={data_scope_filtered}"
            )
            return rows, totals, timestamp, data_scope_filtered
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

