# 交易实时监控 — Trade Real-time Monitor

> 可扩展的实时风控监控平台，覆盖 MT4 + MT5 全部交易服务器。
>
> **Agent Skill**: `.cursor/skills/risk-monitor/SKILL.md` (精简版，写代码时优先读 Skill)
>
> **变更记录**: 2026-03-26 移除 Scale-In (持仓累积) 规则的前后端代码，第二个 Tab 改为「缺口交易」(开发中)。原 Scale-In 的设计文档保留在本文件中供参考。

## 1. 系统概览

### 目标

每 10 分钟扫描所有交易服务器的持仓和近期成交，检测高杠杆 / 高资金利用率客户，预警风控团队。

### 业务背景

KCM 是 B-Book CFD 券商：客户亏损 = 公司盈利。真正的风险是客户资金利用率高（杠杆开得猛），一旦方向押对，公司面临大额亏损。本系统的目的**不是保护客户**，而是**识别对公司 B-Book P&L 构成风险的高敞口客户**。

### 覆盖范围

| 服务器 | 数据库 | 持仓数据 | 成交数据 | 账户数据 |
|--------|--------|---------|---------|---------|
| MT4 Live (SID 1) | `mt4_live` | `mt4_trades` (CLOSE_TIME='1970') | `mt4_trades` | `mt4_users` |
| MT4 Live2 (SID 6) | `mt4_live2` | `mt4_trades` (CLOSE_TIME='1970') | `mt4_trades` | `mt4_users` |
| MT5 | `mt5_live` | `mt5_positions` | `mt5_deals` | `mt5_users` |

**所有库在同一台 MySQL Slave 上**，单个 pymysql 连接即可跨库查询。

### 架构总览

```
┌──────────────────────────────────────────────────────────────┐
│                     前端 (每 10 分钟轮询)                      │
│  GET /api/v1/risk-monitor/scan                               │
└──────────────────────┬───────────────────────────────────────┘
                       │
                       ▼
┌──────────────────────────────────────────────────────────────┐
│                      后端 (FastAPI)                           │
│                                                              │
│  ┌────────────────────────────────────────────────────────┐  │
│  │               数据采集层 (Data Collector)                │  │
│  │                                                        │  │
│  │  ┌──────────┐  ┌──────────┐  ┌──────────┐             │  │
│  │  │ MT4 Live │  │MT4 Live2 │  │   MT5    │             │  │
│  │  │ Adapter  │  │ Adapter  │  │ Adapter  │             │  │
│  │  └────┬─────┘  └────┬─────┘  └────┬─────┘             │  │
│  │       └──────────────┼──────────────┘                  │  │
│  │                      ▼                                 │  │
│  │          统一持仓格式 (Normalized Position)              │  │
│  └──────────────────────┬─────────────────────────────────┘  │
│                         ▼                                    │
│  ┌────────────────────────────────────────────────────────┐  │
│  │               规则引擎 (Rule Engine)                     │  │
│  │                                                        │  │
│  │  ┌─────────────────┐  ┌────────────────┐              │  │
│  │  │ 持仓累积检测     │  │ 批量平仓检测   │  ← 当前      │  │
│  │  │ (Scale-In)      │  │ (Batch-Close)  │              │  │
│  │  └─────────────────┘  └────────────────┘              │  │
│  │  ┌─────────────────┐  ┌────────────────┐              │  │
│  │  │ 未来规则 A      │  │ 未来规则 B     │  ← 扩展      │  │
│  │  └─────────────────┘  └────────────────┘              │  │
│  └──────────────────────┬─────────────────────────────────┘  │
│                         ▼                                    │
│  ┌────────────────────────────────────────────────────────┐  │
│  │               告警输出 (Alert Output)                    │  │
│  │  Redis 去重 → API 响应 + Email 通知                     │  │
│  └────────────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────────┘
                       │
                       ▼ (单个 pymysql 连接)
┌──────────────────────────────────────────────────────────────┐
│              MySQL Slave (Azure)                              │
│  ┌────────────┐ ┌────────────┐ ┌────────────┐               │
│  │ mt4_live   │ │fxbackoffice│ │ mt5_live   │               │
│  │ mt4_trades │ │ mt4_trades │ │ mt5_deals  │               │
│  │ mt4_users  │ │ mt4_users  │ │mt5_positions│              │
│  │            │ │ users      │ │ mt5_users  │               │
│  └────────────┘ └────────────┘ └────────────┘               │
└──────────────────────────────────────────────────────────────┘
```

---

## 2. 数据采集层

### 设计原则

MT4 和 MT5 的表结构完全不同，但风控规则不应关心数据来源。采集层负责将不同服务器的数据**标准化为统一格式**，供规则引擎使用。

### MT4 vs MT5 数据差异

