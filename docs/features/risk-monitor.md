# 交易实时监控 — Trade Real-time Monitor

> 可扩展的实时风控监控平台，覆盖 MT4 + MT5 全部交易服务器。
>
> **Agent Skill**: `.cursor/skills/risk-monitor/SKILL.md` (精简版，写代码时优先读 Skill)
>
> **历史归档**: [risk-monitor-archive.md](./risk-monitor-archive.md) — 已完成的开发计划、探索性 SQL、已移除规则设计、已实施调研记录
>
> **变更记录**:
> - 2026-05-12（同日两次迭代）Gap Trade Tab **UI 对齐 + 日志增强**：
>   - **UI 重构**：AG-Grid 切到 `risk-monitor-theme h-[420px]` + `gridOptions={{ theme: "legacy" }}`，跟前 3 个 tab 完全一致；时间筛选改成 `<Select>` 下拉（与其他 tab 同款 `h-9 sm:w-40`），选项 = 今天 / 昨天（默认）/ 最近 3 天 / 7 天 / 30 天 / 自定义；检测 A 首列从 `⚠` 图标改成「**是否同 IP**」（"是"琥珀色 / "否"灰色，整行同时黄色高亮保持不变）；删除「同 IP 强信号」summary 卡片（信息已由整行高亮 + 列表达）；删除「刷新」按钮（数据每天 02:05 才更新一次，刷新无意义；切 Tab 自动 fetch）；抽出 `renderLoginSidLink()` 共用渲染函数。
>   - **日志增强**：`rule_gap_trade_so_service` / `rule_gap_trade_gap_service` 新增 5 条 INFO（detect 开始 + SQL 行数 + IP enrich 摘要 + net_deposit 命中率 + done 耗时）+ 1 条 funnel 统计（gap-profit 的 "clients=N profitable=N dropped(no_deposit, low_deposit, below_threshold) → N alerts"），出问题时可一眼判断是 SQL 没数据 / IP 文件全缺 / 阈值卡死 / 净入金过滤掉了。Scheduler 最终汇总日志拆出 SO+AB 与 gap-profit 各自计数。
> - 2026-05-12 **Gap Trade Tab 上线（第 4 个 Tab，rule_ids 71 / 81）**：休市开盘缺口监控，每个工作日 MT 02:05 cron 扫描前 2h 窗口（MT 00:00–02:00）。两个子检测同一 Tab 内分两段表展示：
>   - **检测 A** `rule_id = 71` — SO + AB 配对（W04 `Azure_Function_BAU/W04_Blowup_Audit_Weekly.py` 移植）：找窗口内 COMMENT 前缀 `[so` / `so:` / `cso:` 的强平 L 腿，配同 symbol/反向/开仓 ±300s/手数 0.5–2× 的 C 腿，约束 **同 groupsid 但 userid 不同**（跨客户串通）。读取 `backend/data/login_ip/<YYYYMMDD>/analysis_ip_to_accounts.json`，L 与 C 在持仓期间共享 IP → 前端整行黄色高亮。
>   - **检测 B** `rule_id = 81` — 按 `userid` 聚合窗口内**纯平仓** P&L（CEN ÷100 后），`total_profit / net_deposit_hist ≥ 1×` **或** `total_profit ≥ $1000` 任一满足即触发，`triggered_by` 字段记录是哪个条件命中。
>
>   前端按天筛选（Today / Yesterday 默认 / 3d / 7d / 30d / Custom date range），点击行打开右侧 Sheet 详情面板（移动端从底部弹出）。`GapTradeConfig` 单行 JSON 存于 SQLite；scheduler 走独立 `CronTrigger(day_of_week="mon-fri", hour=2, minute=5, timezone="Etc/GMT-3")`，**不提供"立即扫描"按钮**（数据每天只更新一次）。CSV 导出列宽容两个子规则的字段。详见 [Skill](../../.cursor/skills/risk-monitor/SKILL.md) §"Rule 4: Gap Trade"。
> - 2026-05-08 **三 Tab 页眉操作区移动端适配**：`RiskMonitor.tsx` 使用 `RISK_MONITOR_HEADER_ROW` / `RISK_MONITOR_HEADER_ACTIONS`（窄屏纵向堆叠 + 操作按钮 `flex-wrap`），避免「导出 CSV / 规则配置 / 立即扫描 / 刷新浮动盈亏」在单行 `flex` 下与 `Button` 的 `shrink-0` 组合导致横向溢出；根容器与 Tabs 增加 `min-w-0`。详见 [risk-monitor-reusable-patterns.md §11](./risk-monitor-reusable-patterns.md)。
> - 2026-03-26 移除 Scale-In 规则代码，Tab 2 曾改为「缺口交易」占位；**后已移除该 Tab**（当前仅 **批量下单 + 快开快平** 两 Tab，见 §5）。
> - 2026-04-23 归档已完成历史章节到 [risk-monitor-archive.md](./risk-monitor-archive.md)。
> - 2026-04-28 (commit 9713e3f) 前端轮询从硬编码 30s 改为跟随后端 `scan_interval_min`，并同步刷新本文档 §1 / §3 / §4 / §5 / §6 中残留的旧规则（Scale-In / Frequent Open / Batch-Close / `/scan` API）描述，让主文档与 Burst Open v2 实际实现一致。
> - 2026-04-29 **批量下单 Tab UI 对齐快开快平**：每条规则一张紧凑 SummaryCard（`by_rule` 聚合）；工具栏增加「全部规则 / Rule n」筛选（仅作用于表格与导出，不影响卡片）。`/burst-open/alerts/stats` 返回 `by_rule`。详见 [risk-monitor-reusable-patterns.md §11](./risk-monitor-reusable-patterns.md)。
> - 2026-05-07 **快速获利 Tab 上线**（Phase 1）：第三个 Tab，按账户聚合滑动窗口 P&L（已实现 + 可选浮动）超过阈值即告警。`rule_id` 区间 61-70；新增 6 个 API（含 `/quick-profit/floating-refresh` 浮动轻量刷新）；前端 `PositionStatusBadge` 三态彩色 + AG-Grid `applyTransaction` 局部更新。Phase 2（入金比例规则 A2）延期。详见下方 §7「Quick Profit Detection」。
> - 2026-05-07 **三 Tab 加 Net Deposit 列**：`account_enrichment.get_net_deposit_hist_map` 提供 **client-level** 历史净入金（按 `userId` 聚合后映射回 loginsid，过滤 `sid IN (1,2,5,6)` + `GROUP NOT LIKE '%demo%'`，公式与 client-return-rate "历史净入金" 一致：`SUM(deposit) + SUM(withdrawal + ib withdrawal)`，CEN 自动 ÷100）。监控目标是 account-level，但展示的净入金是该客户名下所有合规账户的总值——同一客户的多个被告警 loginsid 拿到相同数。前端 ≥0 绿 / <0 红 / null 显示 "—"，仅展示，不参与触发。同步下线快速获利 Tab 的 1d/7d/30d 入金/出金 6 列与 `deposit_enrichment.py`。
> - 2026-05-07 hotfix（同日两处）：(a) 第一版 SQL 用 `JOIN ON mu.userId = st.userId` + `GROUP BY mu.loginsid`，**形式上**已是 client-level 但缺 demo/sid 过滤，改为标准嵌套查询（外层 target → loginsid，内层 client-level 聚合再 LEFT JOIN）。(b) PyMySQL 占位符坑：`LIKE '%demo%'` 必须写成 `'%%demo%%'`，否则 mogrify 会把 `%` 当作格式化指令并报 `TypeError: %d format: a real number is required, not str`，扫描失败 → `net_deposit_hist` 全部写 NULL。
> - 2026-05-07（同日 hotfix）快速获利 Tab 三个问题：
>   1. **CEN 归一化后阈值过滤失效** — `scan_quick_profit` 里 `norm_rules.id` 错用了 SQLite 主键（1,2…），与 detect 输出的 alert.rule_id（61,62…）对不上，导致二次阈值过滤回退到 0，CEN 账户出现一堆几十 USD 甚至负数的低值告警。改为 `QUICK_PROFIT_RULE_ID_BASE + idx`。
>   2. **浮动 P&L 30s 自动轮询去掉** — 改成工具栏「刷新浮动盈亏」按钮，用户主动点才刷，避免后台异步把告警快照覆盖成低值/负数。同时把跨扫描去重从「绝对时间桶」改成「时间差 ≤ lookback_min」，避免跨桶边界（如 11:55 / 12:01）泄漏重复告警。
>   3. **Dedup 在多次扫描间静默失效** — `rule_quick_profit_detect` 输出的 alert dict 没有 `scanned_at` 字段（`append_scan_and_events` 单独传入到 SQL），下次扫描读 `_latest_result.alerts` 作为 prev_alerts 时全部 scanned_at=None，新版 dedup `if scanned is None: continue` 直接跳过 → prev_latest 永远是空 → dedup 无效。修复：detect 给每条 alert 写 `scanned_at`。同时在 `_run_scan` 开头若 `_latest_result` 为空（重启场景），用 `get_recent_quick_profit_alerts(max_lookback_min)` 从 SQLite 加载最近告警 seed prev pool，让 dedup 跨进程重启也工作。
> - 2026-05-12 hotfix（Quick Profit 4h 内同账号重复告警）— 两层根因，同日一并修复：
>   1. **运维层（L1）**：dev + prod 两个后端容器**共享同一份 SQLite**（`backend/data/risk_monitor.db`）但**各跑各的 APScheduler**，写入互相不可见。dev compose 加 `BURST_SCAN_ENABLED=false` + `GAP_TRADE_SCAN_ENABLED=false`，dev 退化为「只读 + 手动 `/scan-now`」，唯一的周期扫描归 prod 负责。
>   2. **代码层（L2）**：`_run_scan` 里 SQLite seed 的条件 `if not any(rule_id >= 61 in prev_alerts)` 只在 in-memory 完全没 QP 告警时才补种；只要本轮有任何**新** QP 告警进入 `_latest_result.alerts`，下一轮 prev_alerts 就丢掉所有**更早的**同 key 告警 → 它们在仍处于 lookback 窗口时复发。改为**无条件 seed**：抽出 `_build_quick_profit_prev_alerts` helper，每轮都把 `get_recent_quick_profit_alerts(max_lookback)` 合并进 prev_alerts，由 `_dedup_by_time_bucket` 按 key 取最新 `scanned_at`；新增 `tests/test_burst_open_scheduler_prev_alerts.py` 5 条回归测试钉住"新+老 QP 共存"、"冷启动"、"无 QP 规则"、"SQLite 失败"、"非 QP 透传"五条路径。

