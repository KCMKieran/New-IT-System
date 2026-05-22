---
id: OPT-0027
title: Risk-monitor 批量下单 tab 加聚合视图（按账户折叠，复用 hedge-open 模式）
status: done
priority: P2
area: mixed
effort: S
created: 2026-05-22
claimed: 2026-05-22
completed: 2026-05-22
branch: opt/risk-monitor-burst-aggregated-view
related: [[OPT-0021]], [[OPT-0028]]
---

## 问题

`/risk-monitor?tab=burst-open`（批量下单）默认是**每条 alert 一行**的明细视图。在长时间窗（7d / 30d）下，同一账户会因为 5min 扫描间隔产出几十到上百条告警，分析师要心算「这个 login 总共开了多少单 / 累计多少手 / 涉及几个 symbol」很累。

[[OPT-0021]] 已经为对冲刷单 tab（hedge-open）做了**聚合按钮**——一键把同账户多条 alert 折叠成一行，验证了：
- 后端 CTE 模式（`ranked` → `agg` → JOIN latest enrichment）在 SQLite 上跑得动且语义干净
- 前端 toggle 按钮 + 独立 column-persist key 的 UX 模式分析师能接受
- Stats / CSV / Summary cards 在聚合模式下保持原样（只切换中间那张表的行折叠方式）不会让用户迷惑

现在把同款移植到批量下单 tab。本 OPT 只做 burst-open——快开快平 / 快速获利 tab 等用户对 burst-open 聚合视图的反馈再决定是否横向推广（YAGNI）。

## 背景

### Hedge-open 现有实现（参照样板）

| 资产 | 路径 |
|---|---|
| 后端 SQL | `backend/app/core/risk_monitor_db.py:1652` `aggregate_hedge_open_by_login` + `_HEDGE_AGG_SORT_COLS:1639` |
| 后端 route | `backend/app/api/v1/routes/risk_monitor.py:1693` `/hedge-open/alerts/aggregated` |
| 前端 row 类型 | `frontend/src/pages/RiskMonitor.tsx:362` `HedgeOpenAggregatedRow` |
| 前端 column defs | `frontend/src/pages/RiskMonitor.tsx:4868` `aggregatedColumnDefs` |
| 前端 toggle button | `frontend/src/pages/RiskMonitor.tsx:5052` 琥珀/翠绿 toggle |
| localStorage key | `RISK_MONITOR_HEDGE_OPEN_AGGREGATED_V1`（toggle 状态）+ `RISK_MONITOR_HEDGE_OPEN_AGG_GRID_STATE_V1`（聚合视图独立列设置） |
| Sortable 列白名单 | `_HEDGE_AGG_SORT_COLS`（9 列） |
| 测试 | 16 个集成测试覆盖 SUM 正确性 / 最新 enrichment / 过滤继承 / 分页 / 排序 |

### Burst-open vs hedge-open — 关键差异

| 项 | hedge-open | burst-open（本 OPT） |
|---|---|---|
| `total_count` SQL | `SUM(COALESCE(buy_count,0) + COALESCE(sell_count,0))`（来自 `alert_hedge_open_detail` LEFT JOIN） | `SUM(order_count)`（`alert_events` 主表直接拿） |
| 方向拆分列 | `buy_lots_sum` / `sell_lots_sum` | **删掉** — burst-open 不区分 buy/sell 方向，rule 也不依赖方向 |
| `total_lots` 语义 | 双向之和（= 2× 实际对冲量，列 tooltip 要点明） | 普通 sum，无双向语义，tooltip 写「窗口内 alert 的总手数累加」 |
| CTE FROM clause | `_ALERT_FROM_CLAUSE`（带 `alert_hedge_open_detail` LEFT JOIN） | 同一个 FROM clause——detail 表 LEFT JOIN 即使是 NULL 也不影响聚合 SELECT 不引用的字段；不需要新 from clause |
| Rule_id 范围 | 91–100（`HEDGE_OPEN_RULE_ID_BASE / MAX`） | 1–50（`BURST_RULE_MAX_ID`，历史 grandfathered 50 槽位） |
| `account_group` / `currency` / `zipcode` / `net_deposit_hist` "取最新" 逻辑 | 同 | 同 |

### 复用度估算

- 后端 SQL：80% 复制 + 删除 `buy_count` / `sell_count` / `buy_lots` / `sell_lots` 字段引用、把 `SUM(buy_count + sell_count)` 改为 `SUM(order_count)`
- 后端 route：90% 复制 + 改 rule_id 范围 + 改 URL 前缀
- 前端 column defs：~80% 复制 + 删 buy/sell 两列、改 `total_lots` 的 tooltip
- 前端 toggle button：100% 复制（用同款琥珀/翠绿 + Layers icon）
- 前端 fetch 分支：100% 复制 + 改 endpoint URL