| 差异 | MT4 | MT5 |
|------|-----|-----|
| 交易记录 | 单表 `mt4_trades`，开平仓都在一条记录 | `mt5_deals` 开仓一条 + 平仓一条，用 `PositionID` 关联 |
| 持仓判断 | `CLOSE_TIME = '1970-01-01'` | 独立的 `mt5_positions` 表 |
| 手数换算 | `VOLUME / 100` | `Volume / 10000` |
| 方向字段 | `CMD`: 0=Buy, 1=Sell | `Action`: 0=Buy, 1=Sell |
| 隔夜利息 | `SWAPS` | `Storage` |
| 总盈亏 | `PROFIT + SWAPS + COMMISSION` | `Profit + Storage + Commission` |
| 时间索引 | `INDEX_OPENTIME`, `INDEX_CLOSETIME` | `Timestamp` (Windows FILETIME, 有索引) |
| 合约信息 | 无 (需硬编码) | `ContractSize`, `PriceCurrent` (直接可用) |
| 账户余额 | `mt4_users` 待确认 | `mt5_users.Balance` ✅ |
| 账户杠杆 | `mt4_users` 待确认 | `mt5_users.Leverage` ✅ |

### 统一持仓格式

```python
@dataclass
class NormalizedPosition:
    """规则引擎使用的统一持仓格式，屏蔽 MT4/MT5 差异"""
    server: str           # "MT4_Live" | "MT4_Live2" | "MT5"
    login: int            # 账户号
    group: str            # 账户组
    symbol: str           # 品种
    direction: str        # "Buy" | "Sell"
    lots: float           # 手数 (已换算)
    open_time: datetime   # 开仓时间
    profit: float         # 浮动盈亏 / 已实现盈亏
    balance: float | None # 账户余额 (可能无法获取)
    leverage: int | None  # 杠杆倍数
    contract_size: float | None  # 合约大小 (MT5 可用)
    current_price: float | None  # 当前价格 (MT5 可用)
```

### SQL 查询

#### MT4 Live — 未平仓持仓

```sql
SELECT
    'MT4_Live' AS server,
    t.LOGIN AS login,
    t.SYMBOL AS symbol,
    CASE WHEN t.CMD = 0 THEN 'Buy' ELSE 'Sell' END AS direction,
    t.VOLUME / 100 AS lots,
    t.OPEN_TIME AS open_time,
    t.OPEN_PRICE AS open_price,
    t.PROFIT AS profit,
    t.SWAPS AS swaps,
    t.COMMISSION AS commission,
    t.PROFIT + t.SWAPS + t.COMMISSION AS total_profit
FROM mt4_live.mt4_trades t
WHERE t.CLOSE_TIME = '1970-01-01 00:00:00'
  AND t.CMD IN (0, 1)
  AND t.LOGIN NOT LIKE '7%'
  AND NOT EXISTS (
      SELECT 1 FROM mt4_live.mt4_users u
      WHERE u.LOGIN = t.LOGIN
        AND (u.`NAME` LIKE '%test%'
             OR u.`GROUP` LIKE '%demo%'
             OR u.`GROUP` LIKE '%test%')
  )
ORDER BY t.LOGIN, t.OPEN_TIME;
```

#### MT4 Live2 — 未平仓持仓

```sql
SELECT
    'MT4_Live2' AS server,
    t.LOGIN AS login,
    t.SYMBOL AS symbol,
    CASE WHEN t.CMD = 0 THEN 'Buy' ELSE 'Sell' END AS direction,
    t.VOLUME / 100 AS lots,
    t.OPEN_TIME AS open_time,
    t.OPEN_PRICE AS open_price,
    t.PROFIT AS profit,
    t.SWAPS AS swaps,
    t.COMMISSION AS commission,
    t.PROFIT + t.SWAPS + t.COMMISSION AS total_profit
FROM mt4_live2.mt4_trades t
WHERE t.CLOSE_TIME = '1970-01-01 00:00:00'
  AND t.CMD IN (0, 1)
  AND t.LOGIN NOT LIKE '7%'
  AND NOT EXISTS (
      SELECT 1 FROM mt4_live2.mt4_users u
      WHERE u.LOGIN = t.LOGIN
        AND (u.`NAME` LIKE '%test%'
             OR u.`GROUP` LIKE '%demo%'
             OR u.`GROUP` LIKE '%test%')
  )
ORDER BY t.LOGIN, t.OPEN_TIME;
```

#### MT5 — 当前持仓 (从 positions 表)

```sql
SELECT
    'MT5' AS server,
    p.Login AS login,
    u.`Group` AS `group`,
    p.Symbol AS symbol,
    CASE WHEN p.Action = 0 THEN 'Buy' ELSE 'Sell' END AS direction,
    p.Volume / 10000 AS lots,
    p.TimeCreate AS open_time,
    p.PriceOpen AS open_price,
    p.PriceCurrent AS current_price,
    p.Profit AS profit,
    p.Storage AS swaps,
    p.ContractSize AS contract_size,
    u.Balance AS balance,
    u.Leverage AS leverage
FROM mt5_live.mt5_positions p
INNER JOIN mt5_live.mt5_users u ON p.Login = u.Login
WHERE u.`Group` NOT LIKE '%demo%'
  AND u.`Group` NOT LIKE '%test%'
ORDER BY p.Login, p.TimeCreate;
```

