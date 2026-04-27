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
│  05:10 HKT  login_ip_download_job                         │
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
| MT4 Live / MT4 Live2 / MT5 登录日志 | FTP/FTPS (3 台) | 每日 05:10 HKT 拉 `YYYYMMDD.log*`（见 §3.1 时序） |
| `monitored_accounts` | `login_ip.db` | 运营维护的被监控账户（通过 Tab 2 或 REST 写库） |
| `login_history` | `login_ip.db` | 过去 7 天每 (account, ip, day, server) 的登录记录 |
| `admin_whitelist` | `it_system.db` (IB Financial) | IB Financial 等模块的验证码白名单；**Login IP 监控账户写操作不再经此表** |
| CRM MySQL (`KCM_fxbackoffice.users`) | MySQL Slave | 关联账户中文姓名 enrichment |
| `login_ip_mail_recipients` | `login_ip.db` | 收日报/告警的 `to` / `cc` 列表 |
| `login_ip_scheduler_runs` | `login_ip.db` | 调度审计（UI 运维 Tab 展示） |
| `login_ip_export_tasks` | `login_ip.db` | 搜索 CSV 异步导出任务状态 |

MT4 vs MT5 日志格式小差异（MT5 多几列），`login_ip_analyzer_service.py` 里按服务器
类型走不同 `split` 路径，统一成 `{account_id, ip_address, login_date, server_name}`。

### 3.1 MT5 journal 日志：列结构、第 4 列语义、与 Login IP 的关系

本模块日常只解析 **login 行**；MT5 同一文件里还包含 **成交/订单/强平** 等事件，对后续 **AB 仓 / 对敲** 类扩展有参考价值。

| 项 | 说明 |
|----|------|
| 编码 | **UTF-16-LE**（与 MT4 的 UTF-8 不同），行尾 **CRLF**。解析必须用**文本方式**按 `utf-16-le` 读；若用**二进制按 `\n` 切行**，会错位导致整行解码失败。 |
| 分隔 | 制表符 `\t` 分列；**至少 6 列**才有完整「时间 + 第 4 列 + 消息体」。 |
| [0] | 短码（可忽略，用于行首分类）。 |
| [1] / [2] | level、thread 等。 |
| [3] | **MT 服务器本地时间** `HH:MM:SS.mmm`（与业务约定的 MT 时区一致，例如 UTC+3 时**不做额外换算**）。 |
| [4] | **语义随事件类型变化**，这是最容易误解的一列。 |
| [5]+ | 消息正文；登录行形如 `'<account>': login (...)`；交易相关常有 `'<account>': order performed ...` / `deal performed ...` 等。 |

**第 [4] 列三种常见形态**

1. **合法 IPv4**：多见于客户 **login、logout** 以及大量 **order / deal / modify / position / buy / sell** 行。此时 [4] 可视为「与本次事件关联的**客户端**出口 IP」（与 `login` 行规则一致、位置相同）。
2. **空**（两截 `\t\t` 之间无内容）：常见于 **dealer/服务器内部**触发的成交流（如止损/止盈激活后的路由），或部分 **order/deal** 行，表示**没有可归因到终端客户的 IP**。
3. **非 IP 字符串**（如 `DealerLogic769`、`StopOut.All`、`Leverate MT5 Trading Plugin`）：表示 **子系统/模块名**，不是公网地址。`StopOut.*` 与 **强制平仓/爆仓** 链相关，通常不应当作「客户当时所在的 IP」。

**统计级参考**（在单日全量 20260423 样本上跑过 `backend/scripts/probe_mt5_log_for_ip.py` 探测；不同日波动属正常）：

- `login` / `logout`：第 4 列 **几乎恒为** IPv4。
- `order performed` / `deal performed`：**约八成左右** 行在 [4] 为 IPv4；**其余** 多为 [4] 空（服务端触发链），不是「少抓了 IP」而是事件性质不同。
- `modify`、挂单行 `buy`/`sell`（limit/stop）、`position modified`：绝大多数 [4] 为 IPv4。
- `close position ... by stopout`、`stopout at ...%`：第 4 列多为 **`StopOut.All`** 等 logger，**不是客户 IP**。

