# MT Login IP 监测项目 —— 迁移到 KCM IT System 的完整提示词手册

> 这是一份**给 AI 助手（Cursor / Claude / ChatGPT）使用的迁移提示词文档**。
> 你的目标是把独立项目 `46-MT-Server-Login-Detect`（基于 crontab + Jinja2 + SQLite 的
> 单体 FastAPI 服务）整体并入 `New-IT-System`（React + FastAPI + ClickHouse/MySQL/Redis +
> APScheduler 的分层 Web 平台）。
>
> 本文档包含：
>
> 1. 源项目全貌（业务 + 代码级细节）
> 2. 源 → 目标的映射决策
> 3. 新增页面 / API / 后台任务设计
> 4. **分阶段可直接拷贝给 AI 的提示词**（§7）
> 5. 数据迁移脚本草案
> 6. 验收清单与已知坑

---

## 0. 一句话目标

> **把"MT 服务器登录 IP 监测 + 关联账户告警"这条流水线
> （FTP 下载 → 日志解析 → 关联分析 → 邮件告警 + Web 查询）
> 从独立项目整体迁移到 KCM IT System，作为一个新业务模块存在**，
> 原项目下线（crontab 任务删除、目录归档）。

---

## 1. 源项目（`46-MT-Server-Login-Detect`）全貌

### 1.1 业务目的

监控 MT4 / MT5 交易服务器上**被关注账户**的每日登录 IP，通过 7 天历史 IP 识别
**共用 IP 的关联账户**（反小号 / 风控），命中则推送 HTML 邮件告警。
另提供一个轻量 FastAPI Web 面板，用于维护监控账户列表、查看历史报告、
按账号 / IP 手动搜索。

### 1.2 数据流

```
06:00  log_download.py          ├─ FTP/FTPS 下载 3 台服务器昨日日志
                                 └─ 清理 7 天前 .log + 30 天前 cron_*.log

08:30  log_login_analyzer.py    ├─ 流式解析 .log（按 \t 切分）
                                 ├─ 生成 3 个 JSON（ip_to_accounts / account_logins / raw_logins）
                                 └─ 把被监控账户的登录写入 SQLite login_history

08:35  send_report.py           ├─ 读 JSON + 近 7 天历史 IP 池
                                 ├─ 关联分析（命中则发邮件）
                                 └─ 通过 CRM MySQL 补中文名

FastAPI (main.py)                ├─ CRUD 监控账户
                                 ├─ 查看历史日期报告（复用 generate_html_report）
                                 └─ 手动搜索账号 / IP + CSV 导出
```

### 1.3 py 文件逐个说明

| 文件                                                | 作用                           | 关键细节                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                              |
| --------------------------------------------------- | ------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `log_download.py`                                   | FTP / FTPS 下载 + 清理         | 3 台服务器配置（MT4、MT5、MT4_Live2）。MT4_Live2 走 FTPS + `FTP_TLS_IgnoreHost`（复用 TLS session）。`cleanup_old_logs(7)` 删 `logs/YYYYMMDD/*.log` 并 rmdir 空目录。`cleanup_old_cron_logs(30)` 删 `cron_YYYYMMDD.log`。密码从 `.env` 读取（`MT4_PASSWORD` / `MT5_PASSWORD` / `MT4_LIVE2_PASSWORD`）。                                                                                                                                                                                                                               |
| `log_login_analyzer.py`                             | **核心解析引擎**               | 按服务器切换编码与列索引：**MT4 / MT4_Live2** UTF-8，IP=`parts[2]`、账号=`parts[3]`；**MT5** UTF-16-LE，IP=`parts[4]`、账号=`parts[5]`。预过滤：只保留含 `login` 且匹配 `'<id>': login` 或 `:\tlogin` 的行。IP 校验：含 `.` 或 `:`（天然兼容 IPv6）。账号从 `'123456': login...` 抽数字。**发现非监控但共 IP 的账户后，会对文件做第二次扫描**补抓原始日志；每账户最多保留 `MAX_LOGS_TO_STORE = 10` 条。输出：`analysis_ip_to_accounts.json` / `analysis_account_logins.json` / `analysis_raw_logins.json`，落在 `logs/YYYYMMDD/` 下。 |
| `send_report.py`                                    | 报告生成 + 邮件                | 从数据库取被监控账户 + 近 7 天 `login_history` 的 IP 池。遍历当日每个 IP → 若在历史 IP 池里 → 提取非监控的"关联账户"→ 映射回对应被监控账户。`generate_html_report()` 输出带 CSS 的 HTML：核心关联摘要 + 原始登录附录。邮件主题 `MT服务器关联账户登录警报 - YYYYMMDD`。**仅当 `any_correlation_found == True` 才发邮件。**                                                                                                                                                                                                             |
| `database.py`                                       | SQLite 封装（`monitoring.db`） | 两张表：`monitored_accounts (id, account_id UNIQUE, server_name, remarks)` 和 `login_history (id, account_id, ip_address, login_date, server_name)`。`get_historical_ips(days=7)` 返回 `{ip: {'last_seen': 'YYYYMMDD', 'accounts': [id,...]}}`。每次分析前 `cleanup_old_login_history(7)`。                                                                                                                                                                                                                                           |
| `search.py`                                         | 手动搜索 + CRM 补名            | `pymysql` 连 fxbackoffice MySQL（`.env` 中 `DB_HOST/DB_USER/DB_PASSWORD/DB_NAME`，与新平台**同库**）。SQL：`mt4_live.mt4_users u LEFT JOIN fxbackoffice.user_custom_fields cf ON u.ID=cf.userid AND cf.k='custom_chinese_name' WHERE u.LOGIN IN (...)`。按 `account_id` 搜索时**过滤掉"同 client_id 的相关账户"**（同一主体名下的自己账号不算关联）。按 `ip_address` 搜索不做过滤。                                                                                                                                                   |
| `email_utils.py`                                    | SMTP 封装                      | 读 `SMTP_SERVER / SMTP_PORT / USERNAME_MAIL / PASSWORD_MAIL / MAIL_SEND_TOO / MAIL_CCC`。**不要迁移到新平台——直接复用新平台的 `app/services/email_service.py`。**                                                                                                                                                                                                                                                                                                                                                                     |
| `main.py`                                           | 旧 FastAPI + Jinja2            | 路由：`/`（监控列表 + 近 7 日快速统计）、`POST /add`、`POST /delete/{id}`、`POST /update_remark/{id}`、`GET /search` / `POST /search`、`POST /export_csv`、`GET /history/{date_str}`。所有业务逻辑待迁移到新平台的 `routes/services`。                                                                                                                                                                                                                                                                                                |
| `update_crontab.py` / `test_historical_ip_setup.py` | 一次性脚本                     | 不迁移，归档。                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                        |

