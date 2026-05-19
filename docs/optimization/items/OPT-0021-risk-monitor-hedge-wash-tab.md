---
id: OPT-0021
title: Risk-monitor 新增「对冲刷单」Tab — 抓同账户/同客户跨账户 buy+sell 同 symbol 锁仓刷单
status: idea
priority: P1
area: mixed
effort: L
created: 2026-05-19
related: [[risk-monitor]]
---

## 问题

Risk team 手动发现真实案例 `loginsid='5-67038515'` 在 2026-05-18 19:28:22 同一秒
开了 ~120 单 `NZDCHF.cent`，buy (cmd=0) + sell (cmd=1) 混合，每单 199 lots，
极短时间内被 stop-out 爆仓（close comment `[so at -X%]`，亏损 ~$1.4M）。

行为指纹：
- 同 `(login, symbol)`、同一秒、双向开仓 → wash trading 经典模式
- 动机猜测：刷 IB rebate / 满足成交量返佣门槛 / 操纵 internal volume
- 这次因为手数过大被自己 spread 打爆，**手数小的时候现有 4 个 tab 完全抓不到**

现有 4 个 tab 为什么漏：

| Tab | 漏的原因 |
|---|---|
| 批量下单 (Burst Open, rule 1-50) | 这次手数大→实际命中了；但若 `min_lots_per_order` 调高 / 客户用小手数刷，**rule 不要求双向**，漏 |
| 快开快平 (Quick OC, rule 51-60) | 只看已平仓短持仓；对冲单常被 stop-out 平或长期持有，不算 quick close |
| 快速获利 (Quick Profit, rule 61-70) | 看 P&L 聚合，对冲 P&L ≈ 0，绝不触发 |
| Gap Trade (rule 71-90) | 只跑 MT 00-02 缺口窗 + 只看 SO+AB 跨账户配对（不看**单账户内部**对冲） |

→ 这是一个独立、且现有 rule 没有覆盖的检测维度，应开新 tab。

## 背景

### 数据形态（user 提供的真实 case）

- Server: MT5 (sid=5)，查询走 `fxbackoffice.mt4_trades`（跨 server 统一视图）
- Symbol: `NZDCHF.cent`
- ~120 orders 同秒 19:28:22 broker time
- buy/sell 比例肉眼 ~1:1，每单 19900 raw / 100 = 199 lots
- 全部因为 stop-out 被强平，每单亏 -$10k ~ -$13k

### 现有可复用基建

- ✅ MT4 / MT5 collector（`risk_monitor_service.py` 已经在拉 `CMD IN (0,1)`）
- ✅ `account_enrichment` 一行拿 equity / balance / leverage / group / currency / net_deposit_hist
- ✅ SQLite `alert_events` + OPT-0008 detail 表 1:1 JOIN 模式
- ✅ 前端 AG-Grid + 时间筛选 + summary cards + CSV 导出 + Sheet 详情 + 列持久化 (OPT-0015)
- ✅ Scan-now / config drawer 同模板（参考 Burst Open / Quick OC）
- ✅ SSE 推送（OPT-0013）会自动覆盖新 rule_id

### Rule ID 段（按 SKILL.md 约定）

下一个 free band = **91-100**（10 个 slot）。
- 91-95 留给单账户对冲（不同 window / strictness 配置）
- 96-100 留给跨账户同 userid 对冲

## AC（v1 范围）

### 后端

1. 新建 `backend/app/services/rule_hedge_open_service.py`：
   - 输入：同 `scan_burst_open` 的 raw orders 列表（已经有 cmd / lots / open_time / login / symbol）
   - 检测 1（rule_id 91）单账户内部对冲：
     - 滑窗 `window_sec`（默认 10s）内 per `(server, login, symbol)`
     - count(cmd=0) ≥ `min_orders_per_side` AND count(cmd=1) ≥ `min_orders_per_side`
     - hedge_ratio = `min(buy_lots, sell_lots) / max(buy_lots, sell_lots)` ≥ `min_hedge_ratio`（默认 0.5）
   - 检测 2（rule_id 96）跨账户同 userid 对冲：
     - 先按 `mt4_users.userId` 把同一 broker user 的多个 loginsid 聚到一起
     - 在 user 维度跑同样的 buy+sell 滑窗检测
     - 输出每条 alert 包含 contributing_login_sids 列表
   - 输出符合 `AlertEvent` schema 的 dict
2. 新表 `alert_hedge_open_detail`（OPT-0008 模式）：
   ```sql
   CREATE TABLE alert_hedge_open_detail (
     id INTEGER PRIMARY KEY,           -- 1:1 与 alert_events.id
     buy_count INTEGER,
     sell_count INTEGER,
     buy_lots REAL,
     sell_lots REAL,
     hedge_ratio REAL,                 -- 计算列，写入冗余便于排序
     window_start TEXT,                -- ISO8601 UTC
     window_end TEXT,
     contributing_login_sids TEXT      -- JSON array，跨账户场景才填
   );
   ```
   `risk_monitor_db.py` 的 `_ALERT_SELECT_SQL` / `_ALERT_FROM_CLAUSE` / `_SORT_COL_DB_NAME` 三处补 LEFT JOIN 和 sort 映射；`append_scan_and_events` 加 `elif rule_id in HEDGE_RANGE:` 路由 INSERT。