**与 MySQL 的关系**

- `fxbackoffice.mt4_trades` 等表 **不存逐笔 IP**；IP 在 **MT 服务器 journal 日志**里。要做「某笔 order 的 IP」，以日志为准；库表用于**盈亏/品种/手数/时间**对齐。

### 3.2 抓取与解析注意点

| 技巧 | 原因 |
|------|------|
| 下载等 **MT 日界切换之后** 再拉取（见下方与 `05:10` HKT 的对应关系） | 与 `YYYYMMDD.log` 的**日切**一致；过早拉会拿到**未写完**的当日文件。生产调度已用 **HKT 05:10** 下载**昨天**的日志。 |
| 大文件**流式**读（`for line in f`），勿一次性读入内存 | 单日 MT5 日志可达数 GiB。 |
| 用 **probe 脚本** 先看分布再写业务解析 | `backend/scripts/probe_mt5_log_for_ip.py`：按行统计「动作词」、各桶第 4 列是否 IPv4、并打样本，避免凭印象猜列。 |
| 若做 AB/对敲，优先盯 **`deal performed` / `order performed` 行** 再回库对账 | 这两类行在样本中带客户 IP 的比例高、字段里含**账号、方向、手数、品种、订单/成交号**；需 profit 时再用 DB 的 `mt4_trades` 关联合并。 |
| 区分 **客户主动** vs **系统/条件单触发** | [4] 为空的 `order`/`deal` 多为**服务端/路由**产生；**强平/爆仓** 与 `StopOut` 行一组，[4] 常不是客户公网 IP。业务规则上宜**单独分桶**（见下）。 |

**HKT 05:10 与 MT 日界（为何不是凌晨 2 点拉）**  
MT 使用例如 **UTC+3** 的「MT 日」时，**MT 00:00 = HKT 05:00**。若在 **HKT 02:00** 去拉 `YYYYMMDD.log`，对应 MT 仍在前一日 **21:00–24:00**，文件**尚未日切/仍在写入**，会漏掉**当天 MT 日末**一段记录。`05:10` 给 **日切后约 10 分钟** 缓冲，兼顾 FTP 落盘延迟。

### 3.3 AB 仓与「系统强制平仓 / 强平」要不要算在一起？

**风控上常说的 AB 仓 / 对敲**一般指：**同一（或协同一批）控制人**，用**多个账户**，在**相近时间、相同品种、有意为之的反向仓位**，以套取返利、洗亏或规避规则；**典型证据链是「人」层面的协同**，例如同 IP 上的主动下单、或可与 IP/设备关联的主动操作链。

**纯系统强平/爆仓**（`StopOut.All`、连续 `close position ... by stopout` 等）是 **保证金/风控规则触发的清盘**：

- 日志里往往 **没有可归因的客户端公网 IP**（第 4 列是子系统名或空），**不能**和「该客户此刻从哪个 IP 点下单」混为一谈。
- 这类成交**不体现「双方自愿同时建对冲单」的意图**；用它们去和另一边的「主动单」做 **同 IP 对敲**会引入大量**假阳性**（行情波动、连锁爆仓、流动性枯竭都会偶然形成一亏一赚）。

**建议规则（可写进产品/策略）**

| 做法 | 说明 |
|------|------|
| **默认在 AB/对敲模型中排除**「仅因 stopout/强平链闭合」产生的成交/平仓行，或单列为 **「强平不纳入对敲评分」** | 与「同 IP 主动开平仓」**分层**，报表更清晰。 |
| **保留在「资金结果」或事后复盘**里 | 强平仍会产生真实亏损与对手方盈利，**会计与风控总览**可能需要，但与「对敲**意图**」分开统计。 |
| **边界情况由业务拍板** | 若一方**主动**建仓、对家因**波动被强平**，仍可能形成结构上的**一亏一赢**；是否算「AB」取决于你们是否只关心**双边主动对敲**还是**任意一方含被动平仓**。这不在技术层一次判定，需要 **risk team 定口径**。 |

