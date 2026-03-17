# IB 资金监控 (IB Financial Monitor)

> Feature spec for the IB Financial Monitor page under Risk Control.
> Replaces the standalone D08 cron script with an integrated web-based solution.

## 1. Overview

This page allows risk/CS team members to:
- **Manage** which IBs are monitored (add/remove via UI)
- **Query** real-time financial data (deposits, withdrawals, equity, differences)
- **Send** report emails manually or on a daily schedule
- **Configure** email recipients, CC, schedule time, and on/off toggle

All write operations require **email verification** via a 6-digit code sent to whitelisted admin emails.

**URL**: `/ib-financial-monitor`
**Sidebar**: Risk Control > IB 资金监控

---

## 2. Architecture

```
Frontend (IBFinancialMonitor.tsx)
  ├── Tab 1: 数据查询    → GET /query, POST /send-report
  ├── Tab 2: IB 管理      → GET /watchlist, POST /request-code, POST /verify-action
  └── Tab 3: 报表设置    → GET /config, GET /audit-log, GET /whitelist

Backend (FastAPI)
  ├── routes/ib_financial.py    → 10 API endpoints
  ├── services/ib_financial_service.py → Business logic
  ├── services/email_service.py → SMTP email sending
  ├── core/database.py          → SQLite config store
  └── core/scheduler.py         → APScheduler daily job

Data stores:
  - SQLite (backend/data/ib_financial.db) → watchlist, report_config, admin_whitelist, audit_log
  - MySQL fxbackoffice (read-only)        → stats_transactions, stats_balances, ib_tree_with_self
  - Redis                                 → verification codes (TTL 5min)
```

---

## 3. Database Schema (SQLite)

File: `backend/data/ib_financial.db`

### watchlist
| Column | Type | Description |
|--------|------|-------------|
| id | INTEGER PK | Auto-increment |
| ib_id | TEXT UNIQUE | IB account ID |
| ib_name | TEXT | Display name, e.g. "张三(123456)" |
| added_by | TEXT | Operator email |
| added_at | DATETIME | Timestamp |
| is_active | INTEGER | 1=active, 0=soft-deleted |

### report_config (single row, id=1)
| Column | Type | Description |
|--------|------|-------------|
| mail_to | TEXT | Comma-separated TO recipients |
| mail_cc | TEXT | Comma-separated CC recipients |
| schedule_time | TEXT | Daily send time in HKT, e.g. "17:00" |
| is_enabled | INTEGER | 1=enabled, 0=disabled |
| updated_by | TEXT | Last modifier email |
| updated_at | DATETIME | Last modification time |

### admin_whitelist
| Column | Type | Description |
|--------|------|-------------|
| email | TEXT PK | Whitelisted admin email address |

Current whitelist:
- `kieran.xiang@kohleservices.com`
- `lawrence.li@kohleservices.com`
- `teresa.wong@kohleservices.com`

### audit_log
| Column | Type | Description |
|--------|------|-------------|
| id | INTEGER PK | Auto-increment |
| action | TEXT | add_ib / remove_ib / update_config |
| detail | TEXT | JSON description of the change |
| operator | TEXT | Who performed the action |
| created_at | DATETIME | Timestamp |

---

## 4. API Endpoints

Base: `/api/v1/ib-financial/`

| Endpoint | Method | Auth Required | Description |
|----------|--------|---------------|-------------|
| `/watchlist` | GET | No | List active IBs |
| `/query` | GET | No | Query financial data (param: `target_date`) |
| `/config` | GET | No | Get report email config |
| `/request-code` | POST | No | Send 6-digit code to whitelisted email |
| `/verify-action` | POST | No | Verify code and execute add/remove/update |
| `/send-report` | POST | No | Manually send report email |
| `/audit-log` | GET | No | View operation history (param: `limit`) |
| `/whitelist` | GET | No | List admin whitelist emails |

### Verification Flow

```
User clicks Add/Remove/Save
  → Frontend opens Verification Dialog
  → User selects whitelisted email
  → POST /request-code → backend sends 6-digit code via SMTP
  → Code stored in Redis (key: ib_fin_verify:{email}:{action_hash}, TTL=300s)
  → User enters code
  → POST /verify-action → backend verifies code, executes operation, writes audit_log
```

