# Dashboard (Home Page)

> Landing page of the KCM Analytics System — aggregates key widgets from other pages.

## 1. Overview

The Dashboard (`/` or `/home`) is the first page users see after login. It displays a summary of critical business data by embedding compact widgets from existing feature pages, avoiding the need to navigate between multiple pages for quick situational awareness.

### Route & Entry Points

| Entry Point | Behavior |
|---|---|
| URL `/` | Renders Dashboard (index route) |
| URL `/home` | Alias route, same page |
| Sidebar "Dashboard" item | Direct link to `/` |
| Sidebar logo click | Navigates to `/` |

---

## 2. Layout

```
┌──────────────────────────────────────────────────────────┐
│  grid-cols-4 (lg), grid-cols-1 (mobile)                  │
│                                                          │
│  ┌──────────┐  ┌────────────────────────────────────┐    │
│  │ Left 1/4 │  │ Right 3/4                          │    │
│  │          │  │                                    │    │
│  │ CN渠道   │  │ ┌────────────────────────────────┐ │    │
│  │ 支付成功率│  │ │ 实时持仓 (PositionSummary)    │ │    │
│  │          │  │ │ - 跨服务器品种汇总表           │ │    │
│  │ (sticky) │  │ │ - Auto-load on mount           │ │    │
│  │          │  │ └────────────────────────────────┘ │    │
│  │ Coming   │  │                                    │    │
│  │ Soon     │  │ ┌────────────────────────────────┐ │    │
│  │          │  │ │ 客户收益率 (ReturnRateSummary) │ │    │
│  │          │  │ │ - AG Grid (6h/24h toggle)      │ │    │
│  │          │  │ │ - CN/Global + AKCM filters     │ │    │
│  │          │  │ └────────────────────────────────┘ │    │
│  │          │  │ ┌──────────────┐ ┌──────────────┐   │    │
│  │          │  │ │近两日客户   │ │ 可疑客户     │   │    │
│  │          │  │ │平仓净盈亏   │ │ (table)      │   │    │
│  │          │  │ └──────────────┘ └──────────────┘   │    │
│  │          │  │                                      │    │
│  │          │  │ ┌────────────────────────────────┐   │    │
│  │          │  │ │近两日客户平仓净盈亏 (Group)   │   │    │
│  │          │  │ │ 行: 按账户组(GROUP)分组      │   │    │
│  │          │  │ └────────────────────────────────┘   │    │
│  └──────────┘  └────────────────────────────────────┘    │
└──────────────────────────────────────────────────────────┘
```

- **Left column**: `self-start lg:sticky lg:top-4` — stays at natural height, sticky on scroll
- **Right column**: `flex flex-col gap-4` — widgets stack vertically; PnL by Country + Suspicious Clients side-by-side (`lg:grid-cols-2`), PnL by Group full width below; stacked on mobile
- **Responsive**: Single column on mobile, 1:3 split on `lg+`

---

## 3. Widgets

### 3.1 实时持仓 (PositionSummary)

**File**: `frontend/src/components/dashboard/PositionSummary.tsx`

**What it shows**:
- Cross-server symbol summary table (MT4, MT5, MT4Live2)
- XAUUSD/XAGUSD selection with fuzzy matching option
- Data fetch timestamp (精确到秒)

**API**: `GET /api/v1/open-positions/symbol-summary?symbol=XAUUSD`

**Data source**: MySQL `fxbackoffice` — direct query, **no Redis cache**

**Behavior**:
- Auto-fetches XAUUSD summary on page mount
- User can switch symbol and click "查询" for manual refresh
- "查看全部" links to `/position` for full details

### 3.2 客户收益率 (ReturnRateSummary)

**File**: `frontend/src/components/dashboard/ReturnRateSummary.tsx`

