---
id: OPT-0040
title: XAUUSD 持仓分钟级快照记录 + /position 页顶部 24h 图表 + 自定义范围 CSV 导出
status: wip
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

（实施后填写）
