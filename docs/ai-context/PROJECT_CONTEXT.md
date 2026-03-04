# KCM IT System - Complete Project Context

> This document provides comprehensive context for AI assistants and new team members.
> For quick reference, see the Cursor Rule at `.cursor/rules/project-context.mdc`

## 1. Project Overview

**KCM IT System** is an internal financial trading risk control and analytics platform for KCM Trade, a forex broker. The system provides real-time monitoring, reporting, and analysis capabilities for trading operations.

### Business Domain
- **Industry**: Forex/CFD Trading
- **Users**: Risk management team, operations team, IB (Introducing Broker) managers
- **Data Sources**: MT4/MT5 trading servers, CRM (fxbackoffice)

### Key Capabilities
1. **Real-time Position Monitoring** - Track open positions across all MT servers
2. **Client P&L Analysis** - Analyze customer profitability with filtering and export
3. **IB Commission Reports** - Generate broker commission and transaction reports
4. **Equity Monitoring** - Monitor account balances and equity changes
5. **Trade Aggregation** - Summarize trading volumes and profits

---

## 2. Technical Architecture

### System Overview

```
┌─────────────────────────────────────────────────────────────────┐
│                         Frontend (React)                         │
│  - Pages: Position, ClientPnL, IBReport, Equity, TradeSummary   │
│  - UI: shadcn/ui + Tailwind CSS + AG-Grid                       │
│  - Port: 5173 (dev)                                              │
└─────────────────────────┬───────────────────────────────────────┘
                          │ HTTP/REST API
                          ▼
┌─────────────────────────────────────────────────────────────────┐
│                      Backend (FastAPI)                           │
│  - API: /api/v1/* endpoints                                      │
│  - Layers: routes → schemas → services                          │
│  - Port: 8001                                                    │
└───────┬─────────────────┬─────────────────┬─────────────────────┘
        │                 │                 │
        ▼                 ▼                 ▼
┌───────────────┐ ┌───────────────┐ ┌───────────────┐
│  ClickHouse   │ │    MySQL      │ │    Redis      │
│  (Analytics)  │ │  (MT4/MT5)    │ │   (Cache)     │
│  - Reports    │ │  - Trades     │ │  - TTL: varies│
│  - Stats      │ │  - Users      │ │               │
└───────────────┘ └───────────────┘ └───────────────┘
```

### Tech Stack Details

| Component | Technology | Notes |
|-----------|------------|-------|
| Frontend | React 18 + TypeScript + Vite | SPA with client-side routing |
| UI Library | shadcn/ui + Tailwind CSS | Dark/light theme support |
| Data Grid | AG-Grid v34 Community | Server-side pagination |
| Backend | Python FastAPI | Async, auto-docs at /docs |
| Primary DB | ClickHouse | Analytics, large aggregations |
| Trading DB | MySQL | MT4/MT5 direct connection |
| Cache | Redis | Query result caching |
| Data Processing | DuckDB, Parquet | Local data transformations |

---

## 3. Project Structure