---

## 5. SQL Query (ported from D08)

The financial query uses a CTE-based approach against MySQL `fxbackoffice`:

1. **Target_IB_List** — `ib_tree_with_self` to find all clients under each IB
2. **Transaction_Stats** — `stats_transactions` for daily and all-time deposit/withdrawal sums
3. **Balance_Snapshot** — `stats_balances` for MT4 equity and IB wallet equity on target date
4. **All_Keys** — UNION to simulate FULL OUTER JOIN on (ib_id, currency)
5. **Final output** — today deposit/withdrawal, total deposit/withdrawal, MT4 equity, IB wallet equity, difference

Key difference from D08: uses parameterised `%s` placeholders instead of f-string interpolation to prevent SQL injection.

---

## 6. Scheduled Reports (APScheduler)

- Scheduler starts on FastAPI startup via `lifespan` event
- Reads `schedule_time` from `report_config` table (default: 17:00 HKT)
- When config is updated via API, `reschedule()` dynamically adjusts the job trigger
- Job function: queries all active watchlist IBs → builds HTML report → sends via SMTP
- Skip conditions: `is_enabled=0` or no `mail_to` configured

---

## 7. Frontend Page

**File**: `frontend/src/pages/IBFinancialMonitor.tsx`

### Tab 1: 数据查询
- Date picker (default: yesterday)
- "实时查询" button → `GET /api/v1/ib-financial/query?target_date=YYYY-MM-DD`
- Results displayed in shadcn Table with formatted numbers
- "发送邮件" button → `POST /api/v1/ib-financial/send-report`
- Red-colored withdrawal columns for visual distinction

### Tab 2: IB 管理
- Table showing current watchlist (IB ID, Name, Added By, Added At)
- "添加 IB" button → Dialog for IB ID + Name input → Verification Dialog
- Per-row delete button → Verification Dialog
- All add/remove operations require email verification

### Tab 3: 报表设置
- Form fields: TO, CC, Schedule Time (HKT), Enable/Disable toggle
- "保存配置" → Verification Dialog → `POST /verify-action` with `update_config`
- Audit log panel showing recent operations (action, detail, operator, timestamp)

### Shared: VerificationDialog component
- Displays whitelisted email buttons for selection
- "获取验证码" → sends code
- 6-digit code input → "确认执行"

---

## 8. File Inventory

### Backend (new files)
| File | Purpose |
|------|---------|
| `backend/app/core/database.py` | SQLite init, schema, `get_db()` context manager |
| `backend/app/core/scheduler.py` | APScheduler integration, `start/stop/reschedule` |
| `backend/app/services/email_service.py` | SMTP sending, `send_email()`, `send_verification_code()` |
| `backend/app/services/ib_financial_service.py` | Watchlist CRUD, financial query, config CRUD, audit log |
| `backend/app/schemas/ib_financial.py` | Pydantic models for all request/response types |
| `backend/app/api/v1/routes/ib_financial.py` | 10 API endpoints |

### Backend (modified files)
| File | Change |
|------|--------|
| `backend/app/core/config.py` | Added SMTP_SERVER, SMTP_PORT, SMTP_USERNAME, SMTP_PASSWORD |
| `backend/app/main.py` | Added `lifespan` with `init_db()` + `start_scheduler()` / `stop_scheduler()` |
| `backend/app/api/v1/routers.py` | Registered `ib_financial_router` |
| `backend/requirements.txt` | Added `apscheduler` |

### Frontend (new files)
| File | Purpose |
|------|---------|
| `frontend/src/pages/IBFinancialMonitor.tsx` | Main page component with 3 tabs |

### Frontend (modified files)
| File | Change |
|------|--------|
| `frontend/src/App.tsx` | Added route `/ib-financial-monitor` |
| `frontend/src/components/app-sidebar.tsx` | Added menu item under Risk Control |
| `frontend/src/components/site-header.tsx` | Added title mapping |
| `frontend/src/pages/ConfigPlaceholder.tsx` | Synced navigation item |
| `frontend/src/i18n/locales/zh-CN.ts` | Added `ibFinancialMonitor` translation |
| `frontend/src/i18n/locales/en-US.ts` | Added `ibFinancialMonitor` translation |

