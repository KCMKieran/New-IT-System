# IT System — Trading Risk Control & Analytics Platform

An internal web platform for monitoring, reporting, and analyzing trading
operations for a forex/CFD brokerage. It surfaces real-time positions, client
profitability, introducing-broker commissions, and a configurable risk-rule
engine over data sourced from MT4/MT5 trading servers and a CRM backend.

> **Note:** This repository contains application source only. All hosts,
> credentials, internal endpoints, and customer data are supplied at runtime
> via environment variables and are **not** part of this repo. Use your own
> infrastructure values when deploying.

## Tech Stack

| Layer | Technology |
|-------|------------|
| Frontend | React 19 · TypeScript · Vite 7 · shadcn/ui · Tailwind CSS 4 · AG-Grid v34 |
| Backend | Python 3.11 · FastAPI (async) |
| Data stores | ClickHouse (analytics) · MySQL (trading) · PostgreSQL (ETL) · Redis (cache) · SQLite (feature config) |
| Infra | Docker Compose · Nginx · reverse-proxy / tunnel for external access |

## Architecture

```
┌────────────────────────────┐
│   Frontend (React SPA)      │  Vite dev server / Nginx static (prod)
└─────────────┬──────────────┘
              │ HTTP REST  (/api/v1/*)
┌─────────────▼──────────────┐
│   Backend (FastAPI)         │  routes → schemas → services → core
└──┬───────────┬───────────┬─┘
   ▼           ▼           ▼
ClickHouse   MySQL       Redis
(analytics) (trading)    (cache)
```

Backend layering convention:

- **routes/** — HTTP request/response only
- **schemas/** — Pydantic request/response models
- **services/** — business logic + database queries
- **core/** — config, middleware, schedulers, caching utilities

## Features

- **Real-time position monitoring** across multiple trading servers
- **Client P&L analysis** with filtering, column toggles, and CSV export
- **Introducing-broker commission reports** with single-pass SQL aggregation
- **Deposit / withdrawal queries** by broker or region
- **Client return-rate analysis** with deposit-bucket adjustments
- **Configurable risk-rule engine** with scheduled background scanning and
  alerting
- **Login-IP correlation monitoring** with scheduled ingestion and reporting

## Getting Started

### Prerequisites

- Docker & Docker Compose
- Node.js (for local frontend tooling)
- Access to your own ClickHouse / MySQL / PostgreSQL / Redis instances

### Configuration

Copy the example environment files and fill in **your own** values:

```bash
cp backend/.env.example backend/.env
cp frontend/.env.example frontend/.env.development
```

**Backend (`backend/.env`)**

```env
DB_HOST=your_clickhouse_host
DB_USER=your_user
DB_PASSWORD=your_password
DB_NAME=your_database
DB_PORT=9000

REDIS_HOST=localhost
REDIS_PORT=6379

CORS_ORIGINS=http://localhost:5173
API_KEY=your_generated_api_key   # required in production
```

**Frontend (`frontend/.env.development`)**

```env
VITE_DISABLE_AUTH=true            # skip login in dev
VITE_API_KEY=your_generated_api_key
```

> `VITE_API_KEY` must match the backend `API_KEY`. Generate a strong random
> value (e.g. `openssl rand -hex 32`) and keep it out of version control.

### Run (development, hot reload)

```bash
cd backend  && docker compose -f docker-compose.dev.yml up -d && cd ..
cd frontend && docker compose -f docker-compose.dev.yml up -d && cd ..
```

- Frontend dev server: `http://localhost:5173`
- Backend API docs (Swagger): `http://localhost:8001/docs`

### Build (production)

Production orchestration is defined in `docker-compose.prod.yml`. Provide your
own host, domain, and credential values via environment / deployment config.

## API Conventions

Endpoints live under `/api/v1/*`. List responses use a consistent envelope:

```json
{
  "data": [],
  "total": 0,
  "page": 1,
  "page_size": 50,
  "total_pages": 0,
  "statistics": { "from_cache": true, "query_time_ms": 0 }
}
```

Common query parameters: `page`, `page_size`, `sort_by`, `sort_order`,
`start_date`, `end_date`.

## Conventions

- **Frontend fetches** use a shared `apiFetch()` helper that injects the API
  key header; all data-fetching `useEffect` hooks use `AbortController` for
  React StrictMode safety.
- **Lazy-loaded pages** with chunk-load retry + error boundary.
- **Comments in code are English.**
- **Caching**: Redis with per-feature TTLs; a single-flight utility coalesces
  duplicate concurrent queries.

## License

Proprietary — internal use only. Not licensed for redistribution.
