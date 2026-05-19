---
id: OPT-0021
title: Risk-monitor 新增「对冲刷单」Tab — 抓同账户 buy+sell 同 symbol 锁仓刷单
status: ready
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
| 批量下单 (Burst Open, rule 1-50) | 这次手数大 → 实际命中了；但若 `min_lots_per_order` 调高 / 客户用小手数刷，**rule 不要求双向**，漏 |
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

- ✅ MT4 / MT5 collector（`risk_monitor_service.py:78-` 已经在拉 `CMD IN (0,1)`） — **完全复用**，零 SQL 增量
- ✅ `account_enrichment` 一行拿 equity / balance / leverage / group / currency / net_deposit_hist
- ✅ SQLite `alert_events` + OPT-0008 detail 表 1:1 JOIN 模式
- ✅ 前端 AG-Grid + 时间筛选 + summary cards + CSV 导出 + Sheet 详情 + 列持久化 (OPT-0015)
- ✅ Scan-now / config drawer 同模板（参考 Burst Open / Quick OC）
- ✅ SSE 推送（OPT-0013）会自动覆盖新 rule_id
- ✅ 节奏跟 slow tier（5-10 min）同队，不进 fast tier

### Rule ID 段（按 SKILL.md 约定）

下一个 free band = **91-100**（10 个 slot）。
- **rule 91**：v1 唯一上线规则 — 单账户内部对冲
- **rule 92-100**：v1 不用，留给 future（v2 cross-loginsid / v3 cross-userid / v4 hedge_ratio severity 分级）

### Scheduler 模式现状（dedup 设计依据）

| 环境 | 扫描模式 | 依据 |
|---|---|---|
| Dev (10.6.20.138:5173) | Cursor / HWM watermark | `docker-compose.dev.yml:44` `CURSOR_SCAN_ENABLED=true` |
| Prod (analysis.kohleservices.com) | overlap window（`scan_interval + 30s`） | prod compose 没设 env，默认 `false` |

→ dedup key 设计必须同时支持**两种模式**：cursor 下基本 no-op；overlap 下防同窗口重复触发。
顺手在实施中提一个 SKILL.md 文档修正（标明 prod/dev 当前的实际状态）。

## AC（v1 范围）

### 后端

1. 新建 `backend/app/services/rule_hedge_open_service.py`：
   - 输入：burst-open `_collect_recent_opens()` 已归一化的 dict 列表
     （含 `cmd_label='Buy'/'Sell'`, `lots`, `open_time_raw`, `login`, `symbol`, `server`）
   - **不重新跑 SQL**，直接复用 burst-open 的 data — 在 `_run_scan` 里同一份 data 喂给两个 detector
   - 检测逻辑（rule 91 单账户内部对冲）：
     ```python
     # 按 (server, login, symbol) 分组
     # 对每个组：按 open_time 排序，3s 滑窗
     # 窗内：
     #   buys  = orders where cmd_label == 'Buy'
     #   sells = orders where cmd_label == 'Sell'
     #   if len(buys) >= min_orders_per_side AND len(sells) >= min_orders_per_side:
     #       buy_lots  = sum(o.lots for o in buys)
     #       sell_lots = sum(o.lots for o in sells)
     #       if abs(buy_lots - sell_lots) < 0.01:               # 浮点容差硬编码
     #           if min(buy_lots, sell_lots) >= min_total_lots:  # 单边手数门槛
     #               emit alert
     ```
   - 输出符合 `AlertEvent` schema 的 dict，rule-specific 字段：
     `buy_count`, `sell_count`, `buy_lots`, `sell_lots`, `window_start`, `window_end`,
     `orders`（窗内所有 ticket 明细，供 Sheet 详情用）

2. 新表 `alert_hedge_open_detail`（OPT-0008 模式）：
   ```sql
   CREATE TABLE alert_hedge_open_detail (
     id INTEGER PRIMARY KEY,         -- 1:1 与 alert_events.id
     buy_count INTEGER,
     sell_count INTEGER,
     buy_lots REAL,
     sell_lots REAL,
     window_start TEXT,              -- ISO8601 UTC
     window_end TEXT
   );
   ```
   `risk_monitor_db.py` 的 `_ALERT_SELECT_SQL` / `_ALERT_FROM_CLAUSE` / `_SORT_COL_DB_NAME`
   三处补 LEFT JOIN 和 sort 映射；`append_scan_and_events` 加 `elif rule_id in HEDGE_RANGE:`
   路由 INSERT。