### 1.4 源目录数据（迁移时需要带走的）

```
monitoring.db                    # SQLite：配置 + 近 7 天登录历史
logs/YYYYMMDD/                   # 按日期的原始日志 + 3 个 JSON 缓存
archive/cron.log.*.gz            # 老 cron 归档（可丢）
.env                             # MT 密码、SMTP、CRM DB —— SMTP/DB 与新平台有重叠
crm-phone-monitor/               # 独立子项目，不属于本次迁移
```

### 1.5 环境变量（源 `.env`）

```
MT4_PASSWORD=...        # 必须迁移到新平台（新 key 名）
MT5_PASSWORD=...
MT4_LIVE2_PASSWORD=...
DB_HOST / DB_USER / DB_PASSWORD / DB_NAME   # CRM MySQL（与新平台 MYSQL_* 可能同库）
SMTP_SERVER / SMTP_PORT / USERNAME_MAIL / PASSWORD_MAIL
MAIL_SEND_TOO / MAIL_CCC
```

---

## 2. 目标平台（`New-IT-System`）架构约束

> 迁移必须遵循以下约定，不得新增顶层目录或打破分层：

- **分层**：`routes/` 只处理 HTTP；`schemas/` Pydantic；`services/` 业务逻辑 & DB；`core/` 配置 / 调度 / 中间件
- **数据库**：
  - **小型配置 + 状态** → 新建独立 SQLite（参照 `core/database.py`、`core/risk_monitor_db.py`、`core/client_return_export_db.py`）
  - **CRM / MT 数据** → 复用 MySQL 连接
  - 本模块**不需要** ClickHouse
- **定时任务** → **APScheduler**，参考 `core/scheduler.py` / `core/burst_open_scheduler.py`
- **邮件发送** → 复用 `app/services/email_service.py`
- **前端** → React 19 + shadcn/ui + AG-Grid；所有 `/api/*` 必须用 `apiFetch()`（自动带 `X-API-Key`）；`useEffect` 里必须 `AbortController`
- **侧边栏 / 标题** → `app-sidebar.tsx` + `site-header.tsx` titleMap 同步增加
- **API 基础路径** → `/api/v1/login-ip/*`
- **客户 / 员工过滤**：若后续要对"关联账户"附上客户画像，按平台规约排除 demo 账户（`GROUP NOT LIKE '%demo%'`）和员工（`users.isEmployee=0`）

---

## 3. 迁移总体方案（源 → 目标映射）

| 源                                    | 目标                                                               | 说明                                                                       |
| ------------------------------------- | ------------------------------------------------------------------ | -------------------------------------------------------------------------- |
| crontab 06:00 `log_download.py` + 08:30 `log_login_analyzer.py` | APScheduler job `login_ip_download_job`（每日 **02:00 HKT**）       | 合并：下载 + parse + 写 login_history + 清理 tmp。时区 HKT（ZoneInfo('Asia/Hong_Kong')）         |
| crontab 08:35 `send_report.py`        | APScheduler job `login_ip_analyze_report_job`（每日 **08:30 HKT**） | 用 `email_service` 发邮件；失败 / partial 都会发 ⚠️ 告警邮件                                     |
| `main.py` Web UI                      | React 页面（§4）+ FastAPI routes `/api/v1/login-ip/*`              |                                                                            |
| `monitoring.db`                       | `backend/data/login_ip.db`（两表结构不变）                         | 一次性迁移脚本见 §5                                                        |
| `logs/YYYYMMDD/*.json`                | `backend/data/login_ip/YYYYMMDD/*.json`                            | 历史保留；新 JSON 按新路径写                                               |
| `email_utils.py`                      | 删除，统一用 `email_service`                                       |                                                                            |
| `search.py` 中 CRM 查询               | 合并到新 `login_ip_enrichment_service`，复用平台 MySQL             |                                                                            |
| `.env` 新增                           | `LOGIN_IP_{MT4,MT5,MT4_LIVE2}_{HOST,PORT,USER,PASSWORD,REMOTE_DIR,USE_FTPS}` | 9 段 × 3 服务器；密码含 `$` 等特殊字符需用单引号包裹（见 §9.11）。与平台现有 `MYSQL_*` / SMTP 共用 |

---

## 4. 新增页面设计（前端）

### 4.x Login IP Monitor（主页面，命名 `LoginIPMonitor.tsx`）

**目的**：展示"被监控账户"的近 7 天登录情况与关联账户告警，并支持账户维护。

**三个 Tab**

1. **今日 / 指定日期报告**（默认选中）
   - 顶部日期选择器（默认昨日，可选 log 存在的最近 30 天）
   - 卡片式布局：每张卡片 = 一个被监控账户
     - 头部：`账号 ID` + `备注` + `服务器` + 当日是否登录
     - 当日总登录次数 / 使用 IP 数
     - **关联账户列表**：每个关联账户一行（展示 `账号（中文名）`、共享 IP 数、共享 IP 列表，标注是"当日"还是"历史 YYYYMMDD"）
   - 无关联时显示绿色"未发现关联账户"
   - **实现**：后端已生成的 HTML 不再使用；前端直接渲染结构化 JSON

