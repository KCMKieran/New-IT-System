# Dev & Prod Deployment Guide

> Comprehensive guide for development workflow, production deployment, and Cloudflare Tunnel configuration on the internal Ubuntu server.

## Architecture Overview

```
Ubuntu Server (10.6.20.138)
┌───────────────────────────────────────────────────────────────┐
│                                                               │
│  ┌─ Production (docker-compose.prod.yml) ──────────────────┐  │
│  │                                                         │  │
│  │  Nginx (:3000)──→ FastAPI (internal:8001) ──→ Redis     │  │
│  │  Pre-built static     Code baked into image              │  │
│  │  Fast, stable         No --reload                        │  │
│  └─────────────────────────────────────────────────────────┘  │
│        ↑                                                      │
│   analysis.kohleservices.com (via Cloudflare Tunnel)          │
│   http://10.6.20.138:3000  (direct internal access)          │
│                                                               │
│  ┌─ Development (frontend & backend docker-compose.dev.yml)┐  │
│  │                                                         │  │
│  │  Vite dev (:5173) ──→ FastAPI (:8001) ──→ Redis         │  │
│  │  Mounts live code      --reload, auto-restart            │  │
│  │  Hot refresh           Code changes take effect instantly │  │
│  └─────────────────────────────────────────────────────────┘  │
│        ↑                                                      │
│   http://10.6.20.138:5173  (internal only)                    │
│                                                               │
│  ┌─ Other Services ────────────────────────────────────────┐  │
│  │  :80  → blacklist-frontend-prod (csblacklist domain)    │  │
│  │  :8000 → login_analysis_service (/ipmonitor/*)          │  │
│  └─────────────────────────────────────────────────────────┘  │
└───────────────────────────────────────────────────────────────┘
```

---

## Dev vs Prod Comparison

| | **Dev** | **Prod** |
|---|---|---|
| **Frontend server** | Vite dev server (on-the-fly ESM) | Nginx (pre-built static bundle) |
| **Backend server** | Uvicorn + `--reload` | Uvicorn 2 workers (no reload) |
| **Code source** | Volume-mounted from disk (live) | Copied into Docker image at build time |
| **After code change** | Browser auto-refreshes | No effect until `./deploy.sh` |
| **HTTP requests** | 200-500 (unbundled modules) | 5-10 (bundled + gzip) |
| **Auth** | Disabled (`VITE_DISABLE_AUTH=true`) | Disabled (Cloudflare Zero Trust handles auth) |
| **Port** | Frontend `:5173`, Backend `:8001` | Nginx `:3000` (unified entry, proxies `/api` internally) |
| **Access URL** | `http://10.6.20.138:5173` | `http://10.6.20.138:3000` or `analysis.kohleservices.com` |
| **Use case** | Writing & debugging code | Serving end users |

---

## Access Methods

| Address | What you see | Traffic path |
|---|---|---|
| `http://10.6.20.138:5173` | Dev (live code) | Direct to server, fastest |
| `http://10.6.20.138:3000` | Prod (stable build) | Direct to server, fast |
| `https://analysis.kohleservices.com` | Prod (stable build) | Browser → Cloudflare Edge → Tunnel → `:3000` |

> **Note**: Even from the internal network, `analysis.kohleservices.com` resolves to Cloudflare IPs (not `10.6.20.138`). Traffic always goes through Cloudflare. A bypass rule skips Zero Trust authentication for the office IP range, so internal users don't see the login prompt.

---

## Daily Development Workflow

### Step 1: Write code (Dev mode)

Open Cursor, connect to the server, edit code. View changes at `http://10.6.20.138:5173`.

Code changes → Browser auto-refreshes → Instant feedback.

### Step 2: Deploy to Production

When you're satisfied with your changes:

```bash
# Option A: Commit first, then deploy (recommended)
git add . && git commit -m "feat: description" && git push origin main
./deploy.sh

# Option B: Deploy without committing (testing prod build)
docker compose -f docker-compose.prod.yml up -d --build
```

`deploy.sh` does: `git pull` → rebuild images → restart prod containers. Takes ~20 seconds. **Dev containers are not affected.**

### Step 3: Verify

Open `http://10.6.20.138:3000` to check the production build looks correct.

---

## Docker Container Map

### Production containers (`docker-compose.prod.yml`)

| Container | Image | Port | Notes |
|---|---|---|---|
| `new-it-frontend-prod` | Nginx + static build | `3000:80` | Serves React build + proxies `/api` |
| `new-it-backend-prod` | FastAPI (Uvicorn) | internal only | 2 workers, no `--reload` |
| `new-it-redis-prod` | Redis 7 Alpine | internal only | Prod cache |

### Development containers