3. 新增 `HedgeOpenConfig` schema（参考 fund-flow `FundFlowRule` 加 `name` 字段）：
   ```python
   class HedgeOpenRuleConfig(BaseModel):
       name: str = Field(min_length=1, max_length=100)   # ← 规则命名（fund-flow 模板）
       enabled: bool = True                               # ← per-rule 开关
       window_sec: int = Field(3, ge=1, le=60)
       min_orders_per_side: int = Field(1, ge=1, le=50)
       min_total_lots: float = Field(0.01, ge=0.01, le=10000)

   class HedgeOpenConfig(BaseModel):
       enabled: bool = True                               # ← 总开关
       rules: list[HedgeOpenRuleConfig] = Field(default_factory=list, max_length=10)
   ```
   - **不暴露 `min_hedge_ratio`**（v1 硬编码完美 1:1 + 0.01 lot EPS）
   - **不暴露 `cross_account`**（v1 只做单账户）

4. 新增 API endpoints（模板与 quick-open-close 完全一致）：
   - `GET/POST /api/v1/risk-monitor/hedge-open/config`
   - `GET /api/v1/risk-monitor/hedge-open/alerts`
   - `GET /api/v1/risk-monitor/hedge-open/alerts/stats`
   - `GET /api/v1/risk-monitor/hedge-open/alerts/export`

5. `routes/risk_monitor.py` 加：
   ```python
   HEDGE_OPEN_RULE_ID_BASE = 91
   HEDGE_OPEN_RULE_ID_MAX = 100
   ```
   `/alerts` 按 `BETWEEN 91 AND 100` 过滤。

6. `burst_open_scheduler.py` 的 `_run_scan` 加 hedge-open 检测调用：
   ```python
   # 已经 collect 完 data
   burst_alerts = detect_burst_open(data, burst_rules)
   hedge_alerts = detect_hedge_open(data, hedge_rules)   # ← 复用 data，零 SQL 增量
   all_alerts = burst_alerts + qoc_alerts + qp_alerts + hedge_alerts
   ```
   跟 slow tier 一起跑（5-10 min），不进 60s fast tier（wash trading 不需要分秒响应）。

7. dedup：`(91, server, login, symbol, window_first_open_time)`
   - 含义：同一批"首开仓时间"只算一次
   - cursor 模式下：HWM 自然不重扫，dedup 是 no-op（防御性写法）
   - overlap 模式下：防同窗口跨 scan 重复触发

8. `rule_label` 写入 alert 时格式：`f"Rule {idx + 1} — {rule.name}"`
   - 例：用户配置 rule[0].name = "高频小手数对冲"
   - alert 显示 "Rule 1 — 高频小手数对冲"
   - 筛选下拉仍按 `rule_id` 主导

### 前端

9. `RiskMonitor.tsx` 加第 4 个 tab（**插入快速获利和 Gap Trade 之间**）：
   - `value="hedge-open"`，label「对冲刷单」
   - `<TabsList>` 的 `grid-cols-4` → **`grid-cols-5`**；mobile 视图测一下挤不挤，挤就改 wrap
   - 顺序：批量下单 → 快开快平 → 快速获利 → **对冲刷单** → Gap Trade

10. 新建 `HedgeOpenTab` 组件，结构沿用 `QuickOpenCloseTab`：
    - 时间筛选（presets + 自定义，复用 burst 同款）
    - Summary cards：by_rule 拆分（rule 91 触发次数 + 命中 login 数）
    - 规则筛选下拉：`全部规则 / Rule 1 — <name> / Rule 2 — <name> / ...`
    - AG-Grid 列：
      ```
      规则 | 扫描时间 | 服务器 | Login | Symbol |
      Buy单 | Sell单 | Buy手数 | Sell手数 |
      窗口开始 | 窗口结束 |
      Equity | Balance | 货币 | 持仓总手 | 杠杆 | Group | 历史净入金 | 邮编
      ```
    - 右侧 Sheet 详情面板：列窗口内所有 buy / sell 单 ticket + open_time + lots + open_price + close_comment
    - 配置 Drawer（参考 fund-flow `RulesDrawer.tsx:125-` 的 rule 卡片样式，顶部 `<Input>` 放规则名）
    - CSV 导出
    - `useGridColumnPersist` key = `RISK_MONITOR_HEDGE_OPEN_GRID_STATE_V1`

### 测试