**文档与代码谁为准**：Tab 列表与页面结构以 `frontend/src/pages/RiskMonitor.tsx` 为准；`rule_id` 区间以 `backend/app/api/v1/risk_monitor.py`（`BURST_RULE_MAX_ID` / `QUICK_RULE_ID_BASE`）及检测服务为准；风控 Tab 的卡片 + 筛选分工以 [risk-monitor-reusable-patterns.md §11](./risk-monitor-reusable-patterns.md) 为准。

## 1. 系统概览

### 目标

每 `scan_interval_min` 分钟（默认 10，前端可调，最小 5）扫描所有交易服务器的近期开仓成交，检测短时间内同品种密集下大单的可疑交易行为（Burst Open Detection），预警风控团队。

> 详细规则定义在 §6 Burst Open Detection。本文档 §3 / §4 / §5 中的 Scale-In / Frequent Open / Batch-Close 设计已被 Burst Open 取代，原始记录归档在 [risk-monitor-archive.md](./risk-monitor-archive.md)。

### 业务背景

KCM 是 B-Book CFD 券商：客户亏损 = 公司盈利。真正的风险是客户资金利用率高（杠杆开得猛），一旦方向押对，公司面临大额亏损。本系统的目的**不是保护客户**，而是**识别对公司 B-Book P&L 构成风险的高敞口客户**。

### 覆盖范围

| 服务器 | 数据库 | 持仓数据 | 成交数据 | 账户数据 |
|--------|--------|---------|---------|---------|
| MT4 Live (SID 1) | `mt4_live` | `mt4_trades` (CLOSE_TIME='1970') | `mt4_trades` | `mt4_users` |
| MT4 Live2 (SID 6) | `mt4_live2` | `mt4_trades` (CLOSE_TIME='1970') | `mt4_trades` | `mt4_users` |
| MT5 | `mt5_live` | `mt5_positions` | `mt5_deals` | `mt5_users` |

**所有库在同一台 MySQL Slave 上**，单个 pymysql 连接即可跨库查询。

### 架构总览

```
┌──────────────────────────────────────────────────────────────────┐
│  前端 (RiskMonitor.tsx)                                            │
│  - GET /burst-open/alerts          ← 时间范围列表 (主数据源)         │
│  - GET /burst-open/alerts/stats    ← 按规则 SummaryCard (`by_rule`)   │
│  - GET /burst-open                 ← 最近一次扫描元数据 (脚注 + 立即扫描) │
│  - GET /burst-open/config          ← 规则 + scan_interval_min       │
│  轮询频率: scan_interval_min × 60s (跟随后端配置, custom 范围不轮询)   │
└─────────────────────────┬────────────────────────────────────────┘
                          │
                          ▼
┌──────────────────────────────────────────────────────────────────┐
│  后端 (FastAPI + APScheduler)                                      │
│                                                                   │
│  ┌────────────────────────────────────────────────────────────┐  │
│  │  BurstOpenScheduler (单例后台线程)                            │  │
│  │  每 scan_interval_min 分钟触发 _locked_scan():               │  │
│  │    1) MT4 Live + MT4 Live2 + MT5 三个 SQL 拉最近 N 秒开仓     │  │
│  │       (check_interval_sec = scan_interval_min*60 + 30s buffer)│  │
│  │    2) Python 滑动窗口 rule_burst_open_detect(orders, rules)   │  │
│  │       按 (server, login, symbol) 分组, 多规则共享同一份数据    │  │
│  │    3) 跨扫描去重 + equity / zipcode / currency enrichment    │  │
│  │    4) 写入 _latest_result (内存) + scan_history + alert_events│  │
│  │       (SQLite, 30 天保留)                                     │  │
│  └────────────────────────────────────────────────────────────┘  │
│                                                                   │
│  Routes (`api/v1/routes/risk_monitor.py`):                        │
│    /burst-open                  读 _latest_result 缓存             │
│    /burst-open/config           SQLite read / write rules         │
│    /burst-open/scan-now         立即触发一次扫描 (加锁)              │
│    /burst-open/alerts           SQLite alert_events 时间范围查询     │
│    /burst-open/alerts/stats     SQLite 聚合 + optional `by_rule`      │
│    /burst-open/alerts/export    SQLite 流式 CSV (StreamingResponse) │
└─────────────────────────┬────────────────────────────────────────┘
                          │ 单个 pymysql 连接 (跨库查询)
                          ▼
┌──────────────────────────────────────────────────────────────────┐
│  MySQL Slave (Azure)  —  非 ClickHouse, 实时性要求决定的            │
│   mt4_live      mt4_live2     mt5_live       fxbackoffice         │
│   mt4_trades    mt4_trades    mt5_deals      mt4_users            │
│   mt4_users     mt4_users     mt5_positions  (currency / zipcode) │
│                               mt5_users                            │
└──────────────────────────────────────────────────────────────────┘
```

> **关于扫描数据源**：本页面是项目里少数几个**不走 ClickHouse 的页面**之一。Burst Open 检测的是"几秒内的密集开仓"，需要秒级新鲜数据，而 ClickHouse 同步有几分钟到几小时延迟。MySQL Slave 跟主库延迟通常在毫秒级，正好满足。

---

## 2. 数据采集层

### 设计原则

MT4 和 MT5 的表结构完全不同，但风控规则不应关心数据来源。采集层负责将不同服务器的数据**标准化为统一格式**，供规则引擎使用。

### MT4 vs MT5 数据差异

| 差异 | MT4 | MT5 |
|------|-----|-----|
| 交易记录 | 单表 `mt4_trades`，开平仓都在一条记录 | `mt5_deals` 开仓一条 + 平仓一条，用 `PositionID` 关联 |
| 持仓判断 | `CLOSE_TIME = '1970-01-01'` | 独立的 `mt5_positions` 表 |
| 手数换算 | `VOLUME / 100` | `Volume / 10000` |
| 方向字段 | `CMD`: 0=Buy, 1=Sell | `Action`: 0=Buy, 1=Sell |
| 隔夜利息 | `SWAPS` | `Storage` |
| 总盈亏 | `PROFIT + SWAPS + COMMISSION` | `Profit + Storage + Commission` |
| 时间索引 | `INDEX_OPENTIME`, `INDEX_CLOSETIME` | `Timestamp` (Windows FILETIME, 有索引) |
| 合约信息 | 无 (需硬编码) | `ContractSize`, `PriceCurrent` (直接可用) |
| 账户余额 | `mt4_users` 待确认 | `mt5_users.Balance` ✅ |
| 账户杠杆 | `mt4_users` 待确认 | `mt5_users.Leverage` ✅ |

### 统一持仓格式