#### MT4 Live — 最近 20 分钟已平仓

```sql
SELECT
    'MT4_Live' AS server,
    t.TICKET AS ticket,
    t.LOGIN AS login,
    t.SYMBOL AS symbol,
    CASE WHEN t.CMD = 0 THEN 'Buy' ELSE 'Sell' END AS direction,
    t.VOLUME / 100 AS lots,
    t.OPEN_TIME AS open_time,
    t.CLOSE_TIME AS close_time,
    TIMESTAMPDIFF(SECOND, t.OPEN_TIME, t.CLOSE_TIME) AS hold_seconds,
    t.PROFIT + t.SWAPS + t.COMMISSION AS total_profit
FROM mt4_live.mt4_trades t
WHERE t.CLOSE_TIME >= DATE_SUB(NOW(), INTERVAL 20 MINUTE)
  AND t.CLOSE_TIME != '1970-01-01 00:00:00'
  AND t.CMD IN (0, 1)
  AND t.LOGIN NOT LIKE '7%'
ORDER BY t.LOGIN, t.CLOSE_TIME;
```

#### MT5 — 最近 20 分钟已平仓

```sql
SELECT
    'MT5' AS server,
    d.Deal AS ticket,
    d.Login AS login,
    d.Symbol AS symbol,
    CASE WHEN d.Action = 0 THEN 'Buy' ELSE 'Sell' END AS direction,
    d.Volume / 10000 AS lots,
    d.Time AS close_time,
    d.Profit + d.Commission + d.Storage AS total_profit,
    d.PositionID
FROM mt5_live.mt5_deals d
WHERE d.Timestamp >= UNIX_TIMESTAMP(DATE_SUB(NOW(), INTERVAL 20 MINUTE))
  AND d.Action IN (0, 1)
  AND d.Entry IN (1, 3)
ORDER BY d.Login, d.Time;
```

### 性能预估 (单次轮询)

| 查询 | 预估行数 | 预估耗时 |
|------|---------|---------|
| MT4 Live 未平仓 | ~3,400 | 4ms |
| MT4 Live2 未平仓 | ~2,000 | 3ms |
| MT5 持仓 | ~数千 | <10ms |
| MT4 Live 20min平仓 | ~数百 | <5ms |
| MT5 20min平仓 | ~数百 | <5ms |
| **合计** | | **<30ms** |

每 10 分钟 30ms 查询负载，对 Slave DB 无压力。

---

## 3. 规则引擎

### 设计原则

- 每条规则是一个**独立函数**，接收标准化数据，返回告警列表
- 规则可独立启用/禁用，互不影响
- 新增规则只需添加函数 + 注册到引擎，不改现有代码

### 规则注册表

```python
RULES: list[RuleFunc] = [
    rule_frequent_open_detect, # ✅ 已实现: 频繁开仓检测
    # rule_scale_in_detect,    # ❌ 已移除 (2026-03): 持仓累积检测
    # rule_gap_trading_detect, # 🚧 开发中: 缺口交易检测
    rule_batch_close_detect,   # 设计中: 同秒批量平仓检测
    # rule_xxx,                # 未来: 新增规则只需在此注册
]
```

### 当前规则: 频繁开仓检测 (Frequent Opening Detection)

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

查询示例:
```sql
WHERE d.Timestamp >= (UNIX_TIMESTAMP(DATE_SUB(NOW(), INTERVAL 8 MINUTE)) + 11644473600) * 10000000
```

**API**:

```
GET /api/v1/risk-monitor/frequent-open
  ?check_interval=8
  ?min_order_count=3
  ?equity_per_lot_threshold=2000
  ?server=mt5              (optional)
  ?login=12345             (optional)
```

**响应**:
```json
{
  "alerts": [{ "rule": "FREQUENT_OPEN", "server": "MT4_Live", "login": 12345, "severity": "ALERT",
    "details": { "order_count": 5, "total_lots": 5.0, "symbols": "XAUUSD,EURUSD",
                 "equity": 10000.0, "balance": 12000.0, "equity_per_lot": 2000.0,
                 "leverage": 500, "group": "real\\kcm_std",
                 "first_open": "2026-03-20 11:20:00", "last_open": "2026-03-20 11:25:00",
                 "position_status": "部分平仓", "floating_pnl": -320.50 } }],
  "summary": { "alert_count": 2, "watch_count": 5, "total_accounts_scanned": 150 },
  "params": { "check_interval": 8, "min_order_count": 3, "equity_per_lot_threshold": 2000 },
  "scan_time_ms": 35,
  "scanned_at": "2026-03-20T07:00:00Z"
}
```

