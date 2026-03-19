# 交易风控监控系统 — Trade Risk Monitor

> 可扩展的实时风控监控平台，覆盖 MT4 + MT5 全部交易服务器。

## 1. 系统概览

### 目标

每 10 分钟扫描所有交易服务器的持仓和近期成交，检测可疑交易模式，提前预警风控团队。

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
| 时间索引 | `INDEX_OPENTIME`, `INDEX_CLOSETIME` | `Timestamp` (Unix, 有索引) |
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
    rule_scale_in_detect,      # 当前: 持仓累积检测
    rule_batch_close_detect,   # 当前: 同秒批量平仓检测
    # rule_xxx,                # 未来: 新增规则只需在此注册
]
```

### 当前规则: 持仓累积 + 资金比 (Scale-In Detection)

**触发条件**: 同一账户 + 同一品种 + 同一方向，持有 ≥ 3 笔未平仓单。

**输出字段**: 账户、品种、方向、持仓笔数、总手数、浮动盈亏、余额、单手资金比、保证金比例

**告警等级** (基于单手资金比):

| 单手资金比 | 等级 | 含义 |
|-----------|------|------|
| > $5,000 | NORMAL | 资金充裕 |
| $2,000 ~ $5,000 | WATCH | 需关注 |
| $500 ~ $2,000 | HIGH | 高危 |
| < $500 | **CRITICAL** | 极高风险，接近爆仓 |

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
| `/api/v1/risk-monitor/scan` | GET | 全量扫描 (MT4+MT5 持仓+近期平仓) |

**查询参数**:
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

### Email 告警

仅对 `CRITICAL` 和 `HIGH` 等级发送邮件，通过 Redis 去重:

```python
key = f"risk_alert:{alert.server}:{alert.login}:{alert.rule}"
if not redis.exists(key):
    send_email(alert)
    redis.set(key, 1, ex=3600)  # 1 小时内不重复
```

---

## 5. 前端设计

### 页面: `/risk-monitor`

每 10 分钟自动刷新 + 手动刷新按钮。

```
┌──────────────────────────────────────────────────────────────────────┐
│  交易风控监控                          上次更新: 14:32  [🔄 刷新]     │
│                                        自动刷新: 每10分钟            │
├──────────────────────────────────────────────────────────────────────┤
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐            │
│  │ CRITICAL │  │  HIGH    │  │  WATCH   │  │  扫描耗时 │            │
│  │    1     │  │    3     │  │    5     │  │   28ms   │            │
│  └──────────┘  └──────────┘  └──────────┘  └──────────┘            │
├──────────────────────────────────────────────────────────────────────┤
│  筛选: [全部服务器 ▼] [全部规则 ▼] [CRITICAL+HIGH ▼]               │
├──────────────────────────────────────────────────────────────────────┤
│  AG-Grid 表格                                                        │
│  列: 等级 | 规则 | 服务器 | 账户 | 品种 | 方向 | 持仓数 | 总手数 |    │
│      余额 | 单手资金比 | 保证金% | 浮动盈亏 | 建仓时间               │
│                                                                      │
│  功能: 排序, 筛选, CSV 导出                                           │
│  颜色: CRITICAL=红色, HIGH=橙色, WATCH=黄色                           │
└──────────────────────────────────────────────────────────────────────┘
```

### Dashboard 组件

`SuspiciousClients.tsx` → 显示最新 CRITICAL + HIGH 告警摘要，链接到 `/risk-monitor`。

---

## 6. 开发计划

| 阶段 | 范围 | 预估 |
|------|------|------|
| **Phase 1** | 后端: MT5 持仓累积检测 + 资金比 + API | 1 天 |
| **Phase 2** | 后端: 加入 MT4 Live + MT4 Live2 数据采集 | 0.5 天 |
| **Phase 3** | 前端: 页面 (表格 + 统计卡片 + 自动刷新 + 筛选) | 1-2 天 |
| **Phase 4** | 后端: 批量平仓检测规则 | 0.5 天 |
| **Phase 5** | Dashboard 组件 + Email 告警 + Redis 去重 | 1 天 |
| **Phase 6** | 阈值调优 (跑一周, 分析误报率) | 持续 |

**总计: ~5 天 + 持续调优**

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