```python
@dataclass
class NormalizedPosition:
    """规则引擎使用的统一持仓格式，屏蔽 MT4/MT5 差异"""
    server: str           # "MT4_Live" | "MT4_Live2" | "MT5"
    login: int            # 账户号
    group: str            # 账户组
    symbol: str           # 品种
    direction: str        # "Buy" | "Sell"
    lots: float           # 手数 (已换算)
    open_time: datetime   # 开仓时间
    profit: float         # 浮动盈亏 / 已实现盈亏
    balance: float | None # 账户余额 (可能无法获取)
    leverage: int | None  # 杠杆倍数
    contract_size: float | None  # 合约大小 (MT5 可用)
    current_price: float | None  # 当前价格 (MT5 可用)
```

### SQL 查询

#### MT4 Live — 未平仓持仓

```sql
SELECT
    'MT4_Live' AS server,
    t.LOGIN AS login,
    t.SYMBOL AS symbol,
    CASE WHEN t.CMD = 0 THEN 'Buy' ELSE 'Sell' END AS direction,
    t.VOLUME / 100 AS lots,
    t.OPEN_TIME AS open_time,
    t.OPEN_PRICE AS open_price,
    t.PROFIT AS profit,
    t.SWAPS AS swaps,
    t.COMMISSION AS commission,
    t.PROFIT + t.SWAPS + t.COMMISSION AS total_profit
FROM mt4_live.mt4_trades t
WHERE t.CLOSE_TIME = '1970-01-01 00:00:00'
  AND t.CMD IN (0, 1)
  AND t.LOGIN NOT LIKE '7%'
  AND NOT EXISTS (
      SELECT 1 FROM mt4_live.mt4_users u
      WHERE u.LOGIN = t.LOGIN
        AND (u.`NAME` LIKE '%test%'
             OR u.`GROUP` LIKE '%demo%'
             OR u.`GROUP` LIKE '%test%')
  )
ORDER BY t.LOGIN, t.OPEN_TIME;
```

#### MT4 Live2 — 未平仓持仓

```sql
SELECT
    'MT4_Live2' AS server,
    t.LOGIN AS login,
    t.SYMBOL AS symbol,
    CASE WHEN t.CMD = 0 THEN 'Buy' ELSE 'Sell' END AS direction,
    t.VOLUME / 100 AS lots,
    t.OPEN_TIME AS open_time,
    t.OPEN_PRICE AS open_price,
    t.PROFIT AS profit,
    t.SWAPS AS swaps,
    t.COMMISSION AS commission,
    t.PROFIT + t.SWAPS + t.COMMISSION AS total_profit
FROM mt4_live2.mt4_trades t
WHERE t.CLOSE_TIME = '1970-01-01 00:00:00'
  AND t.CMD IN (0, 1)
  AND t.LOGIN NOT LIKE '7%'
  AND NOT EXISTS (
      SELECT 1 FROM mt4_live2.mt4_users u
      WHERE u.LOGIN = t.LOGIN
        AND (u.`NAME` LIKE '%test%'
             OR u.`GROUP` LIKE '%demo%'
             OR u.`GROUP` LIKE '%test%')
  )
ORDER BY t.LOGIN, t.OPEN_TIME;
```

#### MT5 — 当前持仓 (从 positions 表)

```sql
SELECT
    'MT5' AS server,
    p.Login AS login,
    u.`Group` AS `group`,
    p.Symbol AS symbol,
    CASE WHEN p.Action = 0 THEN 'Buy' ELSE 'Sell' END AS direction,
    p.Volume / 10000 AS lots,
    p.TimeCreate AS open_time,
    p.PriceOpen AS open_price,
    p.PriceCurrent AS current_price,
    p.Profit AS profit,
    p.Storage AS swaps,
    p.ContractSize AS contract_size,
    u.Balance AS balance,
    u.Leverage AS leverage
FROM mt5_live.mt5_positions p
INNER JOIN mt5_live.mt5_users u ON p.Login = u.Login
WHERE u.`Group` NOT LIKE '%demo%'
  AND u.`Group` NOT LIKE '%test%'
ORDER BY p.Login, p.TimeCreate;
```

#### MT4 Live — 最近 20 分钟已平仓

```sql
SELECT
    'MT4_Live' AS server,
    t.TICKET AS ticket,
    t.LOGIN AS login,
    t.SYMBOL AS symbol,
    CASE WHEN t.CMD = 0 THEN 'Buy' ELSE 'Sell' END AS direction,
    t.VOLUME / 100 AS lots,
    t.OPEN_TIME AS open_time,
    t.CLOSE_TIME AS close_time,
    TIMESTAMPDIFF(SECOND, t.OPEN_TIME, t.CLOSE_TIME) AS hold_seconds,
    t.PROFIT + t.SWAPS + t.COMMISSION AS total_profit
FROM mt4_live.mt4_trades t
WHERE t.CLOSE_TIME >= DATE_SUB(NOW(), INTERVAL 20 MINUTE)
  AND t.CLOSE_TIME != '1970-01-01 00:00:00'
  AND t.CMD IN (0, 1)
  AND t.LOGIN NOT LIKE '7%'
ORDER BY t.LOGIN, t.CLOSE_TIME;
```

#### MT5 — 最近 20 分钟已平仓

```sql
SELECT
    'MT5' AS server,
    d.Deal AS ticket,
    d.Login AS login,
    d.Symbol AS symbol,
    CASE WHEN d.Action = 0 THEN 'Buy' ELSE 'Sell' END AS direction,
    d.Volume / 10000 AS lots,
    d.Time AS close_time,
    d.Profit + d.Commission + d.Storage AS total_profit,
    d.PositionID
FROM mt5_live.mt5_deals d
WHERE d.Timestamp >= UNIX_TIMESTAMP(DATE_SUB(NOW(), INTERVAL 20 MINUTE))
  AND d.Action IN (0, 1)
  AND d.Entry IN (1, 3)
ORDER BY d.Login, d.Time;
```

### 性能预估 (单次轮询)

| 查询 | 预估行数 | 预估耗时 |
|------|---------|---------|
| MT4 Live 未平仓 | ~3,400 | 4ms |
| MT4 Live2 未平仓 | ~2,000 | 3ms |
| MT5 持仓 | ~数千 | <10ms |
| MT4 Live 20min平仓 | ~数百 | <5ms |
| MT5 20min平仓 | ~数百 | <5ms |
| **合计** | | **<30ms** |

每 10 分钟 30ms 查询负载，对 Slave DB 无压力。

---

## 3. 规则引擎

### 设计原则

- 每条规则是一个**独立函数**，接收标准化数据，返回告警列表
- 规则可独立启用/禁用，互不影响
- 新增规则只需添加函数 + 注册到引擎，不改现有代码（详细脚手架见 [risk-monitor-reusable-patterns.md](./risk-monitor-reusable-patterns.md) 和 [SKILL.md "Adding a New Rule"](../../.cursor/skills/risk-monitor/SKILL.md)）

### 当前规则注册

| 规则 | 状态 | 文档 |
|------|------|------|
| `rule_burst_open_detect` (批量下单) | ✅ 在线 | 本文档 §6 |
| `scan_quick_open_close` / 快开快平 | ✅ 在线（`rule_id` ≥ 51） | [reusable-patterns §11](./risk-monitor-reusable-patterns.md)、Skill Data Model |
| Gap Trading (缺口交易) | 📋 仅路线图，**无前端 Tab**；后端未实现 | [roadmap.md §3](./risk-monitor-roadmap.md) |
| Quick Profit / Scale-In / Leverage Abuse / Martingale 等 | 📋 已设计未实现 | [roadmap.md §3](./risk-monitor-roadmap.md) |
| ~~`rule_frequent_open_detect`~~ | ❌ 2026-03-28 被 Burst Open 取代 | [archive.md "Frequent Opening Detection"](./risk-monitor-archive.md) |
| ~~`rule_scale_in_detect`~~ | ❌ 2026-03-26 移除 | [archive.md "Scale-In Detection"](./risk-monitor-archive.md) |
| ~~`rule_batch_close_detect`~~ | ❌ 设计阶段废弃，从未上线 | [archive.md "Old Python Detection Engine"](./risk-monitor-archive.md) |

### 当前规则: Burst Open Detection (批量下单)

**触发条件**: `burst_window_sec` 秒内，同一 (server, login, symbol) 上 ≥ `min_order_count` 笔订单，每笔 lots ≥ `min_lots_per_order`。买卖都计入，不区分方向。

**完整定义、滑动窗口算法、SQL、配置、API、SQLite 表结构、CEN/zipcode enrichment 等所有细节见下方 §6**。

---

## 4. 后端实现

### 文件结构

```
backend/app/
├── services/
│   └── risk_monitor_service.py    # 数据采集 + 规则引擎 + 告警输出
├── schemas/
│   └── risk_monitor.py            # Pydantic 请求/响应模型
└── api/v1/routes/
    └── risk_monitor.py            # API 接口
```