3. 新增 `HedgeOpenConfig` schema：
   ```
   enabled: bool
   rules: [               max 10
     {
       window_sec: int           1-60, default 10
       min_orders_per_side: int  1-50, default 3
       min_hedge_ratio: float    0.1-1.0, default 0.5
       min_total_lots: float     optional 总手数下限（0 表示不限）
       cross_account: bool       false = 单账户内部；true = 跨账户同 userid
     }
   ]
   ```
4. 新增 API endpoints（模板与 quick-open-close 完全一致）：
   - `GET/POST /api/v1/risk-monitor/hedge-open/config`
   - `GET /api/v1/risk-monitor/hedge-open/alerts`
   - `GET /api/v1/risk-monitor/hedge-open/alerts/stats`
   - `GET /api/v1/risk-monitor/hedge-open/alerts/export`
5. `routes/risk_monitor.py` 加 `HEDGE_OPEN_RULE_ID_BASE = 91`, `HEDGE_OPEN_RULE_ID_MAX = 100`，`/alerts` 按 `BETWEEN` 过滤。
6. `burst_open_scheduler.py` 的 `_run_scan` 加 hedge-open 检测调用，跟 quick-open-close 一起走 slow tier（不进 60s fast tier，wash trading 不需要分秒响应）。
7. dedup：`(rule_id, server, login, symbol, window_start)`（跨账户用 `(rule_id, '', userid, symbol, window_start)`，server 留空区分）。

### 前端

8. `RiskMonitor.tsx` 加第 5 个 tab `value="hedge-open"`，label「对冲刷单」。
9. 新建 `HedgeOpenTab` 组件，结构沿用 `QuickOpenCloseTab`：
   - 时间筛选（presets + 自定义）
   - Summary cards：单账户对冲数 / 跨账户对冲数（rule 91 vs rule 96 by_rule split）
   - AG-Grid 列：rule_label, scanned_at, server, login, symbol, **buy_count, sell_count, buy_lots, sell_lots, hedge_ratio (% + 颜色 ≥80% 红 / 50-80 琥珀 / <50 灰), window_start, window_end**, equity, balance, currency, leverage, group, net_deposit_hist, zipcode, contributing_login_sids (跨账户行才有)
   - 右侧 Sheet 详情面板：列窗口内所有 buy / sell 单 ticket + open_time + lots + open_price
   - 配置 Drawer / CSV 导出
   - `useGridColumnPersist` key = `RISK_MONITOR_HEDGE_OPEN_GRID_STATE_V1`

### 测试

10. `tests/test_rule_hedge_open_service.py`：
    - 单账户对冲基本命中
    - 单账户单方向多单 → 不命中
    - hedge_ratio < min → 不命中
    - 跨账户同 userid 命中
    - 不同 symbol 不混算
    - rule_id override（防 OPT-0008 类 PK 覆盖 bug）

### 文档

11. 改 `.cursor/skills/risk-monitor/SKILL.md`：File Map / API Contracts / Data Model / Current Rules 加 §5 / Rule ID 段表 / Implementation Status
12. 改 `docs/features/risk-monitor.md` §3 加 3.5 节
13. 改 `docs/features/risk-monitor.md` §4 Roadmap 标 v2 = 跨账户跨 userid（待 IP enrichment 数据评估）

## v2（不在本 OPT 范围）

- 跨账户跨 userid 对冲检测（误报高，需要 IP 维度强化）—— 等 v1 上线 1-2 周看 1) 同 userid 跨账户对冲的真实频次  2) IP enrichment 在实时扫描场景的可行性（目前 IP 文件只有当天结束后才齐），再决定怎么做
- 关联到 Gap Trade SO+AB 配对的"同 user 自我对冲"维度（目前 Gap Trade 跨 userid，可能漏同 user 的）

## 开放问题（claim 前需用户确认）

1. **默认阈值是否合理？**`window_sec=10` / `min_orders_per_side=3` / `min_hedge_ratio=0.5` / `min_total_lots=0`（不过滤手数 = 抓小单刷量）—— 上线后看 1-2 周数据调
2. **跨账户聚合的 dedup key 是否合适？**目前提议 `(rule_id, '', userid, symbol, window_start)`，server 留空。等同于"一个 userid 在某 symbol 某窗口只会出一条 alert"。是否够用？
3. **是否要给 hedge_ratio 加分级？**（如 ratio ≥ 0.95 标 "完美对冲"，提升 severity）—— 涉及到全局 severity 体系（roadmap §4.5 还没启动），v1 不做，列展示就行
4. **MT4 vs MT5 数据形状对齐**：MT5 用 `mt5_deals` Entry=0 拉开仓，cmd 字段叫 `Action`，volume 单位 / 10000；要确保 `rule_hedge_open_service` 接收的是已归一化的 dict（沿用 burst-open 的 normalize 路径）

## 结果

（done 后填）
