---
id: OPT-0030
title: 滥用杠杆 (Leverage Abuse) tab — risk-monitor 第 6 个检测规则
status: ready
priority: P1
area: mixed
effort: M
created: 2026-05-28
related: [[OPT-0008]], [[OPT-0011]], [[OPT-0015]], [[OPT-0021]], [[OPT-0025]]
---

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
| "立即扫描" 按钮 | burst / QOC 有 | 无（同 Hedge Open / Gap Trade —— investigative） |

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
- [ ] 注册到 `burst_open_scheduler._run_scan()` slow tier（不进 fast tier — 不需要 60s 粒度；CRM 快照本身没那么新）
- [ ] Rule ID 常量 `LEVERAGE_ABUSE_RULE_ID_BASE = 101` / `LEVERAGE_ABUSE_RULE_ID_MAX = 110`；rule_id override 测试（OPT-0008-class guard）
- [ ] AlertEvent 新字段：`margin_level`、`margin_used`、`margin_free`、`streak_count`（D2 用）；通过 `alert_leverage_abuse_detail` 表 1:1 JOIN（同 OPT-0008 拆 detail 表模式）
- [ ] Pydantic `LeverageAbuseConfig`：master enable + max 10 rules，每条 `name` (fund-flow 风格 OPT-0021) + `enabled` + `max_margin_level` (默认 D1=105.3 / D2=125) + `streak_min` (默认 D1=1 / D2=3) + `severity` (medium/high)
- [ ] API：`GET/POST /api/v1/risk-monitor/leverage-abuse/config` + `GET /alerts` + `GET /alerts/stats` + `GET /alerts/export`
- [ ] Pytest 覆盖：snapshot scan 基线 / streak 状态机连续 3 次触发 / MARGIN=0 守卫（空仓不触发）/ CEN 比例免疫 / rule_id override / D1 + D2 两条规则并存 / per-rule disabled / 阈值边界

### Frontend (`RiskMonitor.tsx`)

- [ ] 新增第 6 个 tab「滥用杠杆」（建议放「对冲刷单」与「Gap Trade」之间，按 severity 顺序）
- [ ] 列：`rule_label` / `scanned_at` / `server` / `login` (CRM link) / `currency` / `group` / `equity` / `margin_used` / `margin_free` / `margin_level` (带颜色：<105% 红 / 105–125% 琥 / 其他 灰) / `streak_count` (D2 才有) / `leverage` / `net_deposit_hist`
- [ ] `useGridColumnPersist` (key `RISK_MONITOR_LEVERAGE_ABUSE_GRID_STATE_V1`) + `<ColumnVisibilityMenu>`（OPT-0015）
- [ ] `useFilterPersist` (key `RISK_MONITOR_LEVERAGE_ABUSE_FILTERS_V1`)（OPT-0025 — rangePreset / ruleFilter / serverFilter 持久化；`loginInput` / `zipcodeInput` **不**持久化）
- [ ] Per-rule summary cards（D1 / D2 各一张，by `AlertsStats.by_rule`）
- [ ] Config drawer：master enable + rule rows（name / enabled / max_margin_level / streak_min / severity）
- [ ] CSV export
- [ ] **没有「立即扫描」按钮**（snapshot scan 跟 Gap Trade / Hedge Open 一致）

### Docs / Skill

- [ ] 更新 `.cursor/skills/risk-monitor/SKILL.md`：File Map / API Contracts / Data Model / Current Rules（新增 Rule 6 段）/ Implementation Status / Rule ID Allocation 表 / Env Flags（如新增）
- [ ] 更新 `docs/features/risk-monitor.md` §4.2 状态从 Roadmap → Shipped，移到 §3（已上线规则）
- [ ] 在 `docs/features/risk-monitor.md §5` 关键约定追加：snapshot-scan 模式与 cursor 模式的边界
- [ ] `docs/features/risk-monitor-reusable-patterns.md` 加一节「snapshot 状态扫描模板」（streak 状态机 + MARGIN=0 守卫这两个坑必写）

## 实施大致路径

1. **预飞行（idea → ready 的 gate）**：跑 Q1 + Q2 两条 SQL，回填本文件「假设」段实际结果。两条结论合格才能 claim。
2. Backend service + schema + db migration（snapshot scan + streak state 是核心 — 参考 OPT-0021 的 service / detail 表模式 + 抄 quick-profit dedup 的 elapsed-time 思想做冷却）
3. API + scheduler 注册（slow tier only）
4. Pytest（重点：streak 状态机 + MARGIN=0 守卫 + rule_id override）
5. Frontend tab（拷 Hedge Open tab 骨架最相近）
6. Docs + skill 同步

## 相关 OPT

- [[OPT-0021]] Hedge Open — service 结构 / `alert_*_detail` 表模式 / per-rule `name` 字段 / 新 tab 模板，**最近的对照参考**
- [[OPT-0015]] / [[OPT-0025]] — 列状态 + 过滤器持久化两条 hook，新 tab 必走
- [[OPT-0008]] — alert_events 拆 detail 表的模式（rule-specific 字段不进主表 23 列共有字段）
- [[OPT-0011]] cursor scan — 本规则**不适用**（snapshot 非 append-only），需在 service 注释 + 文档明确指出

## Open Questions（实施期再答）

1. D2 streak 状态表放 SQLite（小、与 alert_events 同库、跨重启稳）还是内存（重启重置可能误判）？**倾向 SQLite**。
2. snapshot scan 是否共享 `_scan_lock`？snapshot scan 跟 mt4_trades scan 共写 alert_events，需要走同一把锁 —— **是**。
3. 阈值字段语义：给用户直观的"危险线 MARGIN_LEVEL %"（105 / 125）还是裸 ratio（0.95 / 0.80）？**倾向 MARGIN_LEVEL %**（MT 终端就是这个数）。
4. 是否需要 `min_equity_usd` 过滤掉极小账户的假阳（10 美金账户瞬时 MARGIN_LEVEL 100% 没业务意义）？类比 Gap Trade rule 81 的 `min_net_deposit_hist` 100。
5. 同一账户跨扫描的冷却时间是固定常量还是 per-rule 可配？参考 QP elapsed-time dedup 模式。
