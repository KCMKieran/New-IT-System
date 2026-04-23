# Risk Monitor — Historical Archive

> Archived sections from `risk-monitor.md` and `risk-monitor-roadmap.md`.
> These are completed development plans, exploratory SQL, removed rule designs,
> and implemented feature investigation records.
>
> **Active documents**:
> - [risk-monitor.md](./risk-monitor.md) — current design doc
> - [risk-monitor-roadmap.md](./risk-monitor-roadmap.md) — future plans
> - `.cursor/skills/risk-monitor/SKILL.md` — AI skill (quick reference)

---

## From risk-monitor.md

### Old Rule: 频繁开仓检测 (Frequent Opening Detection) — superseded by §10 Burst Open

> **Status**: Superseded by Burst Open Detection (§10) as of 2026-03-28.
> Risk team feedback: 5-8 minutes with 3 orders is normal trading behavior.

**触发条件**: 最近 N 分钟内，同一账户开仓笔数 ≥ 阈值（不区分品种、不区分方向）。

**三个可调参数**:

| 参数 | 含义 | 默认值 | API 参数名 |
|------|------|--------|-----------|
| 检查窗口 | 时间范围 (分钟) | 8 | `check_interval` |
| 最少开仓数 | 开仓笔数阈值 | 3 | `min_order_count` |
| 每手净值阈值 | 低于此值标 ALERT (USD) | 2000 | `equity_per_lot_threshold` |

**核心指标**: `equity_per_lot = equity / total_lots_in_window`

- **Equity** (净值): MT4 从 `mt4_users.EQUITY` 直接取；MT5 计算 `Balance + SUM(mt5_positions.Profit + Storage)`
- 使用 Equity 而非 Balance，因为 Equity 反映账户当前真实可用资金

**告警等级** (两级):

| 条件 | 等级 | 含义 |
|------|------|------|
| 开仓数 ≥ 阈值 且 equity_per_lot < threshold | **ALERT** (红) | 频繁开仓 + 高杠杆 |
| 开仓数 ≥ 阈值 但 equity_per_lot ≥ threshold | **WATCH** (黄) | 频繁开仓但资金充足 |

**数据源差异** (与 Scale-In 不同):

| | Scale-In | Frequent Open |
|--|---|---|
| 扫描范围 | 所有未平仓持仓 | **最近 N 分钟开仓的订单** |
| 分组 | 账户+品种+方向 | **仅账户** |
| 已平仓订单 | 不包含 | **包含** (开了又平也会被捕获) |
| MT4 数据源 | mt4_trades WHERE CLOSE_TIME='1970' | mt4_trades WHERE OPEN_TIME >= cutoff |
| MT5 数据源 | mt5_positions | **mt5_deals WHERE Entry=0** |

**MT5 Timestamp 注意事项**:

`mt5_deals.Timestamp` 存储的是 **Windows FILETIME** 格式 (自 1601-01-01 起的 100 纳秒计数)，**不是** Unix 时间戳。转换公式:

```
filetime = (unix_seconds + 11644473600) × 10000000
```

---

### Old Rule: 持仓累积 + 资金比 (Scale-In Detection) — removed 2026-03

> **Status**: Frontend and backend code removed 2026-03-26. Design doc preserved for reference.

**触发条件**: 同一账户 + 同一品种 + 同一方向，持有 ≥ 3 笔未平仓单。

**输出字段**: 账户、品种、方向、持仓笔数、总手数、浮动盈亏、余额、单手资金比、保证金比例

**告警等级** (基于单手资金比，从公司风险角度):

| 单手资金比 | 等级 | 含义 (公司视角) |
|-----------|------|------|
| < $500 | **CRITICAL** | 杠杆极高，客户方向一对公司亏损巨大 |
| $500 ~ $2,000 | HIGH | 高杠杆操作，需关注 |
| $2,000 ~ $5,000 | WATCH | 中等杠杆，留意 |
| > $5,000 | _(不显示)_ | 杠杆不高，风险可控 |

**案例参考 (MT5 Account 67035072)**:

```
余额: $1,489 | 杠杆: 1:1000 | 持仓: 10笔×1手 XAUUSD
名义价值: $5M | 保证金: $5,000 | 保证金比例: ~154%
单手资金比: $769 → 等级: HIGH
特征: 分批手动建仓, 越跌越买, EA一键全平
一天内亏损 80% (昨日余额 $7,192 → 今日 $1,489)
```