### 数据库连接

使用现有的 `DB_HOST` / `DB_USER` / `DB_PASSWORD` 配置 (Slave DB)，通过跨库查询访问所有数据:

```python
conn = pymysql.connect(
    host=settings.DB_HOST,
    user=settings.DB_USER,
    password=settings.DB_PASSWORD,
    port=int(settings.DB_PORT),
    charset=settings.DB_CHARSET,
    cursorclass=pymysql.cursors.DictCursor,
)
# 跨库查询: mt4_live.mt4_trades, mt5_live.mt5_positions, ...
```

不需要新增任何 `.env` 配置项。

### API

> **完整 API 契约**见 §6.5 / §6.6 (服务端分页接口 + 响应格式)。本节列概览，避免和详细章节重复。

| 接口 | 方法 | 用途 |
|------|------|------|
| `/api/v1/risk-monitor/burst-open` | GET | 读 `_latest_result` 内存缓存，扫描元数据 |
| `/api/v1/risk-monitor/burst-open/config` | GET / POST | 读写 SQLite 中的 `scan_interval_min` + rules |
| `/api/v1/risk-monitor/burst-open/scan-now` | POST | 立即扫描 (加锁，单实例排队) |
| `/api/v1/risk-monitor/burst-open/alerts` | GET | 时间范围 + 过滤 + 服务端分页排序，主数据源 |
| `/api/v1/risk-monitor/burst-open/alerts/stats` | GET | 同范围聚合；`by_rule` 供按规则卡片（与表格 `rule_id` 筛选独立） |
| `/api/v1/risk-monitor/burst-open/alerts/export` | GET | 流式 CSV，按当前 filter + sort 全量导出 |
| ~~`/api/v1/risk-monitor/frequent-open`~~ | — | ❌ 2026-03-28 删除（[archive.md](./risk-monitor-archive.md) 留存原响应格式） |
| ~~`/api/v1/risk-monitor/scan`~~ | — | ❌ 2026-03 删除 |

### Email 告警 (未来阶段)

> **首期不实现**，后续按需加入。

设计方案: 仅对 `CRITICAL` 和 `HIGH` 等级发送邮件，通过 Redis 去重:

```python
key = f"risk_alert:{alert.server}:{alert.login}:{alert.rule}"
if not redis.exists(key):
    send_email(alert)
    redis.set(key, 1, ex=3600)  # 1 小时内不重复
```

复用 `email_service.py` 的 `send_email()` 函数，参考 `.cursor/skills/email-notification/SKILL.md`。

---

## 5. 前端设计

### 页面: `/risk-monitor`

- 侧边栏分组: Risk Control，页面标题: 交易实时监控
- 纯中文 UI，不需要 i18n
- **Tab 切换**: 批量下单 (默认) / 快开快平
- 没有 ALERT / WATCH 分级，所有命中统称"可疑用户"，详见 §6.1

> **历史记录**：早期 Tab 1 是"频繁开仓 + ALERT/WATCH 分级"，2026-03-28 重构为 Burst Open 后取消分级，UI 设计和卡片含义全部变更。原稿归档于 [archive.md "Frequent Opening Detection"](./risk-monitor-archive.md)。

#### Tab 1: 批量下单 (默认)

```
┌──────────────────────────────────────────────────────────────────────┐
│  [批量下单] [快开快平]                                                │
├──────────────────────────────────────────────────────────────────────┤
│  检测短时间内同品种密集下大单的可疑交易行为(EA/算法特征)              │
│  当前范围: 最近 4 小时 · 上次刷新 14:32                              │
│  最近扫描 14:30:00 · 耗时 102ms · 每 N 分钟自动扫描 (N=scan_interval) │
│             [导出 CSV] [规则配置] [立即扫描]                          │
├──────────────────────────────────────────────────────────────────────┤
│  每规则一张卡片: Rule 1·去重账户 | Rule 2·去重账户 … (紧凑 Card)      │
│  (文案含告警条数 + 该规则参数摘要)                                    │
├──────────────────────────────────────────────────────────────────────┤
│  [全部规则▼] [最近 4 小时▼] [自定义日期] [全部服务器▼] zipcode 账户  │
│  (规则筛选仅影响表格/导出；卡片仍展示各规则在全筛选下的聚合)          │
│         共 N 条告警                                                  │
├──────────────────────────────────────────────────────────────────────┤
│  AG-Grid 表格 (服务端分页 + 服务端排序)                              │
│  列: 规则 | 被发现时间(HKT) | 具体时间(开仓窗口) | 服务器 | Zipcode |  │
│      账户(CRM链接) | 币种 | 品种 | 批量笔数 | 批量总手数 | 订单明细 |  │
│      净值(USD) | 每手净值(USD) | 总持仓手数 | 杠杆 | 账户组          │
│  自动刷新: 跟随后端 scan_interval_min (默认 5min, 最小 5min)         │
│  分页: 50 / 100 / 200 / 300 / 500 行 ─ 全量导出走 /alerts/export    │
└──────────────────────────────────────────────────────────────────────┘
```

#### Tab 2: 快开快平

> 与批量下单共用同一套 **规则卡片 + 规则筛选 + 时间/服务器/zip/账户** 工具栏模式；数据接口为 `/quick-open-close/*`。详见 `.cursor/skills/risk-monitor/SKILL.md` 与 [risk-monitor-reusable-patterns.md](./risk-monitor-reusable-patterns.md)。

---

## 6. 批量下单检测 (Burst Open Detection)

> **状态**: ✅ 已实施 (2026-03-28)。
>
> **变更原因**: Risk team 反馈——5-8 分钟内开 3 笔属于正常交易行为，旧"频繁开仓"规则误报率太高。
> 新规则聚焦于真正的 B-Book 风险行为：**短时间内同品种密集下大单**（典型 EA/算法行为）。

### 6.1 旧规则 vs 新规则

| | v1 频繁开仓 | v2 批量下单 |
|--|--|--|
| **检测逻辑** | N 分钟内开仓 ≥ 3 笔 | N 秒内同品种开仓 ≥ 3 笔，且每笔 ≥ 5 手 |
| **分组** | (server, login) | **(server, login, symbol)** |
| **品种** | 不区分 | **同品种** |
| **方向** | 不区分 | 不区分（买卖都计入） |
| **severity** | ALERT / WATCH 两级 | **统一"可疑用户"**，无分级 |
| **equity_per_lot** | 触发条件 | **展示字段**（供风控参考，不作为触发条件） |
| **典型误报** | 手工交易者 | 几乎无（人工不可能 3 秒 3 笔） |
| **扫描驱动** | 前端 setInterval | **后端单一定时任务** |

### 6.2 新规则参数 (4 个 Input)

用户在前端可配置多条 Rule，每条 Rule 包含 3 个参数：

| # | 参数 | 含义 | 默认值 | API 字段名 |
|---|------|------|--------|-----------|
| 1 | **时间窗口** | 几秒内的连续开仓算"批量" | 3 秒 | `burst_window_sec` |
| 2 | **最少笔数** | 时间窗口内至少几笔订单 | 3 笔 | `min_order_count` |
| 3 | **每笔最少手数** | 每笔订单的 lots 必须 ≥ | 5 手 | `min_lots_per_order` |

全局参数（非 Rule 粒度）：

| # | 参数 | 含义 | 默认值 | API 字段名 |
|---|------|------|--------|-----------|
| 4 | **扫描间隔** | 后端每隔几分钟执行一次扫描 | 10 分钟 | `scan_interval_min` |

**全局约束**:
- `scan_interval_min` 前端可调，范围 5 - 60 分钟，整数 (Pydantic schema default 10)
- SQL 回溯窗口 `check_interval_sec` = `scan_interval_min × 60 + 30s`（固定 30s buffer，常量 `_BOUNDARY_BUFFER_SEC` 在 `risk_monitor_service.py`）。30s 比所有规则的 `burst_window_sec`（≤30s）都大，能覆盖跨扫描边界的 burst；多扫部分由跨扫描去重 `(rule_id, server, login, symbol, first_open)` 抹掉，不会重复入库。原始边界问题分析见 §6.9。
- Rules 上限: 10 条
- Rules 持久化: SQLite (重启后恢复)
- Config 更新: POST 整体替换 rules 数组 (last-write-wins)

### 6.3 检测逻辑