```
New-IT-System/
├── frontend/                    # React frontend application
│   ├── src/
│   │   ├── main.tsx            # Entry point
│   │   ├── App.tsx             # Root component, routing
│   │   ├── index.css           # Global styles (Tailwind)
│   │   ├── pages/              # Page components (active)
│   │   │   ├── Home.tsx        # Dashboard home page (landing page)
│   │   │   ├── Position.tsx    # ~800 lines, position monitoring
│   │   │   # ├── ClientPnLMonitor.tsx  # [HIDDEN] 2026-01, use ClientPnLAnalysis
│   │   │   ├── ClientPnLAnalysis.tsx   # Client PnL (ClickHouse, recommended)
│   │   │   ├── IBReport.tsx
│   │   │   └── ...
│   │   │   # Removed/Hidden pages:
│   │   │   # - CustomerPnLMonitor.tsx (replaced by ClientPnLAnalysis, 2025-01)
│   │   │   # - CustomerPnLMonitorV2.tsx (replaced by ClientPnLAnalysis, 2025-01)
│   │   │   # - ClientTradingAnalytics.tsx (deprecated, 2025-01)
│   │   │   # - Downloads.tsx (deprecated, 2025-01)
│   │   │   # - EquityMonitor.tsx (deprecated, 2026-01)
│   │   │   # - Basis.tsx (hidden, 2026-02, 10.6.20.138:8050 service disabled)
│   │   ├── components/         # Reusable components
│   │   │   ├── ui/             # shadcn/ui components
│   │   │   ├── dashboard/      # Dashboard widgets
│   │   │   │   ├── PositionSummary.tsx    # Position summary widget
│   │   │   │   └── ReturnRateSummary.tsx  # Client return rate widget
│   │   │   ├── site-header.tsx # Page titles
│   │   │   └── app-sidebar.tsx # Navigation
│   │   ├── providers/
│   │   │   └── auth-provider.tsx
│   │   └── lib/utils.ts
│   ├── public/                  # Static assets, exported JSONs
│   └── package.json
│
├── backend/                     # FastAPI backend
│   ├── app/
│   │   ├── main.py             # FastAPI app factory
│   │   ├── api/v1/
│   │   │   ├── routers.py      # Route registration
│   │   │   └── routes/         # Endpoint handlers
│   │   │       ├── client_pnl.py
│   │   │       ├── open_positions.py
│   │   │       ├── ib_data.py
│   │   │       └── ...
│   │   ├── schemas/            # Pydantic models
│   │   ├── services/           # Business logic
│   │   │   ├── clickhouse_service.py
│   │   │   ├── client_pnl_service.py
│   │   │   └── ...
│   │   └── core/
│   │       ├── config.py       # Settings from .env
│   │       ├── logging_config.py
│   │       └── singleflight.py # Request coalescing utility
│   ├── main.py                 # ASGI entry (uvicorn main:app)
│   ├── requirements.txt
│   └── Dockerfile
│
├── database/                    # DB scripts
│   ├── clientpnl_full_load.py
│   └── clientpnl_incremental_refresh.py
│
├── docs/                        # Documentation
│   ├── README.md               # Documentation hub
│   ├── architecture/           # System design docs
│   ├── backend/                # Backend dev guides
│   ├── frontend/               # Frontend dev guides
│   ├── features/               # Feature specifications
│   ├── operations/             # Deployment guides
│   └── ai-context/             # This file
│
└── .cursor/rules/              # Cursor AI rules
    └── project-context.mdc     # Auto-loaded context
```

---

## 4. Core Business Modules

### 4.0 Dashboard (`Home.tsx`)
**Purpose**: Landing page aggregating key widgets from other pages for quick situational awareness.

**Key Features**:
- Grid layout: left 1/4 (CN payment placeholder, sticky) + right 3/4 (widgets stacked)
- **实时持仓** widget — cross-server XAUUSD/XAGUSD summary (auto-loads on mount)
- **客户收益率 (6h)** widget — AG Grid with CN/Global + AKCM filters (auto-loads on mount)
- **过去24h客户净盈亏** widget — table by country (framework; data TBD)
- **可疑客户** widget — table list (framework; data TBD)
- Data fetch timestamps displayed on each widget
- Lazy-loaded widgets with Skeleton fallback

**API** (reuses existing):
- `GET /api/v1/open-positions/symbol-summary` (no cache)
- `GET /api/v1/client-return-rate/query` (Redis cache 3h)

**Key Files**:
- `frontend/src/pages/Home.tsx`
- `frontend/src/components/dashboard/PositionSummary.tsx`
- `frontend/src/components/dashboard/ReturnRateSummary.tsx`
- `frontend/src/components/dashboard/Past24hClientPnlByCountry.tsx`
- `frontend/src/components/dashboard/SuspiciousClients.tsx`

**Docs**: [dashboard.md](../features/dashboard.md)

### 4.1 Position Monitor (`Position.tsx`)
**Purpose**: Real-time monitoring of open trading positions across all MT servers.

**Key Features**:
- Cross-server symbol summary (XAUUSD across mt4_live, mt4_live2, mt5)
- Fuzzy matching for symbol variants (.cent, .kcm, .kcmc)
- Parallel queries to multiple databases
- Drill-down to order details (planned)

**API**: `GET /api/v1/open-positions/summary`

### 4.2 Client PnL Analysis (`ClientPnLAnalysis.tsx`) ⭐ Recommended
**Purpose**: Analyze customer profitability with advanced filtering (ClickHouse-based).

**Key Features**:
- Date range filtering
- Client search and group filtering
- Column visibility toggle
- Export to CSV
- Server-side pagination with AG-Grid
- ClickHouse real-time analytics

**API**: `GET /api/v1/client-pnl-analysis/query`

### 4.2b Client PnL Monitor (`ClientPnLMonitor.tsx`) - Hidden (2026-01)
> **Status**: Page hidden from sidebar. Use ClientPnLAnalysis instead.

**Purpose**: Client-level PnL aggregation from PostgreSQL ETL pipeline.

**Key Features**:
- Client-level summary with account drill-down
- Zipcode mapping from CRM
- PostgreSQL-based data

**API**: `GET /api/v1/client-pnl/summary/paginated`, `GET /api/v1/client-pnl/{id}/accounts`

### 4.3 IB Report (`IBReport.tsx`)
**Purpose**: Generate reports for Introducing Broker commissions and transactions.

