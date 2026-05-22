---
id: OPT-0028
title: Risk-monitor 聚合视图硬化（SQL 性能 + 语义确定性，同时覆盖 burst + hedge）
status: ready
priority: P2
area: mixed
effort: M
created: 2026-05-22
related: [[OPT-0021]], [[OPT-0027]]
---

## 问题

[[OPT-0027]] 把对冲刷单 tab 的「聚合 / 已聚合」按钮模式（[[OPT-0021]] 沉淀）
横向复制到批量下单 tab，OPT-0027 close 前的 outsider-review 暴露出 **4 条**
**同时影响 burst-open 和 hedge-open 两个 aggregator** 的硬化空间，本 OPT 把它们包成一组一起修。

不是单独修一个聚合 endpoint 的「孤儿」OPT——目标是让聚合视图模式在第 3 个 tab（如未来 QOC / QP）想用时**直接抽 helper**，不会再继承本 OPT 提到的几个坑。

## 背景

### 现状

- 两个 aggregator 都活在 `backend/app/core/risk_monitor_db.py`：
  - `aggregate_hedge_open_by_login`（[[OPT-0021]]，2026-05-20 上线，rule_id 91-100）
  - `aggregate_burst_open_by_login`（[[OPT-0027]]，2026-05-22 上线，rule_id 1-50）
- 两个对应的 route 在 `backend/app/api/v1/routes/risk_monitor.py`
- 共享 `_ALERT_FROM_CLAUSE`（带 5 个 detail LEFT JOIN）+ `_build_alert_filters`

### 4 条硬化项（reviewer 命名为 F2/F3/F4/F6，每条都 burst + hedge 同修）

#### F2 — Aggregator 拖 4 个 NULL LEFT JOIN

`_ALERT_FROM_CLAUSE` 包含 `qoc / qp / gso / gp / ho` 五个 detail 表的 LEFT JOIN。
- burst-open 行：**五个都 NULL**（rule_id ≤ 50 不写任何 detail 表）
- hedge-open 行：只 `ho` 非 NULL，其余四个 NULL

每行 5 次（hedge 4 次）多余的 index lookup。`_ALERT_FROM_CLAUSE` 是给详情查询（需要展示窗内订单 / 拆分手数等）设计的，对**聚合**路径来说不需要——聚合 SELECT 里压根没引用 detail 字段（hedge agg 引用 `ho.buy_count/sell_count/buy_lots/sell_lots`，burst agg 一个都没引用）。

**Fix 方案**：
- 给 aggregator 用单独的 slim from clause（只 `FROM alert_events ae`）
- 对 hedge：扩展到 `FROM alert_events ae LEFT JOIN alert_hedge_open_detail ho ON ho.id = ae.id`（只保留这一个）
- 不要把 `_ALERT_FROM_CLAUSE` 改窄——详情路径依赖它

#### F3 — `count_sql` 重扫一遍同窗口

```python
sql = "... CTE ranked → agg → JOIN ranked latest WHERE rn=1 ..."
count_sql = """
    SELECT COUNT(*) FROM (
        SELECT 1 FROM alert_events ae ... GROUP BY ae.server, ae.login
    )
"""
```

`count_sql` 几乎 SQL 等价于一次额外的 CTE 物化——同时间窗、同过滤、同 GROUP BY 把 ~100k 行扫两遍。

**Fix 方案**：把 distinct (server, login) 计数放到主 CTE 里：

```sql
WITH ranked AS (... ROW_NUMBER() ...),
agg AS (
    SELECT server, login, ..., COUNT(*) OVER () AS total_distinct
    FROM ranked GROUP BY server, login
)
SELECT agg.*, ... FROM agg JOIN ranked latest ...
```

一次 round-trip 拿走 entries + total。Python 侧从 `entries[0].total_distinct` 读 total（如果 entries 为空，count = 0）。

#### F4 — `GROUP_CONCAT(DISTINCT symbol)` 顺序未定义 → 佣金抖动

SQLite 文档明确 `GROUP_CONCAT` 的输出顺序 **undefined**。
- 两次刷新可能拿到 `"XAUUSD,EURUSD"` 或 `"EURUSD,XAUUSD"`
- 前端 `estCommissionColDef` 取 `symbols.split(",")[0]` 作为「主 symbol」算佣金
- ⇒ **多 symbol 账户的估算佣金在两次刷新之间会跳**，分析师对该列失去信任

**Fix 方案（任选）**：
- SQL 侧：
  ```sql
  (SELECT GROUP_CONCAT(s, ',') FROM (SELECT DISTINCT symbol AS s FROM ranked
   WHERE ranked.server = agg.server AND ranked.login = agg.login ORDER BY s))
  ```
- Python 侧（更简单）：在 `entries` 后处理时 `r["symbols"] = ",".join(sorted(r["symbols"].split(",")))`

倾向 Python 侧——避免 SQL 越来越绕。

#### F6 — `total_lots` 双语义同名

| Aggregator | `total_lots` 含义 |
|---|---|
| hedge-open | `SUM(total_lots)` = 双向之和 = **2× 实际对冲量** |
| burst-open | `SUM(total_lots)` = 普通累加 |

同字段名、同 JSON key、同列 headerName，完全不同的算术意义。下次 QOC tab 想抄一份的 dev 有 50/50 概率挑错语义。OPT-0027 已经在文件「反观点」段 + 列 tooltip + helper 注释（`aggregate_burst_open_by_login` 顶上）三处提到，但**字段名本身不变**仍然是 footgun。