**一句话**：**系统强制平仓这一侧，通常不当作 AB 仓对敲的「主证据」**；与 **客户主动、带 IP 的 order/deal** 分开看，最不容易误导一线。

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
| `login_ip_download_job` | `05:10` 每日 (HKT) | FTP 拉日志 + 解析 + 写 `login_history` | 发 ⚠️ 告警邮件；留 tmp 供手动 retry |
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
- `scripts/analyze_shared_ip_cross_account.py` — 跨客户 IP 共享离线分析（见 §11）
- `scripts/probe_mt5_log_for_ip.py` — **只读**探测 MT5 日志中各事件第 4 列是否为 IP（见 §3.1、§3.2）

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
| 报告显示某账号「未登录」但实际登录了 | 1. 05:10 download 是否成功 → 2. `data/login_ip/YYYYMMDD/` 有没有对应服务器的 JSON → 3. 日志里账号的服务器名是否和 `monitored_accounts.server_name` 完全一致（区分 `MT4_Live` vs `MT4_Live2`）|
| 关联账户没有中文姓名 | CRM MySQL 连接失败；看 `login_ip_enrichment_service` warning 日志 |
| Tab 2 写操作 401/403 | 查平台 `X-API-Key` 是否配置正确（与其它 API 相同，不再单独验邮箱码） |
| 修改 `.env` 密码后 FTP 530 | 密码含 `$` / `!` 等 shell 特殊字符但没加单引号 → 重新改成 `'...'` 再重启容器 |
| 重跑某一天的分析 | UI 运维 Tab → 「立即运行」 analyze_report + 填 `target_date` |
| 运维 Tab 看到某条 `running` 行挂了好几天 | 正常应该 <5min；6h 后会被 `reap_stuck_running_runs` 自动改成 `failed`，`error_msg` 末尾带 `[reaped: stuck in running past 6h]` — 搜 backend log 里这条行的 `run_id` 定位崩溃原因（OOM / docker restart 最常见） |
| 想查更早的调度记录（>90 天） | 默认只留 90 天；改 `DEFAULT_SCHEDULER_RUN_RETENTION_DAYS` 即可，短期可直接 `sqlite3 backend/data/login_ip.db` 手工拉备份 |

## 11. Ad-hoc 分析脚本

脚本 `backend/scripts/analyze_shared_ip_cross_account.py` —— **跨客户 IP 共享**离线分析工具，不在生产流程里。

### 11.1 做什么

把任意日期区间（支持单日 / N 天 / 自定义 start-end）的 `analysis_ip_to_accounts.json` 聚合成 **"哪些 IP 被不同 MT 账户共用"** 的视图，回答风控/合规的一类典型问题：

> 同一个 IP 下面挂着哪些 CRM 客户？`client_id` 分别是谁？是本人的多账户还是多个不同客户？

**纯只读**：不碰数据库，只读 `backend/data/login_ip/YYYYMMDD/analysis_ip_to_accounts.json`，CRM MySQL 也只查 `mt4_users` 拿账号→userId 映射。

### 11.2 关键 flag

| flag | 作用 |
|------|------|
| `--date YYYYMMDD` | 单日快捷方式（最常用） |
| `--days N` | 过去 N 天滚动窗口（默认 14） |
| `--start / --end` | 自定义区间 |
| `--known-accounts-only` | 只保留在 `fxbackoffice.mt4_users.userId IS NOT NULL` 的真实客户账户，同时给 CSV/XLSX 补上 `client_id` + `chinese_name` 列。**强烈推荐开**，能过滤掉 demo/内部/测试账号 |
| `--per-server` | 按 MT4 / MT5 / MT4_Live2 分组输出，否则三台合并成 `ALL` |
| `--min-account-id` | 过滤 server-internal 低位账号（默认 1000） |
| `--top-k` | 终端打印 top-K 最乱的 IP |
| `--xlsx` | 输出 **Excel（推荐给同事看）** |
| `--csv` | 输出 flat CSV，一行一 (IP, 账户) |
| `--csv-pivot` | 输出 pivoted CSV，一行一 IP |

