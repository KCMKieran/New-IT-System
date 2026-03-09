# KCM IT System Documentation

This is the documentation hub for the KCM IT System - an internal financial trading risk control and analytics platform.

## Quick Start

| Role | Start Here |
|------|------------|
| **New Developer** | [Backend Overview](backend/overview.md) / [Frontend File Structure](frontend/file-structure.md) |
| **Frontend Dev** | [Frontend Docs](frontend/) |
| **Backend Dev** | [Backend Docs](backend/) |
| **PM / Business** | [Features Docs](features/) |
| **DevOps** | [Operations Docs](operations/) |
| **AI Assistant** | [AI Context](ai-context/PROJECT_CONTEXT.md) |

## Project Overview

### Tech Stack

| Layer | Technology |
|-------|------------|
| Frontend | React 19 + TypeScript + Vite 7 + shadcn/ui + AG-Grid |
| Backend | Python 3.11 + FastAPI + ClickHouse + Redis |
| Database | ClickHouse (analytics) + MySQL (MT4/MT5) + PostgreSQL (reporting) |
| Deployment | Docker Compose + Nginx + Cloudflare Tunnel |

### Core Modules

| Module | Description | Docs |
|--------|-------------|------|
| Dashboard | Home page with system overview | [dashboard.md](features/dashboard.md) |
| Client PnL Analysis | Customer profit/loss analysis | [Features](features/) |
| Position Monitor | Real-time position monitoring | [position-monitor.md](features/position-monitor.md) |
| IB Report | Broker commission reports | [ib-report.md](features/ib-report.md) |

## Documentation Structure

```
docs/
├── README.md                    # You are here
├── architecture/                # System design & technical decisions
│   ├── caching-strategy.md     # Redis caching implementation
│   ├── logging-system.md       # Logging system design
│   ├── backend-logging.md      # Backend logging details
│   └── mt5-database-schema.md  # MT5 database schema
├── backend/                     # Backend development guides
│   ├── overview.md             # Backend quick start (MUST READ)
│   ├── etl-service-guide.md    # ETL service documentation
│   ├── sync-pnl-summary.md     # PnL sync usage
│   └── client-pnl-refresh-logging.md
├── frontend/                    # Frontend development guides
│   ├── file-structure.md       # Project structure explained
│   ├── ag-grid-integration.md  # AG-Grid v34 integration guide
│   ├── filter-module-design.md # Filter module architecture
│   ├── filter-integration-guide.md
│   ├── filter-static-demo.md
│   ├── swapfree-control.md
│   ├── design-ui.md
│   └── page-operation-guide.md
├── features/                    # Feature specifications & guides
│   ├── position-monitor.md     # Position monitoring features
│   ├── ib-report.md            # IB report design
│   ├── ib-net-deposit-reform.md
│   ├── ib-net-deposit-summary.md
│   ├── open-positions-reform.md
│   ├── dashboard-pnl-by-group.md
│   ├── client-pnl-local-filtering.md
│   ├── client-pnl-column-toggle.md
│   ├── profit-deep-analysis.md
│   ├── pnl-monitor-integration.md
│   └── filter-backend-integration.md
├── deployment/                  # ⭐ Deployment & DevOps
│   └── dev-prod-guide.md      # Dev/Prod workflow, Docker, Cloudflare Tunnel
├── operations/                  # Operations & maintenance
│   ├── clickhouse-connection.md # ClickHouse production setup
│   ├── create-ib_downline_net_deposit_agg.md
│   └── clientid-based-page.md
└── ai-context/                  # AI assistant context
    └── PROJECT_CONTEXT.md      # Detailed project context for AI
```

## Development & Deployment

> **Full guide**: [deployment/dev-prod-guide.md](deployment/dev-prod-guide.md)

Both Dev and Prod run simultaneously on the same server via Docker:

| Environment | Access URL | Purpose |
|---|---|---|
| **Dev** | `http://10.6.20.138:5173` | Write & debug code (hot reload) |
| **Prod** | `http://10.6.20.138:3000` or `analysis.kohleservices.com` | Stable version for end users |

### Quick Commands

```bash
# Start dev containers
cd backend && docker compose -f docker-compose.dev.yml up -d && cd ..
cd frontend && docker compose -f docker-compose.dev.yml up -d && cd ..

# Deploy to production (one-click)
./deploy.sh
```

## Contributing

### Code Style
- All comments in **English**
- Frontend: Functional components + hooks
- Backend: Follow routes/schemas/services separation

### Documentation
- Use kebab-case for filenames (e.g., `my-feature.md`)
- Place docs in appropriate category folder
- Update this README when adding new docs

## Related Resources

- [Backend API Docs](http://localhost:8001/docs) (Swagger UI when running locally)
- [Cursor AI Rules](../.cursor/rules/) - Auto-loaded context for AI assistance
