# Login IP Monitor — MT 登录 IP 关联分析

> **已上线模块**。2026-04 期间从 `/opt/myproject/log_analysis/46-MT-Server-Login-Detect/`
> 迁入 `New-IT-System`，原旧项目将在并行观察期后下线。
>
> **迁移/设计源文档**: [login-ip_migration.md](./login-ip_migration.md) — 完整的阶段性提示词、
> 表结构、迁移脚本、踩坑记录。本文只讲"运行中的系统长啥样"。

## 1. 目的

每日把 MT4 Live / MT4 Live2 / MT5 三台交易服务器的登录日志从 FTP/FTPS 拉下来，
解析出每个账号当天登录用过哪些公网 IP。针对运营维护的一份「被监控账户」清单：

- 如果同一个 IP 被监控账户 A 与任意其他真实账户 B 共用 → 视为 **关联**
- 关联覆盖两类窗口：**当日** 与 **过去 7 天历史**
- 命中关联的账号 → 早上 08:30 邮件告警，内含关联账户中文姓名（来自 CRM）

业务视角：B-Book 券商最怕同一批实际控制人用多个账号刷返佣 / 对冲。共享 IP 是最强
的初筛信号，人工审核时拿着这份表快速收敛就够用。

## 2. 架构一图流

```
┌──────────────────────────────────────────────────────────┐
│         APScheduler (Asia/Hong_Kong)                      │
│                                                           │
│  02:00 HKT  login_ip_download_job                         │
│      ├─ FTP/FTPS × 3 server → data/login_ip/tmp/          │
│      ├─ parse logs → INSERT login_history (SQLite)        │
│      └─ 清 tmp/ (只保留过去 7 天分析 JSON)                 │
│                                                           │
│  08:30 HKT  login_ip_analyze_report_job                   │
│      ├─ 读 monitored_accounts + login_history             │
│      ├─ 当日 + 7 天历史关联分析 → analysis_*.json          │
│      ├─ CRM enrichment (chinese_name)                     │
│      └─ email_service.send_html() → mail_recipients       │
│                                                           │
│  失败 / partial → ⚠️ 告警邮件同样走 email_service          │
│  审计：login_ip_scheduler_runs 表记录 started / finished   │
└──────────────────────────────────────────────────────────┘
           │                                    │
           ▼                                    ▼
 backend/data/login_ip.db         backend/data/login_ip/YYYYMMDD/*.json
 (monitored_accounts,              (raw_logins / analysis_* 结构化结果)
  login_history,
  login_ip_mail_recipients,
  login_ip_scheduler_runs,
  login_ip_export_tasks)
           │
           ▼
 FastAPI /api/v1/login-ip/* (14 个 endpoint)
           │
           ▼
 React 4 Tab 页面 (/login-ips)
 ├─ 每日报告   ReportTab.tsx
 ├─ 监控账户   WatchlistTab.tsx
 ├─ 搜索       SearchTab.tsx
 └─ 运维       OperationsTab.tsx
```

## 3. 数据源

| 数据 | 位置 | 用途 |
|------|------|------|
| MT4 Live / MT4 Live2 / MT5 登录日志 | FTP/FTPS (3 台) | 每日 02:00 拉 `YYYYMMDD.log*` |
| `monitored_accounts` | `login_ip.db` | 运营维护的被监控账户（批量新增需邮箱验证） |
| `login_history` | `login_ip.db` | 过去 7 天每 (account, ip, day, server) 的登录记录 |
| `admin_whitelist` | `it_system.db` (IB Financial) | 允许收验证码的管理员邮箱（多模块共享）|
| CRM MySQL (`KCM_fxbackoffice.users`) | MySQL Slave | 关联账户中文姓名 enrichment |
| `login_ip_mail_recipients` | `login_ip.db` | 收日报/告警的 `to` / `cc` 列表 |
| `login_ip_scheduler_runs` | `login_ip.db` | 调度审计（UI 运维 Tab 展示） |
| `login_ip_export_tasks` | `login_ip.db` | 搜索 CSV 异步导出任务状态 |

MT4 vs MT5 日志格式小差异（MT5 多几列），`login_ip_analyzer_service.py` 里按服务器
类型走不同 `split` 路径，统一成 `{account_id, ip_address, login_date, server_name}`。

## 4. API 清单

Base: `/api/v1/login-ip/*`，全部走平台的 `X-API-Key` 中间件（`apiFetch` 自动附带）。

**读路径（不保护）**

| Method | Path | 说明 |
|--------|------|------|
| GET | `/available-dates` | 目前 `data/login_ip/` 下有 JSON 的日期（newest first） |
| GET | `/report?date=YYYYMMDD` | 指定日期的结构化报告（Tab 1 直渲） |
| GET | `/watchlist` | 被监控账户 flat 列表（Tab 2 表格） |
| POST | `/search` | 批量搜索 `{search_type, terms: string[], days}`（Tab 3） |
| GET | `/scheduler/runs?job=&limit=` | 最近 N 条调度记录（上限 200） |
| GET | `/mail/recipients?active_only=` | 收件人列表（UI 编辑用） |
| GET | `/whitelist` | 白名单邮箱只读（**Tab 2 弹窗用**） |