## 验收标准

### 后端

- [ ] `backend/app/core/risk_monitor_db.py` 新增 `aggregate_burst_open_by_login(...)` + `_BURST_AGG_SORT_COLS` 字典
  - 入参与 `aggregate_hedge_open_by_login` 完全一致（rule_id_min/max、过滤参数、分页、排序）
  - 返回 `(entries, total)`，每个 entry 形如：
    ```python
    {
      "server": str, "login": int,
      "alert_count": int,            # COUNT(*)
      "total_count": int,            # SUM(order_count)
      "total_lots": float,           # SUM(total_lots)
      "first_alert_at": str|None,    # MIN(scanned_at)
      "last_alert_at": str|None,     # MAX(scanned_at)
      "symbols": str|None,           # GROUP_CONCAT(DISTINCT symbol)
      "symbol_count": int,
      "group": str|None, "currency": str|None,
      "zipcode": str|None, "net_deposit_hist": float|None,
    }
    ```
- [ ] `backend/app/api/v1/routes/risk_monitor.py` 新增 `GET /burst-open/alerts/aggregated`，绑定 `BURST_RULE_ID_BASE=1` / `BURST_RULE_MAX_ID=50`
- [ ] `backend/app/schemas/risk_monitor.py` 新增 `BurstOpenAggregatedRow` + `BurstOpenAggregatedResponse` Pydantic models
- [ ] `backend/tests/test_aggregate_burst_open_by_login.py` 至少 6 个 case：
  - SUM(order_count) 正确（多 alert 合并后总笔数对得上）
  - 最新 enrichment 取得对（不同 scan 的 group/zipcode 飘移时取 `MAX(scanned_at)` 那条）
  - 过滤参数继承（`zipcode` / `login` / `symbol` 在 `WHERE` 阶段就过滤，不是 post-fold）
  - 分页 `limit / offset` 正确
  - 排序白名单（合法 key 用、非法 key 落回 `total_lots`）
  - Rule_id 范围隔离（rule 51+ 的 alert 不会混进来）

### 前端

- [ ] `frontend/src/pages/RiskMonitor.tsx` BurstOpenTab：
  - [ ] 顶部加 `BurstOpenAggregatedRow` interface（删 hedge-open 版的 `buy_lots_sum` / `sell_lots_sum` 字段）
  - [ ] 加 toggle button，**与 hedge-open 视觉 100% 一致**（琥珀 → 翠绿，Layers icon，aria-pressed，dark mode 处理，title 文案对齐）
  - [ ] 位置：「设置」按钮右侧（紧贴）
  - [ ] localStorage key `RISK_MONITOR_BURST_OPEN_AGGREGATED_V1`（toggle 状态）
  - [ ] 独立 `aggColumnPersist` with key `RISK_MONITOR_BURST_OPEN_AGG_GRID_STATE_V1`
  - [ ] 独立 `aggSortBy` / `aggSortOrder` state
  - [ ] `aggregatedColumnDefs` ≈ hedge-open 的列减掉 Buy 手数 / Sell 手数 两列：
    服务器 / 账户 / Zipcode / 币种 / 历史净入金 / 累计笔数 / 累计手数 / 估算佣金 / 告警次数 / 涉及 symbols / 涉及账户数 / 首次告警 / 最近告警 / 客户组
  - [ ] fetch 在 `aggregated` 切换时选 `/burst-open/alerts/aggregated` vs `/burst-open/alerts`
  - [ ] 列设置 Drawer 跟随 `aggregated` 状态切换 `label`（明细视图 / 聚合视图） + `persist` + `columnDefs`（抄 hedge-open `:5378`–5382 写法）
  - [ ] 分页文案分支「条 / 个账户」（抄 hedge-open `:5315`–5316）

### 不在范围内（明确划线）

- CSV 导出在聚合模式下保留**走 detail**（同 hedge-open v1 决策），不改 export 路径
- Stats endpoint / Summary cards 不变（仍按 rule 维度）
- 不动 quick-open-close / quick-profit / gap-trade tab
- 不引入跨 rule_id 范围聚合（每 tab 自己的范围）

### 文档

- [ ] `.cursor/skills/risk-monitor/SKILL.md` 的 File Map + API Contracts + Implementation Status 段加 OPT-0027 一行（**gitignored，不进 commit**）
- [ ] `docs/features/risk-monitor.md` 如果有相关章节同步一笔（看实施时是否触及）