2. **监控账户管理**
   - AG-Grid：`account_id` / `server_name` / `remarks` / 操作
   - 顶部表单：批量添加（账号 ID 支持多个，每行一个；服务器下拉 MT4 / MT5 / MT4_Live2；备注）
   - 行内编辑备注 + 删除
   - **考虑加入验证码**（参考 IB Financial 的 `request-code` / `verify-action` 流程），因为会改动风控白名单

3. **手动搜索**
   - 表单：搜索类型（账号 / IP）、搜索内容（支持多个，逗号 / 空格 / 换行分隔）、查询天数（1 / 3 / 7 / 14 / 30，默认 7）
   - 结果 AG-Grid：
     - 账号模式：`搜索账号` / `中文名` / `日期` / `服务器` / `登录 IP` / `登录次数` / `关联账号列表`
     - IP 模式：`搜索 IP` / `日期` / `服务器` / `登录账号列表`
   - CSV 导出（复用 Client Return Rate 的异步导出模式：create task → poll → download）

### 4.y Dashboard Widget（可选，二期）

在 `Home.tsx` 加一个"**近 7 天登录关联告警**"小部件，内容：

- 过去 7 天每天有多少"关联账户被发现"
- 点击跳转到 `LoginIPMonitor` 的对应日期

---

## 5. 数据迁移

### 5.1 SQLite schema（目标 `backend/data/login_ip.db`）

照搬源 schema（见 §1.3 `database.py`），再加索引：

```sql
CREATE INDEX IF NOT EXISTS idx_login_history_date ON login_history(login_date);
CREATE INDEX IF NOT EXISTS idx_login_history_ip   ON login_history(ip_address);
CREATE INDEX IF NOT EXISTS idx_login_history_acc  ON login_history(account_id);
```

### 5.2 一次性迁移脚本（草案）

目标放 `backend/scripts/migrate_login_ip_from_legacy.py`：

```python
# 把 /opt/myproject/log_analysis/46-MT-Server-Login-Detect/monitoring.db
# 的两张表原样复制到 backend/data/login_ip.db
# 再把 logs/YYYYMMDD/ 下的 JSON 拷贝到 backend/data/login_ip/YYYYMMDD/
```

### 5.3 JSON 缓存目录

原路径 `logs/YYYYMMDD/analysis_*.json`
新路径 `backend/data/login_ip/YYYYMMDD/analysis_*.json`
文件名保持不变：`analysis_ip_to_accounts.json` / `analysis_account_logins.json` / `analysis_raw_logins.json`

---

## 6. 后端实现清单（目录 & 文件）

```
backend/app/
├── api/v1/routes/
│   └── login_ip.py                    # 监控账户 CRUD / 报告 / 搜索 / 导出
├── schemas/
│   └── login_ip.py                    # MonitoredAccount / ReportResponse / SearchRequest 等
├── services/
│   ├── login_ip_ftp_service.py        # FTP/FTPS 下载（搬 log_download.py）
│   ├── login_ip_analyzer_service.py   # 解析（搬 log_login_analyzer.py 核心函数）
│   ├── login_ip_report_service.py     # 关联分析 + 渲染结构化结果（搬 send_report.py 逻辑，不再生 HTML）
│   └── login_ip_enrichment_service.py # CRM MySQL 补 chinese_name / client_id
├── core/
│   ├── login_ip_db.py                 # SQLite CRUD（搬 database.py）
│   └── login_ip_scheduler.py          # APScheduler 三个 job
└── data/
    ├── login_ip.db
    └── login_ip/YYYYMMDD/*.json
```

启动时在 `main.py` 注册 scheduler（参考 `burst_open_scheduler`），并在 `api/v1/routers.py` 引入 `login_ip` 路由。

### 6.1 API 清单（初版）

| Method | Path                                          | 作用                                                  |
| ------ | --------------------------------------------- | ----------------------------------------------------- |
| GET    | `/api/v1/login-ip/available-dates`            | 目前有 JSON 的日期列表                                |
| GET    | `/api/v1/login-ip/report?date=YYYYMMDD`       | 指定日期结构化报告（替代原 `history/{date}` 的 HTML） |
| GET    | `/api/v1/login-ip/watchlist`                  | 列出被监控账户（仅读；写操作走验证码流）              |
| POST   | `/api/v1/login-ip/search`                     | 账号 / IP 搜索（含同 client_id 过滤；返回 JSON）      |
| GET    | `/api/v1/login-ip/scheduler/runs`             | 最近 N 次调度运行记录（默认 30，上限 200）            |
| POST   | `/api/v1/login-ip/scheduler/run-now`          | 手动触发 `download` 或 `analyze_report`               |
| GET    | `/api/v1/login-ip/mail/recipients`            | 列出邮件收件人                                        |
| POST   | `/api/v1/login-ip/mail/recipients`            | 新增收件人                                            |
| DELETE | `/api/v1/login-ip/mail/recipients/{id}`       | 软删除收件人                                          |
| POST   | `/api/v1/login-ip/export/tasks`               | 异步导出搜索结果 CSV                                  |
| GET    | `/api/v1/login-ip/export/tasks/{id}`          | 导出任务进度                                          |
| GET    | `/api/v1/login-ip/export/tasks/{id}/download` | 下载 CSV                                              |
| GET    | `/api/v1/login-ip/whitelist`                  | 白名单邮箱只读列表（前端弹窗用；模块解耦入口）        |
| POST   | `/api/v1/login-ip/request-code`               | 向白名单邮箱发 6 位验证码（300s TTL）                 |
| POST   | `/api/v1/login-ip/verify-action`              | 核验验证码并执行 watchlist 写操作                     |

**验证码保护的写操作**（通过 `/verify-action` 的 `action` 字段区分）：

| action                       | payload                                   | 说明                          |
| ---------------------------- | ----------------------------------------- | ----------------------------- |
| `add_monitored_account`      | `{account_ids: int[], server_name, remarks?}` | 批量新增被监控账户，INSERT OR IGNORE 去重 |
| `update_monitored_account`   | `{id: int, remarks: string}`              | 仅改 remarks；account_id / server 不可变 |
| `delete_monitored_account`   | `{id: int}`                               | 硬删除一行 monitored_accounts |