**Key Features**:
- Dynamic group selection (60+ groups)
- Dual-row display (selected range + monthly total)
- Single-pass SQL aggregation with `sumIf`
- Favorite groups stored in localStorage

**API**: `GET /api/v1/ib-report/summary`

### 4.4 Deposit/Withdrawal Query (`IBData.tsx`)
**Purpose**: Query deposit and withdrawal data by IB ID or by region (Company).

**Key Features**:
- IB Deposit/Withdrawal Query: Aggregate by specific IB IDs
- Company Deposit/Withdrawal Query: Aggregate by region (CN/Global based on cid)
- Quick date range selection (week, month, last month, custom)
- Summary row with highlighted totals
- Color-coded values (green for deposits, red for withdrawals)

**Data source**: MySQL `fxbackoffice.stats_transactions` (pre-aggregated by date/type/loginSid) + `ib_tree_with_self` for IB tree; wallet from `mt4_users`. Currency: USD and CEN (CEN normalized with /100).

**APIs**: 
- `POST /api/v1/ib-data/query` - Query by IB IDs
- `POST /api/v1/ib-data/region-query` - Query by region (Company)
- `GET /api/v1/ib-data/last-run` - Get last query timestamp

### 4.5 Client Return Rate (`ClientReturnRate.tsx`)
**Purpose**: Analyze client return rates based on trading profit, deposits, and equity.

**Key Features**:
- Two-phase MySQL query (mt4_trades + stats_transactions)
- Date range filtering (default: past 1 week, supports 6h/1w/2w/1m/custom)
- 6-hour precise filtering via CLOSE_TIME (MT4 server time UTC+2 winter / UTC+3 summer)
- Client ID search (pushed down to all subqueries for fast lookup)
- Adjusted return rates by deposit bucket (0-2K, 2K-5K, 5K-50K, 50K+)
- Negative net deposit return rate: `(equity - A) / A` where `A = MAX(deposits_90d, |net_deposit_hist|)`
- Last 90 days deposit column
- Demo account exclusion
- sessionStorage caching for page navigation restore
- Redis 3-hour server-side cache with frontend clear cache button
- Dedicated `MYSQL_HOST_PRIMARY` config (can override independently from global MYSQL_HOST)

**APIs**:
- `GET /api/v1/client-return-rate/query` - Query with optional `close_time_start` for precise filtering
- `DELETE /api/v1/client-return-rate/cache` - Clear all Redis cache for this page

**Tables**: `fxbackoffice.mt4_trades`, `fxbackoffice.mt4_users`, `fxbackoffice.stats_transactions`

### 4.6 Equity Monitor
**Purpose**: Track account balances and equity changes.

**API**: `GET /api/v1/equity/monitor`

---

## 5. Database Schema (ClickHouse)

### Key Tables

| Table | Database | Purpose |
|-------|----------|---------|
| `fxbackoffice_mt4_trades` | KCM_fxbackoffice | Trade history (closed orders) |
| `fxbackoffice_mt4_users` | KCM_fxbackoffice | User/account info with userId |
| `fxbackoffice_transactions` | KCM_fxbackoffice | Deposits, withdrawals, IB payments |
| `fxbackoffice_tags` | KCM_fxbackoffice | Group/tag definitions (categoryId=6) |
| `fxbackoffice_user_tags` | KCM_fxbackoffice | User-to-tag mappings |
| `stats_balances` | KCM_fxbackoffice | Daily balance/equity snapshots |
| `fxbackoffice_stats_ib_commissions_by_login_sid` | KCM_fxbackoffice | Pre-aggregated IB commissions |

### ClickHouse connections (clickhouse_service.py)
- **Default** (`get_client()`): `CLICKHOUSE_HOST` + `CLICKHOUSE_DB` (default `Fxbo_Trades`).
- **Prod** (`get_client(use_prod=True)`): `CLICKHOUSE_prod_*` + database `KCM_fxbackoffice`. Used by IB Report (groups, search) and Client PnL Analysis (query). Deploy must set prod credentials to the CDC cluster.

### MySQL connections (client_return_service.py)
- **Client Return Rate** uses `MYSQL_HOST_PRIMARY` (falls back to `MYSQL_HOST` if not set) + `MYSQL_DATABASE_FXBACKOFFICE=fxbackoffice` via pymysql.
  - Two-phase query: Phase 1 gets active client_ids from `mt4_trades`, Phase 2 uses `stats_transactions` for deposit data.
  - Optional precise filtering via `CLOSE_TIME` (MT4 server time UTC+2 winter / UTC+3 summer, offset constant `MT4_TZ_OFFSET_HOURS`).
  - Demo accounts excluded via `GROUP NOT LIKE '%demo%'`.
  - Redis cache (3h TTL) with `DELETE /cache` endpoint + sessionStorage for frontend state persistence.

