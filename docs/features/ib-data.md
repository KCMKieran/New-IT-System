# IB Data - Deposit/Withdrawal Query

> 出入金查询模块，支持按 IB ID 或按地区 (Company) 查询出入金数据。

## Overview

This page (`/warehouse/ib-data`) provides two query modules:

1. **IB 出入金查询** - Query deposit/withdrawal by specific IB IDs
2. **Company 出入金查询** - Query deposit/withdrawal aggregated by region (CN/Global)

## Features

### IB Deposit/Withdrawal Query

| Feature | Description |
|---------|-------------|
| Input | Comma-separated IB IDs (e.g., `107779,129860`) |
| Date Range | Week, Month, Custom |
| Output | Per-IB breakdown with totals |
| Data Source | MySQL `fxbackoffice.stats_transactions` + `fxbackoffice.ib_tree_with_self` (wallet: `fxbackoffice.mt4_users`) |

**Metrics**:
- Deposit (USD)
- Total Withdrawal (USD)
- IB Withdrawal (USD)
- IB Wallet Balance (USD)
- Net Deposit (USD)

### Company Deposit/Withdrawal Query

| Feature | Description |
|---------|-------------|
| Input | Date range only (no IB ID needed) |
| Date Range | Past week, This month, Last month, Custom |
| Output | Aggregated by region (CN/Global) |
| Data Source | MySQL `fxbackoffice.stats_transactions` JOIN `fxbackoffice.users` |

**Region Logic**:
- `cid = 0` → CN (China)
- `cid = 1` → Global

**Metrics**:
- Deposit (USD)
- Withdrawal (USD)
- IB Withdrawal (USD)
- Total Withdrawal (USD)
- Net Deposit (USD)

## API Endpoints

### 1. Query by IB IDs

```
POST /api/v1/ib-data/query
```

**Request**:
```json
{
  "ib_ids": ["107779", "129860"],
  "start": "2026-01-01 00:00:00",
  "end": "2026-01-31 23:59:59"
}
```

**Response**:
```json
{
  "rows": [
    {
      "ibid": "107779",
      "deposit_usd": 12345.67,
      "total_withdrawal_usd": -5678.90,
      "ib_withdrawal_usd": -1234.56,
      "ib_wallet_balance": 500.00,
      "net_deposit_usd": 6166.77
    }
  ],
  "totals": { ... },
  "last_query_time": "2026-01-15T10:30:00Z"
}
```

### 2. Query by Region (Company)

```
POST /api/v1/ib-data/region-query
```

**Request**:
```json
{
  "start": "2026-01-01 00:00:00",
  "end": "2026-02-01 00:00:00"
}
```

**Response**:
```json
{
  "regions": [
    {
      "cid": 0,
      "company_name": "CN",
      "deposit": { "tx_count": 5000, "amount_usd": 1234567.00 },
      "withdrawal": { "tx_count": 3000, "amount_usd": -456789.00 },
      "ib_withdrawal": { "tx_count": 200, "amount_usd": -12345.00 },
      "total_deposit_usd": 1234567.00,
      "total_withdrawal_usd": 469134.00,
      "net_deposit_usd": 765433.00
    },
    {
      "cid": 1,
      "company_name": "Global",
      ...
    }
  ],
  "query_time_ms": 154.32
}
```

### 3. Get Last Query Time

```
GET /api/v1/ib-data/last-run
```

**Response**:
```json
{
  "last_query_time": "2026-01-15T10:30:00Z"
}
```

## SQL Logic

Both queries use the pre-aggregated table `fxbackoffice.stats_transactions` (by date, type, loginSid) for better performance; currency is USD or CEN (CEN amounts are normalized with `/100`).

### IB Query (ib_data_service.py)

CTE: IB tree → aggregate from `stats_transactions` by date range and referral user IDs; wallet balance still from `mt4_users`.

```sql
tx_totals AS (
    SELECT ... (deposit_usd, withdrawal_usd, ib_withdrawal_usd)
    FROM (
        SELECT st.type,
               CASE WHEN UPPER(st.currency) = 'CEN' THEN st.amount / 100.0 ELSE st.amount END AS normalized_amount
        FROM fxbackoffice.stats_transactions st
        WHERE st.type IN ('deposit', 'withdrawal', 'ib withdrawal')
          AND st.date >= DATE(start_time) AND st.date <= DATE(end_time)
          AND st.userId IN (SELECT referralId FROM tx_referrals)
    ) st
),
wallet_total AS ( ... FROM fxbackoffice.mt4_users ... )
```

### Region Query (Company)

JOIN `stats_transactions` with `users` on `userId`, group by `cid` and `type`; use `SUM(countTransactions)` for tx_count.

```sql
SELECT u.cid, st.type,
       SUM(st.countTransactions) AS tx_count,
       SUM(CASE WHEN UPPER(st.currency) = 'CEN' THEN st.amount / 100.0 ELSE st.amount END) AS amount_usd
FROM fxbackoffice.stats_transactions st
INNER JOIN fxbackoffice.users u ON st.userId = u.id
WHERE st.type IN ('deposit', 'withdrawal', 'ib withdrawal')
  AND st.date >= DATE(%s) AND st.date < DATE(%s)
GROUP BY u.cid, st.type
```

## UI Design

### Color Scheme

| Field | Color | Meaning |
|-------|-------|---------|
| Deposit | Green (`text-emerald-600`) | Positive (money in) |
| Withdrawal | Red (`text-red-600`) | Negative (money out) |
| IB Withdrawal | Red (`text-red-600`) | Negative (money out) |
| Total Withdrawal | Red (`text-red-600`) | Negative (money out) |
| IB Wallet Balance | Red (`text-red-600`) | Liability |
| Net Deposit | Green/Red | Dynamic based on +/- |

### Visual Distinction

| Section | Background Color |
|---------|------------------|
| IB 出入金查询 | Blue (`bg-blue-50/50`) |
| Company 出入金查询 | Green (`bg-emerald-50/50`) |

## File Locations

| Type | Path |
|------|------|
| Frontend Page | `frontend/src/pages/IBData.tsx` |
| Backend Route | `backend/app/api/v1/routes/ib_data.py` |
| Backend Schema | `backend/app/schemas/ib_data.py` |
| Backend Service | `backend/app/services/ib_data_service.py` |

## Changelog

| Date | Change |
|------|--------|
| 2026-02-02 | Added Company 出入金查询 (region-based query) |
| 2026-02-02 | Renamed page title from "IB 出入金查询" to "出入金查询" |
| 2026-02-02 | Added summary row in tables with highlighted background |
| 2026-02-02 | Unified color scheme (green for deposits, red for withdrawals) |
| 2026-02-27 | IB & Company queries switched to `fxbackoffice.stats_transactions` for performance (CEN/USD only) |