```
后端定时任务 (每 scan_interval_min 分钟执行)
│
├── 1. SQL 采集 (一次，覆盖最近 scan_interval_min 分钟)
│      MT4 Live:  OPEN_TIME >= NOW() - interval
│      MT4 Live2: OPEN_TIME >= NOW() - interval
│      MT5:       mt5_deals.Timestamp >= filetime(NOW() - interval), Entry=0
│
├── 2. Python: 按 (server, login, symbol) 分组
│
├── 3. 对每组，按 open_time 排序后执行滑动窗口:
│      for each Rule in rules:
│          for i in range(len(orders)):
│              j = i
│              while orders[j].time - orders[i].time <= burst_window_sec:
│                  j++
│              window = orders[i:j]
│              if len(window) >= min_order_count:
│                  if all(o.lots >= min_lots_per_order for o in window):
│                      → 命中! 记录 burst 事件
│
├── 4. 去重: 同一 (server, login, symbol) 重叠窗口取最大的
│
├── 5. 对命中账户查询 equity/balance → 计算 equity_per_lot (展示用)
│      equity_per_lot 使用该账户 **全部未平仓持仓的 total_lots**
│
├── 6. 组装结果 → 写入 latest_result (缓存供前端读取)
│
└── 7. 追加到 scan_history (历史日志)
```

**滑动窗口示例**（Rule: 3 秒 / 3 笔 / 5 手）:

```
账户 12345, XAUUSD, 最近 10 分钟开仓:

14:20:01  Buy  10手
14:20:02  Sell  8手
14:20:02  Buy   6手
14:25:30  Buy   2手   ← lots < 5, 即使在窗口内也不满足

窗口 [14:20:01 ~ 14:20:03]:
  3 笔, 每笔 ≥ 5 手 → ✅ 命中

如果其中一笔只有 3 手:
14:20:01  Buy  10手
14:20:02  Sell  3手   ← 不满足 min_lots_per_order
14:20:02  Buy   6手

窗口 [14:20:01 ~ 14:20:03]:
  3 笔, 但 Sell 3手 < 5手 → ✘ 不命中
```

### 6.4 多规则 (Multi-Rule) 支持

用户可配置多条 Rule，SQL 只执行一次，Python 对同一数据集重复执行每条 Rule：

| Rule | burst_window_sec | min_order_count | min_lots_per_order | 场景 |
|------|-----|---|---|---|
| Rule 1 | 3 | 3 | 5 | EA 瞬间大单 |
| Rule 2 | 5 | 5 | 3 | 密集中等手数 |
| Rule 3 | 1 | 3 | 10 | 极端：1秒内超大单 |

**同一账户匹配多条 Rule**: 每条 Rule 独立一行。例如账户 12345 命中 Rule 1 和 Rule 3 → 表格显示 2 行。
每条 Rule 有自动递增 ID (1, 2, 3...) 用于标识。

### 6.5 后端驱动定时扫描架构

```
┌─────────────────────────────────────────────────────────────┐
│                  后端 (FastAPI + Background Task)            │
│                                                             │
│  ┌─────────────────────────────────────────────────────┐    │
│  │  BurstOpenScheduler (单例, FastAPI startup 启动)     │    │
│  │                                                     │    │
│  │  config (内存, 可持久化到 SQLite):                    │    │
│  │  ┌──────────────────────────────────────────────┐   │    │
│  │  │ scan_interval_min: 10                        │   │    │
│  │  │ rules: [                                     │   │    │
│  │  │   {burst_window_sec:3, min_orders:3, lots:5} │   │    │
│  │  │   {burst_window_sec:5, min_orders:5, lots:3} │   │    │
│  │  │ ]                                            │   │    │
│  │  └──────────────────────────────────────────────┘   │    │
│  │                                                     │    │
│  │  循环: sleep(scan_interval) → scan() → 写缓存+日志  │    │
│  └─────────────────────────────────────────────────────┘    │
│                                                             │
│  API:                                                       │
│  GET  /burst-open              → 读 latest_result 缓存     │
│  GET  /burst-open/config       → 读当前 rules + interval   │
│  POST /burst-open/config       → 更新 config, 立即生效     │
│  POST /burst-open/scan-now     → 立即触发一次扫描 (加锁)   │
│  GET  /burst-open/alerts        → 分页 + 排序 + 过滤查询   │
│  GET  /burst-open/alerts/stats  → 过滤后聚合统计           │
│  GET  /burst-open/alerts/export → 流式 CSV 导出 (全量)     │
└─────────────────────────────────────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────────────┐
│                  前端 (只读 SQLite alert_events + 内存缓存)    │
│                                                             │
│  每 scan_interval_min 分钟轮询 (跟随后端配置, 5min fallback)  │
│  - GET /burst-open/alerts        → AG-Grid 表格主数据         │
│  - GET /burst-open/alerts/stats  → 顶部 SummaryCard          │
│  - GET /burst-open               → 扫描元数据 (脚注 + 规则)    │
│  规则配置: 抽屉读写 config API, 保存后 refreshIntervalMs 自动重算 │
│  "立即扫描" → POST scan-now                                 │
│  "导出 CSV" → /alerts/export 流式下载 (不限 page_size)        │
│  时间范围 picker (1h/4h/1d/7d/30d/custom) 取代了旧版 history Drawer │
└─────────────────────────────────────────────────────────────┘
```

> **轮询频率细节** (commit 9713e3f, 2026-04-28)：以前是硬编码 `setInterval(fetchAlerts, 30_000)`，跟后端 scan 节奏不匹配 — 后端 5/10 分钟才出新数据，前端 30s 一次纯属浪费。现在改成 `(config?.scan_interval_min ?? 5) * 60_000`，并把 `refreshIntervalMs` 放进 useEffect 依赖里，所以在 Config Drawer 里改 `scan_interval_min` 保存后，前端 timer 会立即 reschedule，不需要刷新页面。custom (绝对) 范围因为 `until` 是固定时刻，不再需要轮询。

**多用户冲突处理**:

| 场景 | 策略 |
|------|------|
| 多人同时修改 config | Last-write-wins, 前端拉取后显示最新值 |
| 多人同时点"立即扫描" | 后端加锁, 同一时刻只执行一次, 后续请求等待结果 |
| 后端重启 | config 从 SQLite 读取恢复 (TODO: 见 Q4) |

### 6.6 历史 Log 设计 (v2: 批次 + 事件双表)

> **2026-04-17 更新**：为支持时间范围查询视图（短期优化 P1），从"单表 JSON 数组"升级为"批次 + 事件两表"结构。`scan_history` 保留批次元数据，`alert_events` 拍平每条告警到独立行方便时间范围/账户/服务器筛选。

**存储位置**: SQLite 文件 `data/risk_monitor.db`（Python 内置 `sqlite3`，无额外依赖）。

#### 表 1: `scan_history`（每次扫描一行，批次元数据）

```sql
CREATE TABLE scan_history (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    scanned_at        TEXT    NOT NULL,   -- UTC ISO8601
    scan_interval_min INTEGER NOT NULL,
    accounts_scanned  INTEGER NOT NULL,
    suspicious_count  INTEGER NOT NULL,
    scan_time_ms      INTEGER NOT NULL,
    rules_config      TEXT    NOT NULL,   -- JSON 本次 rules 快照
    alerts            TEXT    NOT NULL    -- JSON 完整 alerts（冗余，便于审计回放）
);
```

#### 表 2: `alert_events`（每条告警一行，时间范围视图的主数据源）

```sql
CREATE TABLE alert_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_batch_id   INTEGER NOT NULL,   -- FK → scan_history.id
    scanned_at      TEXT    NOT NULL,   -- 冗余存储 UTC 时间用于快速 range 查询
    rule_id         INTEGER NOT NULL,
    rule_label      TEXT    NOT NULL,
    server          TEXT    NOT NULL,
    login           INTEGER NOT NULL,
    symbol          TEXT    NOT NULL,
    order_count     INTEGER NOT NULL,
    total_lots      REAL    NOT NULL,
    first_open      TEXT,                -- burst 首笔开仓时间 (UTC)
    last_open       TEXT,                -- burst 末笔开仓时间 (UTC)
    equity          REAL,
    balance         REAL,
    equity_per_lot  REAL,
    total_open_lots REAL,
    leverage        INTEGER,
    account_group   TEXT,
    orders_json     TEXT,                -- 订单明细 JSON
    currency        TEXT,                -- "USD" / "CEN"（2026-04-17 新增，equity/balance 已是 USD）
    zipcode         TEXT                 -- 客户 zipcode（2026-04-17 新增，来自 fxbackoffice.mt4_users）
);

CREATE INDEX idx_alert_events_scanned_at   ON alert_events(scanned_at DESC);
CREATE INDEX idx_alert_events_login_scan   ON alert_events(login, scanned_at DESC);
CREATE INDEX idx_alert_events_server_sym   ON alert_events(server, symbol, scanned_at DESC);
```

