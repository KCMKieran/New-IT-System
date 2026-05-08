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
│  - UI: shadcn/ui + Tailwind CSS 4 + AG-Grid                     │
│  - Dev: Vite dev server on :5173                                 │
│  - Prod: Nginx on :3000 (pre-built static files)                 │
└─────────────────────────┬───────────────────────────────────────┘
                          │ HTTP/REST API (/api/*)
                          ▼
┌─────────────────────────────────────────────────────────────────┐
│                      Backend (FastAPI)                           │
│  - API: /api/v1/* endpoints                                      │
│  - Layers: routes → schemas → services                          │
│  - Dev: :8001 (--reload)  |  Prod: internal (via Nginx proxy)   │
└───────┬─────────────────┬─────────────────┬─────────────────────┘
        │                 │                 │
        ▼                 ▼                 ▼
┌───────────────┐ ┌───────────────┐ ┌───────────────┐
│  ClickHouse   │ │    MySQL      │ │    Redis      │
│  (Analytics)  │ │  (MT4/MT5)    │ │   (Cache)     │
│  - Reports    │ │  - Trades     │ │  - TTL: varies│
│  - Stats      │ │  - Users      │ │               │
└───────────────┘ └───────────────┘ └───────────────┘

Deployment: Docker Compose on Ubuntu (10.6.20.138)
External access: Cloudflare Tunnel → analysis.kohleservices.com → :3000
API security: CF Access Bypass /api/* + X-API-Key header (Nginx + FastAPI middleware)
```

### Tech Stack Details

| Component | Technology | Notes |
|-----------|------------|-------|
| Frontend | React 19 + TypeScript + Vite 7 | SPA with client-side routing, ErrorBoundary + lazy retry |
| UI Library | shadcn/ui + Tailwind CSS 4 | Dark/light theme support |
| Data Grid | AG-Grid v34 Community | Server-side pagination |
| Backend | Python 3.11 + FastAPI | Async, auto-docs at /docs |
| Primary DB | ClickHouse | Analytics, large aggregations |
| Trading DB | MySQL | MT4/MT5 direct connection |
| Reporting DB | PostgreSQL | ETL pipeline data |
| Cache | Redis | Query result caching |
| Data Processing | DuckDB, Parquet | Local data transformations |
| Deployment | Docker Compose + Nginx | Dev & Prod run simultaneously |
| External Access | Cloudflare Tunnel + Zero Trust | CF Access Bypass on /api/*, API Key defense-in-depth |

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
│   │   │   ├── IBFinancialMonitor.tsx  # IB financial monitoring (3 tabs)
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
│   │   │   │   ├── ReturnRateSummary.tsx  # Client return rate widget
│   │   │   │   └── Past24hClientPnlByGroup.tsx  # PnL by account group widget
│   │   │   ├── LazyErrorBoundary.tsx # Chunk load error recovery + retry
│   │   │   ├── site-header.tsx # Page titles
│   │   │   └── app-sidebar.tsx # Navigation
│   │   ├── providers/
│   │   │   └── auth-provider.tsx
│   │   └── lib/
│   │       ├── utils.ts
│   │       └── fetch.ts           # apiFetch() — auto-injects X-API-Key header
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
│   │   │       ├── ib_financial.py  # IB Financial Monitor routes
│   │   │       └── ...
│   │   ├── schemas/            # Pydantic models
│   │   ├── services/           # Business logic
│   │   │   ├── clickhouse_service.py
│   │   │   ├── client_pnl_service.py
│   │   │   ├── client_return_export_service.py # Async CSV export worker
│   │   │   ├── ib_financial_service.py  # IB Financial Monitor
│   │   │   ├── email_service.py  # SMTP email sending
│   │   │   └── ...
│   │   └── core/
│   │       ├── config.py       # Settings from .env
│   │       ├── database.py     # SQLite for IB Financial config
│   │       ├── risk_monitor_db.py # SQLite for risk monitor config/history
│   │       ├── client_return_export_db.py # SQLite for client return export tasks
│   │       ├── scheduler.py    # APScheduler for daily reports
│   │       ├── burst_open_scheduler.py # APScheduler for burst open scanning
│   │       ├── logging_config.py
│   │       ├── api_key_middleware.py # X-API-Key validation for /api/*
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
│   ├── deployment/             # ⭐ Dev/Prod workflow & Cloudflare Tunnel
│   ├── architecture/           # System design docs
│   ├── backend/                # Backend dev guides
│   ├── frontend/               # Frontend dev guides
│   ├── features/               # Feature specifications
│   ├── operations/             # Operations & maintenance
│   └── ai-context/             # This file
│
├── docker-compose.prod.yml      # Production orchestration
├── deploy.sh                    # One-click production deployment
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
- **客户收益率 (6h/24h)** widget — AG Grid with time toggle, CN/Global + AKCM filters (auto-loads on mount)
- **近两日客户平仓净盈亏** widget — table by country/sales team (today + yesterday, MT Server; expandable rows)
- **近两日客户平仓净盈亏 (Group)** widget — PnL + IB commission grouped by mt4_users.GROUP with expandable sales_team detail; click-to-sort on all 4 data columns
- **可疑客户** widget — table list (framework; data TBD)
- Data fetch timestamps displayed on each widget
- Lazy-loaded widgets with Skeleton fallback

**API** (reuses existing + dashboard):
- `GET /api/v1/open-positions/symbol-summary` (no cache)
- `GET /api/v1/client-return-rate/query` (Redis cache 3h)
- `GET /api/v1/dashboard/pnl-by-sales-team` (no cache; today/yesterday PnL by sales team + country)
- `GET /api/v1/dashboard/pnl-by-group` (no cache; PnL by account group + sales team)

**Key Files**:
- `frontend/src/pages/Home.tsx`
- `frontend/src/components/dashboard/PositionSummary.tsx`
- `frontend/src/components/dashboard/ReturnRateSummary.tsx`
- `frontend/src/components/dashboard/Past24hClientPnlByCountry.tsx`
- `frontend/src/components/dashboard/Past24hClientPnlByGroup.tsx`
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
- Date range filtering (default: past 1 week, supports 1h/6h/24h/today/this_week/1w/this_month/1m/custom)
- Sub-day modes (1h/6h/24h) use precise CLOSE_TIME filtering on `mt4_trades` (MT4 server time UTC+2 winter / UTC+3 summer); day-level modes use the fast `stats_trading` aggregated path
- Client ID search (pushed down to all subqueries for fast lookup)
- Adjusted return rates by deposit bucket (0-2K, 2K-5K, 5K-50K, 50K+)
- Negative net deposit return rate: `(equity - A) / A` where `A = MAX(deposits_90d, |net_deposit_hist|)`
- **ROACE long-term return**: `profit_hist / avg_daily_equity × 100`, using full-history daily equity snapshots from `stats_balances`. Opt-in via `include_avg_equity=true` (full page only, Dashboard skips for performance)
- Last 90 days deposit column
- Demo account exclusion
- sessionStorage caching for page navigation restore
- Redis 3-hour server-side cache with frontend clear cache button
- Async CSV export flow (create task -> poll status -> download)
- Export task persistence in SQLite with file expiration cleanup
- Dedicated `MYSQL_HOST_PRIMARY` config (can override independently from global MYSQL_HOST)

**APIs**:
- `GET /api/v1/client-return-rate/query` - Query with optional `close_time_start` for precise filtering, optional `include_avg_equity` for ROACE calculation
- `DELETE /api/v1/client-return-rate/cache` - Clear all Redis cache for this page
- `POST /api/v1/client-return-rate/export/tasks` - Create async CSV export task
- `GET /api/v1/client-return-rate/export/tasks/{task_id}` - Query export task status and progress
- `GET /api/v1/client-return-rate/export/tasks/{task_id}/download` - Download generated CSV file

**Tables**: `fxbackoffice.mt4_trades`, `fxbackoffice.mt4_users`, `fxbackoffice.stats_transactions`, `fxbackoffice.stats_balances` (ROACE)

### 4.6 Equity Monitor
**Purpose**: Track account balances and equity changes.

**API**: `GET /api/v1/equity/monitor`

### 4.7 IB Financial Monitor (`IBFinancialMonitor.tsx`)
**Purpose**: Monitor IB financial status — deposits, withdrawals, equity, and differences. Replaces the standalone D08 cron script.

**Key Features**:
- Configurable watchlist (add/remove via UI, stored in SQLite; supports **batch add** for multiple IDs at once)
- Supports both **IB IDs** (expands downstream clients via `ib_tree_with_self`) and **plain client IDs** (queries own data only, auto-detected by `_classify_ids()`)
- Real-time financial data query from MySQL fxbackoffice
- Manual email report sending + scheduled daily auto-send (APScheduler), with distinct footer text per source
- Report config management (TO/CC/schedule time/enable toggle)
- Email verification required for all config changes (6-digit code, Redis TTL 5min, admin whitelist)
- Full audit log of all operations

**APIs**:
- `GET /api/v1/ib-financial/watchlist` - List active IBs
- `GET /api/v1/ib-financial/query` - Query financial data by date
- `POST /api/v1/ib-financial/send-report` - Manually send report email
- `GET /api/v1/ib-financial/config` - Get report config
- `POST /api/v1/ib-financial/request-code` - Request verification code
- `POST /api/v1/ib-financial/verify-action` - Verify code and execute action
- `GET /api/v1/ib-financial/audit-log` - View operation history
- `GET /api/v1/ib-financial/whitelist` - List admin emails

**Data sources**: MySQL `fxbackoffice` (stats_transactions, stats_balances, ib_tree_with_self), SQLite `ib_financial.db` (config)

**Key Files**:
- `frontend/src/pages/IBFinancialMonitor.tsx`
- `backend/app/api/v1/routes/ib_financial.py`
- `backend/app/services/ib_financial_service.py`
- `backend/app/services/email_service.py`
- `backend/app/core/database.py` (SQLite)
- `backend/app/core/scheduler.py` (APScheduler)

### 4.8 Trade Real-time Monitor (`RiskMonitor.tsx`)
**Purpose**: Scan all MT servers for clients exhibiting suspicious batch ordering patterns. B-Book perspective: flags clients whose high-exposure rapid trading poses risk to company P&L.

**Key Features**:
- Tab-based UI: 批量下单 (default) / 快开快平 / 快速获利（以 `RiskMonitor.tsx` 为准）
- Burst Open Detection (批量下单): sliding window algorithm detects N orders (each ≥ M lots) within T seconds on the same symbol
- Quick Open-Close (快开快平): short hold + min count + min merged P&L within SQL lookback; `rule_id` 51-60 in `alert_events`
- Quick Profit (快速获利): aggregate window P&L (realized + optional floating) ≥ threshold; `rule_id` 61-70; per-rule `lookback_min` (10-60) **decoupled from `scan_interval_min`**; live floating P&L refreshed on-demand via dedicated `/quick-profit/floating-refresh` endpoint, triggered by the toolbar "刷新浮动盈亏" button (no scheduler trigger, no auto-poll)
- Backend-driven scanning via APScheduler (single background task, frontend reads cached result)
- Multi-rule support: up to 10 configurable rules with independent parameters
- Config persistence in SQLite (`backend/data/risk_monitor.db`)
- Cross-server scanning (MT4 Live + MT4 Live2 + MT5)
- 30s boundary buffer + deduplication to prevent missed/duplicate detections
- All matched accounts labeled "可疑用户" (no ALERT/WATCH severity levels)
- CEN (cent) account handling: equity/balance from MT are cents — backend looks up `fxbackoffice.mt4_users` by `loginsid` ({sid}-{login}), divides by 100 for CEN, and tags `currency` on every alert so the frontend "币种" column shows USD / CEN correctly (equity/balance columns labeled "(USD)")
- Zipcode enrichment (same `fxbackoffice.mt4_users` query as currency): `alert_events.zipcode` stored; frontend has a toolbar LIKE filter to cluster same-address accounts
- Timezone convention: backend stores `scanned_at` / `first_open` / `last_open` / `orders[].open_time` all in UTC (`...Z`). Broker MT4/MT5 servers run in UTC+3 (Indian/Antananarivo, no DST), so SQL queries wrap `OPEN_TIME` / `Time` with `CONVERT_TZ(..., '+03:00', '+00:00')` + `DATE_FORMAT(..., '%Y-%m-%dT%TZ')` to normalize before persisting. Frontend renders in `Asia/Hong_Kong` (HKT).

**Frontend view**: Time-range alert view (default last 4h, presets: 1h/4h/1d/7d/30d/custom). 30 days retention. Config Drawer (multi-rule) + CSV export via AG-Grid. No history drawer (time-range picker replaces it). Mobile: each tab’s header stacks description above actions; CSV / config / scan / floating-refresh buttons use shared `RISK_MONITOR_HEADER_ROW` + `RISK_MONITOR_HEADER_ACTIONS` (`flex-wrap` below `sm`) so the row does not overflow when `Button` applies `shrink-0`.

**APIs**:
- `GET /api/v1/risk-monitor/burst-open` — Latest cached scan result (scan metadata + immediate-scan refresh)
- `GET /api/v1/risk-monitor/burst-open/alerts?since=&until=&zipcode=...` — Time-range alert events view (primary data source); `zipcode` is a LIKE '%x%' substring filter
- `GET /api/v1/risk-monitor/burst-open/alerts/stats?since=&until=&zipcode=...` — Time-range aggregates + `by_rule` per burst rule card; same filters as alerts except **no** `rule_id` (cards stay overview when table is filtered by rule)
- `GET /api/v1/risk-monitor/burst-open/config` — Current config from SQLite
- `POST /api/v1/risk-monitor/burst-open/config` — Update config + reschedule scanner
- `POST /api/v1/risk-monitor/burst-open/scan-now` — Trigger immediate scan
- `GET /api/v1/risk-monitor/quick-open-close/alerts?since=&until=&...` — 快开快平事件（`rule_id` ≥ 51）
- `GET /api/v1/risk-monitor/quick-open-close/alerts/stats?...` — 快开快平聚合（含 `by_rule`）
- `GET` / `POST /api/v1/risk-monitor/quick-open-close/config` — 快开快平配置
- `GET /api/v1/risk-monitor/quick-open-close/alerts/export` — 快开快平 CSV
- `GET /api/v1/risk-monitor/quick-profit/alerts?since=&until=&...` — 快速获利事件（`rule_id` 61-70）
- `GET /api/v1/risk-monitor/quick-profit/alerts/stats?...` — 快速获利聚合（含 `by_rule`）
- `GET` / `POST /api/v1/risk-monitor/quick-profit/config` — 快速获利配置（`lookback_min` 10-60、`min_profit_usd` ≥100、`include_floating`）
- `GET /api/v1/risk-monitor/quick-profit/alerts/export` — 快速获利 CSV
- `GET /api/v1/risk-monitor/quick-profit/floating-refresh?ids=...` — 浮动 P&L 轻量按需刷新（用户点工具栏「刷新浮动盈亏」按钮触发，仅查 `position_status != 'closed'` 行；不写库）

**Data sources**: MySQL Slave (`mt4_live`, `mt4_live2`, `mt5_live`, `fxbackoffice`) — same DB_HOST config

**Key Files**:
- `frontend/src/pages/RiskMonitor.tsx` (BurstOpenTab + QuickOpenCloseTab + QuickProfitTab + drawers + `PositionStatusBadge`; Tab UI patterns: `docs/features/risk-monitor-reusable-patterns.md` §11; aggregate-window + live-refresh pattern: §12)
- `backend/app/api/v1/routes/risk_monitor.py` (burst-open + quick-open-close + quick-profit endpoints)
- `backend/app/services/rule_quick_open_close_service.py` (快开快平检测)
- `backend/app/services/rule_quick_profit_service.py` (快速获利检测 + 浮动刷新辅助函数)
- `backend/app/services/account_enrichment.py` (CRM 字段批量富化 + `get_net_deposit_hist_map` **client-level** 历史净入金：按 `userId` 聚合后映射回 loginsid，过滤 demo / `sid IN (1,2,5,6)`，CEN ÷100；与 client-return-rate 公式完全一致)
- `backend/app/services/risk_monitor_service.py` (SQL + sliding window rule engine + CEN currency enrichment)
- `backend/app/schemas/risk_monitor.py` (Pydantic models)
- `backend/app/core/burst_open_scheduler.py` (APScheduler + in-memory cache)
- `backend/app/core/risk_monitor_db.py` (SQLite config/history CRUD, `alert_events` event-level table; quick_profit_config + quick_profit_rules + QP 列；`net_deposit_hist` 列三个 tab 共用)
- `backend/scripts/backfill_alert_events_currency.py` (one-off migration for legacy currency=NULL rows)
- `backend/scripts/backfill_alert_events_open_time.py` (one-off migration: broker UTC+3 naive timestamps → UTC ISO8601 in `first_open`/`last_open`/`orders_json`)

**Docs**: [risk-monitor.md](../features/risk-monitor.md) | [Roadmap](../features/risk-monitor-roadmap.md) | **Skill**: `.cursor/skills/risk-monitor/SKILL.md`

---

### 4.9 Login IP Monitor (`LoginIPs.tsx`)
**Purpose**: Daily correlation analysis of MT login IPs to surface accounts sharing public IPs with other real accounts. Migrated in 2026-04 from standalone `46-MT-Server-Login-Detect/` project.

**Key Features**:
- 4-tab UI: 每日报告 / 监控账户 / 搜索 / 运维
- Scheduled ingestion via APScheduler (`Asia/Hong_Kong`): 05:10 download + parse FTP/FTPS logs; 08:30 correlation analysis + HTML email report
- Correlation windows: same-day + previous 7 days (`login_history`)
- Watchlist CRUD via REST (`POST`/`PATCH`/`DELETE` `/watchlist`); no per-action email verification (same `X-API-Key` gate as other APIs)
- Manual search (account_id / IP) with async CSV export (ThreadPoolExecutor + UTF-8-BOM)
- Tab 3 last successful search: `sessionStorage` cache (`frontend/src/lib/login-ip-search-cache.ts`); key scoped by `localStorage.auth_token`; cleared on `logout` via `clearAllLoginIpSearchCaches()` in `auth-provider.tsx`
- Scheduler audit trail: `login_ip_scheduler_runs` table surfaced in ops tab
- Failure alerts: ⚠️ email on job failure / partial success

**APIs**: 15 endpoints under `/api/v1/login-ip/*`
- `GET /available-dates`, `GET /report?date=`, `GET /watchlist`, `POST /search`
- `POST`/`PATCH`/`DELETE` `/watchlist` (batch add, update remarks, delete row)
- `GET /scheduler/runs`, `POST /scheduler/run-now`
- `GET|POST /mail/recipients`, `DELETE /mail/recipients/{id}`
- `POST /export/tasks`, `GET /export/tasks/{id}`, `GET /export/tasks/{id}/download`

**Data Sources**:
- FTP/FTPS × 3 MT servers (MT4_Live / MT4_Live2 / MT5) for raw login logs
- SQLite `backend/data/login_ip.db` (5 tables: monitored_accounts, login_history, mail_recipients, scheduler_runs, export_tasks)
- MySQL Slave (`KCM_fxbackoffice.users`) for Chinese-name enrichment

**Key Files**:
- `frontend/src/pages/LoginIPs.tsx` (tab shell) + `frontend/src/pages/login-ip/` (ReportTab, WatchlistTab, SearchTab, OperationsTab, `types.ts`); `frontend/src/lib/login-ip-search-cache.ts` (Tab 3 manual search state cache)
- `backend/app/api/v1/routes/login_ip.py` (15 endpoints)
- `backend/app/services/login_ip_{ftp,analyzer,report,search,export,enrichment}_service.py`
- `backend/app/core/login_ip_db.py` + `backend/app/core/login_ip_scheduler.py`
- `backend/scripts/migrate_login_ip_from_legacy.py` (one-off migration from legacy `monitoring.db`)
- `backend/scripts/login_ip_deep_audit.py` (2026-05) — **on-demand deep audit** for one watchlist account on one MT day. Pulls correlated logins from the structured report, resolves real `loginsid` via `mt4_users` (bypasses miskeyed `server_name`), enriches with CRM info + **client-level lifetime / N-day net deposit**, derives same-minute / same-symbol / same-direction (集体下单) and same-minute / opposite-direction (AB-pair) signals, then ships an HTML email to `BLOWUP_AUDIT_MAIL_TO`. Auto-filters `7`-prefix demo logins. **Net deposit formula must use `+` not `-`** (see [login-ip.md §11.2](../features/login-ip.md#112-净入金口径重要必须与-client_return_rate-对齐) — same as `client_return_service.py:178`).

**Env**: `LOGIN_IP_{MT4,MT5,MT4_LIVE2}_{HOST,PORT,USER,PASSWORD,REMOTE_DIR,USE_FTPS}` (18 vars). Passwords containing `$`/`!`/etc. MUST be single-quoted — see [dev-prod-guide.md](../deployment/dev-prod-guide.md) §环境变量特殊字符转义. Deep audit script reuses `BLOWUP_AUDIT_MAIL_TO` / `BLOWUP_AUDIT_MAIL_CC`.

**Docs**: [login-ip.md](../features/login-ip.md) | [Migration history](../features/login-ip_migration.md)

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
- Single prod cluster after decommissioning the Tokyo/`Fxbo_Trades` instance. All workloads target `KCM_fxbackoffice` on the Singapore CDC cluster.
- **Default** (`get_client()`): `CLICKHOUSE_HOST` + `CLICKHOUSE_DB` (default `KCM_fxbackoffice`). Used by the startup health probe.
- **Prod** (`get_client(use_prod=True)`): `CLICKHOUSE_prod_*` + database `KCM_fxbackoffice`. Used by IB Report (groups, search) and Client PnL Analysis (query). In single-cluster mode `CLICKHOUSE_prod_*` should mirror `CLICKHOUSE_*`.

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
- Production: `https://analysis.kohleservices.com/api/v1/` (via Nginx proxy)
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
- **API calls**: Use `apiFetch()` from `@/lib/fetch` instead of native `fetch()` for all `/api/*` requests. It auto-injects the `X-API-Key` header in production.
- **Lazy Loading**: All pages use `lazyWithRetry()` (auto-retry 2x on network failure). `LazyErrorBoundary` catches chunk errors, auto-reloads on first failure, shows retry button on repeated failure. Vendor libraries split via `manualChunks` in `vite.config.ts` (react, ag-grid, recharts, three, ui icons).
- **Comments**: English only

### Backend
- **Routes** (`routes/`): Handle HTTP request/response only
- **Schemas** (`schemas/`): Define request/response shapes (Pydantic)
- **Services** (`services/`): Implement business logic, database queries
- **Config** (`core/`): Centralized settings from .env

**Client report filtering (fxbackoffice)**: For any report that shows client/trading data (e.g. Client Return Rate, Dashboard PnL by country), exclude **demo accounts** (`GROUP NOT LIKE '%demo%'`, `sid IN (1,5,6)`) and **employee accounts** (`INNER JOIN users u ON u.id = <userId> AND COALESCE(u.isEmployee, 0) = 0`). See database-context skill “Client / report filtering” and `docs/features/client-return-rate.md` §6.

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

Fetch functions should accept an optional `signal?: AbortSignal` param and pass it to `apiFetch()`.
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
| API 403 Forbidden | Check X-API-Key header. See `docs/deployment/cloudflare-api-blocked.md` §方案C |
| ClickHouse connection | Check VPN, verify credentials |
| CEN account wrong amounts | Ensure dividing by 100 |
| Cache not updating | Wait for TTL expiry (PnL: 30min, IB: 10min, Return Rate: 3h) or use clear cache button |
| Page blank after deploy | Old chunk URLs 404 after rebuild. `LazyErrorBoundary` auto-reloads once; if still fails, shows retry button. Users on stale tabs recover automatically. |
| Chunk load failure on mobile | Network instability over Cloudflare Tunnel. `lazyWithRetry()` retries 2x before showing error. Vendor bundle split reduces per-request size. |

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
API_KEY=<your_key>  # Required in prod; if unset, API Key middleware is skipped (dev mode)
```

### Frontend (.env.development / .env.production)
```env
VITE_DISABLE_AUTH=true    # Skip login for dev (prod sets via Dockerfile ENV)
VITE_API_KEY=<your_key>  # API Key for X-API-Key header (required for /api/* access)
```

> `VITE_API_KEY` must match `API_KEY` in `backend/.env` and the key in `frontend/nginx.conf`.
> See `docs/deployment/cloudflare-api-blocked.md` §方案C for the full key rotation procedure.

---

## 10. Contact & Resources

- **Backend API Docs**: http://localhost:8001/docs
- **Documentation Hub**: [docs/README.md](../README.md)
- **Cursor AI Rules**: `.cursor/rules/project-context.mdc`