## 假设 / 待验证

- [ ] `BurstOpenAlert` schema 里 `order_count` 在所有 burst-open 历史行都非空（聚合 `SUM(order_count)` 才有意义）—— claim 后写测试前 `SELECT COUNT(*) WHERE rule_id BETWEEN 1 AND 50 AND order_count IS NULL` 确认一次
- [ ] 是否需要在聚合视图里展示「涉及规则」列（同账户跨 rule 命中）？—— 默认**不加**，因为 burst-open 当前只配 1-2 个 rule，加列噪声大于价值；如果分析师催再 file follow-up
- [ ] 聚合视图的默认排序：`total_lots DESC`（同 hedge-open，"biggest offender first"），需要验证用户接受

## 笔记

- 这是 OPT-0021 hedge-open 聚合按钮模式的**第二次落地**——属于 [[OPT-0002]]（浏览器缓存模式归纳文档）那类「在 risk-monitor 5 个 tab 横向推广同一模式」的范畴。如果未来 QOC / QP 也要加，应该考虑抽 `aggregate_by_login` 的 SQL 模板而不是第三次复制（参考 OPT-0015 的 hook 抽象阈值「3 次以上 copy-paste / 5 次以上必抽 hook」）
- 跟 [[OPT-0015]]（grid 列自定义）+ [[OPT-0025]]（filter 持久化）**零冲突**——本 OPT 用的两个 localStorage key 都是新的，列设置走的是已有的 `useGridColumnPersist` hook
- 跟 [[OPT-0001]]（tab 切换缓存）**有交互**——OPT-0001 还没做；如果它先做了，本 OPT 的 aggregated/detail toggle 会让 cache 失效一次（fetch 不同 endpoint）；这是正确行为不是 bug
- 后端 SQL 不引入新的 from clause——`_ALERT_FROM_CLAUSE` 已经 LEFT JOIN 了 `alert_hedge_open_detail` 和 `alert_quick_profit_detail`，对 burst-open 行来说这些 JOIN 返回 NULL，聚合 SELECT 不引用就行

### 反观点 / 风险

- **「为什么不做完整三 tab」**：复制 3 份代码不优雅，但抽通用 SQL helper 现在风险大于收益——burst-open 和 hedge-open 列差异已经明显（buy/sell 拆分 vs 不拆分），quick-profit 还有 realized/floating 拆分，过早抽象会变成牵强的可配置 SQL 生成器。先把第二份做扎实，等第三份要做时再决定要不要抽
- **「rule_id 显示问题」**：聚合按 `(server, login)` 折叠跨 rule 命中，所以聚合行没有 `rule_id` 字段。这不是 bug——hedge-open 也是这样，分析师从 stats 卡片看 rule 维度，从聚合表看账户维度
- **`total_lots` 语义混淆**：hedge-open 的 `total_lots` 是双向之和（= 2× 实际对冲量），burst-open 的是普通 sum。**两个 tab 同名同义不同算**容易让 reader 困惑。缓解：列 tooltip 一定要写清楚 + Drawer 列说明文案对齐；这条**重要不可省**
- **测试覆盖盲区**：detail 表 LEFT JOIN 在 burst-open 行上返回 NULL，需要一个测试 case 显式验证「detail 字段全 NULL 的 burst-open 行不会让 SUM 报错」

## 结果

实际交付与 AC 一致，**无 scope 缩减**。

**核心**

- `backend/app/core/risk_monitor_db.py`：`aggregate_burst_open_by_login()` + `_BURST_AGG_SORT_COLS`（7 个可排序列）。CTE 模式 `ranked → agg → JOIN ranked latest WHERE rn=1` 与 hedge-open 同结构，关键差异：`total_count = SUM(order_count)`、无 buy/sell 拆分字段、`total_lots` 是普通 sum
- `backend/app/api/v1/routes/risk_monitor.py`：`GET /burst-open/alerts/aggregated`，绑 `rule_id_max=BURST_RULE_MAX_ID (50)`
- `backend/app/schemas/risk_monitor.py`：`BurstOpenAggregatedRow` + `BurstOpenAggregatedResponse`
- `backend/tests/test_burst_open_aggregated.py`：19 集成测试覆盖 SUM 正确 / 最新 enrichment / detail JOIN NULL 不爆 / 过滤继承 / 分页 / 排序白名单 / rule_id 段隔离

**前端 (`RiskMonitor.tsx` BurstOpenTab)**