### ~~当前规则~~ 已移除 (2026-03): 持仓累积 + 资金比 (Scale-In Detection)

> **注意**: Scale-In 规则的前后端代码已于 2026-03-26 删除。以下设计文档保留供参考。

**触发条件**: 同一账户 + 同一品种 + 同一方向，持有 ≥ 3 笔未平仓单。

**输出字段**: 账户、品种、方向、持仓笔数、总手数、浮动盈亏、余额、单手资金比、保证金比例

**告警等级** (基于单手资金比，从公司风险角度):

| 单手资金比 | 等级 | 含义 (公司视角) |
|-----------|------|------|
| < $500 | **CRITICAL** | 杠杆极高，客户方向一对公司亏损巨大 |
| $500 ~ $2,000 | HIGH | 高杠杆操作，需关注 |
| $2,000 ~ $5,000 | WATCH | 中等杠杆，留意 |
| > $5,000 | _(不显示)_ | 杠杆不高，风险可控 |

> **注意**: NORMAL 等级不在前端显示，减少噪音。等级标签用英文 (CRITICAL/HIGH/WATCH)。

**案例参考 (MT5 Account 67035072)**:

```
余额: $1,489 | 杠杆: 1:1000 | 持仓: 10笔×1手 XAUUSD
名义价值: $5M | 保证金: $5,000 | 保证金比例: ~154%
单手资金比: $769 → 等级: HIGH
特征: 分批手动建仓, 越跌越买, EA一键全平
一天内亏损 80% (昨日余额 $7,192 → 今日 $1,489)
```

### 当前规则: 同秒批量平仓 (Batch-Close Detection)

**触发条件**: 同一账户在同一秒内平仓 ≥ 3 笔。

**输出字段**: 账户、品种、平仓时间、批次大小、总手数、总盈亏、胜率

**数据源**: 最近 20 分钟已平仓成交

### Python 检测引擎

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

## 4. 后端实现

### 文件结构

```
backend/app/
├── services/
│   └── risk_monitor_service.py    # 数据采集 + 规则引擎 + 告警输出
├── schemas/
│   └── risk_monitor.py            # Pydantic 请求/响应模型
└── api/v1/routes/
    └── risk_monitor.py            # API 接口
```

### 数据库连接

使用现有的 `DB_HOST` / `DB_USER` / `DB_PASSWORD` 配置 (Slave DB)，通过跨库查询访问所有数据:

```python
conn = pymysql.connect(
    host=settings.DB_HOST,
    user=settings.DB_USER,
    password=settings.DB_PASSWORD,
    port=int(settings.DB_PORT),
    charset=settings.DB_CHARSET,
    cursorclass=pymysql.cursors.DictCursor,
)
# 跨库查询: mt4_live.mt4_trades, mt5_live.mt5_positions, ...
```

不需要新增任何 `.env` 配置项。

### API

| 接口 | 方法 | 说明 |
|------|------|------|
| `/api/v1/risk-monitor/frequent-open` | GET | 频繁开仓检测 |
| ~~`/api/v1/risk-monitor/scan`~~ | ~~GET~~ | ❌ 已移除 (2026-03) |

**频繁开仓查询参数**:
- `check_interval` (默认 8): 检查窗口（分钟）
- `min_order_count` (默认 3): 最少开仓笔数
- `equity_per_lot_threshold` (默认 2000): 每手净值阈值（USD）
- `login` (可选): 过滤特定账户
- `server` (可选): 过滤特定服务器 (`mt4_live`, `mt4_live2`, `mt5`)

**响应格式**:

```json
{
  "alerts": [
    {
      "rule": "SCALE_IN",
      "server": "MT5",
      "login": 67035072,
      "severity": "HIGH",
      "details": {
        "symbol": "XAUUSD",
        "direction": "Buy",
        "open_count": 10,
        "total_lots": 10.0,
        "capital_per_lot": 769.0,
        "balance": 7692.0,
        "floating_pnl": 1636.0
      }
    }
  ],
  "summary": {
    "critical": 1,
    "high": 3,
    "watch": 5,
    "total_accounts_scanned": 2400
  },
  "scan_time_ms": 28,
  "scanned_at": "2026-03-18T07:00:00"
}
```

### Email 告警 (未来阶段)

> **首期不实现**，后续按需加入。

设计方案: 仅对 `CRITICAL` 和 `HIGH` 等级发送邮件，通过 Redis 去重:

```python
key = f"risk_alert:{alert.server}:{alert.login}:{alert.rule}"
if not redis.exists(key):
    send_email(alert)
    redis.set(key, 1, ex=3600)  # 1 小时内不重复
```

复用 `email_service.py` 的 `send_email()` 函数，参考 `.cursor/skills/email-notification/SKILL.md`。

---

## 5. 前端设计

### 页面: `/risk-monitor`

