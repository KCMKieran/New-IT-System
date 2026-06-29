---
id: OPT-0040
title: XAUUSD 持仓分钟级快照记录 + /position 页顶部 24h 图表 + 自定义范围 CSV 导出
status: done
priority: P1
area: mixed
effort: L
created: 2026-06-29
related: [[OPT-0006]] [[OPT-0035]]
---

## 需求

在 `/position` 页（`http://10.6.20.138:5173/position`）**最上方**加一块 XAUUSD 持仓实时记录：

1. **分钟级快照**：每 1 分钟记录一次 XAUUSD 相关 symbol 的总持仓（Buy 手数、Sell 手数、Net = Buy − Sell）。
2. **区分维度**：按 **server × 产品** 明细记录——3 个 server（mt4_live / mt4_live2 / mt5）× 各 XAUUSD 前缀产品（`XAUUSD` / `XAUUSD.kcmc` / `XAUUSD.cent` …），每个组合一行。
3. **24h 图表**：页面顶部一张 recharts 折线图，**同时画 Buy / Sell / Net 三条线**，窗口为过去 24 小时。
   - ⚠ 存储是 1min 粒度，但**图表按 5min（或 10min）降采样**显示以减少数据点（24h × 1min = 1440 点/序列太多）。降采样在**后端 history 端点**做，前端只渲染降采样后的点。
4. **"记录中"状态指示**：图表区显示绿点 + 最后一次快照时间（复用页面已有 Badge 模式）。
5. **CSV 导出**：用户**自定义时间范围**导出明细快照。数据保留 **60 天**。

用户原话："记录 XAUUSD 相关 symbol 公司的总持仓（Buy、Sell 及 Net Position）的实时记录，分钟级。区分不同 server 和不同产品，但都是 XAUUSD 开头的。图表显示过去 24h 变化，显示'再记录中'，给出汇出 CSV 功能，用户自定义选择时间范围。图表放 /position 页最上方。图表 5min/10min 降采样减少数据量。CSV 保留 60 天。"

## 锁定的决策（用户已拍板 2026-06-29）

| 决策 | 选择 |
|---|---|
| 快照存哪里 | **复用 `backend/data/risk_monitor.db`，加一张新表**（不新建 DB 文件） |
| 记录粒度 | **按 server × 产品 明细**（不聚合） |
| 图表指标 | **同时画 Buy / Sell / Net 三线** |
| 图表降采样 | **5min（或 10min）桶**，在后端 history 端点降采样 |
| 数据保留 | **60 天** |
| 图表位置 | `/position` 页（`Position.tsx`）**最顶部** |

## 背景（涉及文件 + 行号，已调查）

### 前端
- 路由：`frontend/src/App.tsx:92` → `<Route path="position" element={<PositionPage />} />`
- 页面组件：`frontend/src/pages/Position.tsx`。当前展示：跨 server XAUUSD/XAGUSD 汇总区 + 5 个 stat card + server 切换工具栏 + 按 symbol 聚合的主表（TanStack react-table）。Net 当前在前端算 = `volume_buy - volume_sell`。
- 图表库：`recharts ^2.15.4` 已装且多页在用（`Profit.tsx` / `DashboardPnlHistory.tsx`），shadcn 封装在 `frontend/src/components/ui/chart.tsx`。**无需新增依赖**。
- 前端 fetch 用 `apiFetch()`（`@/lib/fetch`，自动注入 X-API-Key）。useEffect 取数必须用 `AbortController`（StrictMode 去重），catch 忽略 `AbortError`。

### 后端 — 数据源（已存在，需小改）
- 路由：`backend/app/api/v1/routes/open_positions.py`
  - `GET /api/v1/open-positions/today?source=...`（按 symbol 分组，单 server，所有产品）
  - `GET /api/v1/open-positions/symbol-summary?symbol=XAUUSD`（3 server 并行 + 模糊匹配 `XAUUSD%`，但**聚合成每 server 一行**，丢产品维度）