**前端展示**: 页面主视图直接查询 `alert_events`（默认最近 4 小时）。时间范围支持快捷预设（1h / 4h / 1d / 7d / 30d）+ 自定义日期范围 picker，支持服务器/账户/zipcode 筛选，列头点击即按 **服务端排序**（scanned_at / login / total_lots / equity 等白名单列，见 `SORTABLE_ALERT_COLS`），分页走服务端（默认每页 50，可切到 500）。CSV 导出通过 `/burst-open/alerts/export` 后端流式 CSV 输出，不受分页行数限制。不再需要"查看历史" Drawer。

#### 查询接口（2026-04-24 升级到服务端分页）

| 参数 | 类型 | 默认 | 说明 |
|------|------|------|------|
| `since` / `until` | ISO8601 UTC | 最近 4h | 时间窗口 |
| `page` | int ≥ 1 | 1 | 1-based 页码；与 `limit`/`offset` 同时出现时 `page` 优先 |
| `page_size` | int ∈ [1, 500] | 50 | 每页行数上限 500；全量导出请走 `/alerts/export` |
| `sort_by` | str | `scanned_at` | 仅接受 `SORTABLE_ALERT_COLS` 白名单，其它值静默回退到 `scanned_at`（防 SQL 注入） |
| `sort_order` | `asc` \| `desc` | `desc` | 大小写不敏感 |
| `server` / `login` / `symbol` / `rule_id` / `zipcode` | — | — | 等值 / LIKE 过滤 |
| `limit` / `offset` | int | — | 旧客户端兼容；`page` 传了就忽略 |

`/alerts/stats` 接同一套 `server/login/zipcode` 过滤（无 `rule_id`），让规则卡片与表格在「时间 + 服务器 + zip + 账户」维度一致；表格另可选用 `rule_id` 只看单规则。`/alerts/export` 参数同 `/alerts` 但**不接受** `page/page_size/limit/offset`，用 `StreamingResponse + csv.writer`，每 5000 行一批从 SQLite `fetchmany`，内存占用与行数无关；响应带 UTF-8 BOM，Excel 直接打开中文不乱码。

**写入路径**: `burst_open_scheduler._run_scan()` → `append_scan_and_events()` 在单事务内写入批次行 + 所有告警事件行。

**迁移逻辑**: `init_risk_monitor_db()` 在首次升级时（`alert_events` 为空且 `scan_history` 非空）自动把旧批次的 JSON alerts 拍平回填到 `alert_events`，不丢失历史。

**时区约定**:
- 所有 `scanned_at` / `first_open` / `last_open` 后端按 UTC 存，字符串格式 `YYYY-MM-DDTHH:MM:SSZ`
- 前端展示统一转换为 `Asia/Hong_Kong`（HKT，UTC+8）
- CSV 导出时同样按 HKT 展示

**Broker → UTC 转换（2026-04-17 修复）**:
- 背景: MT4/MT5 服务器时区是 `Indian/Antananarivo`（UTC+3，无 DST），`t.OPEN_TIME` / `d.Time` 在 MySQL 里是 **UTC+3 的 naive datetime**。早期版本直接 `SELECT t.OPEN_TIME AS open_time`，写到 SQLite 时就变成 naive UTC+3 字符串，和 Python `datetime.now(UTC)` 生成的 `scanned_at` 差 3 小时。前端 `parseBackendTime` 对 naive 字符串按 UTC 解析，再叠一次 `+3h → HKT` 偏移，导致页面上 "被发现时间" 看起来比 "具体时间（开仓）" 还早。
- 方案: 在 `_query_mt4_recent_opens` / `_query_mt5_recent_opens` 的 SELECT 子句里用 `DATE_FORMAT(CONVERT_TZ(t.OPEN_TIME, '+03:00', '+00:00'), '%Y-%m-%dT%TZ')` 直接在 MySQL 端转成 UTC ISO8601。broker 时区写死 `+03:00`（不依赖 `@@session.time_zone`，broker 多年稳定，CONVERT_TZ 也更快）。`WHERE t.OPEN_TIME >= DATE_SUB(NOW(), ...)` 保持原样，因为 `t.OPEN_TIME` 和 `NOW()` 两边都是 broker local，比较仍正确。
- 回填: `backend/scripts/backfill_alert_events_open_time.py` 一次性把旧的 10142 行 `first_open` / `last_open` 以及 `orders_json` 里 73960 笔 `open_time` 统一减 3 小时并加 `Z` 后缀。脚本幂等（已带 `Z` 或带明确 offset 的值跳过），支持 dry-run，2026-04-17 已执行。

**保留策略**: **30 天**（常量 `_RETENTION_DAYS` 集中控制）。清理在两个地方触发：

1. 每次扫描写入后同步 `DELETE FROM scan_history / alert_events WHERE scanned_at < datetime('now', '-30 days')`（热路径，默认每 10min 一次）。
2. 后端启动时 `init_risk_monitor_db()` 多运行一次同样的清理，避免扫描长期停摆时旧数据堆积。

前端对应约束：`frontend/src/pages/RiskMonitor.tsx` 的日期选择预设最长「最近 30 天」，自定义日历 `disabled={{ before: now - 30d }}` 禁掉更旧的日期，`buildRangeIso()` 里额外 `clampToRetention()` 做双保险——即使 UI 放大了范围，发往后端的 `since` 也不会超窗。

#### 币种处理（2026-04-17 上线）

MT 服务器上 CEN（美分）账户的 `equity` / `balance` 是美分单位（100 倍膨胀），CRM 侧权威来源是 `fxbackoffice.mt4_users`，按 `loginsid` (`{sid}-{login}`) 查询：

| server | sid |
|--------|-----|
| MT4_Live | 1 |
| MT4_Live2 | 6 |
| MT5 | 5 |

处理流程（`risk_monitor_service._enrich_account_info`）：

1. `_get_currency_map` 一次批量查 `fxbackoffice.mt4_users` 拿到所有 alert 的 `loginsid → CURRENCY`
2. `CURRENCY='CEN'` 的 alert：`equity /= 100`、`balance /= 100`
3. `equity_per_lot` 用调整后的 equity 重算，保持三者单位一致
4. `alert.currency` 字段写 USD / CEN 供前端"币种"列展示（未知默认 USD，绝不把 USD 账户误当 CEN 除 100）
5. `total_lots` / `total_open_lots` 不动（合约规格 CEN/USD 一致）

**一次性回填迁移**: `backend/scripts/backfill_alert_events_currency.py` — 针对 currency 列上线前已写入的旧行，幂等可重跑，默认 dry-run，`--apply` 才写 DB。2026-04-17 首次执行回填 9871 行（CEN 7424 / USD 2447），0 缺失。

#### Zipcode 筛选（2026-04-17 上线）

`fxbackoffice.mt4_users.ZIPCODE` 作为同一客户多账户的识别特征（同 zipcode + 批量下单 = 刷单农场强信号）。和 currency 共用 `_get_account_info_map`，**单次 SQL 同时捞出 CURRENCY + ZIPCODE**，无额外 DB load。

**后端 API**：

```
GET /burst-open/alerts?zipcode=<substr>&...
GET /burst-open/alerts/stats?zipcode=<substr>&...
```