---

### Old Python Detection Engine (pre-Burst Open)

```python
from dataclasses import dataclass
from datetime import datetime
from itertools import groupby
from collections import defaultdict


@dataclass
class Alert:
    rule: str
    server: str
    login: int
    severity: str        # CRITICAL | HIGH | WATCH | NORMAL
    details: dict


def run_all_rules(
    positions: list[dict],
    recent_closes: list[dict],
) -> list[Alert]:
    """主入口: 对标准化数据执行所有已注册的规则"""
    alerts = []
    alerts.extend(rule_scale_in_detect(positions))
    alerts.extend(rule_batch_close_detect(recent_closes))
    return alerts


# ---------- 规则 1: 持仓累积检测 ----------

def rule_scale_in_detect(positions: list[dict]) -> list[Alert]:
    alerts = []

    # group by (server, login, symbol, direction)
    key_fn = lambda p: (p["server"], p["login"], p["symbol"], p["direction"])
    positions.sort(key=key_fn)

    for key, group in groupby(positions, key=key_fn):
        orders = list(group)
        if len(orders) < 3:
            continue

        server, login, symbol, direction = key
        total_lots = sum(o["lots"] for o in orders)
        floating_pnl = sum(o["profit"] for o in orders)
        balance = orders[0].get("balance")

        # capital per lot
        capital_per_lot = balance / total_lots if balance and total_lots > 0 else None

        # severity
        if capital_per_lot is None:
            severity = "WATCH"
        elif capital_per_lot < 500:
            severity = "CRITICAL"
        elif capital_per_lot < 2000:
            severity = "HIGH"
        elif capital_per_lot < 5000:
            severity = "WATCH"
        else:
            severity = "NORMAL"

        alerts.append(Alert(
            rule="SCALE_IN",
            server=server,
            login=login,
            severity=severity,
            details={
                "symbol": symbol,
                "direction": direction,
                "open_count": len(orders),
                "total_lots": total_lots,
                "floating_pnl": floating_pnl,
                "balance": balance,
                "leverage": orders[0].get("leverage"),
                "capital_per_lot": capital_per_lot,
                "first_open": min(o["open_time"] for o in orders),
                "last_open": max(o["open_time"] for o in orders),
            },
        ))

    return alerts


# ---------- 规则 2: 同秒批量平仓检测 ----------

def rule_batch_close_detect(recent_closes: list[dict]) -> list[Alert]:
    alerts = []

    # group by (server, login, close_time rounded to second)
    key_fn = lambda c: (
        c["server"], c["login"], c["symbol"],
        c["close_time"].replace(microsecond=0) if isinstance(c["close_time"], datetime)
        else c["close_time"]
    )
    recent_closes.sort(key=key_fn)

    for key, group in groupby(recent_closes, key=key_fn):
        orders = list(group)
        if len(orders) < 3:
            continue

        server, login, symbol, close_time = key
        total_profit = sum(o["total_profit"] for o in orders)
        wins = sum(1 for o in orders if o["total_profit"] > 0)

        severity = "HIGH" if total_profit > 1000 else "WATCH"

        alerts.append(Alert(
            rule="BATCH_CLOSE",
            server=server,
            login=login,
            severity=severity,
            details={
                "symbol": symbol,
                "close_time": close_time,
                "batch_size": len(orders),
                "total_lots": sum(o["lots"] for o in orders),
                "total_profit": total_profit,
                "win_rate": wins / len(orders),
            },
        ))

    return alerts
```

---

### §6 开发计划 (Phase 1-5, all completed)

> **Status**: All phases completed. Archived 2026-04-23.

#### Phase 1: 后端骨架 + MT5 数据验证

**目标**: 最小可用后端，先通 MT5 一个数据源，验证整条链路。

| 步骤 | 做什么 | 细节 |
|------|--------|------|
| 1a | `schemas/risk_monitor.py` | 定义 Pydantic 模型：AlertDetail、Alert、ScanSummary、ScanResponse，字段严格按 Skill 中的 API Contract |
| 1b | `services/risk_monitor_service.py` | `_query_mt5_positions()`: MT5 SQL → 标准化 dict 列表。`rule_scale_in_detect()`: 分组+计算+分级。`scan()`: 主入口串联采集→规则→响应组装 |
| 1c | `routes/risk_monitor.py` | GET `/scan` 路由，接收 login/server 可选参数 |
| 1d | `routers.py` | 注册 risk_monitor router，prefix `/risk-monitor` |