- service：`backend/app/services/open_positions_service.py`
  - 已有 3-server 并行（ThreadPoolExecutor，3 workers）、cent 账户除 100、未平仓过滤 `closeDate='1970-01-01'`、`CMD IN (0,1)`（0=Buy 1=Sell）、排除 demo/test + `LOGIN LIKE '7%'`。
  - SID 映射：`mt4_live`=1、`mt4_live2`=6、`mt5`=5。
  - **缺口**：symbol-summary 用 `GROUP_CONCAT` + SUM 把一个 server 下所有 `XAUUSD*` 合成一行。本需求要产品明细 → 需要一个新查询函数：`SYMBOL LIKE 'XAUUSD%'` + `GROUP BY t.SYMBOL`，返回 (server, symbol, volume_buy, volume_sell) 明细行。可照搬 `/today` 的 `GROUP BY t.SYMBOL` 写法加 `LIKE 'XAUUSD%'` 过滤。
- schema：`backend/app/schemas/open_positions.py`（`OpenPositionsRow` / `SymbolSummaryRow`，字段 volume_buy/sell, profit_buy/sell/total；**后端不算 net**）。
- 数据库：MySQL 只读副本 `mt4_trades` 表。关键列：`sid`, `SYMBOL`, `CMD`(0 buy/1 sell), `lots`(虚拟列), `totalProfit`, `closeDate`(='1970-01-01' 为未平仓), `LOGIN`。cent 账户：`SYMBOL LIKE '%.kcmc' OR '%.cent'` 时 `lots/100`。

### 后端 — 存储（复用 risk_monitor.db）
- `backend/data/risk_monitor.db`，初始化在 `backend/app/core/risk_monitor_db.py:567-577`。已开 **WAL**（`journal_mode=WAL, synchronous=NORMAL, busy_timeout=5000`），已有时序结构 + 30 天清理模式（`DELETE ... WHERE scanned_at < datetime('now','-30 days')`）+ 原子批量写入函数 `append_scan_and_events()`（:1620-1745，可参考写法）。
- 新增表（建在 `risk_monitor_db.py` 的 init 里，CREATE TABLE IF NOT EXISTS）：
  ```sql
  CREATE TABLE IF NOT EXISTS xauusd_position_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    captured_at  TEXT NOT NULL,   -- ISO8601 UTC, e.g. 2026-06-29T06:32:00Z
    server       TEXT NOT NULL,   -- mt4_live | mt4_live2 | mt5
    symbol       TEXT NOT NULL,   -- XAUUSD | XAUUSD.kcmc | XAUUSD.cent ...
    volume_buy   REAL NOT NULL,
    volume_sell  REAL NOT NULL,
    net_position REAL NOT NULL    -- volume_buy - volume_sell（存好，省得前端反复算）
  );
  CREATE INDEX IF NOT EXISTS idx_xau_snap_time ON xauusd_position_snapshots(captured_at DESC);
  CREATE INDEX IF NOT EXISTS idx_xau_snap_srv_sym ON xauusd_position_snapshots(server, symbol, captured_at DESC);
  ```
- 体量：约 3 server × 3 产品 ≈ 9 行/分钟 ≈ 1.3 万行/天，60 天 ≈ 78 万行——SQLite 无压力。

### 后端 — 调度（APScheduler 框架已在用）
- 已有多个 scheduler（`backend/app/core/scheduler.py`、`burst_open_scheduler.py`、`fund_flow_scheduler.py` 等）。
- 新建 `backend/app/core/xauusd_snapshot_scheduler.py`，`IntervalTrigger(minutes=1)`：跑新明细查询 → 批量 INSERT 一批快照行（统一 `captured_at`）→ 顺手做 60 天保留清理。
- 参照现有 scheduler 的注册方式接入（启动钩子、env 开关如 `XAUUSD_SNAPSHOT_SCHEDULER_ENABLED`，prod=true / dev 按需）。注意时区：后端存 UTC ISO8601（`...Z`），前端按 `Asia/Hong_Kong` 渲染。