**What it shows**:
- AG Grid table of clients with closed trades in the selected time window
- **6h / 24h toggle** in CardHeader (default 6h, switching auto-refetches)
- Columns: 客户ID, 净值(Excl. IB Wallet), 历史净入金, 历史总利润, 过去N小时内利润, 收益率%, 负净入金收益率%
- 收益率% and 负净入金收益率% columns have ℹ️ info tooltip showing formula
- CN/Global and AKCM toggle filters (frontend filtering)
- Data fetch timestamp (精确到秒)

**API**: `GET /api/v1/client-return-rate/query?...&close_time_start=...`

**Data source**: MySQL `fxbackoffice` → **Redis cache (TTL 3h)**. Excludes demo and employee accounts (see `docs/features/client-return-rate.md` §6).

**Behavior**:
- Auto-fetches on page mount (default 6h window); switching to 24h triggers refetch
- User can click "刷新" for manual refresh
- AG Grid features: sortable columns, column filters, pagination (100/page)
- "查看全部" links to `/client-return-rate` for full date range queries

### 3.3 近两日客户平仓净盈亏 (Past24hClientPnlByCountry)

**File**: `frontend/src/components/dashboard/Past24hClientPnlByCountry.tsx`

**What it shows**:
- Card title "近两日客户平仓净盈亏" with subtitle "时间口径：MT Server 时间" (CardDescription, smaller text)
- Table: **今日Profit** / **今日IB佣金** / **昨日Profit** / **昨日IB佣金** four columns (no 合计); time scope: MT Server natural day
- Profit = client PnL (totalPlClosed from `stats_trading`); IB佣金 = IB rebate cost (commission from `stats_ib_commissions`, all IB levels summed, grouped by client's sales team)
- Rows grouped by country, default sort by **昨日** PnL ascending (min to max); click a country row to expand and see per–sales-team rows (same columns, same sort); zebra striping; value columns left-aligned
- IB commission columns in muted gray color; overall font size `text-xs` for compact layout
- Fixed-height card, table scrolls when needed

**API**: `GET /api/v1/dashboard/pnl-by-sales-team`

**Data source**: MySQL `fxbackoffice` — `stats_trading` (PnL) + `stats_ib_commissions` (IB rebate) + sales team tags (categoryId=6); country from backend mapping (see `docs/features/dashboard-pnl24h-by-country-sql.md`). Excludes demo and employee accounts (see §9.1 of that doc).

### 3.4 近两日客户平仓净盈亏 - Group (Past24hClientPnlByGroup)

**File**: `frontend/src/components/dashboard/Past24hClientPnlByGroup.tsx`

**What it shows**:
- Card title "近两日客户平仓净盈亏 (Group)" with subtitle "时间口径：MT Server 时间 · 按账户组分组"
- Table columns: **今日Profit** / **今日IB佣金** / **昨日Profit** / **昨日IB佣金** (same 4 columns as PnL by Country)
- PnL from `stats_trading`; IB佣金 from `stats_ib_commissions_by_login_sid` (account-level, JOINs mt4_users for GROUP)
- Rows grouped by mt4_users.GROUP (expandable to per-sales-team breakdown)
- All 4 data columns support **click-to-sort**: click once ascending, click again descending; default sort by 昨日Profit ascending
- IB commission columns in muted gray; overall font size `text-xs`

**API**: `GET /api/v1/dashboard/pnl-by-group`

**Data source**: MySQL `fxbackoffice` — same `stats_trading` data as PnL by Country, but grouped by `mu.GROUP` additionally. No IB commission query. Excludes demo and employee accounts.

**Docs**: [dashboard-pnl-by-group.md](dashboard-pnl-by-group.md)

### 3.5 可疑客户 (SuspiciousClients)

**File**: `frontend/src/components/dashboard/SuspiciousClients.tsx`

**What it shows**:
- Table: suspicious clients list (framework only; data/API TBD)
- Fixed-height card, table scrolls when needed

**API**: TBD

### 3.6 CN渠道支付成功率 (CnPaymentSuccessRate)

**File**: `frontend/src/components/dashboard/CnPaymentSuccessRate.tsx`

**What it shows**:
- Per-PSP channel deposit stats (CN channels only, `psps.cid = 0`)
- Time window toggle: 3h / 6h / 24h (default 3h)
- Each channel card: total/approved/declined/fresh counts + approved total amount + mini progress bar
- Collapsible "Top 3 approved" section per channel (default collapsed): order ID, amount, user ID (with CRM link)
- Overall summary bar: total orders + overall success rate (color coded)

**API**: `GET /api/v1/dashboard/cn-payment-success-rate?hours=3`

**Data source**: MySQL `fxbackoffice` — `transactions` (type='deposit', createdAt within window) INNER JOIN `psps` (cid=0 for CN channels). Groups by `psps.displayName`. Top 3 uses `ROW_NUMBER()` window function.

**Behavior**:
- Auto-fetches on page mount (default 3h window)
- ToggleGroup to switch time window (3h/6h/24h), auto-refetches on change
- Refresh button for manual reload
- Data fetch timestamp displayed
- Top 3 approved orders collapsible per channel (click to expand/collapse)

**Docs**: [cn-payment-success-rate.md](cn-payment-success-rate.md)

---

## 4. Auto-load & Data Freshness

Both widgets auto-fetch data when the Dashboard mounts (including browser refresh). Each widget displays a timestamp showing when data was last retrieved.

| Widget | Auto-load | Cache | Typical Latency |
|---|---|---|---|
| PositionSummary | On mount (XAUUSD) | None | 1-3s |
| ReturnRateSummary | On mount (6h default, 24h optional) | Redis 3h TTL | <100ms (cached) / 5-15s (fresh) |
| Past24hClientPnlByCountry | On mount | None | 1–3s |
| Past24hClientPnlByGroup | On mount | None | 1–3s |
| CnPaymentSuccessRate | On mount (3h window) | None | 1–3s |
| SuspiciousClients | — | — | Framework only |

---

## 5. Concurrency & Scaling Notes

### Current State (≤10 concurrent users)

Works fine as-is. MySQL handles the load without issues.

### When to Add Optimization

| Symptom | Solution | Effort |
|---|---|---|
| Position queries slow under load | Add Redis cache (TTL 30-60s) to `open_positions_service.py` | Low |
| Cache stampede on return rate (many users hit expired cache simultaneously) | Add singleflight/lock in `client_return_service.py` (project already has `core/singleflight.py`) | Low |
| MySQL connection errors | Replace `pymysql` short connections with connection pool (`SQLAlchemy` or `DBUtils.PooledDB`) | Medium |
| >50 concurrent users | Add `GET /api/v1/dashboard/summary` endpoint that batches all dashboard queries server-side, cached with short TTL | Medium |

### Connection Pool Migration Guide

Current pattern (short connection per request):
```python
conn = pymysql.connect(host=..., user=..., password=...)
with conn:
    with conn.cursor() as cur:
        cur.execute(sql)
# connection closed after `with` block
```

Recommended pool pattern:
```python
# In core/database.py (new file)
from dbutils.pooled_db import PooledDB
import pymysql

pool = PooledDB(
    creator=pymysql,
    maxconnections=20,      # max concurrent connections
    mincached=2,            # idle connections to keep
    maxcached=5,            # max idle connections
    blocking=True,          # block when pool exhausted
    host=settings.DB_HOST,
    user=settings.DB_USER,
    password=settings.DB_PASSWORD,
    database=settings.FXBACK_DB_NAME,
    port=int(settings.DB_PORT),
    charset=settings.DB_CHARSET,
    cursorclass=pymysql.cursors.DictCursor,
)

# Usage in services:
conn = pool.connection()
try:
    with conn.cursor() as cur:
        cur.execute(sql)
finally:
    conn.close()  # returns to pool, not actually closed
```

### Singleflight Pattern

The project already has `backend/app/core/singleflight.py`. To prevent cache stampede:

```python
from app.core.singleflight import singleflight

@singleflight(key_fn=lambda **kw: f"client_return:{kw['month_start']}_{kw['close_time_start']}")
def get_client_return_rate_data(**kwargs):
    # existing logic...
```

This ensures only one request queries MySQL when multiple users trigger the same query simultaneously.

---

## 6. File Inventory

| File | Purpose |
|---|---|
| `frontend/src/pages/Home.tsx` | Dashboard page — grid layout with lazy-loaded widgets |
| `frontend/src/components/dashboard/PositionSummary.tsx` | Position summary widget |
| `frontend/src/components/dashboard/ReturnRateSummary.tsx` | Client return rate widget (AG Grid) |
| `frontend/src/components/dashboard/Past24hClientPnlByCountry.tsx` | 近两日客户平仓净盈亏（按国家/团队，可展开） |
| `frontend/src/components/dashboard/Past24hClientPnlByGroup.tsx` | 近两日客户平仓净盈亏（按账户组(GROUP)分组） |
| `frontend/src/components/dashboard/CnPaymentSuccessRate.tsx` | CN渠道支付成功率 widget (per-PSP channel cards) |
| `frontend/src/components/dashboard/SuspiciousClients.tsx` | Suspicious clients list (table framework) |
| `backend/app/api/v1/routes/open_positions.py` | API: `/api/v1/open-positions/symbol-summary` |
| `backend/app/api/v1/routes/client_return_rate.py` | API: `/api/v1/client-return-rate/query` |
| `backend/app/api/v1/routes/dashboard.py` | API: `/api/v1/dashboard/pnl-by-sales-team`, `/pnl-by-group`, `/cn-payment-success-rate` |
| `backend/app/services/open_positions_service.py` | Position query logic (MySQL, no cache) |
| `backend/app/services/client_return_service.py` | Return rate query logic (MySQL + Redis cache) |
| `backend/app/services/dashboard_pnl_service.py` | Dashboard PnL by sales team (MySQL stats_trading + country mapping) |
| `backend/app/services/dashboard_pnl_group_service.py` | Dashboard PnL by account group (MySQL stats_trading + GROUP grouping) |
| `backend/app/services/cn_payment_service.py` | CN payment success rate (transactions + psps, cid=0) |
| `backend/app/schemas/cn_payment.py` | Pydantic schemas for CN payment API |

---

## 7. Known Pitfalls

### AG Grid Zebra Striping

The project's CSS variables (e.g., `--primary`) use **oklch** format. AG Grid's `--ag-odd-row-background-color` expects a valid CSS color value. Do NOT write:

```css
/* WRONG — oklch nested inside hsl() is invalid */
--ag-odd-row-background-color: hsl(var(--primary) / 0.04);
```

Use direct `rgba()` instead:

```css
/* CORRECT */
--ag-odd-row-background-color: isDarkMode ? "rgba(255,255,255,0.04)" : "rgba(0,0,0,0.03)";
```

### ToggleGroup Pill Shape

The base `ToggleGroupItem` component (`toggle-group.tsx`) no longer sets `rounded-none first:rounded-l-md last:rounded-r-md`. This was removed to prevent overriding custom `rounded-*` classes. Always set the desired border-radius on both the `ToggleGroup` (container) and each `ToggleGroupItem`.

---

## 8. Future Enhancements

- [x] CN渠道支付成功率 — per-PSP deposit success rate (past 3h, psps.cid=0)
- [ ] Connection pool for MySQL (`DBUtils.PooledDB`)
- [ ] Short-TTL Redis cache for position summary (30-60s)
- [ ] Singleflight for return rate queries
- [ ] Dedicated `GET /api/v1/dashboard/summary` batch endpoint (when >50 users)
- [ ] Auto-refresh with configurable interval (e.g., every 5 minutes)
- [ ] Fix `hsl(var(--primary))` in other AG Grid pages (ClientPnLAnalysis, IBReport, ClientPnLMonitor)