- 子串模糊匹配（`WHERE zipcode LIKE '%x%' ESCAPE '\\'`，用户的 `%` `_` `\` 会被转义）
- 空字符串/纯空格视为"不筛选"
- NULL 行永不命中（CRM 未填 zipcode 的账户会被筛走，这是预期行为）

**前端**：
- 列顺序：服务器 → **Zipcode** → 账户 → 币种 → 品种 → ...
- Zipcode 列 NULL 值以灰色 `—` 显示
- Toolbar 独立输入框（300ms debounce），和时间范围、服务器下拉同级
- `stats` 接口同步带 zipcode 参数 → **规则 SummaryCard**（`by_rule`）与表格在「时间 + 服务器 + zip + 账户」维度一致；表格另可选 `rule_id` 筛选单规则（不影响卡片汇总）

**不做回填**：2026-04-17 起新数据带 zipcode；历史 9871 行 zipcode 永远 NULL，不在筛选结果中。若未来需要，可仿 `backfill_alert_events_currency.py` 写同款脚本。

### 6.7 可行性分析

| 关注点 | 分析 | 结论 |
|--------|------|------|
| **SQL 对 DB load** | 10min 窗口的开仓查询使用 OPEN_TIME / Timestamp 索引，预估返回数百行，耗时 <10ms。后端单任务每 10min 查一次，Slave 零压力 | ✅ 可行 |
| **Python 滑动窗口** | 按 (server, login, symbol) 分组后每组通常 <20 条。排序 O(N log N) + 滑动 O(N)，多条 rules 重复执行也在亚毫秒级 | ✅ 可行 |
| **后端定时任务** | `BurstOpenScheduler` 由 `main.py` lifespan 启动，内置 **APScheduler** 周期扫描；单实例锁避免并发扫描。**（旧稿曾写 asyncio 自旋循环，已废弃。）** | ✅ 可行 |
| **多规则** | SQL 执行一次，Python 对同一数据集执行 3-5 条 rules，额外开销忽略不计 | ✅ 可行 |
| **SQLite 历史日志** | 内置模块，单文件，写频率极低。每条记录 ~1-5KB (取决于 alerts 数量)，1000 条 ≈ 1-5MB | ✅ 可行 |
| **前端改动量** | 多规则 Config Drawer + 时间范围视图 + AG-Grid；增量 Tab 复用 [§11](./risk-monitor-reusable-patterns.md) 模式 | ✅ 可行 |

### 6.8 确认答案汇总

| Q | 问题 | 确认结果 |
|---|------|---------|
| Q1 | scan_interval 前端可调？ | ✅ 可调，最小 5min，整数 |
| Q2 | check_interval = scan_interval？ | ✅ 同意，回溯=扫描 |
| Q3 | Rules 上限 | 10 条 |
| Q4 | 持久化 | SQLite |
| Q5 | Log 保留 | 30 天（2026-04-17 调整，配合时间范围视图，详见 §6.6） |
| Q6 | 多 Rule 命中展示 | 每条 Rule 一行 |
| Q7 | equity_per_lot 的 total_lots | 全部未平仓持仓 |
| Q8 | 历史 UI | Drawer (宽版) |
| Q9 | API 路径 | `/burst-open` (新 endpoint) |

### 6.9 架构审查 — 关键问题

#### 🔴 Critical: 扫描窗口边界丢失 (Boundary Problem)

**问题**: 如果 `check_interval` 严格等于 `scan_interval`，批量事件恰好跨越两次扫描的边界时会被**漏检**。

```
scan_interval = 10min

扫描 A (14:00) 回溯范围: [13:50:00, 14:00:00)
扫描 B (14:10) 回溯范围: [14:00:00, 14:10:00)

账户在 13:59:59, 14:00:00, 14:00:01 各开 1 笔 10 手 XAUUSD (3 秒内 3 笔):