## 验收标准（AC）

1. `risk_monitor.db` 新增 `xauusd_position_snapshots` 表，schema 如上，含两个索引。
2. 新查询函数返回 (server, symbol, volume_buy, volume_sell, net_position) 明细行，覆盖 3 server × 所有 `XAUUSD%` 产品，cent 账户已除 100，复用现有 demo/test 排除逻辑。
3. 新 scheduler 每 1min 写一批快照；带 60 天保留清理；有 env 开关。
4. 新增 2 个只读端点：
   - `GET /api/v1/xauusd-positions/history?hours=24&bucket_min=5`：返回**降采样后**的时间序列（每桶取桶内最后一条快照，按 server×symbol 分序列），供三线图（Buy/Sell/Net）渲染。`bucket_min` 可选 5 或 10。
   - `GET /api/v1/xauusd-positions/export?start=...&end=...`：按用户自选范围返回明细快照 CSV（量级小，可同步生成；范围内行数即 1min 全量）。
5. `Position.tsx` **顶部**新增图表区：recharts 折线图（Buy/Sell/Net 三线，24h，可按 server×产品筛选/分组）+ "记录中" Badge（绿点 + 最后快照 HK 时间）+ CSV 导出按钮 + 时间范围选择器。
6. 走 `verify.sh` 红绿闸门（tsc + vitest + pytest）全绿。

## 开放问题（执行时定，非阻塞）

- history 端点降采样默认 `bucket_min`：建议默认 **5**，前端给 5/10 切换。桶内取"最后一条"（position 是 stock 量，最新状态最有意义），不要 sum/avg。
- 图表三线 × 多 server×产品序列可能线条过多——执行时考虑：默认聚合所有 server×产品成 3 条总线（Buy/Sell/Net 全公司合计），再给筛选器下钻到单 server / 单产品。与用户确认或按此默认实现。
- CSV 时间范围选择器：复用页面/项目已有的 time range 组件（若无则用 shadcn 的 date range picker）。

## 结果

实施于 2026-06-29（branch `opt/xauusd-position-snapshots`）。

### 交付 vs AC

| AC | 状态 | 说明 |
|---|---|---|
| 1. 新表 + 2 索引 | ✅ | `xauusd_position_snapshots` 表 + `idx_xau_snap_time` / `idx_xau_snap_srv_sym` 加进 `risk_monitor_db.py` 的 `_SCHEMA_SQL`（CREATE TABLE/INDEX IF NOT EXISTS），复用 `risk_monitor.db`（无新 DB 文件）。 |
| 2. 明细查询函数 | ✅ | `open_positions_service.get_xauusd_position_detail()`：3 server 并行（ThreadPoolExecutor）、`SYMBOL LIKE 'XAUUSD%'` + `GROUP BY t.SYMBOL`、cent 账户 /100、复用 `_get_excluded_groupsids` + `LOGIN NOT LIKE '7%'` 排除逻辑、`net_position = buy - sell`（`compute_net_position()` helper）。 |
| 3. 1min scheduler + 60 天保留 + env 开关 | ✅ | `core/xauusd_snapshot_scheduler.py`，`IntervalTrigger(minutes=1)`，env `XAUUSD_SNAPSHOT_SCHEDULER_ENABLED`（默认 true），启动即先抓一次。保留清理在写入路径 `append_xauusd_snapshots()`（`_XAUUSD_SNAPSHOT_RETENTION_DAYS = 60`）。在 `main.py` lifespan 按现有模式注册（受 scheduler flock 选主保护）。 |
| 4. 2 只读端点 | ✅ | `GET /api/v1/xauusd-positions/history?hours=24&bucket_min=5[&server=&symbol=]` 与 `GET /api/v1/xauusd-positions/export?start=&end=`（CSV）。 |
| 5. Position.tsx 顶部图表 | ✅ | `components/position/XauusdPositionChart.tsx` 渲染在页面最顶部：recharts 三线图（Buy/Sell/Net）、"记录中" 绿点 Badge + 最后快照 HK 时间、5/10min 桶切换、server/产品筛选下拉、自定义范围 CSV 导出（apiFetch blob 下载，带 X-API-Key）、60s 轮询。 |
| 6. verify.sh 全绿 | ⚠ 见下 | 我的改动这一切片全绿；但 backend pytest 闸门因**与本 OPT 无关的预存在 date-rot 失败**整体红。 |

