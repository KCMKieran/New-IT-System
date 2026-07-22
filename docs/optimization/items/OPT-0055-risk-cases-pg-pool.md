---
id: OPT-0055
title: risk-cases 路由脱离事件循环（def 化）+ 云 PG 连接池
status: done
priority: P2
area: backend
effort: S
created: 2026-07-22
related: [[OPT-0054]], [[OPT-0047]]
---

## 问题

risk-cases 三个只读路由（watchlist / open-positions / case_detail）都是 `async def`
却直接跑**同步阻塞**的 psycopg2 / PyMySQL 调用——每个请求把整个 uvicorn 事件循环
卡住 1s+。并发请求（多 tab 轮询、watchlist + open-positions 同时加载）或撞上 60s
调度任务时**串行排队**，延迟成倍放大。

另外 `risk_cases_conn()` **每请求新建一条到 Azure 云 PG 的连接**（WAN + TLS 握手），
实测建连本身 412ms——纯浪费，且叠加在每一发查询上。

**实测证据（2026-07-22，HAR + 容器内逐段计时）**：

| 环节 | 耗时 |
|---|---|
| Azure PG connect（每请求新建，TLS over WAN） | **412ms** |
| `_OPEN_POSITIONS_SQL`（5-CTE 聚合）+ fetch | 792ms |
| CRM MySQL zipcode 查询 | 79ms |
| **空闲时总计** | **~1.33s** |
| HAR 实测 wait（撞上并发/调度时） | **2.7s / 5.2s**（`query_time_ms` 仍只有 ~1.3s → 差值全是事件循环排队） |

## 背景（改哪里）

- 路由：[`backend/app/api/v1/routes/risk_cases.py`](../../../backend/app/api/v1/routes/risk_cases.py)
  —— `watchlist`（L39）、`open_positions`（L97）、`case_detail`（L129）三个
  `async def`，全部只做同步 DB 调用，无任何 `await`。
- 连接层：[`backend/app/core/risk_cases_pg.py`](../../../backend/app/core/risk_cases_pg.py)
  —— `connect_risk_cases()`（L206，每次 `psycopg2.connect`，`connect_timeout=5`）+
  `risk_cases_conn()` contextmanager（L232，本 OPT 动的就是这层的**内部**）。
- `risk_cases_conn` 的**其他调用方**（不许破坏）：
  `services/case_engine_service.py`、`services/case_metrics_service.py`（调度器
  **后台线程**里跑的写管道）+ `services/risk_cases_service.py`（API 读路径）。
  → 池必须**线程安全**（`psycopg2.pool.ThreadedConnectionPool`）。
- 部署形态：prod 4 个 uvicorn worker（`backend/Dockerfile:28`）→ 池是**每进程一个**，
  总连接数 = maxconn × 4，取小值（maxconn 4 → 上限 16，Azure flexible server 无压力）。

## 方案要点

1. **路由 def 化**：`routes/risk_cases.py` 三个 handler `async def` → `def`
   （FastAPI 自动丢线程池跑，事件循环不再被 DB 调用卡住）。函数体**不动**
   （open_positions 函数体是 OPT-0054 的地盘，两 OPT 并行开发，只有签名行相邻）。
2. **连接池**：`core/risk_cases_pg.py` 内部用 `ThreadedConnectionPool`
   （minconn 0 或 1、maxconn 4），`risk_cases_conn()` 对外接口**完全不变**
   （callers 零改动）。要点：
   - **fail-open 契约原样保留**：PG 不可达 → `RiskCasesUnavailable`（API → 503、
     写管道 → 下 tick 重试）；池本身创建失败也走同一异常，绝不让 app 启动挂掉。
   - **陈旧连接处理**：Azure 会掐空闲连接。borrow 后先 `SELECT 1` 探活
     （<1ms，相对 412ms 重建连接完全值得），死连接丢弃重借/重建一次。
   - **归还语义**：正常归还池；异常时 rollback 后归还；连接已坏则 `putconn(close=True)`。
   - **池耗尽**：`getconn` 抛 `PoolError` 时降级为直连一次性连接（保可用性）。