验证码复用 IB Financial 的 **Redis** + **admin_whitelist 表**：
- Redis key：`login_ip_verify:{email}:{sha256(action)[:16]}`（与 IB Financial 的 `ib_fin_verify:` 前缀隔离）
- 白名单判断：内部 `ib_financial_service.is_whitelisted(email)`（共享 `admin_whitelist` 表）；对前端暴露独立入口 `GET /api/v1/login-ip/whitelist`，让 Login IP 模块不直接耦合 `/api/v1/ib-financial/*` 命名空间。若未来需要 Login IP 独立管理员集，复制一张 `login_ip_admin_whitelist` 表替换即可。

---

## 7. 分阶段 AI 提示词（直接拷贝使用）

> 每段提示词**独立可用**；按顺序逐段丢给 Cursor / AI，并在上下文里附带源项目 `46-MT-Server-Login-Detect/`
> 与目标项目 `New-IT-System/` 的路径（AI 可直接读文件）。

### 提示词 Phase 1 — 数据库 & 一次性迁移脚本

```
在 New-IT-System 中新增 login_ip 模块的 SQLite 层。

要求：
1. 在 backend/app/core/login_ip_db.py 中实现：
   - get_db_connection() 使用 sqlite3（参考 core/risk_monitor_db.py 模式）
   - create_tables()：monitored_accounts(id, account_id UNIQUE, server_name, remarks)、
     login_history(id, account_id, ip_address, login_date, server_name) + 3 个索引
   - get_monitored_accounts() -> dict[server_name, list[{account_id, remarks, id}]]
   - add_monitored_accounts(list[tuple])、delete_monitored_account(id)、
     update_remark(id, remarks)
   - add_login_history(list[tuple])、get_historical_ips(days=7)、cleanup_old_login_history(days=7)
   语义与源文件 /opt/myproject/log_analysis/46-MT-Server-Login-Detect/database.py 保持一致。
2. DB 文件路径：backend/data/login_ip.db（参考现有 risk_monitor.db 做法）
3. 在 backend/app/main.py startup event 里调用 login_ip_db.create_tables()。
4. 新增 backend/scripts/migrate_login_ip_from_legacy.py：
   - 从 /opt/myproject/log_analysis/46-MT-Server-Login-Detect/monitoring.db 读两张表
   - 写入 backend/data/login_ip.db（INSERT OR IGNORE）
   - 把 /opt/myproject/log_analysis/46-MT-Server-Login-Detect/logs/YYYYMMDD/*.json
     拷贝到 backend/data/login_ip/YYYYMMDD/*.json（只拷 analysis_* 的 3 个 JSON）
   - 打印迁移摘要：账户数 / 历史登录条数 / 拷贝文件数

完成后：写 python -m backend.scripts.migrate_login_ip_from_legacy 的运行说明到 docs/features/login-ip.md 的 §数据迁移 小节。
```

### 提示词 Phase 2 — FTP 下载 Service

```
把 /opt/myproject/log_analysis/46-MT-Server-Login-Detect/log_download.py 的下载能力
迁移到 backend/app/services/login_ip_ftp_service.py。

要求：
1. 保留 FTP_TLS_IgnoreHost / ReusedSslSocket 两个自定义类（MT4_Live2 FTPS session 复用必要）。
2. 对外暴露一个函数：
   def download_daily_logs(target_date: str, base_dir: str) -> dict[server_name, bool]
   - target_date 格式 YYYYMMDD
   - 从环境变量读 MT4_FTP_PASSWORD / MT5_FTP_PASSWORD / MT4_LIVE2_FTP_PASSWORD
     （在 backend/app/core/config.py 里加这三个字段）
   - 输出目录：{base_dir}/{target_date}/{target_date}_{server_name}.log
3. 再加一个 cleanup_old_log_dirs(base_dir: str, days_to_keep: int = 7):
   - 删 base_dir 下名称为 YYYYMMDD 且早于 cutoff 的目录里的 *.log
   - 目录清空后 os.rmdir
   - 静默跳过不需要处理的目录（参考源文件最新实现，不要打印 Checking old directory）
4. 用 logging.getLogger(__name__) 替换所有 print。

不要再去搬 cleanup_old_cron_logs：新平台日志走 logging，不存在 cron_*.log 文件。
```

### 提示词 Phase 3 — 分析 Service

```
把 /opt/myproject/log_analysis/46-MT-Server-Login-Detect/log_login_analyzer.py 的核心
analyze_log_file() 搬到 backend/app/services/login_ip_analyzer_service.py。

关键语义（禁止丢失）：
- MT4 / MT4_Live2：UTF-8 编码，IP=parts[2]、账号=parts[3]
- MT5：UTF-16-LE 编码，IP=parts[4]、账号=parts[5]
- 过滤条件：line 含 "login"，并且含 "': login" 或 ":\tlogin"
- IP 校验：含 "." 或 ":"（兼容 IPv6）
- 账号抽取：acc_part.split("':")[0].strip("'")，且 isdigit()
- 发现非监控但共 IP 的账户后，需要对文件做第二次扫描补抓原始日志
- 每账户 raw log 截断到 MAX_LOGS_TO_STORE=10

对外暴露：
def analyze_date(target_date: str, base_dir: str) -> dict
 - 调 login_ip_db.get_monitored_accounts() 拿监控列表
 - 解析 {base_dir}/{target_date}/ 下的所有 *.log
 - 把 3 份结果写成 analysis_ip_to_accounts.json / analysis_account_logins.json / analysis_raw_logins.json
 - 把当日被监控账户的 (account_id, ip, date, server) 写入 login_history
 - 返回摘要 {server: {total_logins, monitored_logins, unique_ips}}

不要自己实现 sys.exit / argparse，只暴露函数。日志用 logging。
```