**写路径 / 任务触发**

| Method | Path | 说明 |
|--------|------|------|
| POST | `/scheduler/run-now` | `{job: "download"\|"analyze_report", target_date?}` 手动跑 |
| POST | `/mail/recipients` | 新增收件人（无验证码，运维内部） |
| DELETE | `/mail/recipients/{id}` | 软删除收件人 (`is_active=0`) |
| POST | `/export/tasks` | 创建异步 CSV 导出（搜索结果） |
| GET | `/export/tasks/{id}` | 轮询 status |
| GET | `/export/tasks/{id}/download` | 下载 CSV（`utf-8-sig`，Excel 直开）|
| POST | `/request-code` | 发验证码到白名单邮箱（Redis TTL 300s） |
| POST | `/verify-action` | 消费验证码并执行 watchlist 写 |

**验证码保护的 action**（`POST /verify-action` 的 `action` 字段）：

- `add_monitored_account` — `{account_ids: int[], server_name, remarks?}`
- `update_monitored_account` — `{id, remarks}`
- `delete_monitored_account` — `{id}`

> 设计选择：不单独暴露 `POST/PATCH/DELETE /watchlist`，写操作只能通过
> `/verify-action` 进入，保证操作员绕不过邮箱验证码。

## 5. 定时任务

| Job name | Cron (HKT) | 作用 | 失败行为 |
|----------|-----------|------|---------|
| `login_ip_download_job` | `02:00` 每日 | FTP 拉日志 + 解析 + 写 `login_history` | 发 ⚠️ 告警邮件；留 tmp 供手动 retry |
| `login_ip_analyze_report_job` | `08:30` 每日 | 关联分析 + 发日报 | 同上；写 `login_ip_scheduler_runs.status='failed'` |

- 并发保护：`threading.Lock`，同一 job 不会重入
- 时区硬编码 `ZoneInfo('Asia/Hong_Kong')`，跟 `docker-compose.prod.yml` 的 `TZ=Asia/Hong_Kong` 一致
- 可通过 `LOGIN_IP_SCHEDULER_ENABLED=false` 在 dev 关停调度
- 手动触发入口：UI 运维 Tab → 「立即运行」 或 `POST /scheduler/run-now`

## 6. SQLite Schema

单文件 `backend/data/login_ip.db`，5 张表：

- `monitored_accounts(id, account_id UNIQUE, server_name, remarks)`
- `login_history(id, account_id, ip_address, login_date, server_name)` + 3 个索引（account/ip/date）
- `login_ip_mail_recipients(id, email, role, is_active, remarks, created_at)`
- `login_ip_scheduler_runs(id, job_name, target_date, started_at, finished_at, status, summary_json, error_msg)`
- `login_ip_export_tasks(id, status, search_type, terms_json, days, requested_ip, row_count, file_path, ...)`

建表逻辑在 `backend/app/core/login_ip_db.py`，`main.py lifespan` 启动时执行 `create_tables()`。

## 7. `.env` 配置

3 台 FTP 服务器 × 6 段 = 18 个变量（不要漏 `USE_FTPS`）：

```bash
LOGIN_IP_MT4_HOST=...
LOGIN_IP_MT4_PORT=21
LOGIN_IP_MT4_USER=...
LOGIN_IP_MT4_PASSWORD='...'   # 单引号：密码含 $ / ! / & 等特殊字符时必须
LOGIN_IP_MT4_REMOTE_DIR=/logs
LOGIN_IP_MT4_USE_FTPS=true

LOGIN_IP_MT5_...              # 同上 6 段
LOGIN_IP_MT4_LIVE2_...        # 同上 6 段
```

⚠️ **运维提醒**：任何 `LOGIN_IP_*_PASSWORD` 含有 shell 特殊字符（`$` / `!` / `"` / `\`
/ 反引号）时 **必须用单引号包裹整段值**，否则 `docker compose --env-file` 展开时
会把 `$xxx` 当变量吃掉，导致 FTP 静默 530 失败。改密码时若要发 ⚠️ 告警邮件但没
收到，优先检查这一点。详细排障见 [dev-prod-guide.md](../deployment/dev-prod-guide.md#环境变量特殊字符转义)。

## 8. 前端页面

路由 `/login-ips`，组件拆分：

```
frontend/src/pages/LoginIPs.tsx                    # shell（4 个 Tab 切换）
frontend/src/pages/login-ip/
  ├── types.ts                    # 镜像后端 Pydantic schema
  ├── useVerification.ts          # 验证码 hook
  ├── VerificationDialog.tsx      # 验证码输入弹窗
  ├── ReportTab.tsx               # Tab 1 每日报告（shadcn Table）
  ├── WatchlistTab.tsx            # Tab 2 监控账户（shadcn Table + 白名单弹窗）
  ├── SearchTab.tsx               # Tab 3 搜索（AG-Grid + 异步 CSV）
  └── OperationsTab.tsx           # Tab 4 运维（调度审计 + 邮件收件人）