3. **不做**：跨 worker 共享池（pgbouncer 级别，杀鸡用牛刀）；其他模块的 MySQL 连接
   池化（另一个话题）。

## 验收标准

- [ ] 三个 handler 均为 `def`（threadpool 执行）；两个并发的 open-positions 请求
      总耗时 ≈ 单发耗时（不再 2 倍串行）——并发实测或事件循环无阻塞的等价证明。
- [ ] 复用池后 open-positions 空闲 P50 从 ~1.33s 降到 ~0.9s（省掉 412ms 建连）。
- [ ] PG down：API 仍 503（`RiskCasesUnavailable`）、app 启动正常、调度写管道
      fail-open 不崩——现有行为一个不变。
- [ ] 陈旧连接：手动 kill 池内连接后下一请求自动恢复（不 5xx）。
- [ ] `backend/tests/test_risk_cases_api.py` 既有测试不破；新增池行为单测
      （复用 / 探活丢弃 / 耗尽降级 / fail-open）。

## 假设 / 待验证

- [ ] `conn.cursor_factory = RealDictCursor` 是连接级属性——归还池前是否要重置？
      （所有 caller 都走 `risk_cases_conn` 统一设置，实际无影响，确认即可。）
- [ ] Azure PG 空闲掐线的实际 idle timeout（探活兜底后无所谓，记录即可）。

## 笔记

- 与 [[OPT-0054]]（TTL 缓存 + singleflight）同日并行开发，**合并顺序固定
  0055 → 0054**：singleflight 的 `threading.Event.wait` 必须踩在 def 化
  （threadpool）之上才不会卡事件循环。
- 本 OPT 是「连接/并发层」，0054 是「缓存层」，正交；0054 命中缓存后本 OPT
  收益主要体现在 cache-miss 发和 watchlist / case_detail 两条未缓存路由上。

## 结果

2026-07-22 close，两个 commit（`7f688d8` 实现 + `9b88b2b` 冷审硬化）。

**交付 vs AC**：三个 handler 全部 `def` 化（线程池执行）✅；`ThreadedConnectionPool`
（minconn 1 / maxconn 8——冷审后从 4 提到 8）✅；fail-open 契约原样保留 ✅；
陈旧连接探活 + 重试上界 `POOL_MAX_CONN + 1`（冷审 finding：原 2 次上界在隔夜
全池同死时会假 503）✅；测试 30 passed（含 live-PG 集成，端到端验证硬化 DSN）✅。
「并发实测 P50 0.9s」两条 AC 留待 prod 部署后验证（worktree 无真实流量）。

**冷审（outsider-review）处理记录**——6 条全部当场修（`9b88b2b`）：
1. 重试上界 2 → `POOL_MAX_CONN + 1`
2. DSN 硬化：keepalives ×4 + `sslmode=require` + `application_name` +
   `statement_timeout=30000`（30s 不能再低——写管道共享 DSN，锁等待计入）
3. 查询中途连接死亡 `OperationalError`/`InterfaceError` → 重新抛
   `RiskCasesUnavailable`（503 可重试，不再 500）；`QueryCanceled` 是
   `OperationalError` 子类，超时同样走 503
4. `close_risk_cases_pools()` 接入 lifespan teardown（调度器停止之后）
5. 池创建移出 `_POOL_LOCK`（double-checked）+ 5s failure cooldown fail-fast
   （PG 故障时不再全 app 串行放大）
6. maxconn 8，注释如实写 8×4 worker = 32 池内 + 瞬态直连溢出

**Follow-up（live with，冷审 nice-to-have）**：
- 探活是 2 次 WAN 往返（非 <1ms）——keepalives 落地后可改「空闲 >N 秒才探活」
- 线程池饥饿观察项：sync 路由共享每 worker 40 token 的 anyio 池，viewer 上
  30+ 且 p95 >1s 时再动（观察 `query_time_ms`）
- 多线程并发 borrow 的压测型测试未写（现有单测覆盖机制、不覆盖并发压力）