### 提示词 Phase 4 — 关联分析 + 邮件

```
把 /opt/myproject/log_analysis/46-MT-Server-Login-Detect/send_report.py 的关联分析
重构到 backend/app/services/login_ip_report_service.py。

产出两个函数：
1. build_report_data(target_date: str, base_dir: str) -> dict
   - 载入当日 3 份 JSON
   - 调 login_ip_db.get_historical_ips(7)
   - 返回结构化 report：
     {
       'date': target_date,
       'any_correlation_found': bool,
       'accounts': [
         {
           'account_id': str, 'server_name': str, 'remarks': str,
           'logged_in': bool, 'total_logins': int,
           'logins_by_ip': {ip: count},
           'correlated': [
             {
               'account_id': str, 'chinese_name': str | None, 'client_id': int | None,
               'shared_ips': [{'ip': str, 'historical_date': str | None}]
             }
           ]
         }
       ]
     }
   - 用 login_ip_enrichment_service.get_account_details() 补中文名 / client_id
   - 复用源 search.py 的"过滤同 client_id"逻辑：关联账户与被监控账户同 client_id 的要排除

2. send_daily_report(target_date: str) -> None
   - 调 build_report_data
   - 若 any_correlation_found == False：直接返回，不发邮件
   - 否则用 app/services/email_service.py 发 HTML
   - HTML 渲染可以直接复用源 send_report.py 的 generate_html_report() 代码（CSS + 卡片结构），
     但把输入改成新的结构化 dict
   - 主题：MT服务器关联账户登录警报 - {target_date}
   - 收件人从环境变量或新 SQLite 表读（参考 IB Financial Monitor 的 config 做法，允许 UI 配置）

不要再新建 email_utils.py，必须复用 email_service.py。
```

### 提示词 Phase 5 — APScheduler 两个 Job（已实现）

> ⚠️ 本节已根据实际落地更新。原始设计有 3 个 job（下载 / 分析 / 发报），最终
> 实现合并为 2 个 job（下载阶段内联做 parse+写 login_history）。

```
在 backend/app/core/login_ip_scheduler.py 里用 APScheduler 注册 2 个 daily job，
参考 core/scheduler.py 的 ZoneInfo('Asia/Hong_Kong') 用法。

时区：HKT（与 docker-compose 的 TZ=Asia/Hong_Kong 一致）

Jobs（target_date 默认 = HKT yesterday）:
1. login_ip_download_job  每日 02:00 HKT
   - 调 download_daily_logs(target_date, TMP_ROOT)
   - 调 analyze_date(target_date, log_dir=TMP_ROOT, out_dir=DATA_DIR)
     （顺便写 login_history）
   - 调 cleanup_old_log_dirs(TMP_ROOT, days_to_keep=7)
2. login_ip_analyze_report_job  每日 08:30 HKT
   - 调 send_daily_report(target_date, dry_run=False)

保障：
- 每次运行在 login_ip_scheduler_runs 表插 1 行审计：
  (job_name, target_date, started_at, finished_at, status, summary_json, error_msg)
- 任何异常 → 立即发 ⚠️ 告警邮件给 login_ip_mail_recipients 里 active='to' 的收件人
- "partial" 状态也发告警（比如 3 个 server 只下成功 2 个）
- 每个 job 有独立 threading.Lock，避免 "run now" API 与 cron 重入
- 对外暴露 trigger_download_now() / trigger_report_now() 供 Phase 6 API 调用

ENV:
- LOGIN_IP_SCHEDULER_ENABLED=false 可在 dev 环境关停（默认 true）

在 backend/app/main.py lifespan 里调 start_login_ip_scheduler() / stop_login_ip_scheduler()。
```

### 提示词 Phase 6 — Routes + Schemas（已实现）

> ⚠️ 本节已根据实际落地更新。共 14 个 endpoint 注册到 `/api/v1/login-ip/*`，
> 全部通过本地冲烟测试（异步 CSV 导出端到端验证通过）。
> 2026-04-23 补丁：新增 `GET /whitelist` 只读端点（前端白名单弹窗去耦合 IB Financial）。

```
在 backend/app/ 新增 routes / schemas，暴露 §6.1 API 清单里的全部 endpoints。

要求：
1. schemas/login_ip.py：
   - MonitoredAccountOut / MonitoredAccountBatchCreate(account_ids: list[int], server_name, remarks)
   - MonitoredAccountUpdate（仅 remarks 可改）
   - ReportCorrelationEntry / ReportIPSharedAnalysis / ReportAccountItem / ReportResponse
   - AvailableDatesResponse
   - SearchRequest(search_type: Literal['account_id','ip_address'], terms: list[str], days: int)
   - SearchResponse（results / not_found / error 三态合一，避免三种 shape 的 Union）
   - MailRecipientOut / MailRecipientCreate
   - SchedulerRunOut / SchedulerRunsResponse / SchedulerRunNowRequest
   - RequestCodeRequest / VerifyActionRequest
   - ExportTaskCreateRequest / ExportTaskCreateResponse / ExportTaskStatusResponse

2. api/v1/routes/login_ip.py （单文件聚合，~380 行）：
   - router = APIRouter(prefix="/login-ip")
   - 所有读路径不保护；所有 watchlist 写路径只通过 /verify-action 暴露（见下方 4）
   - GET /report?date= 无数据 → 404；date 非 YYYYMMDD → 400
   - 路径计算：_DATA_BASE_DIR = Path(__file__).resolve().parents[4] / "data" / "login_ip"
     （routes/ → v1/ → api/ → app/ → backend/，parents[4] 才是 backend）

3. services + core 新增：
   - services/login_ip_search_service.py     # 移植 legacy search.py；含同 client_id 过滤
   - services/login_ip_export_service.py     # 异步 CSV 导出（ThreadPoolExecutor + 懒清理）
   - core/login_ip_db.py                     # 新增 login_ip_export_tasks 表 + 7 个 CRUD 函数
                                             # 并入既有 login_ip.db，不另开 DB 文件
   - services/login_ip_report_service.py     # 新增 build_structured_report()：
                                             # 把 build_report_data 的 defaultdict/set 结构
                                             # 扁平成前端可直渲的 JSON，含中文名 enrichment

4. 验证码保护（复用 IB Financial 的 Redis + admin_whitelist）：
   - POST /request-code           {email, action} → 查 admin_whitelist → 403/200
                                  Redis key: login_ip_verify:{email}:{sha256(action)[:16]}
                                  TTL 300s；一次性消耗
   - POST /verify-action          {email, code, action, payload?}
                                  action 白名单：
                                  - add_monitored_account    payload: MonitoredAccountBatchCreate
                                  - update_monitored_account payload: {id: int, remarks: str}
                                  - delete_monitored_account payload: {id: int}

5. 在 api/v1/routers.py 注册：
   from .routes.login_ip import router as login_ip_router
   api_v1_router.include_router(login_ip_router, tags=["login-ip"])
```