- 工具栏「设置」右侧加 toggle button（琥珀/翠绿同 hedge-open，含 dark mode）
- 独立 `RISK_MONITOR_BURST_OPEN_AGGREGATED_V1`（toggle 状态）+ `RISK_MONITOR_BURST_OPEN_AGG_GRID_STATE_V1`（聚合视图列设置）
- 独立 `aggSortBy / aggSortOrder` state + `BURST_AGG_SORTABLE_COL_IDS` 白名单
- `aggregatedColumnDefs`（13 列）：服务器 / 账户 / Zipcode / 币种 / 历史净入金 / 累计笔数 / 累计手数 / 估算佣金 / 告警次数 / 涉及品种 / 首次告警 / 最近告警 / 客户组
- `fetchAlerts` 按 `aggregated` 切 URL + parse 不同 response 类型；page-reset effect deps + fetchAlerts deps 都加 `aggregated / aggSortBy / aggSortOrder`
- 列设置 Drawer `columnGroups` 跟随 `aggregated` 切换 label / persist / columnDefs

**测试 / 验证**

- backend pytest：19 burst + 16 hedge = 35/35 全过（无回归）
- frontend tsc --noEmit clean，npm test 48/48
- dev curl 真数据：endpoint 返回 50+ 个折叠账户，total_lots DESC 默认排序正确

**Stage 1 outsider-review — 12 条 finding 处理**

| Finding | 处理 |
|---|---|
| F1 — 缺复合索引 `(server, login, scanned_at DESC, id DESC)` | **实测无效**回滚：在 89k 行规模下 planner 仍走 `scanned_at` 索引，查询时间 247ms vs 246ms 无差异 |
| F2 — burst aggregator 拖 4 个 NULL LEFT JOIN | 拆到 **[[OPT-0028]]** hardening（同时修 hedge-open） |
| F3 — `count_sql` 重扫一遍同窗口 | 拆到 **[[OPT-0028]]**（同时修 hedge-open） |
| F4 — `GROUP_CONCAT(DISTINCT symbol)` 顺序未定义 → 佣金抖动 | 拆到 **[[OPT-0028]]**（同时修 hedge-open） |
| F5 — `rule_id_min=1` 默认 None | live with（99% burst 行 rule_id=1，加上意义不大） |
| F6 — `total_lots` 双语义同名 | 拆到 **[[OPT-0028]]**（hedge 重命名 `total_lots_double_sided` 或新增 `hedged_lots`） |
| F7 — 前端 race condition | **off-base**：AbortController cleanup 已经处理，快速 toggle 时旧 fetch 会被 abort。不修 |
| F8 — 聚合模式下 CSV 导出仍是明细 | **当场修**：burst + hedge 两个 tab 的导出按钮都在 `aggregated` 时 disabled + tooltip 提示 |
| F9 — localStorage 数量 | live with（约定已在 risk-monitor SKILL doc） |
| F10 — 测试 gap | 部分 live with；GROUP_CONCAT 顺序确定性测试纳入 [[OPT-0028]] |
| F11 — toggle 视觉 | live with（已与 hedge 一致） |
| F12 — 3rd-copy 抽 helper 引导 | **当场修**：`aggregate_burst_open_by_login` 顶上加注释，引用 hedge sibling + 提及 OPT-0028 hardening 几个共性硬化点 |

**Follow-up（拆 OPT 追踪）**

- [[OPT-0028]] risk-monitor-aggregator-hardening（**Ready**，effort M）— 4 条 F2/F3/F4/F6 一并修，同时覆盖 burst + hedge 两个 aggregator，避免 3rd tab 继承坏模式

**笔记**

- 这是 [[OPT-0021]] 模式的第 2 次落地。第 3 次（如 QOC / QP 想加同款）时按 OPT-0015 阈值要求抽 `_aggregate_by_login` helper，那次的 effort 里包含「合并 OPT-0028 修过的共性逻辑」
- 用户手动浏览器验证留给用户验收（auto mode 下我跑了 tsc + 单元测试 + dev curl 真数据，未做浏览器交互验证）
- merge 时遇到的（值得追踪）：并发 worktree `/opt/myproject/New-IT-System-opt0025` 有别的 session 的零散 staged 改动（含 backlog.md / done.md 重叠），临时 git stash 走它们才能在 main 上做 merge。这条 stash 标签 `OPT-0027 Stage 2: parking other worktree work to free main`，merge 完用户应 `cd /opt/myproject/New-IT-System-opt0025 && git stash pop` 恢复（done.md / backlog.md 可能冲突需手动 resolve）