11. `tests/test_rule_hedge_open_service.py`：
    - 单账户对冲基本命中（5-67038515 case 缩小版）
    - 单账户单方向多单 → 不命中
    - `|buy_lots − sell_lots| > 0.01` → 不命中
    - `min(buy_lots, sell_lots) < min_total_lots` → 不命中
    - 不同 symbol 不混算
    - 不同 server 不混算（即使同 login）
    - 3s 滑窗边界：3.0s 内命中、3.1s 不命中
    - rule_id override（防 OPT-0008 类 PK 覆盖 bug — 业务 rule_id 必须覆盖 SQLite 主键）
    - dedup：同窗口跨两次 scan 只 emit 一次

### 文档

12. 改 `.cursor/skills/risk-monitor/SKILL.md`：
    - File Map 加 `rule_hedge_open_service.py`
    - API Contracts 加 hedge-open 4 个 endpoints
    - Data Model 加 `HedgeOpenConfig` + `alert_hedge_open_detail` 字段
    - Current Rules 加 §3.5 Hedge Open Detection
    - Rule ID 段表加 91-100
    - Implementation Status 标 done
    - **顺手修正**：Env Flags 表 / Architecture 段标明 prod 当前 `CURSOR_SCAN_ENABLED=false`（不是已开），dev 是 true

13. 改 `docs/features/risk-monitor.md` §3 加 §3.5 对冲刷单
14. 改 `docs/features/risk-monitor.md` §4 Roadmap 加：
    - "v2 跨 loginsid（同 clientid）" — 等 v1 上线 1-2 周看真实频次
    - "v3 跨 clientid + IP 强化" — 等 IP enrichment 实时方案就位
    - "v4 hedge_ratio severity 分级" — 等全局 severity 体系（§4.5）启动

## v2+（不在本 OPT 范围）

明确不做的（避免 scope creep）：

- **跨 loginsid（同 clientid）**：等 v1 上线 1-2 周看真实频次再评估
- **跨 clientid 配对**：误报高，需 IP 维度强化；等 IP enrichment 实时方案
- **hedge_ratio 列展示和配置**：v1 硬编码完美 1:1 + 0.01 lot 容差
- **hedge_ratio severity 分级**（如 ≥95% 标 Critical）：等全局 severity 体系（roadmap §4.5）启动
- **max_price_spread 检测**（buy/sell 平均开仓价差）：用来排除"先买后跌再卖"伪阳；v1 不需要
- **跨 server 配对**（同 login 在 MT4 + MT5 互锁）：v1 只看单 server 内部
- **关联 Gap Trade SO+AB**（同 user 自我对冲）：scope 太大，独立 OPT

## 设计决策记录（讨论沉淀）

| 决策 | 选项 | 选了 | 理由 |
|---|---|---|---|
| `window_sec` 默认 | 3s / 5s / 10s | **3s** | case 是同秒发生，3s 留少量余地；过宽会把不相关 manual 单合进来 |
| `min_orders_per_side` 默认 | 1 / 2 / 3 | **1** | 极宽，靠"对称 + 同窗口"过滤误报；与 `min_total_lots` 联合把关 |
| 对冲判定方式 | 严格等 / ratio 门槛 / hedge_ratio 列 | **严格 1:1 + 0.01 lot EPS** | v1 简化；ratio 列展示和分级留 v2/v3 |
| `min_total_lots` 默认 | 0.01 / 1.0 / 100 | **0.01** | 最敏感默认 — 即使 0.01 手对冲也报；小手数刷单是核心场景 |
| `min_total_lots` 语义 | min(buy,sell) / sum / max | **min(buy,sell)** | 含义最直观："对冲了多少手" |
| 浮点容差 | 0 / 0.001 / 0.01 lot | **0.01 lot** | 比 MT4 最小手数 step 还小一格，绝对安全 |
| 检测范围 | 单 login / 跨 loginsid / 跨 clientid | **单 login only** | v1 简化，避免误报；cross-account 留 v2 |
| 跨 server | 单 server / 跨 server 配对 | **每个 server 独立扫描，不跨配对** | 同 login 跨 server 不可能；跨 server 配对属 cross-account 范畴 |
| 节奏 | 60s fast tier / 5-10min slow tier | **slow tier** | wash trading 不需要分秒响应 |
| SQL 策略 | 独立 query / 复用 burst data | **复用 burst data** | 零 SQL 增量；数据需求完全重合 |
| Rule 命名 | 不支持 / 加 `name` 字段 | **加 `name`（fund-flow 模板）** | 多 rule 时分析师能看出每条在干嘛；筛选仍按 rule_id |
| Tab 位置 | 末尾 / 中间插入 | **快速获利和 Gap Trade 之间** | user 指定 |

## 结果

（done 后填）