**实际落地 14 个 endpoint**：

```
GET    /api/v1/login-ip/available-dates        # 有 JSON 的日期列表（newest first；skip tmp/）
GET    /api/v1/login-ip/report?date=YYYYMMDD   # 结构化报告（Tab 1）
GET    /api/v1/login-ip/watchlist              # 被监控账户列表（Tab 2，只读）
POST   /api/v1/login-ip/search                 # 手动搜索（Tab 3）
GET    /api/v1/login-ip/scheduler/runs         # 调度审计时间线（ops 面板）
POST   /api/v1/login-ip/scheduler/run-now      # 手动触发 download / analyze_report
GET    /api/v1/login-ip/mail/recipients        # 收件人列表
POST   /api/v1/login-ip/mail/recipients        # 新增收件人
DELETE /api/v1/login-ip/mail/recipients/{id}   # 软删除收件人
POST   /api/v1/login-ip/export/tasks           # 异步 CSV 导出：create
GET    /api/v1/login-ip/export/tasks/{id}      # poll status
GET    /api/v1/login-ip/export/tasks/{id}/download  # 下载 CSV（utf-8-sig，Excel 直开）
GET    /api/v1/login-ip/whitelist              # 白名单邮箱只读列表（前端弹窗用）
POST   /api/v1/login-ip/request-code           # 验证码基建：发码
POST   /api/v1/login-ip/verify-action          # 验证码基建：执行 watchlist 写
```

**关键决策与坑**：

- **watchlist 写操作不单独开 endpoint**：不暴露 `POST/PATCH/DELETE /watchlist[/{id}]`，避免绕过验证码。所有写都走 `/verify-action`，路由层强制单点。
- **admin_whitelist 表复用 IB Financial**：同一批风控管理员管多个模块；若未来需要 Login IP 独立白名单，复制一张 `login_ip_admin_whitelist` 即可替换。
- **Redis key namespace**：`login_ip_verify:` vs IB Financial 的 `ib_fin_verify:` 隔离，两模块的验证码不会互相污染。
- **export task DB 并入 login_ip.db**：加 `login_ip_export_tasks` 表而非新建 `login_ip_export.db`，减少磁盘 DB 文件数量；schema 字段与 `client_return_export_db` 对齐，便于将来统一清理策略。
- **CSV 编码**：`utf-8-sig`（带 BOM），Excel 直接打开中文不乱码；`correlated_accounts` 列表用 `"; "` 拼接成单元格字符串。
- **懒清理**：每次 create / status / download 都触发一次 `_cleanup()`，succeeded 过期任务 → `expired` 态 + 删文件；超保留窗口（默认 7 天）的 terminal 任务 → 删行。
- **并发**：`ThreadPoolExecutor(max_workers=1)` 默认单线程，通过 `LOGIN_IP_EXPORT_MAX_WORKERS` 调；避免多导出同时打 MySQL 做 enrichment。
- **路径坑（已修）**：routes 文件在 `backend/app/api/v1/routes/`，要算到 `backend/` 需要 `parents[4]`，最初写成 `parents[3]` 落到 `app/` 导致 `available-dates` 返回 0 个。


### 提示词 Phase 7 — 前端页面（已实现）

> ⚠️ 本节已根据实际落地更新。最终文件结构是 `frontend/src/pages/LoginIPs.tsx`
> (shell) + `frontend/src/pages/login-ip/` 子目录拆 4 个 Tab 组件，比提示词
> 里的单文件方案更便于维护；中途把 Tab 1（日报告）和 Tab 2（监控账户）
> 从 AG-Grid 改成 shadcn `<Table>`，原因是数据量 < 200 行且需要深度定制
> 表头样式（黑底白字 + 圆角裁剪），AG-Grid 主题覆盖性价比低。
> 只有 Tab 3（搜索结果）保留 AG-Grid，利用它的分页 + CSV 快速筛选能力。

```
页面入口: frontend/src/pages/LoginIPs.tsx
  ├─ 4 个 Tab: 每日报告 / 监控账户 / 搜索 / 运维
  └─ 内部 child: frontend/src/pages/login-ip/
       ├── types.ts                ← 镜像 schemas/login_ip.py 的所有 interface
       ├── useVerification.ts      ← 邮箱验证码 hook（request-code → verify-action）
       ├── VerificationDialog.tsx  ← 验证码输入弹窗（UI 等价于 IBFinancialMonitor 内嵌）
       ├── ReportTab.tsx           ← Tab 1：每日报告（shadcn Table）
       ├── WatchlistTab.tsx        ← Tab 2：监控账户（shadcn Table + 白名单弹窗）
       ├── SearchTab.tsx           ← Tab 3：账号/IP 搜索（AG-Grid + 异步 CSV 导出）
       └── OperationsTab.tsx       ← Tab 4：调度审计 + 邮件收件人（无验证码）

新增基础设施：
- frontend/src/components/ui/textarea.tsx（项目原本缺这个 shadcn 组件）

路由 & 导航（已经在早期 Phase 加好，本阶段无改动）：
- App.tsx 的 lazy 路由 /login-ips
- app-sidebar.tsx 菜单项（风控分组 + Fingerprint 图标）
- site-header.tsx titleMap（i18n 零散补齐）
```

