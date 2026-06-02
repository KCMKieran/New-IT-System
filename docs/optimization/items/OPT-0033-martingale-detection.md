---
id: OPT-0033
title: 马丁策略检测 (Martingale) tab — risk-monitor 第 7 个检测规则
status: wip
priority: P1
area: mixed
effort: M
created: 2026-06-02
related: [[OPT-0008]], [[OPT-0021]], [[OPT-0030]], [[OPT-0015]], [[OPT-0025]]
---

## 问题

[risk-monitor](http://10.6.20.138:5173/risk-monitor) 缺一条针对**马丁策略**的检测。马丁是典型赌徒策略：持仓亏损时不止损，反而**同产品同方向加仓**（常翻倍），赌价格回归一次性回本+利润。B-Book 模型下客户方向一旦做对，公司单笔大出血——这是核心事前风险信号之一。

用户（业务方）给的判定口径：

> **条件**：持仓订单浮动亏损下，同产品同方向加仓
> - 规则可设置：浮动亏损 > 0；或某个负数数值（浮亏金额门槛）
> - 规则可设置：加仓倍数 1:1 / 1:2…（第二笔相对**第一笔（建仓笔）**的手数倍数；1:1 = 等手数即算，1:2 = 第二笔 ≥ 2 倍）

注意：这版定义基于**当前持仓（未平仓）的浮亏 + 新开仓**，与 `docs/features/risk-monitor.md §4.3` 登记的「按已平仓订单序列 + `account_order_buffer` 跨扫描表」那版**不同**——本版天然落进 event-gated 范式，绕开了 §4.3 标注的「最高难点」。

## 背景 — 为什么 effort 是 M 而非 H

三块数据全部现成，零新基建：

| 需要的数据 | 现成来源 | 复用位置 |
|---|---|---|
| 新开仓事件（谁刚开仓、方向、手数）| `_query_mt4_recent_opens` / `_query_mt5_recent_opens` | burst / hedge / leverage 都在用 |
| 持仓浮亏（同 login/symbol/direction 未平仓浮动 P&L）| MT4 `mt4_trades WHERE CLOSE_TIME='1970-01-01'` 的 `PROFIT+SWAPS`；MT5 `mt5_positions` 的 `Profit+Storage` | Quick Profit 的 `_query_mt4_floating` / `_query_mt5_floating` |
| 建仓笔手数 | 同上持仓快照里 `OPEN_TIME` 最早那笔的 `VOLUME` | 同上 raw 行 |

**唯一需要新写的 SQL**：把 Quick Profit 的账户级 `(server, login)` 浮动查询**改聚合粒度**到 `(server, login, symbol, direction)`，并保留逐笔 `OPEN_TIME` / `VOLUME`（用于取「建仓笔」+ 算浮亏）。不是新数据源。

复用 OPT-0030 的 event-gated 守卫范式：**SETTLE 延迟 60s**（让浮动快照追上新开仓）+ **`MODIFY_TIME >= 开仓时间` 守卫** + **dedup**。无需 `account_order_buffer` 跨扫描表（「建仓笔」= 单次扫描内持仓快照里 `OPEN_TIME` 最早那笔）。

## 设计

**Rule band**：111–120（下一个空闲段；`LEVERAGE_ABUSE` 占 101–110）。常量对 `MARTINGALE_RULE_ID_BASE = 111` / `MARTINGALE_RULE_ID_MAX = 120`，v1 用 111。

**规则引擎（event-gated，slow tier）**：
```
每轮扫描:
  1. 取最近开仓窗内的新开仓单 (复用 opens 查询, cursor_time=None overlap)
  2. 对每笔新开仓 (server, login, symbol, direction):
       拉该 (login,symbol,direction) 全部当前持仓:
         a. 建仓笔 = 这批里 OPEN_TIME 最早的那笔
         b. 浮亏判定: SUM(floating_pnl) < -floating_loss_floor_usd  (floor 默认 0 = 任意浮亏)
         c. 加仓判定: 新单.lots >= 建仓笔.lots × lot_multiplier
       三条全满足 → 告警 (默认加仓 1 次即报; min_add_count 门槛可配)
  3. dedup: (rule_id, server, login, symbol, direction, new_open_time)
  4. SETTLE 60s + MODIFY_TIME 守卫 (同 OPT-0030)
```

**配置 `MartingaleRuleConfig`**（用户决策 2026-06-02 锁定）：
```
enabled: bool
rules: [{
  name: str                      # 用户命名, 同 hedge/leverage 模式
  enabled: bool
  floating_loss_floor_usd: float # 默认 0 = 任意浮亏; 设 500 = 浮亏需 > $500
  lot_multiplier: float          # 默认 1.0 (1:1); 2.0 = 1:2; 和【建仓笔】比
  min_add_count: int             # 默认 1 = 加仓 1 次即报
}]
```

## 验收标准

- [ ] 后端 `rule_martingale_service.py`：event-gated 检测函数，复用 opens + 改粒度的浮动查询，返回 AlertEvent base 字段；rule_id override guard（落 111–120）
- [ ] `alert_martingale_detail` 表（rule-specific 字段：`floating_pnl` / `anchor_lots`（建仓笔手数）/ `new_lots` / `lot_ratio` / `add_count` / `direction`），1:1 JOIN `alert_events`；`_ALERT_SELECT_SQL` + `_ALERT_FROM_CLAUSE` + `_SORT_COL_DB_NAME` + `append_scan_and_events` 路由分支
- [ ] schema `MartingaleRuleConfig` + `load/save_martingale_config`
- [ ] 4 endpoint：`GET/POST /martingale/config` + `GET /martingale/alerts` + `/alerts/stats` + `/alerts/export`（统一 response shape + zipcode 后端 LIKE）
- [ ] 挂现有 `_scheduler` slow tier（**不**新建 BackgroundScheduler）+ 进 all tier（scan-now）
- [ ] 前端 `MartingaleTab`：4 个强制 hook（apiFetch / AbortController / useGridColumnPersist + ColumnVisibilityMenu / useFilterPersist）+ per-rule 卡片 + config drawer + `netDepositColDef` 工厂；有「立即扫描」（看是否归类为 investigative — 默认有）
- [ ] CEN：浮亏金额阈值 ÷100 归一；手数/倍数不受影响
- [ ] 单测：1:1 vs 1:2 边界、任意浮亏 vs 金额门槛、建仓笔取最早、min_add_count、direction/symbol 隔离、rule_id override guard、CEN 归一、dedup、双 cluster(MT4/MT5)、disabled rule
- [ ] 回写 SKILL.md（File Map / API Contracts / Data Model / Current Rules / Implementation Status）+ docs/features/risk-monitor.md §4.3（标记本版已实现，注明与原 closed-order 设计的差异）

## 决策记录（2026-06-02）

| 决策点 | 选择 |
|---|---|
| 浮亏判定 | 可配阈值，默认任意浮亏（`floating_pnl < 0`，金额门槛 floor 默认 0）|
| 加仓倍数参照 | 和**第一笔（建仓笔）**比 |
| 触发门槛 | 可配 `min_add_count`，默认加仓 1 次即报 |

## Follow-up（拆 Phase 2，不阻塞 v1）

- Severity 分级 + Email 告警（roadmap 建议「上 Martingale 时连同分级一起做」）
- 跨平仓历史的「回本+利润」最强信号识别（§4.3 原设计的 `curr.profit > |累计前几笔亏损|`）—— 需平仓序列，留待评估