#### Phase 2: 加入 MT4 双服务器

**目标**: 数据采集层覆盖全部三个服务器。

| 步骤 | 做什么 | 细节 |
|------|--------|------|
| 2a | `_query_mt4_positions()` | MT4 Live SQL: CLOSE_TIME='1970' + JOIN mt4_users 拿 BALANCE/LEVERAGE/GROUP，返回标准化格式 |
| 2b | 复制改库名 | MT4 Live2 查询与 Live 完全一致，库名 `mt4_live` → `mt4_live2` |
| 2c | `scan()` 合并 | 三个查询结果 concat 后传给规则引擎 |
| 2d | server 参数 | `?server=mt4_live` 只查对应数据源 |

#### Phase 3: 前端页面 — 基础框架

**目标**: 页面能跑起来，看到数据。

| 步骤 | 做什么 | 细节 |
|------|--------|------|
| 3a | 路由/侧边栏注册 | `App.tsx` 加 lazy route。`app-sidebar.tsx` Risk Control 分组下加"交易实时监控"。`site-header.tsx` 加标题映射 |
| 3b | `RiskMonitor.tsx` 骨架 | useEffect + AbortController 调 /scan API |
| 3c | 统计卡片 | 顶部 4 张 Card: CRITICAL/HIGH/WATCH 数量 + 扫描账户数 |
| 3d | AG-Grid 表格 | 14 列 columnDefs，客户端模式，rowData 直接传入 |

#### Phase 4: 前端页面 — 交互完善

**目标**: 筛选、颜色、自动刷新等交互细节。

| 步骤 | 做什么 | 细节 |
|------|--------|------|
| 4a | 行颜色 | getRowStyle 按 severity 设背景色 |
| 4b | 等级徽章 | severity 列 cellRenderer 渲染 Badge |
| 4c | 浮动盈亏颜色 | 正值(客户赚=公司亏)→红色，负值→绿色 |
| 4d | 筛选器 | 服务器下拉 + 等级下拉(默认 CRITICAL+HIGH) + 账户号搜索 |
| 4e | 自动刷新 | setInterval 10min + 手动刷新按钮 + "上次扫描时间"显示 |
| 4f | 排序 | severity 自定义排序 CRITICAL > HIGH > WATCH |
| 4g | 加载状态 | 首次 Skeleton，刷新中按钮 loading |

#### Phase 5: 部署 + 观察调优

**目标**: 上线，观察告警质量，调整参数。

| 步骤 | 做什么 | 细节 |
|------|--------|------|
| 5a | 部署到 prod | `deploy.sh` 部署，确认 prod 能正常查询 MySQL Slave |
| 5b | 观察 1-2 天 | 记录：告警总数、CRITICAL 数、HIGH 数。判断是否合理 |
| 5c | 调整阈值 | CRITICAL 太多 → $500 阈值下调；告警太少 → ≥3 门槛放松 |
| 5d | 确认业务价值 | 和风控团队确认展示的客户是否确实需要关注 |

#### 待实施: UI 重设计 (已讨论，未实施)

基于首期 demo 反馈，以下改动已确认但尚未实施:

1. **移除 severity 分级体系** — CRITICAL/HIGH/WATCH 标签太抽象，用户需要看原始数据自行判断
2. **卡片改为服务器维度** — 3 张卡片 (MT4_Live / MT4_Live2 / MT5)，每张显示: 持仓账户数、总手数、总浮动盈亏
3. **后端 summary 扩展** — scan 响应新增 `by_server` 和 `top_symbols` 数组
4. **移除等级筛选器** — 保留服务器下拉 + 账户号搜索
5. **移除等级列和行颜色** — 表格按 capital_per_lot 升序排列，用户看数值判断
6. **加下次扫描倒计时** — 显示距离下次自动刷新的时间
7. **可选: 自定义扫描间隔** — 下拉 (5min/10min/30min)，纯前端改动
8. **多规则 Tab 设计** — 未来每条规则一个 Tab
9. **Demo banner** — 当前页面顶部有蓝色 banner 说明规则含义

#### 待实施: 后端驱动定时扫描 (已实施为 BurstOpenScheduler)