**Tab 1（每日报告）关键落地**：

- 列: MT 账号 / 服务器 / 是否登录 / 备注 / 登录次数 / IP 数 / IP 明细 / 关联账户（含姓名）
- “是否登录” Badge 三态色（未登录灰 / 已登录绿 / 已登录 + 有关联红），文案简化为「已登录」或「未登录」
- 表头黑底白字 (`bg-black [&_th]:text-white`)，通过父 `overflow-hidden` + `[&_th:first-child]:rounded-tl-xl` 修复圆角被裁剪问题（dark mode 下同样生效）
- 「无关联账户」日不渲染绿色提示卡（用户反馈太抢眼），关联账户改为在表格单元格内红色粗体 inline 显示
- 数据源：`GET /available-dates` + `GET /report?date=`

**Tab 2（监控账户）关键落地**：

- 批量新增表单：两行响应式布局
  - 第 1 行 `<Textarea rows={4}>`（多账号，一行一个）
  - 第 2 行 `grid md:grid-cols-2` — 左服务器 Select、右备注 Input（桌面 50/50，移动端堆叠）
- 「新增（需邮箱验证）」按钮 → 唤起 `VerificationDialog` → `request-code` → `verify-action` (`action=add_monitored_account`)
- 「白名单邮箱」按钮 → shadcn `<Dialog>` 展示允许收码的邮箱列表（当前请求 **`GET /api/v1/login-ip/whitelist`**；早期版本复用 IB Financial 端点，2026-04-23 已切换）
- 列表区改用 shadcn `<Table>`（AG-Grid 换下）：每行有 Input + 「保存备注」按钮（同样走验证码），删除按钮走验证码
- 表头样式与 Tab 1 对齐（黑底白字 + 圆角）

**Tab 3（搜索）关键落地**：

- 表单：RadioGroup (account_id / ip_address) + `<Textarea>` (terms，多行 → `terms: string[]`) + Select (days: 1/3/7/15/30)
- 结果仍走 AG-Grid（支持列排序 + 客户端筛选）
- CSV 导出是异步任务：`POST /export/tasks` → 轮询 `GET /export/tasks/{id}` → `GET /export/tasks/{id}/download` 拉 blob，前端触发下载（`ClientReturnRate.tsx` 同款模式）

**Tab 4（运维）关键落地**：

- 左：调度运行记录（展示最近 30 条 `login_ip_scheduler_runs`）+ 「立即运行」按钮（`POST /scheduler/run-now`，`job=download|analyze_report`）
- 右：邮件收件人列表（`GET /mail/recipients`）+ 新增 + 软删除
- 整个 Tab 不走验证码（读为主，写操作只是运维内部用；需要时后续再追加保护）

**共享验证码流程**：

- `useVerification(action, payload)` hook 统一管理 `stage`（idle / requesting / waiting_code / verifying / done）
- 所有 watchlist 写操作（add / update remark / delete）复用同一个 `VerificationDialog`，只传入 `action` + `payload` 区分意图
- Redis key 前缀：`login_ip_verify:` 与 IB Financial 的 `ib_fin_verify:` 完全隔离

**前端-后端 schema 对齐踩坑**：

- `SearchRequest` 后端要 `terms: List[str]` + `days: int`，前端最初按照提示词里的 `searchTerm` + 日期范围实现，上线前重写为 `<Textarea>` + `<Select>` 对齐后端契约
- 异步导出 task 的 `row_count=0` 在后端被错误序列化成 `None` → 修 `_status_payload` 函数（见 Phase 6 踩坑记录）

**本 Phase 修复 / 优化列表**：

1. Tab 名称 `日报告` → `每日报告`
2. 「日期 / 刷新」控件上下 padding 对齐（`px-4 py-4 md:px-6`）
3. Tab 1 从 Card 列表重构为 Table
4. Tab 1 删掉始终为空的「姓名」列
5. Tab 1 黑色表头 + 白色文字 + 圆角修复（`overflow-hidden` + 首尾 `<TableHead>` 圆角）
6. Tab 1 删掉「未发现关联账户」绿色大块状提示
7. Tab 1 「是否登录」Badge 三色简化（未登录灰 / 绿 / 红）
8. Tab 2 批量新增表单改为响应式两行
9. Tab 2 按钮去掉 `IconPlus`
10. Tab 2 刷新按钮迁到「监控账户列表」header（改名「刷新列表」）
11. Tab 2 新增「白名单邮箱」弹窗
12. Tab 2 AG-Grid → shadcn Table（与 Tab 1 风格一致）
13. Tab 2 备注保存改为行内「保存备注」按钮（不再 inline edit）
14. **2026-04-23**: 白名单弹窗改走独立端点 `/api/v1/login-ip/whitelist`（去耦 IB Financial）

### 提示词 Phase 8 — 文档 + 旧项目下线

```
1. 新建 docs/features/login-ip.md，包含：
   - 业务背景
   - 数据流 & APScheduler 调度
   - API 清单（引用 §6.1）
   - SQLite schema
   - .env 新增字段
   - 数据迁移（§5）
   - 对旧项目 46-MT-Server-Login-Detect 的废弃说明
2. 在 docs/ai-context/project-context.md 的 §4 Core Business Modules 里
   新增一节（照 §4.8 Trade Real-time Monitor 的格式）：Login IP Monitor。
3. 在 .cursor/rules 目录增加（可选）一个 login-ip skill，列出解析关键规则
   （MT4 vs MT5 列位置、过滤条件等）以方便后续 AI 调试。
4. 旧项目下线步骤（写成运维 README）：
   - crontab -e 删除 3 条 46-MT-Server-Login-Detect 的任务
   - 确认新平台已连续跑 7 天
   - 把 /opt/myproject/log_analysis/46-MT-Server-Login-Detect/ 打包归档
   - 原子删除或移到 /opt/archive/
```