| Container | Compose file | Port | Notes |
|---|---|---|---|
| `new-it-frontend-dev` | `frontend/docker-compose.dev.yml` | `5173:5173` | Vite dev server, hot reload |
| `new-it-backend-dev` | `backend/docker-compose.dev.yml` | `8001:8001` | Uvicorn + `--reload` |
| `new-it-redis` | `backend/docker-compose.dev.yml` | internal only | Dev cache |

### Starting containers

```bash
# Start dev (if not already running)
cd /opt/myproject/New-IT-System/backend && docker compose -f docker-compose.dev.yml up -d
cd /opt/myproject/New-IT-System/frontend && docker compose -f docker-compose.dev.yml up -d

# Start prod
cd /opt/myproject/New-IT-System && docker compose -f docker-compose.prod.yml up -d --build
```

### Useful commands

```bash
# View all running containers
docker ps --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"

# View prod logs
docker logs new-it-frontend-prod --tail 50
docker logs new-it-backend-prod --tail 50

# Restart only the prod frontend (e.g. after frontend-only changes)
docker compose -f docker-compose.prod.yml up -d --build web

# Restart only the prod backend
docker compose -f docker-compose.prod.yml up -d --build api
```

---

## Key Files

| File | Purpose |
|---|---|
| `docker-compose.prod.yml` | Production orchestration (root directory) |
| `frontend/docker-compose.dev.yml` | Dev frontend (Vite) |
| `backend/docker-compose.dev.yml` | Dev backend (FastAPI + Redis) |
| `frontend/Dockerfile.prod` | Multi-stage build: Node (build) → Nginx (serve) |
| `frontend/Dockerfile` | Dev image (Node only, for Vite dev server) |
| `backend/Dockerfile` | Backend image (Python, shared by dev & prod) |
| `frontend/nginx.conf` | Nginx config: static files + API proxy + gzip |
| `frontend/.dockerignore` | Excludes `node_modules` from Docker context |
| `deploy.sh` | One-click production deployment script |

---

## Cloudflare Tunnel Configuration

The tunnel runs as a systemd service using a **local config file** (not managed via Cloudflare Dashboard).

### Config file location

```
/etc/cloudflared/config.yml
```

### Current ingress rules

```yaml
ingress:
  - hostname: csblacklist.kohleservices.com
    service: http://localhost:80          # Blacklist frontend

  - hostname: analysis.kohleservices.com
    path: /dash/*
    service: http://10.6.20.138:8050     # Dash service (disabled)

  - hostname: analysis.kohleservices.com
    path: /ipmonitor/*
    service: http://10.6.20.138:8000     # IP login monitor

  - hostname: analysis.kohleservices.com
    service: http://localhost:3000        # Main app (Prod Nginx)

  - service: http_status:404             # Catch-all
```

Rules are matched **top-to-bottom**, first match wins.

### Modifying tunnel config

```bash
sudo nano /etc/cloudflared/config.yml    # Edit the file
sudo systemctl restart cloudflared       # Apply changes
sudo systemctl status cloudflared        # Verify running
```

> **Important**: This tunnel is CLI-managed. Do NOT modify ingress rules via the Cloudflare Zero Trust Dashboard — it will conflict with the local config file.

### DNS resolution

`analysis.kohleservices.com` resolves to Cloudflare IPs (e.g., `104.21.27.230`), not the internal server IP. All traffic goes through Cloudflare, even from the internal network. A Cloudflare Access bypass policy skips authentication for the office IP range.

---

## Why Prod is Faster Than Dev (via Cloudflare)

| Factor | Dev (Vite) | Prod (Nginx) |
|---|---|---|
| HTTP requests per page load | 200-500 | 5-10 |
| Per-request Cloudflare overhead | ~100ms × 200 = **20s** | ~100ms × 5 = **0.5s** |
| Gzip compression | No | Yes |
| Static asset caching | Disabled | 1 year (content-hashed filenames) |
| WebSocket (HMR) | Yes (may fail via tunnel) | No |

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Prod shows login page | `VITE_DISABLE_AUTH` not set at build time | Check `Dockerfile.prod` has `ENV VITE_DISABLE_AUTH=true` before `RUN npm run build`, then rebuild |
| `./deploy.sh` fails on `npm run build` | TypeScript errors | Fix TS errors first (dev mode doesn't catch them because Vite skips `tsc`) |
| Port 3000 already in use | Another container on that port | `docker ps` to find it, stop or change port |
| Cloudflare shows "Authentication error" | Browser has stale cookies | Clear cookies for `analysis.kohleservices.com` or use incognito |
| Tunnel config change not taking effect | `cloudflared` not restarted | `sudo systemctl restart cloudflared` |
| API returns 502 via Cloudflare | Backend container not running | `docker logs new-it-backend-prod` to check, restart if needed |
| Docker build slow (500MB+ context) | Missing `.dockerignore` | Ensure `frontend/.dockerignore` contains `node_modules` |