- 侧边栏分组: Risk Control，页面标题: 交易实时监控
- 纯中文 UI，不需要 i18n，等级标签用英文
- **Tab 切换**: 频繁开仓 (默认) / 缺口交易 (开发中，当前为占位)
- 每个 Tab 独立的 API、刷新间隔、筛选器、表格列
- A-Book 客户不做过滤，在 UI 中显示 GROUP 列即可

#### Tab 1: 频繁开仓 (默认)

```
┌──────────────────────────────────────────────────────────────────────┐
│  [频繁开仓] [持仓累积]                                                │
├──────────────────────────────────────────────────────────────────────┤
│  检测最近 N 分钟内频繁开仓的账户             [🔄 立即扫描]             │
│  上次扫描: 14:32 · 耗时 35ms · 每 8 分钟自动刷新                     │
├──────────────────────────────────────────────────────────────────────┤
│  ┌─ 参数设置 ────────────────────────────────────────────────────┐  │
│  │  检查窗口: [8min ▼]  最少开仓: [3] 笔  每手净值 < [2000] USD │  │
│  └───────────────────────────────────────────────────────────────┘  │
├──────────────────────────────────────────────────────────────────────┤
│  ┌──────────────────┐  ┌──────────────────┐  ┌──────────────────────────┐│
│  │ 🔴 ALERT          │  │ 🟡 WATCH          │  │ 时间窗口内开仓账户数(N分钟)││
│  │    2              │  │    5              │  │      150                 ││
│  │ 每手净值<2,000 USD │  │ 每手净值≥2,000 USD │  │                          ││
│  └──────────────────┘  └──────────────────┘  └──────────────────────────┘│
├──────────────────────────────────────────────────────────────────────┤
│  筛选: [全部服务器 ▼]  [搜索账户号...]                                │
├──────────────────────────────────────────────────────────────────────┤
│  AG-Grid 表格                                                        │
│  列: 等级 | 服务器 | 账户 | 开仓笔数 | 总手数 | 品种 |               │
│      净值(Equity) | 每手净值比 | 持仓状态 | 浮动盈亏 |               │
│      杠杆 | 账户组 | 首笔时间 | 末笔时间                              │
│  持仓状态: 未平仓(绿)/已平仓(灰)/部分平仓(琥珀)                      │
│  浮动盈亏: 仍持仓订单的PnL, 红=亏损 绿=盈利                          │
│  行颜色: ALERT=浅红, WATCH=浅黄                                      │
│  自动刷新: 等于 check_interval 参数值                                 │
└──────────────────────────────────────────────────────────────────────┘
```

#### Tab 2: 缺口交易 (开发中)

> **原 Tab 2 "持仓累积" 已于 2026-03-26 移除**，替换为「缺口交易」检测。
> 当前为占位 UI，显示"开发中 — 检测休市前后的开平仓行为"。
>
> 缺口交易检测目标: 识别客户在休市前（如周五收盘前）开仓压注，利用周一开盘缺口获利的行为。

---

## 6. 开发计划

> **Agent 须知**: 每个 Phase 完成后必须执行「验证」步骤，全部通过才能进入下一阶段。
> 精简版清单见 `.cursor/skills/risk-monitor/SKILL.md` → Implementation Status。

### Phase 1: 后端骨架 + MT5 数据验证

**目标**: 最小可用后端，先通 MT5 一个数据源，验证整条链路。

| 步骤 | 做什么 | 细节 |
|------|--------|------|
| 1a | `schemas/risk_monitor.py` | 定义 Pydantic 模型：AlertDetail、Alert、ScanSummary、ScanResponse，字段严格按 Skill 中的 API Contract |
| 1b | `services/risk_monitor_service.py` | `_query_mt5_positions()`: MT5 SQL → 标准化 dict 列表。`rule_scale_in_detect()`: 分组+计算+分级。`scan()`: 主入口串联采集→规则→响应组装 |
| 1c | `routes/risk_monitor.py` | GET `/scan` 路由，接收 login/server 可选参数 |
| 1d | `routers.py` | 注册 risk_monitor router，prefix `/risk-monitor` |

**验证**:
- [ ] dev 服务器启动无报错
- [ ] `GET /api/v1/risk-monitor/scan` 返回 200，响应结构符合 ScanResponse schema
- [ ] MT5 持仓数据非空（预期几千行），手数换算正确（Volume/10000）
- [ ] ≥3 笔同方向持仓的账户产生了告警，severity 分级合理
- [ ] `?login=xxx` 参数能正确过滤单个账户
- [ ] `scan_time_ms` < 200ms

---

### Phase 2: 加入 MT4 双服务器

**目标**: 数据采集层覆盖全部三个服务器。