```

关键 UI 约定：

- **Tab 1 / Tab 2 表格**：shadcn `<Table>`，黑底白字表头（`bg-black [&_th]:text-white`），圆角通过父 `overflow-hidden` + `[&_th:first-child]:rounded-tl-xl` 实现。深色模式沿用主题色变量。
- **Tab 1 关联展示**：关联账户在单元格内红色粗体 inline 显示，不再用绿色大块「无关联」提示。
- **Tab 1 登录状态 Badge**：三色（未登录灰 / 已登录无关联绿 / 已登录有关联红），文字只留「已登录」「未登录」。
- **Tab 2 批量新增**：两行响应式（Textarea 账号 / Select 服务器 + Input 备注，桌面 50/50，移动端堆叠），「新增（需邮箱验证）」。
- **Tab 2 白名单弹窗**：`Dialog` 展示允许收验证码的邮箱，请求 `/api/v1/login-ip/whitelist`（模块解耦，不复用 IB Financial 端点）。
- **Tab 3 搜索**：Textarea 多行 `terms` + Select `days` (1/3/7/15/30)，结果走 AG-Grid + 异步 CSV（轮询 `/export/tasks/{id}`）。
- **所有 fetch** 必走 `apiFetch`；`useEffect` 必带 `AbortController`。

验证码流：前端先 `POST /request-code` → 用户输邮件里的 6 位码 → `POST /verify-action` 带 `{email, code, action, payload}`。所有 watchlist 写操作共用同一个 `useVerification` hook + `VerificationDialog`。

## 9. 关键文件索引

**Backend**

- `app/api/v1/routes/login_ip.py` — 14 个 endpoint
- `app/schemas/login_ip.py` — Pydantic models
- `app/services/login_ip_ftp_service.py` — FTP/FTPS 下载
- `app/services/login_ip_analyzer_service.py` — 日志解析
- `app/services/login_ip_report_service.py` — 关联分析 + `build_structured_report()`
- `app/services/login_ip_search_service.py` — 手动搜索（含同 client_id 排除）
- `app/services/login_ip_export_service.py` — 异步 CSV 导出 (ThreadPoolExecutor + 懒清理)
- `app/services/login_ip_enrichment_service.py` — CRM MySQL 补中文名
- `app/core/login_ip_db.py` — SQLite CRUD + 建表
- `app/core/login_ip_scheduler.py` — APScheduler 2 个 job + 手动触发入口
- `scripts/migrate_login_ip_from_legacy.py` — 一次性迁移（旧 `monitoring.db` → 新 `login_ip.db`）

**Frontend**

见 §8 的组件结构。

**Docs**

- 本文：运行中系统的速查
- [login-ip_migration.md](./login-ip_migration.md) — 迁移全过程 + 踩坑
- `.cursor/rules/` — 无独立 rule（本模块未达到需要独立 skill 的复杂度）

## 10. 运维手册（常见问题）

| 症状 | 排查顺序 |
|------|---------|
| 08:30 没收到邮件 | 1. 运维 Tab 看 `login_ip_analyze_report_job` 是否 `succeeded` → 2. 查 `login_ip_mail_recipients` 是否 `is_active=1` → 3. 查 SMTP 凭据 |
| 报告显示某账号「未登录」但实际登录了 | 1. 02:00 download 是否成功 → 2. `data/login_ip/YYYYMMDD/` 有没有对应服务器的 JSON → 3. 日志里账号的服务器名是否和 `monitored_accounts.server_name` 完全一致（区分 `MT4_Live` vs `MT4_Live2`）|
| 关联账户没有中文姓名 | CRM MySQL 连接失败；看 `login_ip_enrichment_service` warning 日志 |
| 添加监控账户「验证码无效」 | 1. 收件人是否在 `admin_whitelist` → 2. Redis 是否在线 → 3. 距离发码是否超过 300 秒 |
| 修改 `.env` 密码后 FTP 530 | 密码含 `$` / `!` 等 shell 特殊字符但没加单引号 → 重新改成 `'...'` 再重启容器 |
| 重跑某一天的分析 | UI 运维 Tab → 「立即运行」 analyze_report + 填 `target_date` |

## 11. 废弃旧系统

旧项目路径：`/opt/myproject/log_analysis/46-MT-Server-Login-Detect/`

- 连续观察新系统跑通 7 天后（日报内容 diff 对齐）：
  1. `crontab -e` 删除 3 条 46-MT-Server-Login-Detect cron
  2. 打包旧代码 + 数据到 `/opt/archive/46-MT-Server-Login-Detect-YYYYMMDD.tar.gz`
  3. 删除原目录
- 旧 `monitoring.db` 在迁移时已 `INSERT OR IGNORE` 进新表，不再需要保留。