---

## 8. 验收清单

- [ ] `backend/data/login_ip.db` 已建表 + 索引，旧 `monitoring.db` 数据迁入
- [ ] `backend/data/login_ip/YYYYMMDD/` 下有 JSON（迁移 + 新 job 生成）
- [ ] `.env` 新增 `MT4_FTP_PASSWORD` / `MT5_FTP_PASSWORD` / `MT4_LIVE2_FTP_PASSWORD`
- [ ] APScheduler 3 个 job 在启动时被注册（`/docs` 能看到 scheduler 状态或有日志）
- [ ] 手动触发 `POST /api/v1/login-ip/scheduler/run-now?step=download` 成功
- [ ] Tab 1 能看到昨日报告；关联账户中文名正确（verifying 搜一个已知客户）
- [ ] Tab 2 能批量新增监控账户；删除 / 改备注正常
- [ ] Tab 3 账号搜索排除了同 client_id 的自己账号；CSV 导出 UTF-8 + BOM 能直接 Excel 打开
- [ ] 若无关联，邮件**不发送**；有关联则邮件格式与旧系统一致
- [ ] 旧项目 crontab 三条任务已删除，`46-MT-Server-Login-Detect` 目录已归档
- [ ] `docs/features/login-ip.md` + `project-context.md` 已更新

---

## 9. 已知坑 & 注意事项

1. **MT5 日志编码**必须 `utf-16-le`，而且**列位置和 MT4 不同**，迁移时很容易漏 `MT5` 分支。测试前塞一条假 MT5 日志做单元测试。
2. **关联分析的"历史 IP 池"依赖 `login_history` 表**，新平台首次运行时表是空的 → 需要迁移脚本把旧 `login_history` 带过来，否则头 7 天都告警不出。
3. **第二次扫描（re-scan）补抓原始日志**是为了非监控账户，不要漏掉这一步，否则邮件附录里会出现 `N/A`。
4. **CEN 账户**：本模块不涉及金额，无需 `/100`。但列 `chinese_name` 的 MySQL 查询 **跨库 JOIN**（`mt4_live.mt4_users` × `fxbackoffice.user_custom_fields`），确认新平台的 MySQL 用户有这两个库的权限。
5. **"过滤同 client_id"**（源 `search.py` 特有）迁到新平台时**必须保留**，否则会误报同主体名下的账号。
6. **APScheduler 的时区**：源 crontab 跑在服务器本地时区（UTC+8）。新平台的容器默认 UTC；已在 Phase 5 通过 ① docker-compose `TZ=Asia/Hong_Kong` 统一系统时间，② APScheduler 显式 `timezone=ZoneInfo('Asia/Hong_Kong')`，③ `target_date = now(HKT) - 1day` 的"昨天"口径。改过 MT 所在时区后要同步改这三处。
7. **邮件验证码**：Watchlist 属于风控白名单，Phase 6 已实现 6 位码 + Redis 5 分钟 TTL + 管理员白名单机制，**复用 IB Financial 的 `admin_whitelist` 表**（同一批风控管理员管多个模块）。Redis key prefix 用 `login_ip_verify:` 与 IB Financial 的 `ib_fin_verify:` 隔离。所有 watchlist 写操作只通过 `POST /verify-action` 暴露，不开独立 `POST/PATCH/DELETE /watchlist` endpoint，防止绕过验证码。
8. **FTPS TLS session 复用**：`FTP_TLS_IgnoreHost` + `ReusedSslSocket` 是 MT4_Live2 独有需求，是因为对端 FTPS 实现不支持 session resume 的标准行为——不要"优化"成普通 `ftplib.FTP_TLS`。
9. **.log 文件体积**：单日可达几百 MB（尤其 MT5），**必须流式读**（`for line in f`），不要 `f.read()`。
10. **新旧并行期**：建议新平台先"只读不发邮件"跑 3 天，核对结果与旧系统一致后再切换收件人，最后下线老 crontab。
11. **`.env` 密码里的 `$` / `#` / `)` 等特殊字符**：`python-dotenv` 对裸值会做变量展开，对双引号会做展开，**只有单引号包裹的值是字面量**。`LOGIN_IP_*_PASSWORD` 必须写成 `LOGIN_IP_MT5_PASSWORD='q^N5UcM&6$jSTy8e'`（实测 `$jSTy8e` 不加单引号会被当成未定义变量而截断）。
12. **`login_history` 首日冷启动**：旧 `monitoring.db` 的 `login_history` 表**实际是空的**（旧项目从未持久化过登录历史）。§9.2 提到的"把旧表带过来"→ 换作 `backend/scripts/populate_login_history_from_json.py`：从 backfill 生成的 `analysis_account_logins.json` 里抽监控账户的登录条目回灌 `login_ip.db`，否则新系统头 7 天关联告警全为空。
13. **定时任务失败可见性**：APScheduler job 只 log 是不够的，运维不会每天看。Phase 5 已实现：① 每次运行在 `login_ip_scheduler_runs` 表留一行审计（started_at/finished_at/status/summary/error）；② 任何异常或 partial 下载立刻发 ⚠️ 告警邮件给 `login_ip_mail_recipients`。Phase 6 的页面会展示最近 30 次运行时间线。

---

## 10. 使用本文档的建议

- 把本文件放进 `New-IT-System/docs/ai-context/login-ip-migration.md`
- 每次让 AI 执行迁移时，在 prompt 前附一句：
  > "参考 `docs/ai-context/login-ip-migration.md` §<阶段编号>，并读取源项目 `/opt/myproject/log_analysis/46-MT-Server-Login-Detect/` 下对应文件后，开始执行。"
- 一个阶段跑完，人工验收 + 跑对应 §8 清单里的几项，再进下一阶段
- 所有阶段跑完 + 并行 3~7 天后，才执行 §7 Phase 8 的旧项目下线步骤