### Data
| File | Purpose |
|------|---------|
| `backend/data/ib_financial.db` | SQLite database (auto-created on first startup) |

---

## 9. Environment Variables

Added to `backend/.env`:

```env
SMTP_SERVER='smtp.office365.com'
SMTP_PORT=587
SMTP_USERNAME='ks.system@kohleservices.com'
SMTP_PASSWORD='...'
```

Read by `Settings` class in `backend/app/core/config.py`.

---

## 10. Migration from D08

The standalone D08 script can be deprecated once this feature is validated:

| D08 Feature | New System Equivalent |
|---|---|
| Hardcoded `TARGET_IB_MAP` | SQLite `watchlist` table (UI managed) |
| `.env` D08_MAIL_SEND_TO / CC | SQLite `report_config` table (UI managed) |
| Cron job triggering Python script | APScheduler inside FastAPI |
| `email_utils.py` | `backend/app/services/email_service.py` |
| f-string SQL interpolation | Parameterised `%s` placeholders |
| Jinja2 HTML template | `_build_report_html()` in route |

---

## 11. Deployment (Dev vs Prod)

### Dev / Prod 差异

| | Dev | Prod |
|---|---|---|
| 代码来源 | 挂载本地磁盘，改代码自动重载 | 构建时拷贝进 Docker 镜像，需要 `./deploy.sh` |
| 访问地址 | `http://10.6.20.138:5173` | `http://10.6.20.138:3000` 或 `analysis.kohleservices.com` |
| SQLite 数据 | 宿主机 `backend/data/ib_financial.db`，通过 `.:/app` 挂载持久化 | 宿主机 `backend/data/` 通过 volume 挂载持久化 |
| Redis | `new-it-redis` 容器（`backend_default` 网络） | `new-it-redis-prod` 容器（`new-it-system_default` 网络） |
| APScheduler | 随 dev 后端启动，调试用 | 随 prod 后端启动，正式发送报表 |

### 注意事项

**Redis 连接**：路由中使用 `os.getenv("REDIS_HOST", "localhost")` 获取 Redis 地址。Docker 容器内 `REDIS_HOST` 由各 `docker-compose.*.yml` 分别设置为对应的容器名，不能写死 `localhost`。

**SQLite 数据持久化**：
- **Dev**：`docker-compose.dev.yml` 中 `volumes: - .:/app` 把整个 `backend/` 挂载进容器，`data/ib_financial.db` 天然持久化。
- **Prod**：`docker-compose.prod.yml` 中已配置 `./backend/data:/app/data` volume 挂载，数据在部署重建后保留。白名单、watchlist、report_config 等配置不会因重新部署而丢失。

首次部署 Prod 后仍需手动初始化白名单（因为 DB 文件是全新创建的）。

### 部署到 Prod

```bash
# 1. 提交代码
git add . && git commit -m "feat: IB Financial Monitor" && git push origin main

# 2. 一键部署（约 20-30 秒）
./deploy.sh

# 3. 如果没有持久化 data 目录，初始化白名单
docker exec new-it-backend-prod python -c "
from app.core.database import init_db, get_db
init_db()
with get_db() as conn:
    for email in ['kieran.xiang@kohleservices.com','lawrence.li@kohleservices.com','teresa.wong@kohleservices.com']:
        conn.execute('INSERT OR IGNORE INTO admin_whitelist VALUES (?)', (email,))
"

# 4. 验证
curl -s http://localhost:3000/api/v1/ib-financial/whitelist
```

### 推荐开发流程

```
编写/修改代码
  ↓
Dev 环境自动重载 (http://10.6.20.138:5173)
  ↓
手动测试功能 + API
  ↓
测试通过 → git commit + git push
  ↓
./deploy.sh → Prod 自动构建 + 重启
  ↓
验证 Prod (http://10.6.20.138:3000)
```

**建议**：Dev 和 Prod 分开运行是正确做法。Dev 用来开发调试（热更新、即改即看），Prod 用来给用户使用（稳定、性能好）。两者互不干扰——部署 Prod 不会影响 Dev，改 Dev 代码也不会影响 Prod。这是项目已有的标准流程，IB Financial Monitor 遵循同样的模式。