→ 扫描 A 只看到 13:59:59 的 1 笔 → 不触发
→ 扫描 B 只看到 14:00:00 和 14:00:01 的 2 笔 → 不触发
→ 真实的 burst 被漏掉!
```

**解决方案**: SQL 回溯窗口加一个 buffer。

> **2026-04 实际实现**: 没采用动态 `max(burst_window_sec)`，而是用了**固定 30s buffer**（`_BOUNDARY_BUFFER_SEC = 30` in `risk_monitor_service.py`），因为 `burst_window_sec` schema 上限就是 30s，固定 30s 永远 ≥ 任何规则的窗口，简单且安全。
>
> ```python
> # backend/app/services/risk_monitor_service.py
> _BOUNDARY_BUFFER_SEC = 30
> check_interval_sec = scan_interval_min * 60 + _BOUNDARY_BUFFER_SEC
> ```
>
> 例如 scan_interval = 10min → 回溯 600s + 30s = 630s。多出来的 30s 可能产生重复检测，由跨扫描去重 `(rule_id, server, login, symbol, first_open)` 与 `_latest_result.alerts` 比对消除（`scan_burst_open` 里的 `previous_alerts` 参数）。

> 回复:

#### ⚠️ Important: Rule 参数合法性校验

前端和后端都需要校验 Rule 参数的合理范围，防止误配置：

| 参数 | 建议范围 | 理由 |
|------|---------|------|
| `burst_window_sec` | 1 ~ 30 秒 | <1 无意义，>30 接近旧规则的"频繁"而非"批量" |
| `min_order_count` | 2 ~ 50 笔 | <2 没有"密集"概念，>50 不太现实 |
| `min_lots_per_order` | 0.01 ~ 100 手 | 上限 100 极端罕见；下限 0.01 = MT4 最小手数（CEN 账户场景需要） |
| `scan_interval_min` | 5 ~ 60 分钟 | <5 对 DB 压力增加，>60 实时性太差 |

> 回复 (范围是否合适？):

#### ⚠️ Important: 服务启动行为

后端启动时需要明确：
1. 从 SQLite 读取上次 config（如首次运行则用代码默认值并写入 SQLite）
2. **立即执行一次扫描**还是等待第一个 scan_interval 后再扫描？

建议: 启动后立即执行一次首扫，不等待。这样部署/重启后不需要等 10 分钟才看到数据。

> 回复:

#### ⚠️ Important: 旧 API `/frequent-open` 的处理

新 API 用 `/burst-open`。旧 `/frequent-open` 端点:
- A) 保留但返回 HTTP 410 Gone + 提示信息（过渡期）
- B) 直接删除

建议 B (内部工具，无外部依赖方)。

> 回复:

#### 💡 Nice-to-have: Rule 展示名称

Q6 确认每条 Rule 一行。前端表格需要标识"命中了哪条 Rule"。两种方案:
- A) 用自动 ID 显示 (Rule 1, Rule 2, Rule 3...)
- B) 允许用户给每条 Rule 起名 (如 "EA大单", "密集中单")

A 最简单，B 可读性更好。建议 v1 用 A，后续按需加 B。

> 回复:

### 6.10 原始 Q&A 存档

以下为原始问答记录，确认结果已汇总到 §6.8。

---

### ~~6.8~~ 原始待确认问题

> 请在每个问题下方填写回复。

**Q1: 扫描间隔 (scan_interval) 是否前端可调？**

默认 10 分钟。是否允许前端用户修改为 5min / 15min / 30min？还是后端写死？

> 回复: 允许前端用户修改, 最小是 5min, 要求输入是整数

**Q2: check_interval 是否始终等于 scan_interval？**

扫描间隔 10min → SQL 回溯 10min，无缝覆盖。是否同意？还是希望 check_interval 独立可调？

> 回复: 同意, 回溯时间和扫描时间一致

**Q3: Rules 上限？**

建议最多 5 条。是否合适？

> 回复: 这个rules上限的理由是什么? 如果rules都是在python检测是否没有影响? 设置limit为10条吧

**Q4: Rules 和 config 持久化方式？**

服务重启后：
- A) 回到代码中的默认值（最简单，重启丢失用户修改）
- B) 从 SQLite 读取上次配置（持久化）

推荐 B。你的偏好？

> 回复: 从sqlite读取上次配置, 我需要持久化

**Q5: 历史 Log 保留策略？**

- A) 保留最近 N 条（如 1000 条 × 每 10min = 约 7 天）
- B) 保留最近 N 天（如 30 天）
- C) 不自动清理，手动管理

推荐 A (1000 条)。你的偏好？

> 回复: 保留7天的数据 

**Q6: 同一账户匹配多条 Rule 时如何展示？**

例如账户 12345 在 XAUUSD 上同时命中 Rule 1 和 Rule 3：
- A) 合并为 1 行，标注 "命中规则: Rule 1, Rule 3"
- B) 分为 2 行（每条 Rule 一行）

推荐 A（减少重复）。你的偏好？

> 回复: 分为2行, 每条rule一行

**Q7: equity_per_lot 中的 total_lots 用哪个值？**

- A) 仅 burst 窗口内的 lots（和触发条件一致的那几笔）
- B) 该账户**所有未平仓持仓**的 total lots（反映真实杠杆利用率）

推荐 B（更有风控参考价值）。你的偏好？

> 回复: 使用B吧

**Q8: "查看历史" UI 形式？**

- A) 侧边抽屉 (Drawer) — 不离开主页面
- B) 弹窗 (Dialog/Modal)
- C) 新页面 / 子路由 `/risk-monitor/history`

推荐 A。你的偏好？

> 回复: 使用drawer, 但是考虑移动端读区, web端的话 drawer要页面大一些

**Q9: API 路径命名？**

旧: `/api/v1/risk-monitor/frequent-open`
新规则建议:
- A) `/api/v1/risk-monitor/burst-open` (新 endpoint, 旧的保留/废弃)
- B) 继续用 `/api/v1/risk-monitor/frequent-open` (原地改造)

推荐 A (干净切换)。你的偏好？

> 回复: 使用方案A

## 7. 快速获利检测 (Quick Profit Detection) — Tab 3

> Phase 1 上线于 2026-05-07。Phase 2（A2 入金比例规则）延期到下一阶段。
> 1d/7d/30d 入金/出金列已下线（2026-05-07）；现统一展示 **client-level** 历史
> 净入金 `net_deposit_hist`（公式 + 过滤条件与 client-return-rate "历史净入金"
> 一致：`SUM(deposit) + SUM(withdrawal + ib withdrawal)`，CEN ÷100，过滤
> demo / 非 1·2·5·6 服务器），**四个 tab 共用**（含 Gap Trade）。监控仍是 account-level，但同一
> 客户的多个被告警账户拿到相同的客户级总数；非合规账户返回 NULL（前端"—"）。
> 仅展示，不参与触发条件。

### 7.1 业务诉求

识别"短时间内利润突增"的客户：在窗口期内（默认 30 分钟）已实现利润 +
（可选）当前浮动利润总和超过阈值（默认 \$5000）即触发告警。
触发后再人工 review 是否对冲、是否影响 B-Book 头寸。

### 7.2 调度 vs Lookback 解耦（核心设计）

`scan_interval_min`（5-60，全局）和 `lookback_min`（10-60，每条规则）相互
独立：

- 一次扫描的 SQL 拉取窗口 = `max(rule.lookback_min) * 60 + 30s`
- Python 端再按各规则自身 `lookback_min` 切窗 + 求和
- 一次 SQL 服务多条规则；最大延迟 ≤ `scan_interval_min`，与 lookback 大小无关

> 这是与 §6 批量下单 / 快开快平的关键差异：聚合式规则不能让 SQL 拉取窗口
> 等于扫描间隔，否则一个 30 分钟的求和规则会被一个 10 分钟的扫描间隔截断。

### 7.3 三态 position_status

每个 alert 在检测时根据"窗口内是否有平仓"+"账户当前是否有浮动"分类：

| status | 含义 | Badge | 浮动是否会变 |
|--------|------|-------|------------|
| `closed` | 全部已平仓 | 绿色 已平仓 | 否（最终值） |
| `open` | 全部还在持仓 | 琥珀 持仓中 | 是（用户点「刷新浮动盈亏」按钮触发） |
| `mixed` | 部分平 + 部分浮动 | 蓝色 部分平仓 | 是（用户点「刷新浮动盈亏」按钮触发） |

### 7.4 浮动 P&L 实时刷新（独立轻量端点）

> **痛点**：`alert_events` 是历史快照，扫描时定下来后不会变；但浮动 P&L
> 每秒都在波动。重跑扫描器既慢又会触发去重。

**方案**：新增 `GET /api/v1/risk-monitor/quick-profit/floating-refresh?ids=`

| 字段 | 设计 |
|------|------|
| 入参 | 表格里 `position_status != 'closed'` 行的 alert_events.id 列表 |
| 流程 | 1) 取 (server, login) 集合 → 2) 跑 floating SQL（仅候选账户） → 3) 重新分类 status → 4) 返回 `[{id, realized, floating, total, status}, ...]` |
| 不写 DB | 完全只读，不更新 `alert_events`，不触发 scheduler |
| 性能 | 表格通常 < 100 行，单接口 < 500ms |

**前端**（`QuickProfitTab` in `RiskMonitor.tsx`）：

- 工具栏「刷新浮动盈亏」按钮触发 `handleRefreshFloating()`，仅刷新 open / mixed 行
- 表格里全部都是 closed 时按钮自动 disabled
- 收到响应后用 `gridApi.applyTransaction({ update: rows })` 局部更新
  AG-Grid，避免重排和 selection 丢失
- 没有自动轮询：浮动是用户主动操作（"我现在想看一下当前是什么数"），
  避免后台轮询把告警的 `total_profit_usd` 异步覆盖成低于阈值/负数的实时值

### 7.5 数据来源

| 用途 | SQL |
|------|-----|
| Realized P&L | `mt4_trades / mt5_deals` `WHERE CLOSE_TIME >= DATE_SUB(NOW(), INTERVAL N SECOND)`（沿用快开快平模板，**去掉持单时长限制**，保留 demo/test 排除） |
| Floating snapshot | MT4: `mt4_trades` `WHERE CLOSE_TIME = '1970-01-01 00:00:00' AND LOGIN IN (...)`；MT5: `mt5_positions WHERE Login IN (...)` |
| Deposit / Withdrawal 1d/7d/30d | `fxbackoffice.stats_transactions` JOIN `fxbackoffice.mt4_users` (CEN ÷100 同 `ib_data_service.py`) |

### 7.6 跨扫描去重

按 `(rule_id, server, login, symbol)` 取上一次扫描的 `scanned_at`，若距今
小于当前规则的 `lookback_min` 分钟则抑制本次告警；否则放行。

- 30 分钟规则在触发后的 30 分钟内不重复，60 分钟规则同理
- 早期版本用绝对时间桶 `floor(now_ts / (lookback_min*60))`，但跨桶边界
  会泄漏（11:55 触发的 alert 在 12:01 仍会被复发）；改用「时间差」后稳健
- `rule_quick_profit_detect` 必须给输出 alert 写 `scanned_at`，否则
  `_latest_result.alerts` 喂回 dedup 时会被 `if scanned is None: continue`
  全部跳过，dedup 静默失效（用户表现为「立即扫描」每次都重复入库）
- **SQLite seed 是无条件的**：每轮 `_run_scan` 都通过
  `_build_quick_profit_prev_alerts` 把 `get_recent_quick_profit_alerts(max_lookback)`
  合并进 prev_alerts，与 `_latest_result.alerts` 拼接。`_dedup_by_time_bucket`
  按 `(rule_id, server, login, symbol)` 取最新 `scanned_at`，所以拼接安全。
  之前版本「仅在 in-memory 无 QP 告警时才 seed」的条件会在「新告警 + 老告警
  共存」时把老告警从 dedup 池里挤掉 → 老告警在 lookback 窗口内复发，已 2026-05-12 修复
- **只允许一个 scheduler 写入这张 SQLite**：dev 容器必须 `BURST_SCAN_ENABLED=false`，
  否则 dev + prod 双扫描器对同一 `alert_events` 各写一份，
  每个进程的 `_latest_result` 互相看不到对方的 emit，dedup 链路断裂

### 7.7 CEN 归一化

Realized + floating 在 enrichment 后按 `currency='CEN'` 整体 ÷100；
然后**重新对照 `min_profit_usd` 阈值**过滤一次，避免 CEN 账户的"看起来超阈"
（实际只是分而不是美元）误报。

> **关键 chain**：`scan_quick_profit` 里 `norm_rules.id` 必须等于
> `QUICK_PROFIT_RULE_ID_BASE + idx`（业务 rule_id），不能用 SQLite 主键。
> 否则 detect 写出的 alert.rule_id（61, 62…）和 `min_by_rule` 里的键
> （SQLite PK 1, 2…）对不上，二次过滤回退到 0，所有 CEN 归一化后的
> 低值告警都漏过来。这条对应回归测试
> `test_detect_overrides_sqlite_pk_with_business_rule_id`。

### 7.8 实现文件

| 改动 | 文件 |
|------|------|
| 新增 | `backend/app/services/rule_quick_profit_service.py` |
| 新增 | 6 端点 in `backend/app/api/v1/routes/risk_monitor.py` |
| 改动 | `backend/app/services/account_enrichment.py` (新增 `get_net_deposit_hist_map`，三 tab 共用) |
| 改动 | `backend/app/schemas/risk_monitor.py` (`QuickProfitRule`/`Config` + `AlertEvent` 4 个新字段：`net_deposit_hist`/`realized_profit`/`floating_profit_snapshot`/`position_status` + `QuickProfitFloatingRefresh*`) |
| 改动 | `backend/app/core/risk_monitor_db.py` (新表 `quick_profit_config` + `quick_profit_rules`，`alert_events` 列迁移，`get_alerts_by_ids`) |
| 改动 | `backend/app/core/burst_open_scheduler.py` (`_run_scan` 串入 `scan_quick_profit`) |
| 改动 | `frontend/src/pages/RiskMonitor.tsx` (`QuickProfitTab` + `PositionStatusBadge` + `QuickProfitConfigDrawer`) |
| 测试 | `backend/tests/test_rule_quick_profit_service.py`、`test_quick_profit_floating_refresh.py`、`test_quick_profit_api.py` |

### 7.9 关键决策

1. Rule ID 段 61-70（`QUICK_PROFIT_RULE_ID_BASE = 61`）
2. 触发条件：`total_profit_usd >= min_profit_usd`（含等号）
3. 浮动刷新：用户手动点击工具栏按钮触发（不再 30s 自动轮询，避免后台异步覆盖告警快照）
4. 历史净入金（`net_deposit_hist`）**四 Tab 共用**（含 Gap Trade），公式与 client-return-rate 一致；CEN 账户在 SQL 内 ÷100；红绿配色仅作展示
5. Phase 1 不做"利润 > N% 入金"比较；Phase 2 用 `net_deposit_hist` 做入金比例规则