| 步骤 | 做什么 | 细节 |
|------|--------|------|
| 2a | `_query_mt4_positions()` | MT4 Live SQL: CLOSE_TIME='1970' + JOIN mt4_users 拿 BALANCE/LEVERAGE/GROUP，返回标准化格式 |
| 2b | 复制改库名 | MT4 Live2 查询与 Live 完全一致，库名 `mt4_live` → `mt4_live2` |
| 2c | `scan()` 合并 | 三个查询结果 concat 后传给规则引擎 |
| 2d | server 参数 | `?server=mt4_live` 只查对应数据源 |

**验证**:
- [ ] 不传 server 参数时返回三个服务器的告警
- [ ] `?server=mt4_live` / `?server=mt4_live2` / `?server=mt5` 各自只返回对应数据
- [ ] MT4 手数换算正确（VOLUME/100，注意和 MT5 /10000 的区别）
- [ ] MT4 LOGIN LIKE '7%' 的内部账户已排除
- [ ] demo/test 组账户已排除
- [ ] summary.total_accounts_scanned 包含三个服务器的合计
- [ ] 整体 scan_time_ms 仍 < 200ms

---

### Phase 3: 前端页面 — 基础框架

**目标**: 页面能跑起来，看到数据。

| 步骤 | 做什么 | 细节 |
|------|--------|------|
| 3a | 路由/侧边栏注册 | `App.tsx` 加 lazy route。`app-sidebar.tsx` Risk Control 分组下加"交易实时监控"。`site-header.tsx` 加标题映射 |
| 3b | `RiskMonitor.tsx` 骨架 | useEffect + AbortController 调 /scan API |
| 3c | 统计卡片 | 顶部 4 张 Card: CRITICAL/HIGH/WATCH 数量 + 扫描账户数 |
| 3d | AG-Grid 表格 | 14 列 columnDefs，客户端模式，rowData 直接传入 |

**验证**:
- [ ] 侧边栏 Risk Control 分组下出现"交易实时监控"，点击可导航
- [ ] 页面标题显示"交易实时监控"
- [ ] 页面加载后自动发起 /scan 请求，表格展示数据
- [ ] 4 张统计卡片数字与表格行数匹配
- [ ] 所有 14 列都有数据（balance、leverage 等可能为 null 的字段能优雅处理）
- [ ] 浏览器控制台无报错

---

### Phase 4: 前端页面 — 交互完善

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

**验证**:
- [ ] CRITICAL 行浅红、HIGH 行浅橙、WATCH 行浅黄
- [ ] 浮动盈亏正值显示红色，负值显示绿色
- [ ] 等级下拉默认只显示 CRITICAL + HIGH，切换到"全部"后显示 WATCH
- [ ] 服务器下拉能正确筛选
- [ ] 账户号搜索能精确匹配
- [ ] 等待 10 分钟或手动点刷新，数据自动更新
- [ ] 刷新过程中按钮显示 loading，表格不闪烁
- [ ] 默认排序 CRITICAL 在最上面

---

### Phase 5: 部署 + 观察调优

**目标**: 上线，观察告警质量，调整参数。

| 步骤 | 做什么 | 细节 |
|------|--------|------|
| 5a | 部署到 prod | `deploy.sh` 部署，确认 prod 能正常查询 MySQL Slave |
| 5b | 观察 1-2 天 | 记录：告警总数、CRITICAL 数、HIGH 数。判断是否合理 |
| 5c | 调整阈值 | CRITICAL 太多 → $500 阈值下调；告警太少 → ≥3 门槛放松 |
| 5d | 确认业务价值 | 和风控团队确认展示的客户是否确实需要关注 |

**验证**:
- [ ] 生产环境页面可正常访问
- [ ] 10 分钟自动刷新在生产环境正常工作
- [ ] 告警数量在合理范围（预期 CRITICAL 0-5，HIGH 5-20，WATCH 10-50）
- [ ] 风控团队反馈：展示的客户确实有风险价值

---

### 待实施: UI 重设计 (已讨论，未实施)

基于首期 demo 反馈，以下改动已确认但尚未实施:

1. **移除 severity 分级体系** — CRITICAL/HIGH/WATCH 标签太抽象，用户需要看原始数据自行判断
2. **卡片改为服务器维度** — 3 张卡片 (MT4_Live / MT4_Live2 / MT5)，每张显示: 持仓账户数、总手数、总浮动盈亏。可选: 品种集中度卡片 (XAUUSD / EURUSD 等 top 品种)
3. **后端 summary 扩展** — scan 响应新增 `by_server` 和 `top_symbols` 数组
4. **移除等级筛选器** — 保留服务器下拉 + 账户号搜索
5. **移除等级列和行颜色** — 表格按 capital_per_lot 升序排列，用户看数值判断
6. **加下次扫描倒计时** — 显示距离下次自动刷新的时间
7. **可选: 自定义扫描间隔** — 下拉 (5min/10min/30min)，纯前端改动
8. **多规则 Tab 设计** — 未来每条规则一个 Tab，Tab 上显示命中数量，列定义随规则变化，筛选器部分共享 (服务器/账户)
9. **Demo banner** — 当前页面顶部有蓝色 banner 说明规则含义，UI 重设计后移除