### 11.3 XLSX 文件布局（给风控/同事的主交付物）

两个 Sheet，自带**合并单元格 / 冻结首行 / 严重程度色块**：

**Sheet 1「按 IP 分组」** — 每行一个 `(IP, 客户)` 对：

```
| IP (vmerged) | Client ID | 中文名 | MT 账户 | 服务器 | 客户数 (UIDs) | 账户数 | 出现天数 |
```

- A 列 `IP` 垂直合并：同一 IP 下的多个客户同属一个"块"，视觉上自成一组
- 每换一个 IP 自动切换背景色（白 / 浅灰条纹），方便滚动扫描
- `客户数 (UIDs)` 列按严重程度上色：
  - 🔴 `≥10` 个不同 CRM 客户共用同一 IP —— **高优先级**
  - 🟠 `5–9` 个
  - 🟡 `3–4` 个
  - 无色 = `2` 个
- 排序：`UIDs DESC → 账户数 DESC → IP → client_id`，打开默认头几行就是最可疑的

**Sheet 2「IP 汇总」** — 每行一个 IP：

```
| IP | 服务器 | 客户数 | 账户数 | 出现天数 | 首日 | 末日 | 账户列表 | Client IDs | 中文名 |
```

账户列表 / Client IDs / 中文名都用 `|` 分隔。给搜索 / 过滤 / 透视用。

### 11.4 常用命令

```bash
cd /opt/myproject/New-IT-System/backend
source .venv/bin/activate

python scripts/analyze_shared_ip_cross_account.py \
    --date 20260423 \
    --known-accounts-only --per-server \
    --xlsx scripts/shared_ip_20260423.xlsx \
    --csv  scripts/shared_ip_20260423_flat.csv
```

过去两周 summary（不落盘，只打印 top 20）：

```bash
python scripts/analyze_shared_ip_cross_account.py --days 14 --top-k 20
```

### 11.5 解读示例

`124.220.165.142`（腾讯云出口）4/23 单日：**41 个 MT 账户 / 38 个不同 UID**。

- `38 UID / 41 账户 = 0.93` → 基本每个账户一个不同客户，**只有 3 个账户是同一客户的多账号**（例如 `冯延宁` 开了 3 个 MT4 账号）
- 38 个不同 CRM 客户共用一个腾讯云 IP → 典型"代理池 / 机场出口"嫌疑
- 下一步：查 IP ASN 归属（腾讯云 IDC 段 vs 中国电信家宽）+ 运维/风控决定是否加入忽略白名单

对比 `125.85.8.239` 4/23 单日：**26 账户 / 23 UID** → 3 个重复账户属同客户，剩下 23 人共用 → 量级小但仍需人工核查。

### 11.6 产物不进 git

`backend/scripts/shared_ip_*.{csv,xlsx}` 都是**含客户 PII** 的衍生文件，已在 `.gitignore` 全局排除 `*.csv` / `*.xlsx`。跑完发给同事或归档后可以本地删除，脚本随时能重跑。

## 12. 废弃旧系统

旧项目路径：`/opt/myproject/log_analysis/46-MT-Server-Login-Detect/`

- 连续观察新系统跑通 7 天后（日报内容 diff 对齐）：
  1. `crontab -e` 删除 3 条 46-MT-Server-Login-Detect cron
  2. 打包旧代码 + 数据到 `/opt/archive/46-MT-Server-Login-Detect-YYYYMMDD.tar.gz`
  3. 删除原目录
- 旧 `monitoring.db` 在迁移时已 `INSERT OR IGNORE` 进新表，不再需要保留。
