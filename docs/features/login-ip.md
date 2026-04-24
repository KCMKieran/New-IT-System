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
 FastAPI /api/v1/login-ip/*（15 个 endpoint）
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
| `monitored_accounts` | `login_ip.db` | 运营维护的被监控账户（通过 Tab 2 或 REST 写库） |
| `login_history` | `login_ip.db` | 过去 7 天每 (account, ip, day, server) 的登录记录 |
| `admin_whitelist` | `it_system.db` (IB Financial) | IB Financial 等模块的验证码白名单；**Login IP 监控账户写操作不再经此表** |
| CRM MySQL (`KCM_fxbackoffice.users`) | MySQL Slave | 关联账户中文姓名 enrichment |
| `login_ip_mail_recipients` | `login_ip.db` | 收日报/告警的 `to` / `cc` 列表 |
| `login_ip_scheduler_runs` | `login_ip.db` | 调度审计（UI 运维 Tab 展示） |
| `login_ip_export_tasks` | `login_ip.db` | 搜索 CSV 异步导出任务状态 |

MT4 vs MT5 日志格式小差异（MT5 多几列），`login_ip_analyzer_service.py` 里按服务器
类型走不同 `split` 路径，统一成 `{account_id, ip_address, login_date, server_name}`。

## 4. API 清单

Base: `/api/v1/login-ip/*`，全部走平台的 `X-API-Key` 中间件（`apiFetch` 自动附带）。

**读路径**

| Method | Path | 说明 |
|--------|------|------|
| GET | `/available-dates` | 目前 `data/login_ip/` 下有 JSON 的日期（newest first） |
| GET | `/report?date=YYYYMMDD` | 指定日期的结构化报告（Tab 1 直渲） |
| GET | `/watchlist` | 被监控账户 flat 列表（Tab 2 表格） |
| POST | `/search` | 批量搜索 `{search_type, terms: string[], days}`（Tab 3） |
| GET | `/scheduler/runs?job=&limit=` | 最近 N 条调度记录（上限 200） |
| GET | `/mail/recipients?active_only=` | 收件人列表（UI 编辑用） |

**写路径 / 任务触发**（与平台其它 API 一样依赖 `X-API-Key`；无邮箱验证码层）

| Method | Path | 说明 |
|--------|------|------|
| POST | `/watchlist` | 批量新增监控账户，body: `MonitoredAccountBatchCreate`（`account_ids`, `server_name`, `remarks?`） |
| PATCH | `/watchlist/{id}` | 只改 `remarks`（body: `{ "remarks": string \| null }`） |
| DELETE | `/watchlist/{id}` | 硬删除一条 `monitored_accounts` |
| POST | `/scheduler/run-now` | `{job: "download"\|"analyze_report", target_date?}` 手动跑 |
| POST | `/mail/recipients` | 新增收件人（运维内部） |
| DELETE | `/mail/recipients/{id}` | 软删除收件人 (`is_active=0`) |
| POST | `/export/tasks` | 创建异步 CSV 导出（搜索结果） |
| GET | `/export/tasks/{id}` | 轮询 status |
| GET | `/export/tasks/{id}/download` | 下载 CSV（`utf-8-sig`，Excel 直开）|

> **历史说明**：曾通过 `POST /request-code` + `POST /verify-action` 做邮箱双因子；该流已于 2026-04-24 移除。若需恢复，参见 `docs/features/login-ip_migration.md` 中 Phase 6 旧描述。

## 5. 定时任务

| Job name | Cron (HKT) | 作用 | 失败行为 |
|----------|-----------|------|---------|
| `login_ip_download_job` | `02:00` 每日 | FTP 拉日志 + 解析 + 写 `login_history` | 发 ⚠️ 告警邮件；留 tmp 供手动 retry |
| `login_ip_analyze_report_job` | `08:30` 每日 | 关联分析 + 发日报 + 每日 SQLite 清理 | 同上；写 `login_ip_scheduler_runs.status='failed'` |

- 并发保护：`threading.Lock`，同一 job 不会重入
- 时区硬编码 `ZoneInfo('Asia/Hong_Kong')`，跟 `docker-compose.prod.yml` 的 `TZ=Asia/Hong_Kong` 一致
- 可通过 `LOGIN_IP_SCHEDULER_ENABLED=false` 在 dev 关停调度
- 手动触发入口：UI 运维 Tab → 「立即运行」 或 `POST /scheduler/run-now`

### 5.1 每日 SQLite 清理（`_daily_housekeeping`）

`_report_job` 的 `finally` 里会跑三个清理动作，无论本次日报成功还是失败都执行。
清理失败被吞进日志（`[housekeeping] failed`），不影响主任务的审计记录。

| 动作 | 函数 | 保留窗口 | 说明 |
|------|------|---------|------|
| 清 `login_history` 过期行 | `cleanup_old_login_history(days=7)` | 7 天 | 与关联分析回看窗口一致；迁移后一度未接入，此次补齐 |
| 清 `login_ip_scheduler_runs` 老行 | `cleanup_old_scheduler_runs(days=90)` | 90 天 | 每日约 2 条 cron + 少量手动，90 天 ≈ 200 行，UI 看「上季度出过什么问题」足够 |
| 回收僵尸 `running` 行 | `reap_stuck_running_runs(hours=6)` | 超 6h | 进程被 kill / OOM 导致 `record_run_finish` 没跑，行永远停在 `running`；统一标记为 `failed` 并在 `error_msg` 追加 `[reaped: ...]` 方便区分 |

> 默认常量定义在 `backend/app/core/login_ip_db.py`：`DEFAULT_HISTORY_RETENTION_DAYS` / `DEFAULT_SCHEDULER_RUN_RETENTION_DAYS` / `DEFAULT_STUCK_RUN_HOURS`。调保留窗口改这三个常量即可，不用动调度器。

## 6. SQLite Schema

单文件 `backend/data/login_ip.db`，5 张表：

- `monitored_accounts(id, account_id UNIQUE, server_name, remarks)`
- `login_history(id, account_id, ip_address, login_date, server_name)` + 3 个索引（account/ip/date）
- `login_ip_mail_recipients(id, email, role, is_active, remarks, created_at)`
- `login_ip_scheduler_runs(id, job_name, target_date, started_at, finished_at, status, summary_json, error_msg)`
- `login_ip_export_tasks(id, status, search_type, terms_json, days, requested_ip, row_count, file_path, ...)`

建表逻辑在 `backend/app/core/login_ip_db.py`，`main.py lifespan` 启动时执行 `create_tables()`。

**保留策略**（每日 08:30 报告任务里顺带清理，见 §5.1）：

| 表 | 保留 | 理由 |
|----|------|------|
| `login_history` | 7 天 | 关联分析只回看 7 天 |
| `login_ip_scheduler_runs` | 90 天 + 僵尸 `running` 超 6h 自动收尾 | UI 审计窗口；防止崩溃进程留下永远不结束的行 |
| `login_ip_export_tasks` | 由 `login_ip_export_service` 自己的 `list_export_tasks_for_cleanup` 懒清理 | 已有独立路径 |
| `monitored_accounts` / `login_ip_mail_recipients` | 永久 | 是配置，不是日志 |

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
  ├── ReportTab.tsx               # Tab 1 每日报告（shadcn Table）
  ├── WatchlistTab.tsx            # Tab 2 监控账户（shadcn Table；POST/PATCH/DELETE `/watchlist`）
  ├── SearchTab.tsx               # Tab 3 搜索（AG-Grid + 异步 CSV）
  └── OperationsTab.tsx           # Tab 4 运维（调度审计 + 邮件收件人）
```

关键 UI 约定（**新页面也必须遵守**，详见 [`.cursor/skills/page-style-conventions/SKILL.md`](../../.cursor/skills/page-style-conventions/SKILL.md)）：

- **Card 间距**：`<Card className="gap-3">` + `<CardHeader>`（不写 `pb-3`）→ 标题到内容稳定 12px。直接 `<Card>` 默认 `gap-6` + `pb-3` = 36px 空白，禁止。
- **Tab 1 / 2 / 4 表格**：必须 `<div className="overflow-hidden rounded-xl border bg-card">` 包裹；表头一律 `bg-black [&_th]:font-semibold [&_th]:text-white [&_th:first-child]:rounded-tl-xl [&_th:last-child]:rounded-tr-xl`。`rounded-md` 是历史遗留，已统一为 `rounded-xl`。
- **Tab 3 表格**：AG-Grid，独立样式系统（`bg-black` header 由 CSS 变量 `--ag-header-background-color` 实现），见 `SearchTab.tsx` 的 `gridThemeStyle`。
- **按钮等宽**：Card 底部并排的主/副按钮组父容器加 `[&>button]:min-w-[112px] flex flex-wrap`，杜绝「搜索 vs 导出 CSV」长度差导致的视觉参差。Card header 里的辅助「刷新」用 `size="sm"`，不进按钮等宽组。
- **行内图标按钮**：`h-8 w-8 p-0`，variant=`ghost`，颜色 `text-destructive hover:text-destructive`（统一 8×8，曾出现过 7×7 不一致）。
- **Tab 1 关联展示**：关联账户在单元格内红色粗体 inline 显示，不再用绿色大块「无关联」提示。
- **Tab 1 登录状态 Badge**：三色（未登录灰 / 已登录无关联绿 / 已登录有关联红），文字只留「已登录」「未登录」。
- **Tab 2 批量新增**：两行响应式（Textarea 账号 / Select 服务器 + Input 备注，桌面 50/50，移动端堆叠），「**新增**」→ `POST /watchlist`。
- **Tab 2 行内操作**：`保存备注` → `PATCH /watchlist/{id}`；删除 → `DELETE /watchlist/{id}`。无白名单/验证码弹窗。
- **Tab 3 搜索**：Textarea 多行 `terms` + Select `days` (1/3/7/15/30)，结果走 AG-Grid + 异步 CSV（轮询 `/export/tasks/{id}`）。

### 8.1 Tab 3 手动搜索：上次结果客户端缓存

切换至其它页面再回来，`SearchTab` 会重新挂载。为避免「上一次成功搜索」的表单与表格被清空，前端将**最后一次成功落盘的响应**缓存在**当前浏览器标签页**的 `sessionStorage` 中。

| 项 | 说明 |
|----|------|
| 实现 | `frontend/src/lib/login-ip-search-cache.ts`：`loadLoginIpSearchCache` / `saveLoginIpSearchCache`；`SearchTab.tsx` 挂载时从缓存恢复 `searchType`、`termsText`、`days`、`rows`、`statusMsg`，在 `POST /search` **返回并更新 UI 后**调用 `save` |
| 不写入缓存 | 请求抛错、网络失败等（保留上一次成功结果，不覆盖为错误态） |
| 生命周期 | `sessionStorage`：关闭该标签即失效；不跨标签页、不落服务端 |
| 体积极限 | 单条 JSON 约 4.5MB 上限，超出则放弃写入并 `console.warn`（防撑爆存储） |
| 多用户/会话 **隔离** | 缓存 key 含 `localStorage` 的 `auth_token`：不同 token（换账号登录）使用不同 key，互不覆盖 |
| 登出清理 | `frontend/src/providers/auth-provider.tsx` 在 `logout` 时调用 `clearAllLoginIpSearchCaches()`，删除所有 `login-ip-manual-search:` 前缀项，减少同机下一账号复用同标签的残留数据 |

> 若将来后端 JWT 暴露稳定 `sub`（用户 ID），可改为 key 中优先用用户 ID，语义比「按 token 分桶」更直观；当前以 token 区分已能隔离不同登录会话。

### 8.2 通用 UI 约定（按钮 / Card / Table）

完整规则参见 [`.cursor/skills/page-style-conventions/SKILL.md`](../../.cursor/skills/page-style-conventions/SKILL.md)。本模块本身就是该 skill 的样板实现：

- **按钮**：主/副操作并排时父容器 `[&>button]:min-w-[112px] flex flex-wrap`；辅助 / 行内按钮 `size="sm"`。
- **Card**：`<Card className="gap-3">` + 不写 `pb-3`，标题到内容固定 12px。
- **Table**：黑底白字表头 + `rounded-xl` 圆角 wrapper + `overflow-hidden`。
- **图标按钮**：`h-8 w-8 p-0`，统一 8×8。

新页面必须遵循；改本模块的代码也必须先看 skill 再动手。

- **所有 fetch** 必走 `apiFetch`；`useEffect` 必带 `AbortController`。

## 9. 关键文件索引

**Backend**

- `app/api/v1/routes/login_ip.py` — 15 个 endpoint
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

见 §8 的组件结构。Tab 3 手搜缓存：`frontend/src/lib/login-ip-search-cache.ts`；登出清理见 `auth-provider.tsx`。

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
| Tab 2 写操作 401/403 | 查平台 `X-API-Key` 是否配置正确（与其它 API 相同，不再单独验邮箱码） |
| 修改 `.env` 密码后 FTP 530 | 密码含 `$` / `!` 等 shell 特殊字符但没加单引号 → 重新改成 `'...'` 再重启容器 |
| 重跑某一天的分析 | UI 运维 Tab → 「立即运行」 analyze_report + 填 `target_date` |
| 运维 Tab 看到某条 `running` 行挂了好几天 | 正常应该 <5min；6h 后会被 `reap_stuck_running_runs` 自动改成 `failed`，`error_msg` 末尾带 `[reaped: stuck in running past 6h]` — 搜 backend log 里这条行的 `run_id` 定位崩溃原因（OOM / docker restart 最常见） |
| 想查更早的调度记录（>90 天） | 默认只留 90 天；改 `DEFAULT_SCHEDULER_RUN_RETENTION_DAYS` 即可，短期可直接 `sqlite3 backend/data/login_ip.db` 手工拉备份 |

## 11. 废弃旧系统

旧项目路径：`/opt/myproject/log_analysis/46-MT-Server-Login-Detect/`

- 连续观察新系统跑通 7 天后（日报内容 diff 对齐）：
  1. `crontab -e` 删除 3 条 46-MT-Server-Login-Detect cron
  2. 打包旧代码 + 数据到 `/opt/archive/46-MT-Server-Login-Detect-YYYYMMDD.tar.gz`
  3. 删除原目录
- 旧 `monitoring.db` 在迁移时已 `INSERT OR IGNORE` 进新表，不再需要保留。
