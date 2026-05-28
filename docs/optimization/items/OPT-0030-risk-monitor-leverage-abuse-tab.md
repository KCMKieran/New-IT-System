---
id: OPT-0030
title: 滥用杠杆 (Leverage Abuse) tab — risk-monitor 第 6 个检测规则
status: done
priority: P1
area: mixed
effort: M
created: 2026-05-28
related: [[OPT-0008]], [[OPT-0011]], [[OPT-0015]], [[OPT-0021]], [[OPT-0025]]
---

> ⚠ **Multi-phase OPT**（同 [[OPT-0024]] 模式）。Phase 1（snapshot-scan）已 merge+上线（2026-05-28，merge `bb2f36b`）。**Phase 2 reopen 本 OPT，不开新 OPT**——把检测内核从 snapshot 换成 **event-gated（只看开仓那一刻）**。下面「问题/背景/AC」是 Phase 1 的；Phase 2 设计见文末「## Phase 2」段。

## 问题

[risk-monitor](http://10.6.20.138:5173/risk-monitor) 当前 5 个 tab（批量下单 / 快开快平 / 快速获利 / 对冲刷单 / Gap Trade）全部是**事件流时间窗**检测——扫 `mt4_trades` / `mt5_deals`，按时间窗 + 游标 HWM 滑动。**「账户当前状态」维度的风险完全没覆盖**，其中最关键的缺口是「滥用杠杆 / Leverage Abuse」：

> 客户把保证金占用率推到 80–95% 区间 = 把账户开在 Margin Call 边缘搏大敞口。B-Book 业务下，客户方向一旦做对，公司单笔就大出血——这是「客户损益 = 公司损益相反」模型下**最核心**的事前预警信号。
>
> ——`docs/features/risk-monitor.md §4.2` 原话

§4.2 设计稿在 2026-03 写完后一直未实现，原因是当时列了**三个难点**：合约规范表（XAU=100oz / EURUSD=100000 / BTCUSD=1）+ 手算 `required_margin = lots × contract_size × open_price / leverage` + 扫描时刻捕获 open_price。**新发现** `fxbackoffice.mt4_users` 表里 MT 服务器已经替我们算好 `EQUITY` / `MARGIN` / `MARGIN_LEVEL` / `MARGIN_FREE` 四列——**三个难点全部消失**，工作量从 L 降到 M，值得 claim。

## 背景

### §4.2 设计稿要点（已存在，未实现）

`docs/features/risk-monitor.md` lines 244-255：

- **D1 瞬时档**：`required_margin / equity > 95%` — Medium
- **D2 持续档**：连续 3 次扫描 `margin_ratio > 80%` — High（跨扫描状态机）
- **意图**：B-Book 最核心风险信号 — 保证金用满 80–95% = MC 边缘的大敞口
- **(旧)难点**：`symbol_contract` 合约规范表 + `required_margin` 手算 + open_price 实时捕获 + `account_leverage_streak` 跨扫描表

### 关键数据发现 — `fxbackoffice.mt4_users` 已经有现成列

CRM 统一表（按 `sid` 把 MT4_Live / MT4_Live2 / MT5 三 server 全部纳入，`account_enrichment.get_account_info_map()` 已经在查它）有这 4 列：

| 列 | 含义 |
|---|---|
| `EQUITY` | 净值 = Balance + Credit + 浮动盈亏 |
| `MARGIN` | 已用保证金 = 所有持仓占用押金之和（= 设计稿想算的 `required_margin`） |
| `MARGIN_LEVEL` | = (EQUITY / MARGIN) × 100，百分比 |
| `MARGIN_FREE` | = EQUITY − MARGIN，可用余量 |

**含义**：MT 服务器已经做完所有合约规范聚合，§4.2 三个难点全部消失。直接读三列即可：
- ❌ 不需要 `symbol_contract` 表
- ❌ 不需要 `required_margin` 计算
- ❌ 不需要 open_price 捕获

### 阈值映射（margin_ratio ↔ MARGIN_LEVEL 互为倒数）

```
margin_ratio = MARGIN / EQUITY        ← 占用率（越大越危险）
MARGIN_LEVEL = EQUITY / MARGIN × 100  ← 健康度（越小越危险）
∴ MARGIN_LEVEL = 100 / margin_ratio
```

| §4.2 设计档 | 等价的 SQL 条件 | 等级 |
|---|---|---|
| 瞬时 `margin_ratio > 0.95` | `MARGIN_LEVEL < 105.3% AND MARGIN > 0` | D1 · Medium |
| 持续 `margin_ratio > 0.80`（连续 3 次扫描） | `MARGIN_LEVEL < 125% AND MARGIN > 0`（连续 3 次） | D2 · High |

⚠ **必须 `AND MARGIN > 0` 兜底**——MT 对**空仓账户**约定 `MARGIN_LEVEL = 0`（不是无穷大），不加这个守卫会把所有空仓账户全报成"满杠杆"，灾难性误报。

✅ **CEN 自动免疫**——MARGIN_LEVEL 分子分母同币种 ÷100 抵消，**不需要走 `apply_cen_conversion`**。展示 equity/margin 金额时才 ÷100。

### 架构差异 — snapshot scan vs event window

现有 5 条规则全是「事件流时间窗」。本规则是「账户当前状态快照」，是 risk-monitor **第一条** snapshot-pattern 规则：

| 维度 | 现有 5 条 | 本规则（OPT-0030） |
|---|---|---|
| 数据源 | mt4_trades / mt5_deals（事件流） | `fxbackoffice.mt4_users`（账户快照） |
| 扫描模式 | overlap window 或 cursor HWM | 全表 `WHERE MARGIN > 0 AND MARGIN_LEVEL < 阈值` |
| OPT-0011 cursor 模式 | 适用 | **不适用**（非 append-only） |
| Dedup key | (rule_id, server, login, symbol, first_open) | (rule_id, server, login) + 冷却 |
| 跨扫描状态 | 无（QP elapsed-time dedup 除外） | **D2 需要 `account_leverage_streak` 表**（连续扫描计数） |
| "立即扫描" 按钮 | burst / QOC 有 | **有**（snapshot 即"当前状态"，按需刷新有意义；走共享 `/burst-open/scan-now`） |
| 聚合视图 | hedge 有 | **无**（账户级快照天然一账户一行，dedup 已折叠，无需聚合） |
| severity 分级 | 无 | **无**（用户决策 2026-05-28：分级标准易让人困惑，只把 `margin_level` 显示清楚即可） |

### 扫描间隔（detection cadence）

挂在 **slow tier**，cadence = 共享的 `burst_open_config.scan_interval_min`，**当前线上 = 5 分钟**（范围 5–60，所有 slow-tier detector 共用这一个值，不单独配）。**不进** 60s fast tier（那是 burst 专用；CRM 快照本身刷新没那么快，60s 无意义）。
- 含义：**D2「连续 3 次扫描」≈ 持续 15 分钟**满足高杠杆才触发（3 × 5min）。这跟 Q2 采纳的 `MODIFY_TIME >= NOW()-INTERVAL 15 MINUTE` 新鲜度闸正好对齐（15min 覆盖 3 个 tick）。
- 改 `scan_interval_min` 会同时改变 D2 的"持续时长"语义——文档要写明这层耦合。
- 另有「立即扫描」按需触发（tier='all'，会带上本 detector）。

### 🔒 Scope 决策（2026-05-28 用户拍板 — claim 时按这版落地）

1. **去掉 severity** — 不要 severity 字段 / badge / 配色。两个 tier 仅靠 `rule_label`（"瞬时满杠杆" / "持续高杠杆"）+ 各自 `max_margin_level` 阈值区分。
2. **保留「立即扫描」** — 走共享 `/burst-open/scan-now`，前提是 detector 挂进 `_run_scan` 的 `'all'` + `'slow'` tier。
3. **砍掉聚合视图** — 前端拷 HedgeOpenTab 时删 `aggregated` toggle / `aggregatedColumnDefs` / 第二套 persist key / agg sort。
4. **`symbol` 占位语义**（主表 `symbol`/`order_count`/`total_lots` 是 NOT NULL 且为"交易事件"设计）：账户级快照填 `symbol=""`、`order_count`=持仓笔数（或 0）、`total_lots`=总持仓手数、`first_open`/`last_open`=NULL；margin 三件套 + streak 进 detail 表。**service 注释必须写明**，否则后人困惑"为何这条 alert 没 symbol"。

### Rule ID 号段

按 `.cursor/skills/risk-monitor/SKILL.md` § Step 3 约定，每 tab 占 10 ID。下一个空闲 band = **101–110**（91–100 已被 OPT-0021 Hedge Open 占）。v1 仅用 **101**（D1 瞬时）+ **102**（D2 持续）。

## 假设 / 待验证（claim 前必须先答完这 2 个 — 这是 idea → ready 的 gate）

> 这两个 open Q **直接决定 effort 是 M 还是 L 以及 D1 档是否可行**。任何一个答案不利都要回到 Ready 重新 scope。

> ✅ **2026-05-28 预飞行已跑（slave `fxbackofficeslavedb`，readonly）。两个 gate 全 PASS，effort 维持 M。**
>
> **Q1 结果 — Branch A（MT5 完全有数据）**：sid 1/5/6 的 `rows_with_margin == rows_with_ml` 精确相等（387/387、398/398、63/63）。MT5（sid 5）398 个有持仓账户全部有 `MARGIN_LEVEL`。→ 三 server 一条 SQL 全覆盖，不碰 mt5_live、不建 symbol_contract 表。
>   - 备注：表里有 6 个 sid，但 `SID_MAP` 只认 1/5/6。sid 2/3/4 是未监控的其他 server（sid 4 有 1072 个有 margin 账户），本规则正确只扫 1/5/6，**不扩范围**。
>
> **Q2 结果 — 实质 Branch A（危险账户近实时）**：原始聚合看着吓人（sid 1 平均滞后 28.5 天 / 最坏 3.6 年），但这是**双峰分布**——93–100% 的有持仓账户在 ≤5min 内刷新，长尾的"几年"全是**休眠安全账户**。**决定性诊断**：在告警候选（`MARGIN_LEVEL < 125`）里，sid 1/5/6 共 21 个危险账户**100% 在 2min 内刷新过、零陈旧**（平均滞后 12 / 103 / 125 秒）。逼近 MC 的账户净值剧烈变动 → MT 高频推送，会陈旧的恰是高 ML 休眠账户。→ **D1「瞬时档」可行。**
>   - **设计加固（已采纳）**：仍加一道 `MODIFY_TIME >= NOW() - INTERVAL 15 MINUTE` 新鲜度闸，作为"永不在陈旧行上误报"的安全带（实测危险行无陈旧，成本几乎为零）。
>
> **当前告警量（健全性）**：D1（<105.3%）三 server 共 14 个、D2（<125%）共 21 个账户在危险区。每 tick 全表扫描候选 < 900 行，极便宜。信号量健全（非空、非刷屏）。
>
> **新增发现 → 坐实 OQ#4**：Q3 里 ML 最低的全是 cent 小账户（净值 $2–50，部分 `MARGIN_FREE` 已负、ML 跌破止损线未平）= 噪声。真正有意义的危险账户从净值 $150+ 起。→ **`min_equity_usd` 过滤是必须的**（类比 Gap Trade rule 81 的 `min_net_deposit_hist`）。
>
> 预飞行 SQL 原文见下方 Q1/Q2，诊断用的桶分布 / 危险行新鲜度 / 阈值计数三条补充查询留在 session 记录。

### Q1 — MT5 sid 行的 MARGIN / MARGIN_LEVEL 是否被 CRM 同步填充？

`fxbackoffice.mt4_users` 对 MT5 sid 的 `currency` / `zipcode` 确实有值（risk-monitor 当前 MT5 alert 就靠这张表 enrich），但 `MARGIN_LEVEL` 这种实时数值列**可能只对 MT4 sid 同步**——CRM sync 任务的实现细节决定。

**验证方法**（slave 上跑只读 SQL）：
```sql
SELECT sid,
       COUNT(*)              AS rows,
       SUM(MARGIN > 0)       AS rows_with_margin,
       SUM(MARGIN_LEVEL > 0) AS rows_with_ml,
       ROUND(AVG(NULLIF(MARGIN_LEVEL, 0)), 1) AS avg_ml_nonzero
FROM fxbackoffice.mt4_users
WHERE `GROUP` NOT LIKE '%demo%'
  AND ENABLE = 1
GROUP BY sid;
```

**分支判定**：
- **A** — MT5 sid 行有 MARGIN/MARGIN_LEVEL 值 → 三 server 一条 SQL 全覆盖，effort **M** 不变。
- **B** — MT5 sid 行 MARGIN/MARGIN_LEVEL 全 0/NULL → v1 仅 MT4_Live + MT4_Live2，MT5 留作 v2（或回到 §4.2 原难点，需建 `symbol_contract` 表 — effort 升 **L**，重新 scope）。

### Q2 — `MODIFY_TIME` 距当前多久 = 快照新鲜度

CRM 是**快照表**而非 tick 级实时。`EQUITY` / `MARGIN` 随行情每跳变，但 `mt4_users` 是按 CRM sync 周期刷新——新鲜度直接决定 D1「瞬时档」是否名副其实。

**验证方法**：
```sql
SELECT sid,
       MIN(MODIFY_TIME) AS oldest,
       MAX(MODIFY_TIME) AS newest,
       ROUND(AVG(TIMESTAMPDIFF(SECOND, MODIFY_TIME, NOW()))) AS avg_lag_sec,
       MAX(TIMESTAMPDIFF(SECOND, MODIFY_TIME, NOW()))        AS max_lag_sec
FROM fxbackoffice.mt4_users
WHERE MARGIN > 0
GROUP BY sid;
```

**分支判定**：
- **A** — 平均 lag < 60s → D1「瞬时档」可行，名副其实。
- **B** — 平均 lag 5–10 min → D1 改名「分钟级」，业务上仍可用（接近 MC 是分钟级演化，不是秒级事件）。
- **C** — lag > 1h → D1 不可行，**v1 仅留 D2 持续档**，D1 等 CRM sync 提频或换数据源（直接打 MT 服务器 API）。

## 验收标准

### Backend

- [ ] 新 service `backend/app/services/rule_leverage_abuse_service.py`，包含 `detect_leverage_abuse(users_snapshot, rules, *, streak_state)`（snapshot-scan 模式，**不**走 mt4_trades）
- [ ] 单条 SELECT 拉 `fxbackoffice.mt4_users` 候选行（按 Q1 结果决定是否仅限 MT4 sid），过滤 `GROUP NOT LIKE '%demo%'` + `sid IN (allow-list)` + `LOGIN NOT LIKE '7%'` + `MARGIN > 0` + `MARGIN_LEVEL < max(rules.threshold)`
- [ ] D2 状态表 `account_leverage_streak`（SQLite）：`(rule_id, server, login)` PK + `consecutive_count` + `last_seen_scan_id` + `last_alert_at`
- [ ] 注册到 `burst_open_scheduler._run_scan()` 的 **`'slow'` + `'all'` 两个 tier**（slow = 常规 5min cadence；all = 让「立即扫描」`scan-now`(tier='all') 能带上本 detector）。**不进** fast tier
- [ ] Rule ID 常量 `LEVERAGE_ABUSE_RULE_ID_BASE = 101` / `LEVERAGE_ABUSE_RULE_ID_MAX = 110`；rule_id override 测试（OPT-0008-class guard）
- [ ] 主表 `alert_events` 写入遵守 snapshot-freeze（检测时刻冻结，读取不重算）：`symbol=""` / `order_count`=持仓笔数 / `total_lots`=总持仓手数 / `first_open`=`last_open`=NULL（见 §Scope 决策 4）
- [ ] AlertEvent 新字段：`margin_level`、`margin_used`、`margin_free`、`streak_count`（D2 用）；冻结进 `alert_leverage_abuse_detail` 表，1:1 JOIN（同 OPT-0008 拆 detail 表模式）。**无 `severity` 字段**
- [ ] Pydantic `LeverageAbuseConfig`：master enable + max 10 rules，每条 `name` (fund-flow 风格 OPT-0021) + `enabled` + `max_margin_level` (默认 D1=105.3 / D2=125) + `streak_min` (默认 D1=1 / D2=3) + `min_equity_usd` (默认 100，滤 cent-dust 假阳)。**无 `severity`**
- [ ] API：`GET/POST /api/v1/risk-monitor/leverage-abuse/config` + `GET /alerts` + `GET /alerts/stats` + `GET /alerts/export`（**无 aggregated**）
- [ ] Pytest 覆盖：snapshot scan 基线 / streak 状态机连续 3 次触发 + 回升清零 / MARGIN=0 守卫（空仓不触发）/ CEN 比例免疫 / rule_id override / D1 + D2 两条规则并存 / per-rule disabled / 阈值边界 / `min_equity_usd` 过滤 / 冷却 dedup（同账户不每 tick 重报）

### Frontend (`RiskMonitor.tsx`)

- [ ] 新增第 6 个 tab「滥用杠杆」（放「对冲刷单」与「Gap Trade」之间）；`TabsList` `grid-cols-5`→`grid-cols-6` + `RISK_MONITOR_TABS` 数组 + `isRiskMonitorTab` guard 同步
- [ ] 拷 `HedgeOpenTab` 为模板，**删掉聚合视图**（`aggregated` toggle / `aggregatedColumnDefs` / 第二套 persist key / agg sort）+ 无 detail sheet
- [ ] 列：`rule_label` / `scanned_at` / `server` / `login` (CRM link, 用 userId) / `currency` (badge) / `group` / `equity` / `margin_used` / `margin_free` / `margin_level` (**带颜色：<105% 红 / 105–125% 琥 / 其他 灰** — 这是用户唯一要求"显示清楚"的列) / `streak_count` (D2 才有) / `leverage` / `net_deposit_hist` (用 `netDepositColDef` 工厂)。**无 severity 列**
- [ ] `useGridColumnPersist` (key `RISK_MONITOR_LEVERAGE_ABUSE_GRID_STATE_V1`，注册进 `GRID_STORAGE_KEYS`) + `<ColumnVisibilityMenu>`（OPT-0015）
- [ ] `useFilterPersist` (key `RISK_MONITOR_LEVERAGE_ABUSE_FILTERS_V1`)（OPT-0025 — rangePreset / ruleFilter / serverFilter 持久化；`loginInput` / `zipcodeInput` **不**持久化）
- [ ] Per-rule summary cards（D1 / D2 各一张，by `AlertsStats.by_rule`；单 rule 也用 `grid-cols-2 lg:grid-cols-3 2xl:grid-cols-4` **不居中**）
- [ ] Config drawer：master enable（label 左 + Checkbox 右 + 一句 hint）+ rule rows（name / enabled / max_margin_level / streak_min / min_equity_usd）。**无 severity**
- [ ] CSV export（复用 `_csv_stream`）
- [ ] **保留「立即扫描」按钮**（走共享 `/burst-open/scan-now`；snapshot=当前状态，按需刷新有意义）

### Docs / Skill

- [ ] 更新 `.cursor/skills/risk-monitor/SKILL.md`：File Map / API Contracts / Data Model / Current Rules（新增 Rule 6 段）/ Implementation Status / Rule ID Allocation 表 / Env Flags（如新增）
- [ ] 更新 `docs/features/risk-monitor.md` §4.2 状态从 Roadmap → Shipped，移到 §3（已上线规则）
- [ ] 在 `docs/features/risk-monitor.md §5` 关键约定追加：snapshot-scan 模式与 cursor 模式的边界
- [ ] `docs/features/risk-monitor-reusable-patterns.md` 加一节「snapshot 状态扫描模板」（streak 状态机 + MARGIN=0 守卫这两个坑必写）

## 实施大致路径

1. **预飞行（idea → ready 的 gate）**：跑 Q1 + Q2 两条 SQL，回填本文件「假设」段实际结果。两条结论合格才能 claim。
2. Backend service + schema + db migration（snapshot scan + streak state 是核心 — 参考 OPT-0021 的 service / detail 表模式 + 抄 quick-profit dedup 的 elapsed-time 思想做冷却）
3. API + scheduler 注册（slow tier 常规 + all tier 供 scan-now）
4. Pytest（重点：streak 状态机 + MARGIN=0 守卫 + rule_id override）
5. Frontend tab（拷 Hedge Open tab 骨架最相近）
6. Docs + skill 同步

## 相关 OPT

- [[OPT-0021]] Hedge Open — service 结构 / `alert_*_detail` 表模式 / per-rule `name` 字段 / 新 tab 模板，**最近的对照参考**
- [[OPT-0015]] / [[OPT-0025]] — 列状态 + 过滤器持久化两条 hook，新 tab 必走
- [[OPT-0008]] — alert_events 拆 detail 表的模式（rule-specific 字段不进主表 23 列共有字段）
- [[OPT-0011]] cursor scan — 本规则**不适用**（snapshot 非 append-only），需在 service 注释 + 文档明确指出

## Open Questions

**已决（2026-05-28）**：
- ✅ severity → **不做**（用户决策）。
- ✅ 立即扫描 → **保留**；聚合视图 → **砍掉**。
- ✅ 阈值字段语义 → 用 **MARGIN_LEVEL %**（105 / 125），不暴露裸 ratio（MT 终端就是这个数，分析师直觉一致）。
- ✅ `min_equity_usd` → **做**，默认 100，进 config（预飞行 Q3 坐实 cent-dust 噪声）。
- ✅ 扫描间隔 → slow tier 共享 `scan_interval_min`（线上 5min）；D2 连续 3 次 ≈ 持续 15min。

**实施期再答**：
1. D2 streak 状态表放 SQLite（小、与 alert_events 同库、跨重启稳）还是内存（重启重置可能误判）？**倾向 SQLite**。
2. snapshot scan 共享 `_scan_lock`？跟 mt4_trades scan 共写 alert_events → **是**。
3. 同一账户冷却时间固定常量还是 per-rule 可配？参考 QP elapsed-time dedup；**倾向先固定常量**（= 1 个 scan_interval），简单。
4. streak 状态表的清理：账户回升到安全区后是 reset count 还是删行？删行更省空间但失去"刚刚还危险"的痕迹——倾向 reset count 保留行 + 加 `last_dangerous_at`。

## 结果（done 2026-05-28）

**交付 vs AC**：全部达成。后端 schema / DB（`alert_leverage_abuse_detail` + `account_leverage_streak` 状态表）/ service（snapshot scan + D1/D2 + streak 机）/ scheduler（slow + all tier）/ 5 endpoints；前端第 6 个 tab（拷 HedgeOpenTab 砍聚合视图）+ config drawer + 列/过滤器持久化。`verify.sh` PASS（后端 146 测试 + tsc + vitest 48）。实施期 4 个 open question 落地：(1) SQLite ✓ (2) 共享 `_scan_lock` ✓ (3) 固定 1h 冷却 ✓ (4) 保留行 + `miss_count` 宽限（比原计划的 reset 更稳）。

**实现要点（与原设计差异）**：预飞行发现 `mt4_users.MARGIN_LEVEL` 现成 → 不建 `symbol_contract`、不算 `required_margin`，effort L→M。阈值直接落在 `MARGIN_LEVEL`（105.3 / 125）。15min `MODIFY_TIME` 新鲜度闸兼做 `IDX_MODIFY_TIME` 索引裁剪。`symbol=""` 等占位填充 trade-shaped 共有列。

**Stage 1 outsider-review 处理记录**（8 finding）：
- A（streak 漏扫一次被清零 + 新鲜度闸与扫描间隔解耦）→ **当场修**：新增 `miss_count` 宽限窗（缺席 ≤2 次冻结 streak 不清零）+ 新鲜度 `max(15, interval+5)`。commit `9b14ea4`。
- B（改/删规则后 streak 错配，因 rule_id 是位置式）→ **当场修**：`save_leverage_abuse_config` 清空 streak 表。commit `9b14ea4`。
- C（snapshot SQL 无 LIMIT + 索引未验证）→ **当场修**：防御性 `LIMIT 5000` + 命中告警 + EXPLAIN 注释。commit `9b14ea4`。
- #4（streak save 与 alert write 跨事务，崩溃丢一条 alert）→ **live with**：毫秒级崩溃窗 × 后果是晚 1h 重发（账户仍危险），概率×影响极低。未来若做严格一致性再收。
- #6（占位 `symbol=""` 污染共享 alert_events 读取，特别是 QP dedup）→ **live with**：reviewer 看错——QP dedup 按 rule_id 段(61-70)取，杠杆行 101-110 进不了；空 symbol 索引无害。占位契约已在 service docstring + 本文档写明。
- #7（冷却用墙钟、无单调保护）→ **live with**：失败方向偏"立即重报"（安全），NTP 回拨极低概率。
- #8（前端 `MarginLevelCell` 硬编码 105.3/125 阈值，与可配规则脱节）→ **live with**：那是"距强平危险带"视觉提示而非规则阈值回显；默认规则恰为 105.3/125。

**Follow-up（未来若需要）**：实时 margin 值（照 QP floating-refresh 模式做只读 endpoint，v2）；跨事务一致性（#4）；前端色带跟随配置（#8）。

**未做**：浏览器肉眼验收（环境无浏览器工具，仅 tsc/build/dev-200 验证）——交付给用户在 `http://10.6.20.138:5173/risk-monitor` 看第 6 个 tab。

---

## Phase 2 — event-gated 重构（reopen 2026-05-28）

### 为什么 reopen

Phase 1 的 snapshot-scan 有个用户不想要的误报类:账户 5h 前以 400% margin level 开仓,随后**亏钱**导致 equity 缩水、margin level 漂到 ~105%,会被 snapshot 规则抓出来。但这不是"滥用杠杆开仓",只是个**亏损账户**(B-Book 视角它正在给公司送钱)。`MARGIN_LEVEL = Equity/Margin` 变低有两因:① 仓位大(滥用,想抓)② 亏损(equity↓,不想抓),snapshot 分不出来。

**用户决策**:改后端检测框架为 **event-gated——只看"最近开仓"的账户在开仓那一刻的杠杆**。开仓瞬间 `equity≈balance`(还没浮亏),所以"开仓时 margin level"天然 ≈ 仓位/本金比,自动剔除亏损漂移。**前端同事配的 3 条 rules 不动**(高杠杆重仓<200 / 瞬时满杠杆<150 / 持续高杠杆<125),只改后端"怎么评估"。

### 时序一致性预飞行（已验证 2026-05-28）

`fxbackoffice.mt4_users` 是**混合同步**:逐账户准实时单行更新 + 约每 1 分钟的批量 mark-to-market。最近 60min 开仓的 203 个账户 **203/203** 的 `MODIFY_TIME >= 开仓时间`(最快 17s 追上)。→ 开仓→快照反映延迟是**秒级~1 分钟**。唯一坑:开仓后 <1min 内快照可能还没同步,用 **settle 延迟**(只看 ≥1min 前的开仓)兜住。

### 决策:选项 A（streak_min 废弃）

`max_margin_level` / `min_equity_usd` 在 event-gated 下继续成立;**`streak_min` 没有意义**(开仓是一次性事件,无"连续 N 次扫描")。Rule 3「持续高杠杆」(streak_min=3)退化成"开仓瞬间 <125%"(最严的一档瞬时阈值)。3 条规则变成干净的三档开仓网(200/150/125)。

⚠ **需求矛盾已挑明**:同事配的"持续"规则本是状态持续型(= 漂移误报源),与 event-gated 本意冲突。用户拍板选 A:放弃"持续"语义,Rule 3 退化为瞬时档。

### Phase 2 AC

- [ ] **Backend 检测内核换 event-gated**:`scan_leverage_abuse` 候选 = 最近开仓账户(复用 `_query_mt4_recent_opens`/`_query_mt5_recent_opens`,**强制 `cursor_time=None` 走 overlap-window**,与全局 `CURSOR_SCAN_ENABLED` 解耦)+ settle 延迟(`open_time <= now - 60s`,窗口 `interval*60 + 120s`)
- [ ] 批量读这些账户的 margin 快照(`fxbackoffice.mt4_users`,`MODIFY_TIME` 用 `broker_time_to_utc_iso` 归一到 UTC 与 open_time 同框比较)
- [ ] detect:per rule `margin_level < max_margin_level` AND `MARGIN>0` AND `equity_usd >= min_equity_usd` AND **`MODIFY_TIME >= 该账户最近 settled 开仓时间`**(快照已追上才评估,没追上下一轮 overlap 再抓)
- [ ] dedup `(rule_id, server, login, open_time)`,previous_alerts 从 SQLite seed(新 `get_recent_leverage_abuse_alerts`)+ `_latest_result`;scheduler 传入
- [ ] **`streak_min` 废弃**:backend 容忍该字段(老配置照常 load)但忽略;删 `account_leverage_streak` 表的使用(grace/cooldown 一并移除,改用 event-stream dedup)
- [ ] Pytest 重写:settle 窗口 / MODIFY_TIME>=open 守卫 / dedup / 开仓时阈值 / CEN;删 streak 状态机测试
- [ ] Frontend:config drawer 去掉 `streak_min` 输入框、表格去掉「持续次数」(streak_count)列、文案改"开仓时";3 条规则配置不破
- [ ] Docs:SKILL.md Rule 6 段改写为 event-gated;risk-monitor.md §4.2;lesson 加 Phase 2 注

### Phase 2 待答

- 开仓时阈值沿用同事配的 200/150/125(不动),确认无需新阈值。
- MT5 `_query_mt5_recent_opens` 的 open_time 已是 UTC ISO;MT4 helper 同样 `broker_time_to_utc_iso`。两边与 margin 快照的 UTC 化 MODIFY_TIME 同框 ✓。

### Phase 2 结果（done 2026-05-28）

**交付**：检测内核 snapshot → event-gated（只看最近开仓账户在开仓那一刻的 margin level，剔除亏损漂移误报）。复用 burst/hedge opens 查询（`cursor_time=None` overlap）+ SETTLE 60s + `MODIFY_TIME>=开仓时间` 守卫（UTC 同框）+ dedup `(rule,server,login,open_time)`。弃用 streak 表/grace/冷却。前端同事配的 3 条 rules（200/150/125）原样保留；`streak_min` 废弃（backend 容忍忽略 + 前端去掉输入框 + 表格「持续次数」改「开仓笔数」）。实测：114ms、找到 103 最近开仓账户、命中 2 条（rule 101 <200%）、dedup 重跑 0、MT4+MT5 均通；`verify.sh` PASS。

**Stage 1 outsider-review（Phase 2，8 finding）处理**：
- #3 重启 dedup 失明 → **当场修**：`get_recent_leverage_abuse_alerts(101-110)` seed + 修过时注释。commit `3850176`。
- #9 `save_leverage_abuse_config` 的 `int(r["streak_min"])` KeyError 隐患 → **当场修**：`.get("streak_min",1)`。commit `3850176`。
- #4 overlap 内再开仓重报 → **保持**（用户拍板：再开仓=新滥用事件，值得重报）+ 收紧 docstring。reviewer 提的"settled 被 unsettled 盖住"后半看错（collector 先 settle 再聚合）。
- #1/#2 跳 tick / lookback 漂移漏开仓 → **live with**（用户拍板）：所有 overlap-window 规则（burst/QOC/hedge）共有架构特性，杠杆缓冲更大（60s vs burst 30s）；slow tier 5-10min + 扫描 114ms，跳 tick 极罕见。未来"基于真实经过时间动态 lookback + 跳-tick 告警"应跨规则统一（可另立 OPT）。
- #5 同步滞后静默丢弃无告警 / #6 无 LIMIT / #7 私有 import / #8 server 大小写 → **live with**：低概率或与 burst/hedge 一致；#8 两边都是 "MT4_Live" 标签确认一致。

**Phase 2 follow-up（未来若需要）**：跨规则 HWM-based 动态 lookback + 跳-tick 告警（#1/#2）；同步滞后丢弃计数日志（#5）；opens 查询 LIMIT（#6）；移除 `streak_min` schema 列。