> **Status**: Implemented as APScheduler-based `BurstOpenScheduler`. See §10 in main doc.

**原始设计方案**:

```
┌─────────────────────────────────────────────────────────────┐
│                  后端 (FastAPI + Background Task)            │
│                                                             │
│  ┌─────────────────────────────────────────────────────┐    │
│  │  定时调度器 (APScheduler / asyncio background task)  │    │
│  │                                                     │    │
│  │  每 check_interval 分钟执行一次:                      │    │
│  │  1. 从 config/DB 读取当前参数                         │    │
│  │  2. 执行 scan_frequent_open()                        │    │
│  │  3. 结果写入缓存 (内存 / Redis)                       │    │
│  │  4. 如有 ALERT → 触发 Email 通知                      │    │
│  └─────────────────────────────────────────────────────┘    │
│                                                             │
│  GET /frequent-open → 读缓存                                │
│  POST /frequent-open/config → 更新参数 + 立即扫描           │
│  POST /frequent-open/scan-now → 立即触发一次扫描            │
└─────────────────────────────────────────────────────────────┘
```

#### 后续阶段（按需启动）

| 阶段 | 内容 | 前置条件 |
|------|------|---------|
| **缺口交易规则** | 检测休市前开仓、开市后平仓利用缺口获利 | Tab 占位已创建 |
| 批量平仓规则 | 同秒 ≥3 笔平仓检测 | Tab 框架就绪 |
| Email 告警 | Redis 去重 + send_email() | **后端驱动扫描完成** |
| Dashboard 组件 | SuspiciousClients.tsx 卡片 | 页面稳定 |

---

### §7 规则扩展路线 (superseded by roadmap.md §三)

以下规则已设计但暂不实现，后续按需添加:

- [ ] **批量下单**: 5s 内 3+ 单, 每单 ≥ 5 手
- [ ] **快速开平**: 大手数 + 持仓 ≤ 2 分钟
- [ ] **频繁推仓**: 1s 内同方向 3+ 单
- [ ] **大额敞口**: 单客户单品种未平仓总手数 > 阈值
- [ ] **快速盈利**: 20min 内总利润 > $5,000
- [ ] **同向集中度**: 全客户单品种净头寸 > 阈值 (公司级风险)
- [ ] **高胜率异常**: 胜率 > 80% + 平均持仓 < 5 分钟
- [ ] **跨账户关联**: 同 IP / 同 IB 树的账户同时同方向建仓
- [ ] **新闻窗口**: 重大经济数据发布前后的大额建仓

---

### §8 已确认事项

- [x] `mt4_live.mt4_users` 有 `BALANCE`, `EQUITY`, `MARGIN`, `MARGIN_LEVEL`, `MARGIN_FREE`, `LEVERAGE`, `CURRENCY` 字段
- [x] MT4 Live2 有独立的 `mt4_live2.mt4_trades` 表，结构与 `mt4_live.mt4_trades` 完全一致 (同样的索引)
- [x] MT4 账户的 Group 排除逻辑与 MT5 一致 (`GROUP NOT LIKE '%demo%' AND GROUP NOT LIKE '%test%'`)

---

### §9 探索性 SQL

#### 9.1 三个 Server 未平仓订单明细

**MT4 Live**:

```sql
SELECT
    'MT4_Live' AS server,
    t.TICKET, t.LOGIN, t.SYMBOL,
    CASE WHEN t.CMD = 0 THEN 'Buy' ELSE 'Sell' END AS direction,
    t.VOLUME / 100 AS lots,
    t.OPEN_TIME, t.OPEN_PRICE,
    t.PROFIT, t.SWAPS, t.COMMISSION,
    t.PROFIT + t.SWAPS + t.COMMISSION AS total_profit,
    t.SL, t.TP, t.MAGIC, t.COMMENT
FROM mt4_live.mt4_trades t
WHERE t.CLOSE_TIME = '1970-01-01 00:00:00'
  AND t.CMD IN (0, 1)
  AND NOT EXISTS (
      SELECT 1 FROM mt4_live.mt4_users u
      WHERE u.LOGIN = t.LOGIN
        AND (u.`GROUP` LIKE '%demo%' OR u.`GROUP` LIKE '%test%')
  )
ORDER BY t.LOGIN, t.OPEN_TIME;
```

**MT4 Live2**:

```sql
SELECT
    'MT4_Live2' AS server,
    t.TICKET, t.LOGIN, t.SYMBOL,
    CASE WHEN t.CMD = 0 THEN 'Buy' ELSE 'Sell' END AS direction,
    t.VOLUME / 100 AS lots,
    t.OPEN_TIME, t.OPEN_PRICE,
    t.PROFIT, t.SWAPS, t.COMMISSION,
    t.PROFIT + t.SWAPS + t.COMMISSION AS total_profit,
    t.SL, t.TP, t.MAGIC, t.COMMENT
FROM mt4_live2.mt4_trades t
WHERE t.CLOSE_TIME = '1970-01-01 00:00:00'
  AND t.CMD IN (0, 1)
  AND NOT EXISTS (
      SELECT 1 FROM mt4_live2.mt4_users u
      WHERE u.LOGIN = t.LOGIN
        AND (u.`GROUP` LIKE '%demo%' OR u.`GROUP` LIKE '%test%')
  )
ORDER BY t.LOGIN, t.OPEN_TIME;
```

**MT5**:

```sql
SELECT
    'MT5' AS server,
    p.Position, p.Login, p.Symbol,
    CASE WHEN p.Action = 0 THEN 'Buy' ELSE 'Sell' END AS direction,
    p.Volume / 10000 AS lots,
    p.TimeCreate, p.PriceOpen, p.PriceCurrent,
    p.Profit, p.Storage AS swaps,
    p.ContractSize,
    p.Comment, p.ExpertID
FROM mt5_live.mt5_positions p
INNER JOIN mt5_live.mt5_users u ON p.Login = u.Login
WHERE u.`Group` NOT LIKE '%demo%'
  AND u.`Group` NOT LIKE '%test%'
ORDER BY p.Login, p.TimeCreate;
```

**三个 Server 未平仓订单数量汇总**:

```sql
SELECT 'MT4_Live' AS server, COUNT(*) AS open_count
FROM mt4_live.mt4_trades
WHERE CLOSE_TIME = '1970-01-01 00:00:00' AND CMD IN (0, 1)

UNION ALL

SELECT 'MT4_Live2', COUNT(*)
FROM mt4_live2.mt4_trades
WHERE CLOSE_TIME = '1970-01-01 00:00:00' AND CMD IN (0, 1)

UNION ALL

SELECT 'MT5', COUNT(*)
FROM mt5_live.mt5_positions;
```

#### 9.2 按账户汇总未平仓 (手数 + 盈亏)

**MT4 Live — 按账户汇总**:

```sql
SELECT
    'MT4_Live' AS server,
    t.LOGIN,
    u.`GROUP`,
    u.BALANCE, u.EQUITY, u.LEVERAGE,
    COUNT(*) AS open_count,
    SUM(t.VOLUME / 100) AS total_lots,
    SUM(t.PROFIT) AS total_profit,
    SUM(t.SWAPS) AS total_swaps,
    SUM(t.PROFIT + t.SWAPS + t.COMMISSION) AS total_pnl,
    GROUP_CONCAT(DISTINCT t.SYMBOL ORDER BY t.SYMBOL) AS symbols,
    ROUND(u.BALANCE / NULLIF(SUM(t.VOLUME / 100), 0), 2) AS capital_per_lot
FROM mt4_live.mt4_trades t
INNER JOIN mt4_live.mt4_users u ON t.LOGIN = u.LOGIN
WHERE t.CLOSE_TIME = '1970-01-01 00:00:00'
  AND t.CMD IN (0, 1)
  AND u.`GROUP` NOT LIKE '%demo%'
  AND u.`GROUP` NOT LIKE '%test%'
GROUP BY t.LOGIN, u.`GROUP`, u.BALANCE, u.EQUITY, u.LEVERAGE
ORDER BY total_lots DESC;
```

**MT4 Live2 — 按账户汇总**:

```sql
SELECT
    'MT4_Live2' AS server,
    t.LOGIN,
    u.`GROUP`,
    u.BALANCE, u.EQUITY, u.LEVERAGE,
    COUNT(*) AS open_count,
    SUM(t.VOLUME / 100) AS total_lots,
    SUM(t.PROFIT) AS total_profit,
    SUM(t.SWAPS) AS total_swaps,
    SUM(t.PROFIT + t.SWAPS + t.COMMISSION) AS total_pnl,
    GROUP_CONCAT(DISTINCT t.SYMBOL ORDER BY t.SYMBOL) AS symbols,
    ROUND(u.BALANCE / NULLIF(SUM(t.VOLUME / 100), 0), 2) AS capital_per_lot
FROM mt4_live2.mt4_trades t
INNER JOIN mt4_live2.mt4_users u ON t.LOGIN = u.LOGIN
WHERE t.CLOSE_TIME = '1970-01-01 00:00:00'
  AND t.CMD IN (0, 1)
  AND u.`GROUP` NOT LIKE '%demo%'
  AND u.`GROUP` NOT LIKE '%test%'
GROUP BY t.LOGIN, u.`GROUP`, u.BALANCE, u.EQUITY, u.LEVERAGE
ORDER BY total_lots DESC;
```

**MT5 — 按账户汇总**:

```sql
SELECT
    'MT5' AS server,
    p.Login,
    u.`Group`,
    u.Balance, u.Leverage,
    COUNT(*) AS open_count,
    SUM(p.Volume / 10000) AS total_lots,
    SUM(p.Profit) AS total_profit,
    SUM(p.Storage) AS total_swaps,
    GROUP_CONCAT(DISTINCT p.Symbol ORDER BY p.Symbol) AS symbols,
    ROUND(u.Balance / NULLIF(SUM(p.Volume / 10000), 0), 2) AS capital_per_lot
FROM mt5_live.mt5_positions p
INNER JOIN mt5_live.mt5_users u ON p.Login = u.Login
WHERE u.`Group` NOT LIKE '%demo%'
  AND u.`Group` NOT LIKE '%test%'
GROUP BY p.Login, u.`Group`, u.Balance, u.Leverage
ORDER BY total_lots DESC;
```

#### 9.3 未平仓中同秒开仓 ≥ N 笔 (粗筛)

> **不要用 Self-JOIN 做滑动窗口** — 对数千行未平仓数据做 Self-JOIN 复杂度 O(N²)，会超时。
> 用 GROUP BY 按秒聚合做粗筛 (秒级返回)，> 1 秒的精确窗口检测交给 Python。

**MT4 Live**:

```sql
SELECT
    t.LOGIN, t.SYMBOL,
    CASE WHEN t.CMD = 0 THEN 'Buy' ELSE 'Sell' END AS direction,
    DATE_FORMAT(t.OPEN_TIME, '%Y-%m-%d %H:%i:%s') AS open_second,
    COUNT(*) AS orders_in_1sec,
    SUM(t.VOLUME / 100) AS total_lots,
    GROUP_CONCAT(t.TICKET ORDER BY t.OPEN_TIME) AS tickets,
    GROUP_CONCAT(t.VOLUME / 100 ORDER BY t.OPEN_TIME) AS lots_list
FROM mt4_live.mt4_trades t
WHERE t.CLOSE_TIME = '1970-01-01 00:00:00'
  AND t.CMD IN (0, 1)
  AND NOT EXISTS (
      SELECT 1 FROM mt4_live.mt4_users u
      WHERE u.LOGIN = t.LOGIN
        AND (u.`GROUP` LIKE '%demo%' OR u.`GROUP` LIKE '%test%')
  )
GROUP BY t.LOGIN, t.SYMBOL, t.CMD, DATE_FORMAT(t.OPEN_TIME, '%Y-%m-%d %H:%i:%s')
HAVING orders_in_1sec >= 3
ORDER BY orders_in_1sec DESC;
```

**MT4 Live2**:

```sql
SELECT
    t.LOGIN, t.SYMBOL,
    CASE WHEN t.CMD = 0 THEN 'Buy' ELSE 'Sell' END AS direction,
    DATE_FORMAT(t.OPEN_TIME, '%Y-%m-%d %H:%i:%s') AS open_second,
    COUNT(*) AS orders_in_1sec,
    SUM(t.VOLUME / 100) AS total_lots,
    GROUP_CONCAT(t.TICKET ORDER BY t.OPEN_TIME) AS tickets,
    GROUP_CONCAT(t.VOLUME / 100 ORDER BY t.OPEN_TIME) AS lots_list
FROM mt4_live2.mt4_trades t
WHERE t.CLOSE_TIME = '1970-01-01 00:00:00'
  AND t.CMD IN (0, 1)
  AND NOT EXISTS (
      SELECT 1 FROM mt4_live2.mt4_users u
      WHERE u.LOGIN = t.LOGIN
        AND (u.`GROUP` LIKE '%demo%' OR u.`GROUP` LIKE '%test%')
  )
GROUP BY t.LOGIN, t.SYMBOL, t.CMD, DATE_FORMAT(t.OPEN_TIME, '%Y-%m-%d %H:%i:%s')
HAVING orders_in_1sec >= 3
ORDER BY orders_in_1sec DESC;
```