### 待实施: 后端驱动定时扫描 (重要架构升级)

**问题**: 当前扫描由前端 `setInterval` 驱动，每个浏览器 Tab 各自独立轮询。

| 现状 | 问题 |
|------|------|
| 每个用户打开页面 → 自己的 `setInterval` | 20 个用户 = 20 个独立定时器，同时发 20 个 SQL 查询 |
| 3 个参数存在前端 React state | 用户 A 设置 5min、用户 B 设置 30min，各跑各的，互不影响 |
| 用户关闭页面 / 切换 Tab → `clearInterval` 清理 | **所有用户离线 = 零扫描**，无法发现异常 |
| 用户点"立即扫描" → 仅该用户的请求 | 结果不共享，其他用户看不到 |

**目标**: 后端单一定时任务驱动，前端只读缓存结果。无论用户是否在线，扫描持续运行。

**架构方案**:

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
│  ┌──────────────────────────┐  ┌────────────────────────┐   │
│  │ GET /frequent-open       │  │ POST /frequent-open/   │   │
│  │ → 读缓存，直接返回       │  │      config            │   │
│  │   (不再实时查询 DB)       │  │ → 更新参数 + 立即扫描  │   │
│  └──────────────────────────┘  └────────────────────────┘   │
│                                                             │
│  ┌──────────────────────────┐                               │
│  │ POST /frequent-open/     │                               │
│  │      scan-now             │                               │
│  │ → 立即触发一次扫描        │                               │
│  │   结果更新缓存            │                               │
│  └──────────────────────────┘                               │
└─────────────────────────────────────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────────────┐
│                  前端 (轮询读缓存)                            │
│                                                             │
│  setInterval: 每 30s 拉一次 GET /frequent-open               │
│  → 读到的是后端最近一次扫描的缓存结果                         │
│  → 参数面板读 config API，修改走 POST config                 │
│  → "立即扫描" → POST scan-now → 等返回后刷新                 │
│                                                             │
│  页面关闭 → 前端停止轮询，但后端扫描不受影响                   │
└─────────────────────────────────────────────────────────────┘
```

**新增 API**:

| 接口 | 方法 | 说明 |
|------|------|------|
| `GET /frequent-open` | GET | 改为读缓存返回（不再实时查 DB）|
| `GET /frequent-open/config` | GET | 获取当前参数 |
| `POST /frequent-open/config` | POST | 更新参数（所有用户生效）|
| `POST /frequent-open/scan-now` | POST | 立即触发一次扫描 |

**多用户冲突处理**:

| 场景 | 策略 |
|------|------|
| 多人同时修改参数 | Last-write-wins（最后一个提交的生效），前端刷新后看到最新值 |
| 多人同时点"立即扫描" | 后端加锁，同一时刻只执行一次扫描，后续请求等待结果 |
| 用户 A 修改后用户 B 不知道 | 前端每次打开页面或定时拉取 config，显示最新参数 |

**参数持久化选项**:

| 方案 | 优点 | 缺点 |
|------|------|------|
| 内存变量 | 最简单，无依赖 | 重启丢失，回到默认值 |
| Redis | 持久化 + 快速 | 需要 Redis (当前项目未使用) |
| JSON 文件 | 简单持久化 | 并发写入需加锁 |
| 数据库表 | 最规范 | 需建表 |

**推荐**: v1 用**内存变量** (重启回默认值可接受)，后续需要邮件通知时再引入 Redis。

**实施步骤**:

| 步骤 | 做什么 |
|------|--------|
| 1 | 后端: 新增 `RiskMonitorScheduler` 类，管理定时任务和缓存 |
| 2 | 后端: FastAPI `startup` 事件启动后台任务 |
| 3 | 后端: 新增 config / scan-now API |
| 4 | 前端: `setInterval` 改为 30s 轮询缓存结果 |
| 5 | 前端: 参数面板改为读/写 config API |
| 6 | 前端: "立即扫描" 改为 POST scan-now |

**与 Email 告警的关系**: 后端驱动扫描是 Email 通知的前置条件——只有后端主动扫描才能在用户离线时发现异常并发送邮件。

### 后续阶段（按需启动）

| 阶段 | 内容 | 前置条件 |
|------|------|---------|
| **缺口交易规则** | 检测休市前开仓、开市后平仓利用缺口获利 | Tab 占位已创建 |
| **后端驱动定时扫描** | 上述架构升级 | 首期 demo 观察完成，确认规则有效 |
| 批量平仓规则 | 同秒 ≥3 笔平仓检测，20min 已平仓数据源 | Tab 框架就绪 |
| 爆发开仓规则 | 1-5s 内 3+ 笔 ≥5 手 | Tab 框架就绪 |
| Email 告警 | Redis 去重 + send_email()，收件人: kieran.xiang@kohleservices.com | **后端驱动扫描完成** |
| Dashboard 组件 | SuspiciousClients.tsx 卡片 | 页面稳定 |

**首期总计: Phase 1-4 约 1 天开发 + Phase 5 持续 1-2 天观察**

---

## 7. 规则扩展路线 (TODO)

以下规则已设计但暂不实现，后续按需添加:

- [ ] **爆发下单**: 5s 内 3+ 单, 每单 ≥ 5 手
- [ ] **快速开平**: 大手数 + 持仓 ≤ 2 分钟
- [ ] **频繁推仓**: 1s 内同方向 3+ 单
- [ ] **大额敞口**: 单客户单品种未平仓总手数 > 阈值
- [ ] **快速盈利**: 20min 内总利润 > $5,000
- [ ] **同向集中度**: 全客户单品种净头寸 > 阈值 (公司级风险)
- [ ] **高胜率异常**: 胜率 > 80% + 平均持仓 < 5 分钟
- [ ] **跨账户关联**: 同 IP / 同 IB 树的账户同时同方向建仓
- [ ] **新闻窗口**: 重大经济数据发布前后的大额建仓

新增规则只需:
1. 编写检测函数 (接收标准化数据, 返回 Alert 列表)
2. 注册到 `RULES` 列表
3. 无需修改数据采集层或前端

---

## 8. 已确认事项

- [x] `mt4_live.mt4_users` 有 `BALANCE`, `EQUITY`, `MARGIN`, `MARGIN_LEVEL`, `MARGIN_FREE`, `LEVERAGE`, `CURRENCY` 字段 ✅
- [x] MT4 Live2 有独立的 `mt4_live2.mt4_trades` 表，结构与 `mt4_live.mt4_trades` 完全一致 (同样的索引) ✅
- [x] MT4 账户的 Group 排除逻辑与 MT5 一致 (`GROUP NOT LIKE '%demo%' AND GROUP NOT LIKE '%test%'`) ✅

---

## 9. 探索性 SQL

### 9.1 三个 Server 未平仓订单明细

#### MT4 Live

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

#### MT4 Live2

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

#### MT5

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

#### 三个 Server 未平仓订单数量汇总

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

### 9.2 按账户汇总未平仓 (手数 + 盈亏)

#### MT4 Live — 按账户汇总

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

#### MT4 Live2 — 按账户汇总

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

#### MT5 — 按账户汇总

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

### 9.3 未平仓中同秒开仓 ≥ N 笔 (粗筛)

> **不要用 Self-JOIN 做滑动窗口** — 对数千行未平仓数据做 Self-JOIN 复杂度 O(N²)，会超时。
> 用 GROUP BY 按秒聚合做粗筛 (秒级返回)，> 1 秒的精确窗口检测交给 Python。

#### MT4 Live

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

#### MT4 Live2

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

#### MT5

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

### 9.4 Volume 与 Lots 换算关系验证

MT4 和 MT5 的 Volume 编码方式不同:

| 平台 | Volume 含义 | 换算公式 | 示例 |
|------|------------|---------|------|
| MT4 | lots × 100 | `VOLUME / 100` | Volume=100 → 1.00 手 |
| MT5 | lots × 10000 | `Volume / 10000` | Volume=10000 → 1.00 手 |

原因: MT5 支持更高精度的手数 (最小 0.0001 手)，MT4 最小精度 0.01 手。

```sql
-- MT4 Live: 查看原始 Volume 和换算结果
SELECT TICKET, LOGIN, SYMBOL, VOLUME,
       VOLUME / 100 AS lots_div100,
       VOLUME / 10000 AS lots_div10000
FROM mt4_live.mt4_trades
WHERE CLOSE_TIME = '1970-01-01 00:00:00'
  AND CMD IN (0, 1)
ORDER BY OPEN_TIME DESC
LIMIT 10;

-- MT4 Live2: 同样验证
SELECT TICKET, LOGIN, SYMBOL, VOLUME,
       VOLUME / 100 AS lots_div100,
       VOLUME / 10000 AS lots_div10000
FROM mt4_live2.mt4_trades
WHERE CLOSE_TIME = '1970-01-01 00:00:00'
  AND CMD IN (0, 1)
ORDER BY OPEN_TIME DESC
LIMIT 10;

-- MT5: 查看原始 Volume 和换算结果
SELECT Position, Login, Symbol, Volume,
       Volume / 100 AS lots_div100,
       Volume / 10000 AS lots_div10000
FROM mt5_live.mt5_positions
ORDER BY TimeCreate DESC
LIMIT 10;
```

### 9.5 MT5 账户交易分析

#### 查看账户已平仓记录

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

#### 查看完整交易生命周期 (开仓→平仓配对)

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

### 9.6 账户资金状况

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

### 9.7 数据延迟检查

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