### 降采样如何工作（history 端点）

纯函数在 `services/xauusd_snapshot_service.py`（经 outsider-review 修正后为**单步、瞬时一致**）：
1. `fetch_xauusd_snapshots(start, end, server=, symbol=)` 取窗口内原始 1min 行，server/symbol 过滤**下推到 SQL**（用 `idx_xau_snap_srv_sym` 索引）。下拉选项由 `fetch_xauusd_distinct_dimensions` 单独取（不受筛选影响）。
2. `aggregate_points(rows, bucket_min)`：按 `bucket_start()`（下取整到 5/10min 边界）分桶；**每桶找出该桶内的最大 captured_at（最近真实瞬时），只对该瞬时的行跨 series 求和** → `{time, buy, sell, net}` 升序。position 是 stock 量：桶代表取「最近一个真实快照时刻的公司合计」，中途已平仓的 series（该时刻无行）正确计 0，不再把桶内更早的过期值带进合计（修复 review finding A 的「幻影持仓」）。`bucket_min` 仅接受 5/10，其它回落 5。

### verify.sh 结果（outsider-review 修复后，commit `dbabb9a`）

```
frontend tsc    ✓ PASS
frontend vitest ✓ PASS
backend pytest  ✗ FAIL — 40 failed, 281 passed
VERIFY: FAIL — backend pytest (预存在 date-rot，与本 OPT 无关)
```

**这 40 个失败与本 OPT 无关，是预存在的 date-rot**：对照实验——把本 OPT 改动 stash 掉后 baseline 仍是 `40 failed`；加上本 OPT 后仍是 `40 failed / 281 passed`（本 OPT 14 个新/改测试全过，**0 新增失败**）。根因：`test_net_profit_sort.py` / `test_hedge_open_aggregated.py` / `test_leverage_abuse_filter.py` / `test_burst_open_aggregated.py` 把种子 `scanned_at` 硬编码在 `2026-05` 附近，当前日期 `2026-06-29` 已超 30 天保留窗口，`append_scan_and_events` 的清理在 seed 后立即删行 → 查询返回 0。**已另立跟进 OPT 修复（把这些测试的固定日期改成相对 now，恢复 verify 闸门可信度）。**

本 OPT 自身切片全绿：`test_xauusd_snapshot_service.py` 14 个测试（net_position + 桶瞬时聚合含掉线 series 用例 + 导出范围校验 + server/symbol SQL 过滤 + 流式 + 60 天清理 Z 格式边界 + 单 server 失败容错）全过；frontend tsc + vitest 全过；`XauusdPositionChart.tsx` eslint 0 error。

### Stage 1 outsider-review 处理记录（冷审 → 10 条 finding 全部当场修，commit `dbabb9a`）

冷审（无前置 context 的独立 reviewer）发现 10 条，curate 后用户选「全部当场在 branch 上修」：