**MT5**:

```sql
SELECT
    p.Login, p.Symbol,
    CASE WHEN p.Action = 0 THEN 'Buy' ELSE 'Sell' END AS direction,
    DATE_FORMAT(p.TimeCreate, '%Y-%m-%d %H:%i:%s') AS open_second,
    COUNT(*) AS orders_in_1sec,
    SUM(p.Volume / 10000) AS total_lots,
    GROUP_CONCAT(p.Position ORDER BY p.TimeCreate) AS position_ids,
    GROUP_CONCAT(p.Volume / 10000 ORDER BY p.TimeCreate) AS lots_list
FROM mt5_live.mt5_positions p
INNER JOIN mt5_live.mt5_users u ON p.Login = u.Login
WHERE u.`Group` NOT LIKE '%demo%'
  AND u.`Group` NOT LIKE '%test%'
GROUP BY p.Login, p.Symbol, p.Action, DATE_FORMAT(p.TimeCreate, '%Y-%m-%d %H:%i:%s')
HAVING orders_in_1sec >= 3
ORDER BY orders_in_1sec DESC;
```

#### 9.4 Volume 与 Lots 换算关系验证

| 平台 | Volume 含义 | 换算公式 | 示例 |
|------|------------|---------|------|
| MT4 | lots × 100 | `VOLUME / 100` | Volume=100 → 1.00 手 |
| MT5 | lots × 10000 | `Volume / 10000` | Volume=10000 → 1.00 手 |

原因: MT5 支持更高精度的手数 (最小 0.0001 手)，MT4 最小精度 0.01 手。

#### 9.5 MT5 账户交易分析

**查看账户已平仓记录**:

```sql
SELECT
    d.Deal, d.Login, d.Symbol,
    CASE WHEN d.Action = 0 THEN 'Buy' ELSE 'Sell' END AS direction,
    CASE d.Entry WHEN 0 THEN 'Open' WHEN 1 THEN 'Close' WHEN 3 THEN 'CloseBy' END AS entry_type,
    d.Volume / 10000 AS lots,
    d.Price, d.Profit, d.Commission,
    d.Storage AS swaps,
    d.Profit + d.Commission + d.Storage AS total_profit,
    d.PositionID, d.Time, d.Comment
FROM mt5_live.mt5_deals d
WHERE d.Login = <账户号>
  AND d.Action IN (0, 1)
  AND d.Entry IN (1, 3)
ORDER BY d.Time DESC
LIMIT 50;
```

**查看完整交易生命周期 (开仓→平仓配对)**:

```sql
SELECT
    open_d.Deal AS open_deal,
    open_d.PositionID,
    open_d.Volume / 10000 AS lots,
    CASE WHEN open_d.Action = 0 THEN 'Buy' ELSE 'Sell' END AS direction,
    open_d.Price AS open_price,
    open_d.Time AS open_time,
    close_d.Deal AS close_deal,
    close_d.Price AS close_price,
    close_d.Profit,
    close_d.Time AS close_time,
    TIMESTAMPDIFF(SECOND, open_d.Time, close_d.Time) AS hold_seconds
FROM mt5_live.mt5_deals close_d
INNER JOIN mt5_live.mt5_deals open_d
    ON close_d.PositionID = open_d.PositionID
    AND open_d.Entry = 0
WHERE close_d.Login = <账户号>
  AND close_d.Action IN (0, 1)
  AND close_d.Entry IN (1, 3)
ORDER BY open_d.Time DESC
LIMIT 50;
```

#### 9.6 账户资金状况