### Important Conventions

1. **Cent Account Handling**: Currency = 'CEN' means amounts must be divided by 100
2. **Compound ID Format**: `loginSid` uses format `SID-LOGIN` (e.g., "1-8522845")
3. **Trade Commands**: CMD 0 = Buy, CMD 1 = Sell
4. **Transaction Types**: 'deposit', 'withdrawal', 'ib withdrawal'

---

## 6. API Conventions

### Base URL
- Development: `http://localhost:8001/api/v1/`
- API Docs: `http://localhost:8001/docs` (Swagger UI)

### Common Parameters

| Parameter | Type | Description |
|-----------|------|-------------|
| `page` | int | Page number (1-indexed) |
| `page_size` | int | Items per page (default 50) |
| `sort_by` | string | Column to sort by |
| `sort_order` | 'asc' \| 'desc' | Sort direction |
| `start_date` | string | Filter start (YYYY-MM-DD) |
| `end_date` | string | Filter end (YYYY-MM-DD) |

### Response Structure

```json
{
  "data": [...],
  "total": 1234,
  "page": 1,
  "page_size": 50,
  "total_pages": 25,
  "statistics": {
    "from_cache": true,
    "query_time_ms": 45
  }
}
```

---

## 7. Coding Conventions

### Frontend
- **Components**: Functional components with hooks
- **Styling**: Tailwind CSS classes, shadcn/ui components
- **State**: React useState/useEffect, localStorage for persistence
- **Tables**: AG-Grid with server-side pagination
- **Comments**: English only

### Backend
- **Routes** (`routes/`): Handle HTTP request/response only
- **Schemas** (`schemas/`): Define request/response shapes (Pydantic)
- **Services** (`services/`): Implement business logic, database queries
- **Config** (`core/`): Centralized settings from .env

### Request Deduplication

**Frontend - AbortController in useEffect** (standard React 18 pattern):
All `useEffect` hooks that fetch data MUST use `AbortController` for cleanup.
This prevents duplicate requests caused by React StrictMode double-mounting.

```tsx
useEffect(() => {
  const controller = new AbortController();
  fetchData(controller.signal);
  return () => controller.abort();
}, []);
```

Fetch functions should accept an optional `signal?: AbortSignal` param and pass it to `fetch()`.
Catch blocks should ignore `AbortError`:
```tsx
catch (error) {
  if (error instanceof DOMException && error.name === "AbortError") return;
  // handle real errors
}
```

**Backend - SingleFlight** (`backend/app/core/singleflight.py`):
Coalesces concurrent identical ClickHouse queries so only one thread executes.
Used in `clickhouse_service.py` for `get_pnl_analysis` and `get_ib_groups`.
When 4 identical requests arrive before Redis cache is populated, only 1 hits ClickHouse.

### Adding New Features

**New API Endpoint**:
1. Create schema in `backend/app/schemas/`
2. Implement service in `backend/app/services/`
3. Add route in `backend/app/api/v1/routes/`
4. Register in `backend/app/api/v1/routers.py`

**New Frontend Page**:
1. Create component in `frontend/src/pages/`
2. Add route in `App.tsx`
3. Add to sidebar in `app-sidebar.tsx`
4. Add title in `site-header.tsx` titleMap

---

## 8. Common Issues & Solutions

| Issue | Solution |
|-------|----------|
| AG-Grid blank | Set container height (e.g., `h-[600px]`) |
| AG-Grid error #272 | Register modules in main.tsx |
| CORS errors | Set CORS_ORIGINS in backend .env |
| ClickHouse connection | Check VPN, verify credentials |
| CEN account wrong amounts | Ensure dividing by 100 |
| Cache not updating | Wait for TTL expiry (PnL: 30min, IB: 10min, Return Rate: 3h) or use clear cache button |

---

## 9. Environment Setup

### Backend (.env)
```env
DB_HOST=your_clickhouse_host
DB_USER=your_user
DB_PASSWORD=your_password
DB_NAME=KCM_fxbackoffice
DB_PORT=9000

REDIS_HOST=localhost
REDIS_PORT=6379

CORS_ORIGINS=http://localhost:5173
```

### Frontend (.env.development)
```env
VITE_API_BASE_URL=http://localhost:8001
VITE_DISABLE_AUTH=true  # Skip login for dev
```

---

## 10. Contact & Resources

- **Backend API Docs**: http://localhost:8001/docs
- **Documentation Hub**: [docs/README.md](../README.md)
- **Cursor AI Rules**: `.cursor/rules/project-context.mdc`
