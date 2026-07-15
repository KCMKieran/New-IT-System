---
id: OPT-0049
title: case_metrics 交易腿改走 stats_trading_running_totals —— 生涯全量扫描 1235x 提速
status: ready
priority: P1
area: db
effort: S
created: 2026-07-15
related: [[OPT-0050]] [[OPT-0046]]
---

## 背景

`case_metrics_service.run_daily_baseline()`（/risk-watchlist + 风控V2 案卷层的每日基线 job）
的交易腿 `_TRADES_WINDOWS_SQL`（`backend/app/services/case_metrics_service.py:78-113`）
对 `fxbackoffice.mt4_trades` 做**生涯全量扫描**：

```sql
COUNT(*)            AS n_all,      -- 无日期下界
SUM(t.lots)         AS lots_all,   -- 无日期下界
SUM(t.totalProfit)  AS pnl_all     -- 无日期下界
FROM fxbackoffice.mt4_trades t
JOIN fxbackoffice.mt4_users mu ON mu.loginSid = t.loginSid
WHERE mu.userId IN ({ids})         -- WHERE 里无任何时间过滤
```

日期只出现在 `SUM(CASE WHEN closeDate >= %(d7)s ...)` 里 —— 那是**扫完之后**的条件求和，
不构成扫描剪枝。roster 每涨一个客户，成本线性增加。

**触发点**：rule 122「宽网观察」（commit `e068f52`, 2026-07-14）把 roster 从 178 撑到 **940**
（实测 `SELECT COUNT(*) FROM risk_cases` = 940，全 watching）。这个 job 现在每天扫 940 个
客户的整张交易表。

## 实测数据（2026-07-15，真实 roster 300 客户分块）

| 方案 | 口径 | 耗时 | 结论 |
|---|---|---|---|
| raw `mt4_trades`（现行） | — | **26.96s** | 现状 |
| **`stats_trading_running_totals`** | **原币** + 账户级，生涯 PL + lots | **0.02s** | ✅ **1235x** |
| `stats_trading_running_totals_user` | **EUR** + 客户级 | — | ❌ 币种不对 |
| `stats_trading`（按日 × 账户） | 原币 | 39.87s | ❌ 比 raw 还慢 |
| `mt4_trades` 仅 90d 窗口（带 `closeDate` 下界） | — | 12.53s | 窗口腿保留用它 |
| 仅 `COUNT(*)` 生涯（`orders_all` 的唯一来源） | — | **38.69s** | ⚠ 见开放问题 |

**准确性已验**（5 个真实高额客户对账 `profit_all`）：4 个与 raw 扫描**完全精确匹配**，
第 5 个（uid 103881）差 105 / 1,073 万 = **0.001%**。两张 stats 表彼此完全一致。

**索引已最优**：`EXPLAIN` 窗口查询显示已正确使用 `IDX_IB_COMMISSION2 (loginSid, closeDate)`
（`type=ref`，每账户仅扫 156 行）。12.5s 是固有成本，**不是缺索引**，无需加索引。

## 目标表结构

```
stats_trading_running_totals  (每 loginSid)
    loginSid | currency | userId
    lotsHavingActivityRunningTotal        -- 对应 lots_all
    plClosedHavingActivityRunningTotal    -- 对应 pnl_all（原币，配合 currency 做 CEN /100）
```

注意 `currency` 列的存在正好对上项目「按行自身 currency 归一」的约定
（见 `docs/features/risk-watchlist.md:43`）。

## 交付内容

1. `pnl_all` / `lots_all` 改走 `stats_trading_running_totals`，按 `currency` 做 CEN /100，
   过滤器与现有交易腿保持一致（`sid IN (1,2,5,6)` + 排 demo —— 注意 2026-07-15 刚做过
   三腿过滤器对齐，见下方「注意」）。
2. 窗口腿（`n_7d/30d/90d`、`lots_7d/30d/90d`、`pnl_7d/30d`、短持仓比、持仓时长）保留
   `mt4_trades` + `closeDate >= d90` 下界 —— 这部分已索引最优。
3. `orders_all` 按下方开放问题的决策处理。
4. 对账测试：新旧两条路径在同一批 roster 客户上的 `profit_all` 差异 ≤ 0.01%。

## AC

- [ ] `run_daily_baseline` 的交易腿不再对 `mt4_trades` 做无日期下界的扫描（除非 `orders_all` 决策为保留）
- [ ] 单块（300 客户）交易腿耗时从 ~27s 降到 ~12.5s（保留 orders_all 则无改善，见开放问题）
- [ ] `profit_all` / `lots_all` 与旧口径在真实 roster 上对账差异 ≤ 0.01%
- [ ] 未改动 rule 121/122 的检测阈值（它们走自己的 `_TRADES_AGG_SQL`，本 OPT 不碰）

## 开放问题（需用户决策）

**`orders_all`（生涯订单数）去留** —— 这是本 OPT 的价值分水岭：

- `stats_trading_running_totals` **只有 PL 和 lots，没有笔数列**
- `orders_all` 是前端展示列（`frontend/src/pages/RiskWatchlist.tsx:464`），但**默认不可见**
  —— 它藏在「订单数」列组折叠里（该组主列是 30d，见 `docs/features/risk-watchlist.md:153-155`）
- 实测**光是这个 `COUNT(*)` 生涯查询就要 38.69s/块**

```
保留 orders_all:  仍需付 38.69s/块  → 本 OPT 一分钱省不下
去掉 orders_all:  生涯腿 27s → 0.02s，交易腿总计 12.55s/块（2.1x）
```

即：**一个藏在列组折叠里、默认都不显示的「生涯订单数」，单独扛着 27–39 秒/块的成本**，
每天为 940 个客户付一次。选项：(a) 砍掉该列；(b) 降级为只保留 30d/90d 窗口；
(c) 保留并接受成本（则本 OPT 只剩对账价值，建议 drop）。**这是产品决策，需用户拍板。**

## 注意

- **不要**改 `rule_rebate_arb_service.py` 的 `_TRADES_AGG_SQL` / `_REBATE_AGG_SQL` ——
  rule 121/122 用它们算自己的聚合，改了会动检测阈值。
- 2026-07-15 有一批未提交的工作区改动（净赚列 + 三个高危口径修复，5 个 agent 产出，
  28 个文件）。`case_metrics_service.py` 在其中（三腿过滤器刚对齐为 `sid IN (1,2,5,6)` + 排 demo）。
  **开工前先确认这批改动的去向**（merge / 丢弃），否则会撞车。
- 之前考虑过的「增量方案」（`rule_rebate_arb_service._merge_trade_legs()` 那个模式：
  昨日快照 + 今日增量）**现已无必要** —— 直接用 running_totals 更简单，不用维护增量状态。