```sql
-- MT4 Live
SELECT LOGIN, `GROUP`, BALANCE, EQUITY, MARGIN,
       MARGIN_LEVEL, MARGIN_FREE, LEVERAGE, CURRENCY
FROM mt4_live.mt4_users WHERE LOGIN = <账户号>;

-- MT4 Live2
SELECT LOGIN, `GROUP`, BALANCE, EQUITY, MARGIN,
       MARGIN_LEVEL, MARGIN_FREE, LEVERAGE, CURRENCY
FROM mt4_live2.mt4_users WHERE LOGIN = <账户号>;

-- MT5
SELECT Login, `Group`, Balance, Credit, Leverage,
       BalancePrevDay, EquityPrevDay
FROM mt5_live.mt5_users WHERE Login = <账户号>;
```

#### 9.7 数据延迟检查

```sql
SELECT 'MT4_Live' AS server, OPEN_TIME AS latest
FROM mt4_live.mt4_trades ORDER BY OPEN_TIME DESC LIMIT 1

UNION ALL

SELECT 'MT4_Live2', OPEN_TIME
FROM mt4_live2.mt4_trades ORDER BY OPEN_TIME DESC LIMIT 1

UNION ALL

SELECT 'MT5', Time
FROM mt5_live.mt5_deals ORDER BY Timestamp DESC LIMIT 1;
```

---

## From risk-monitor-roadmap.md (implemented sections)

### §七 CEN / USD 账户处理 (2026-04-17 已实施)

> Moved to archive 2026-04-23. Implementation details preserved for reference.

#### 7.1 问题

MT4/MT5 上 CEN（美分）账户的 equity / balance 是美分值，但表面 `CURRENCY` 显示 `USD`。
直接展示会让 CEN 账户看起来余额很大（实际÷100 才是真实 USD）。

#### 7.2 调研发现

- fxbackoffice.mt4_users 的 `CURRENCY` 字段是唯一准确的 currency source
- MT server 上 CEN 账户报 `CURRENCY='USD'`，不可信

#### 7.3 已实施方案

独立 enrichment 查询，不碰现有扫描 SQL。

在 `backend/app/services/risk_monitor_service.py` 里：

- `_SID_MAP` → 后移到 `sql_helpers.py` 成为 `SID_MAP`
- `_get_currency_map(conn, alerts)` → 重构为 `account_enrichment.py` 的 `get_account_info_map()`
- CEN alert 的 equity/balance 除 100
- alert 对象新增 `currency` 字段

数据库：`alert_events` 表新增 `currency TEXT` 列，幂等迁移。

前端：新增"币种"列，CEN 用琥珀色高亮。

#### 7.5 约定与边界

- lots 类字段不变：CEN 和 USD 口径一致
- 未知 currency 默认 USD：避免误除 100
- sid=2（IB wallet）不参与风控

#### 7.7 历史数据回填迁移 (2026-04-17 执行)

脚本 `backend/scripts/backfill_alert_events_currency.py`：

| 项 | 数量 |
|----|------|
| 待处理行 | 9871 |
| 唯一 loginsid | 444 |
| CEN（÷100 + 打标） | 7424 |
| USD（仅打标） | 2447 |

---

### §九 MT4 vs MT5 检测数量差异调研 (2026-04-17 completed)

> MT4 Live 命中远多于 MT5 — 原因是 MT4 每笔挂单成交拆成独立 ticket，
> 而 MT5 同品种同方向合并为一个 position。
> 结论：这是 MT4/MT5 架构差异，不需要修复。

---

### §十 Zipcode enrichment + 后端模糊筛选 (2026-04-17 已实施)

> 从 fxbackoffice CRM 获取 zipcode，写入 alert_events，
> 前端新增 zipcode 列 + 搜索框（走后端 LIKE 模糊匹配）。

实现要点：
- `get_account_info_map()` 同时返回 currency 和 zipcode
- `alert_events` 新增 `zipcode TEXT` 列
- `/alerts` 和 `/alerts/stats` 新增 `zipcode` query parameter
- 后端 `WHERE zipcode LIKE '%{zipcode}%'`（非 AG-Grid 列筛选）

---

### §十一 Broker 时间 UTC 统一 (2026-04-17 已实施)

> 所有 broker-local 时间（UTC+3）在 SQL SELECT 端通过 `CONVERT_TZ()` 转为 UTC ISO8601。
> Python 和前端不做任何时区转换。

实现要点：
- `sql_helpers.py` 新增 `broker_time_to_utc_iso(col, alias)` 和 `BROKER_TZ_OFFSET`
- 旧数据一次性回填 10142 行