| # | 严重度 | finding | 处理 |
|---|---|---|---|
| A | 🔴 正确性 | 降采样跨瞬时求和 → 桶边幻影 Net（series 中途平仓时旧值仍被求和） | 修：`aggregate_points` 改为「每桶只对最近瞬时的行求和」，掉线 series 计 0；加 dropout 用例测试 |
| B | 🔴 scaling | CSV 导出无范围上限 + 全量物化，长读饿死 WAL checkpoint → 其他 scheduler `SQLITE_BUSY` 丢 tick | 修：服务端 7 天上限（超限/非法 400）+ `StreamingResponse` + `fetchmany(1000)` 游标流式 |
| C | 🔴 scaling | `/history` `hours` 上限 1440（60d），可拉 78 万行进 Python | 修：上限降到 48h；server/symbol 过滤下推 SQL；下拉选项走单独 DISTINCT |
| D | 🔴 故障 | pymysql 无 connect/read 超时，副本卡死则快照永久冻结 | 修：两处 connect 加 `connect_timeout=5, read_timeout=20` |
| E | 🟡 | `executor.map` all-or-nothing：一个 server 失败 → 全部 server 零行 | 修：每 server 独立 try，healthy 的照写；items 空时 scheduler 跳过写入（避免假性「全平仓」） |
| F | 🟡 | 前端 60s poll 的 AbortController 从不 abort（泄漏 + 无 last-write-wins 守卫 + spinner 闪烁） | 修：controller 存 ref、每 tick/卸载 abort、`{background}` 跳过 setLoading、isMountedRef 守卫 |
| G | 🟡 | `idx_xau_snap_srv_sym` 死索引（过滤在 Python）→ 纯写放大 | 由 C 解决：过滤下推 SQL 后该索引被用上 |
| H | 🟡 | CSV 日界用浏览器本地时区，非 HK 浏览器导出窗口与图表不符 | 修：`onExport` 显式按 HK（UTC+8 固定）构造日界再 `toISOString()` |
| I | ⚪ | `_get_excluded_groupsids` docstring 称「cached per request」实则每 tick 重查（第 4 个 MySQL 查询/min） | 修：加模块级 3600s TTL 缓存（Lock 守卫），docstring 改为属实 |
| J | ⚪ | 60 天清理 cutoff 格式（空格无 Z）与 `captured_at`（`...T..Z`）字典序比较错位（宽松 <1 天） | 修：cutoff 改 `strftime('%Y-%m-%dT%H:%M:%SZ','now','-60 days')`，比较精确 |

冷审同时确认 scheduler flock 选主 + insert/purge 原子事务**无跨 worker 双写**，设计正确。

### 开放问题的默认决策

- **多 series 线条过多** → 默认聚合全部 server×产品为 3 条公司合计线（Buy/Sell/Net），图表上给 server 下拉 + 产品下拉下钻到单 server / 单产品（"全部" 为默认）。后端 history 端点接 `server`/`symbol` 可选参数实现。
- **降采样默认桶** = 5min，前端给 5/10 切换胶囊。
- **CSV 范围选择器** = 复用 `DashboardPnlHistory` 同款 shadcn `Calendar` range popover；默认范围近 24h；起止按浏览器（HK）日界转 UTC ISO（`toISOString()` 带 Z）传给后端。

### 跟进项 / 注意

- ⚠ **上线时发现并修正**：原以为「flock per-container 选主 → dev/prod 不双写」是**错的**。dev 挂载 `backend -> /app`、prod 挂载 `backend/data -> /app/data`，指向**同一个** `backend/data/risk_monitor.db`；flock 只在容器内部去重 worker，**不跨容器**——dev leader 与 prod leader 各自往同一文件写，确实双写（实测 dev `:21` 节奏 + prod `:33` 节奏，无重复键但双倍写入 + 双倍 MySQL 负载）。修复：`backend/docker-compose.dev.yml` 加 `XAUUSD_SNAPSHOT_SCHEDULER_ENABLED=false`，与既有 `BURST_SCAN_ENABLED=false`/`GAP_TRADE_SCAN_ENABLED=false` 同约定（dev 共享 prod SQLite，所有 scheduler 在 dev 必须显式关）。已重启 dev 验证停写。
- ✅ MySQL 联调已在 prod 完成（之前本环境无副本访问）：scheduler 启动即写入首批 **9 行/tick**（3 server × 各 XAUUSD 产品），数值合理、`net=buy-sell` 正确、UTC Z 格式正确。明细 SQL（`/today` 写法 + `LIKE 'XAUUSD%'`）在真副本跑通。
- `view-profiles` manifest 未纳入本图表的筛选偏好（bucket/server/symbol 为调查上下文，按约定不持久化）。