**Fix 方案（reviewer 建议）**：
- 把 hedge 的字段重命名为 `total_lots_double_sided`（或在 schema 加一个 `hedged_lots = MIN(buy_lots, sell_lots)` 并把 `total_lots` 改为普通 sum，统一两个 tab）
- 同步改前端 column field + headerName + tooltip + 任何外部消费者

这是个 breaking change（聚合视图 API 的 row shape 改），需要前后端同改。

## 验收标准

### Backend

- [ ] `_ALERT_FROM_AE_ONLY` 或类似常量在 `risk_monitor_db.py`：`"FROM alert_events ae"`（仅主表）
- [ ] `_ALERT_FROM_AE_AND_HEDGE`：`"FROM alert_events ae LEFT JOIN alert_hedge_open_detail ho ON ho.id = ae.id"`
- [ ] `aggregate_burst_open_by_login` 用 `_ALERT_FROM_AE_ONLY`
- [ ] `aggregate_hedge_open_by_login` 用 `_ALERT_FROM_AE_AND_HEDGE`
- [ ] 两个 aggregator 把 distinct (server, login) 计数合并进主 CTE，去掉独立 `count_sql`
- [ ] 两个 aggregator 在返回前 Python 侧 sort `symbols` 字符串
- [ ] `HedgeOpenAggregatedRow.total_lots` 改名为 `total_lots_double_sided` **或** 新增 `hedged_lots`，前者更明确推荐；同步改 schema + frontend
- [ ] 性能基准：在 ~100k 行测试 DB 上，单次 aggregated query 从当前 ~250ms 降到 < 100ms（≥ 2x 提速）

### Frontend (`RiskMonitor.tsx`)

- [ ] hedge-open / burst-open 的聚合列 def 同步改字段名（如果 F6 走重命名方案）
- [ ] tooltip 文案对齐新字段名
- [ ] `estCommissionColDef` 拿 `symbols.split(",")[0]` 这段不需要改（SQL 侧已经排好序）

### Tests

- [ ] `test_burst_open_aggregated.py` + `test_hedge_open_aggregated.py` 各加 1 个 test：连续两次调用 `aggregate_*_by_login` 返回的 `symbols` 字符串完全相同（pin 顺序确定性）
- [ ] 加一个 perf smoke test：seed ~5000 rows，aggregator 跑 < 50ms
- [ ] 现有 16 + 19 测试全过

### 文档

- [ ] `.cursor/skills/risk-monitor/SKILL.md` File Map 段：把两个 aggregator 的注释更新到新模式
- [ ] OPT-0028 close body 写明 burst 和 hedge 两侧都改了哪些点 + 性能基准前后对比

## 假设 / 待验证

- [ ] F1（reviewer 提议加复合索引 `(server, login, scanned_at DESC, id DESC)`）已经在 OPT-0027 review 阶段实测**无效**：当前数据量下 planner 不挑这个索引，查询时间 247ms vs 246ms 无差异。本 OPT 不加这个索引——如果做完 F2 后 EXPLAIN 还能看到 TEMP B-TREE 再考虑
- [ ] F6 的 `total_lots_double_sided` 重命名是 breaking change：本 OPT 上线时旧的 hedge 聚合 column persist localStorage 会失效一次（列 hidden / order 被重置）；用户能接受？还是要走双字段过渡（先加 `total_lots_double_sided`，下个版本删 `total_lots`）？claim 时定
- [ ] hedge agg 的 `buy_lots_sum` / `sell_lots_sum` 留着吗？如果 F6 引入 `hedged_lots`，buy/sell 拆分还有意义吗？claim 时定

## 笔记

- 这条 OPT 是 [[OPT-0027]] outsider-review 的派生 hardening。reviewer 总共提了 12 项，OPT-0027 close 时**当场修了 F1 / F8 / F12**（F1 测了无效就回滚 + 加注释；F8 聚合模式 disable CSV 按钮；F12 加引导 3rd-copy 抽 helper 的注释）。**F2 / F3 / F4 / F6 落到本 OPT**。
- F5（`rule_id_min=1` 加上）reviewer 评估为 "live-with"——当前数据量下用不到 `idx_alert_events_rule_scanned`，且 99% burst 行 rule_id=1，加上意义不大。先不动
- F7（前端 race）实测 reviewer **off-base**：`AbortController` cleanup 已经处理，快速 toggle 时旧 fetch 会被 abort。不修
- F9 / F10 / F11 都是 nit，跟 OPT-0027 close body 一起 live-with
- 这条 OPT 的 effort 估为 M（半天）——四个改动都聚焦在两个文件（aggregator + schema），加上前端列 def 同步 + 测试更新

### 反观点

- **「为什么不顺便抽 helper」**：抽 helper 需要满足 OPT-0015 阈值「3 次以上 copy-paste」。当前只有 2 份。本 OPT 修 2 份代码 → 等 QOC / QP 任一个 tab 想做聚合时（变成第 3 份），那次任务的 effort 里就包含「抽出 `_aggregate_by_login` helper」
- **F6 重命名争议**：用户可能觉得「就是个名字，写注释就够了」。但 dual semantic 是经典 silent bug 源，commission 估算列已经有抖动（F4 修完后才稳）；继续叠累似乎不智。Claim 时让用户最终拍板
