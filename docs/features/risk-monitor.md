# 交易实时监控 — Trade Real-time Monitor

> 可扩展的实时风控监控平台，覆盖 MT4 + MT5 全部交易服务器。
>
> **Agent Skill**: `.cursor/skills/risk-monitor/SKILL.md` (精简版，写代码时优先读 Skill)
>
> **历史归档**: [risk-monitor-archive.md](./risk-monitor-archive.md) — 已完成的开发计划、探索性 SQL、已移除规则设计、已实施调研记录
>
> **变更记录**: 2026-03-26 移除 Scale-In 规则代码，Tab 2 改为「缺口交易」(开发中)。2026-04-23 归档已完成历史章节。

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

### 当前规则: 同秒批量平仓 (Batch-Close Detection)

**触发条件**: 同一账户在同一秒内平仓 ≥ 3 笔。

**输出字段**: 账户、品种、平仓时间、批次大小、总手数、总盈亏、胜率

**数据源**: 最近 20 分钟已平仓成交

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

## 6. 批量下单检测 (Burst Open Detection)

> **状态**: ✅ 已实施 (2026-03-28)。
>
> **变更原因**: Risk team 反馈——5-8 分钟内开 3 笔属于正常交易行为，旧"频繁开仓"规则误报率太高。
> 新规则聚焦于真正的 B-Book 风险行为：**短时间内同品种密集下大单**（典型 EA/算法行为）。

### 6.1 旧规则 vs 新规则

| | v1 频繁开仓 | v2 批量下单 |
|--|--|--|
| **检测逻辑** | N 分钟内开仓 ≥ 3 笔 | N 秒内同品种开仓 ≥ 3 笔，且每笔 ≥ 5 手 |
| **分组** | (server, login) | **(server, login, symbol)** |
| **品种** | 不区分 | **同品种** |
| **方向** | 不区分 | 不区分（买卖都计入） |
| **severity** | ALERT / WATCH 两级 | **统一"可疑用户"**，无分级 |
| **equity_per_lot** | 触发条件 | **展示字段**（供风控参考，不作为触发条件） |
| **典型误报** | 手工交易者 | 几乎无（人工不可能 3 秒 3 笔） |
| **扫描驱动** | 前端 setInterval | **后端单一定时任务** |

### 6.2 新规则参数 (4 个 Input)

用户在前端可配置多条 Rule，每条 Rule 包含 3 个参数：

| # | 参数 | 含义 | 默认值 | API 字段名 |
|---|------|------|--------|-----------|
| 1 | **时间窗口** | 几秒内的连续开仓算"批量" | 3 秒 | `burst_window_sec` |
| 2 | **最少笔数** | 时间窗口内至少几笔订单 | 3 笔 | `min_order_count` |
| 3 | **每笔最少手数** | 每笔订单的 lots 必须 ≥ | 5 手 | `min_lots_per_order` |

全局参数（非 Rule 粒度）：

| # | 参数 | 含义 | 默认值 | API 字段名 |
|---|------|------|--------|-----------|
| 4 | **扫描间隔** | 后端每隔几分钟执行一次扫描 | 10 分钟 | `scan_interval_min` |

**全局约束**:
- `scan_interval_min` 前端可调，最小 5 分钟，必须为整数
- SQL 回溯窗口 `check_interval` = `scan_interval_min` + `max(burst_window_sec)`，无缝覆盖且处理边界 (见 §6.9)
- Rules 上限: 10 条
- Rules 持久化: SQLite (重启后恢复)
- Config 更新: POST 整体替换 rules 数组 (last-write-wins)

### 6.3 检测逻辑

```
后端定时任务 (每 scan_interval_min 分钟执行)
│
├── 1. SQL 采集 (一次，覆盖最近 scan_interval_min 分钟)
│      MT4 Live:  OPEN_TIME >= NOW() - interval
│      MT4 Live2: OPEN_TIME >= NOW() - interval
│      MT5:       mt5_deals.Timestamp >= filetime(NOW() - interval), Entry=0
│
├── 2. Python: 按 (server, login, symbol) 分组
│
├── 3. 对每组，按 open_time 排序后执行滑动窗口:
│      for each Rule in rules:
│          for i in range(len(orders)):
│              j = i
│              while orders[j].time - orders[i].time <= burst_window_sec:
│                  j++
│              window = orders[i:j]
│              if len(window) >= min_order_count:
│                  if all(o.lots >= min_lots_per_order for o in window):
│                      → 命中! 记录 burst 事件
│
├── 4. 去重: 同一 (server, login, symbol) 重叠窗口取最大的
│
├── 5. 对命中账户查询 equity/balance → 计算 equity_per_lot (展示用)
│      equity_per_lot 使用该账户 **全部未平仓持仓的 total_lots**
│
├── 6. 组装结果 → 写入 latest_result (缓存供前端读取)
│
└── 7. 追加到 scan_history (历史日志)
```

**滑动窗口示例**（Rule: 3 秒 / 3 笔 / 5 手）:

```
账户 12345, XAUUSD, 最近 10 分钟开仓:

14:20:01  Buy  10手
14:20:02  Sell  8手
14:20:02  Buy   6手
14:25:30  Buy   2手   ← lots < 5, 即使在窗口内也不满足

窗口 [14:20:01 ~ 14:20:03]:
  3 笔, 每笔 ≥ 5 手 → ✅ 命中

如果其中一笔只有 3 手:
14:20:01  Buy  10手
14:20:02  Sell  3手   ← 不满足 min_lots_per_order
14:20:02  Buy   6手

窗口 [14:20:01 ~ 14:20:03]:
  3 笔, 但 Sell 3手 < 5手 → ✘ 不命中
```

### 6.4 多规则 (Multi-Rule) 支持

用户可配置多条 Rule，SQL 只执行一次，Python 对同一数据集重复执行每条 Rule：

| Rule | burst_window_sec | min_order_count | min_lots_per_order | 场景 |
|------|-----|---|---|---|
| Rule 1 | 3 | 3 | 5 | EA 瞬间大单 |
| Rule 2 | 5 | 5 | 3 | 密集中等手数 |
| Rule 3 | 1 | 3 | 10 | 极端：1秒内超大单 |

**同一账户匹配多条 Rule**: 每条 Rule 独立一行。例如账户 12345 命中 Rule 1 和 Rule 3 → 表格显示 2 行。
每条 Rule 有自动递增 ID (1, 2, 3...) 用于标识。

### 6.5 后端驱动定时扫描架构

```
┌─────────────────────────────────────────────────────────────┐
│                  后端 (FastAPI + Background Task)            │
│                                                             │
│  ┌─────────────────────────────────────────────────────┐    │
│  │  BurstOpenScheduler (单例, FastAPI startup 启动)     │    │
│  │                                                     │    │
│  │  config (内存, 可持久化到 SQLite):                    │    │
│  │  ┌──────────────────────────────────────────────┐   │    │
│  │  │ scan_interval_min: 10                        │   │    │
│  │  │ rules: [                                     │   │    │
│  │  │   {burst_window_sec:3, min_orders:3, lots:5} │   │    │
│  │  │   {burst_window_sec:5, min_orders:5, lots:3} │   │    │
│  │  │ ]                                            │   │    │
│  │  └──────────────────────────────────────────────┘   │    │
│  │                                                     │    │
│  │  循环: sleep(scan_interval) → scan() → 写缓存+日志  │    │
│  └─────────────────────────────────────────────────────┘    │
│                                                             │
│  API:                                                       │
│  GET  /burst-open              → 读 latest_result 缓存     │
│  GET  /burst-open/config       → 读当前 rules + interval   │
│  POST /burst-open/config       → 更新 config, 立即生效     │
│  POST /burst-open/scan-now     → 立即触发一次扫描 (加锁)   │
│  GET  /burst-open/alerts        → 时间范围查询 alert_events │
│  GET  /burst-open/alerts/stats  → 时间范围聚合 stats        │
└─────────────────────────────────────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────────────┐
│                  前端 (只读缓存)                              │
│                                                             │
│  每 30s 轮询 GET /burst-open → 展示最新扫描结果              │
│  参数面板: 展示/编辑 rules (读写 config API)                 │
│  "立即扫描" → POST scan-now                                 │
│  "查看历史" → GET history → 侧边抽屉/弹窗展示               │
└─────────────────────────────────────────────────────────────┘
```

**多用户冲突处理**:

| 场景 | 策略 |
|------|------|
| 多人同时修改 config | Last-write-wins, 前端拉取后显示最新值 |
| 多人同时点"立即扫描" | 后端加锁, 同一时刻只执行一次, 后续请求等待结果 |
| 后端重启 | config 从 SQLite 读取恢复 (TODO: 见 Q4) |

### 6.6 历史 Log 设计 (v2: 批次 + 事件双表)

> **2026-04-17 更新**：为支持时间范围查询视图（短期优化 P1），从"单表 JSON 数组"升级为"批次 + 事件两表"结构。`scan_history` 保留批次元数据，`alert_events` 拍平每条告警到独立行方便时间范围/账户/服务器筛选。

**存储位置**: SQLite 文件 `data/risk_monitor.db`（Python 内置 `sqlite3`，无额外依赖）。

#### 表 1: `scan_history`（每次扫描一行，批次元数据）

```sql
CREATE TABLE scan_history (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    scanned_at        TEXT    NOT NULL,   -- UTC ISO8601
    scan_interval_min INTEGER NOT NULL,
    accounts_scanned  INTEGER NOT NULL,
    suspicious_count  INTEGER NOT NULL,
    scan_time_ms      INTEGER NOT NULL,
    rules_config      TEXT    NOT NULL,   -- JSON 本次 rules 快照
    alerts            TEXT    NOT NULL    -- JSON 完整 alerts（冗余，便于审计回放）
);
```

#### 表 2: `alert_events`（每条告警一行，时间范围视图的主数据源）

```sql
CREATE TABLE alert_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_batch_id   INTEGER NOT NULL,   -- FK → scan_history.id
    scanned_at      TEXT    NOT NULL,   -- 冗余存储 UTC 时间用于快速 range 查询
    rule_id         INTEGER NOT NULL,
    rule_label      TEXT    NOT NULL,
    server          TEXT    NOT NULL,
    login           INTEGER NOT NULL,
    symbol          TEXT    NOT NULL,
    order_count     INTEGER NOT NULL,
    total_lots      REAL    NOT NULL,
    first_open      TEXT,                -- burst 首笔开仓时间 (UTC)
    last_open       TEXT,                -- burst 末笔开仓时间 (UTC)
    equity          REAL,
    balance         REAL,
    equity_per_lot  REAL,
    total_open_lots REAL,
    leverage        INTEGER,
    account_group   TEXT,
    orders_json     TEXT,                -- 订单明细 JSON
    currency        TEXT,                -- "USD" / "CEN"（2026-04-17 新增，equity/balance 已是 USD）
    zipcode         TEXT                 -- 客户 zipcode（2026-04-17 新增，来自 fxbackoffice.mt4_users）
);

CREATE INDEX idx_alert_events_scanned_at   ON alert_events(scanned_at DESC);
CREATE INDEX idx_alert_events_login_scan   ON alert_events(login, scanned_at DESC);
CREATE INDEX idx_alert_events_server_sym   ON alert_events(server, symbol, scanned_at DESC);
```

**前端展示**: 页面主视图直接查询 `alert_events`（默认最近 4 小时）。时间范围支持快捷预设（1h / 4h / 1d / 7d / 30d）+ 自定义日期范围 picker，支持服务器/账户筛选和 CSV 导出。不再需要"查看历史" Drawer。

**写入路径**: `burst_open_scheduler._run_scan()` → `append_scan_and_events()` 在单事务内写入批次行 + 所有告警事件行。

**迁移逻辑**: `init_risk_monitor_db()` 在首次升级时（`alert_events` 为空且 `scan_history` 非空）自动把旧批次的 JSON alerts 拍平回填到 `alert_events`，不丢失历史。

**时区约定**:
- 所有 `scanned_at` / `first_open` / `last_open` 后端按 UTC 存，字符串格式 `YYYY-MM-DDTHH:MM:SSZ`
- 前端展示统一转换为 `Asia/Hong_Kong`（HKT，UTC+8）
- CSV 导出时同样按 HKT 展示

**Broker → UTC 转换（2026-04-17 修复）**:
- 背景: MT4/MT5 服务器时区是 `Indian/Antananarivo`（UTC+3，无 DST），`t.OPEN_TIME` / `d.Time` 在 MySQL 里是 **UTC+3 的 naive datetime**。早期版本直接 `SELECT t.OPEN_TIME AS open_time`，写到 SQLite 时就变成 naive UTC+3 字符串，和 Python `datetime.now(UTC)` 生成的 `scanned_at` 差 3 小时。前端 `parseBackendTime` 对 naive 字符串按 UTC 解析，再叠一次 `+3h → HKT` 偏移，导致页面上 "被发现时间" 看起来比 "具体时间（开仓）" 还早。
- 方案: 在 `_query_mt4_recent_opens` / `_query_mt5_recent_opens` 的 SELECT 子句里用 `DATE_FORMAT(CONVERT_TZ(t.OPEN_TIME, '+03:00', '+00:00'), '%Y-%m-%dT%TZ')` 直接在 MySQL 端转成 UTC ISO8601。broker 时区写死 `+03:00`（不依赖 `@@session.time_zone`，broker 多年稳定，CONVERT_TZ 也更快）。`WHERE t.OPEN_TIME >= DATE_SUB(NOW(), ...)` 保持原样，因为 `t.OPEN_TIME` 和 `NOW()` 两边都是 broker local，比较仍正确。
- 回填: `backend/scripts/backfill_alert_events_open_time.py` 一次性把旧的 10142 行 `first_open` / `last_open` 以及 `orders_json` 里 73960 笔 `open_time` 统一减 3 小时并加 `Z` 后缀。脚本幂等（已带 `Z` 或带明确 offset 的值跳过），支持 dry-run，2026-04-17 已执行。

**保留策略**: **30 天**。每次扫描后同步 `DELETE FROM scan_history / alert_events WHERE scanned_at < datetime('now', '-30 days')`。

#### 币种处理（2026-04-17 上线）

MT 服务器上 CEN（美分）账户的 `equity` / `balance` 是美分单位（100 倍膨胀），CRM 侧权威来源是 `fxbackoffice.mt4_users`，按 `loginsid` (`{sid}-{login}`) 查询：

| server | sid |
|--------|-----|
| MT4_Live | 1 |
| MT4_Live2 | 6 |
| MT5 | 5 |

处理流程（`risk_monitor_service._enrich_account_info`）：

1. `_get_currency_map` 一次批量查 `fxbackoffice.mt4_users` 拿到所有 alert 的 `loginsid → CURRENCY`
2. `CURRENCY='CEN'` 的 alert：`equity /= 100`、`balance /= 100`
3. `equity_per_lot` 用调整后的 equity 重算，保持三者单位一致
4. `alert.currency` 字段写 USD / CEN 供前端"币种"列展示（未知默认 USD，绝不把 USD 账户误当 CEN 除 100）
5. `total_lots` / `total_open_lots` 不动（合约规格 CEN/USD 一致）

**一次性回填迁移**: `backend/scripts/backfill_alert_events_currency.py` — 针对 currency 列上线前已写入的旧行，幂等可重跑，默认 dry-run，`--apply` 才写 DB。2026-04-17 首次执行回填 9871 行（CEN 7424 / USD 2447），0 缺失。

#### Zipcode 筛选（2026-04-17 上线）

`fxbackoffice.mt4_users.ZIPCODE` 作为同一客户多账户的识别特征（同 zipcode + 批量下单 = 刷单农场强信号）。和 currency 共用 `_get_account_info_map`，**单次 SQL 同时捞出 CURRENCY + ZIPCODE**，无额外 DB load。

**后端 API**：

```
GET /burst-open/alerts?zipcode=<substr>&...
GET /burst-open/alerts/stats?zipcode=<substr>&...
```

- 子串模糊匹配（`WHERE zipcode LIKE '%x%' ESCAPE '\\'`，用户的 `%` `_` `\` 会被转义）
- 空字符串/纯空格视为"不筛选"
- NULL 行永不命中（CRM 未填 zipcode 的账户会被筛走，这是预期行为）

**前端**：
- 列顺序：服务器 → **Zipcode** → 账户 → 币种 → 品种 → ...
- Zipcode 列 NULL 值以灰色 `—` 显示
- Toolbar 独立输入框（300ms debounce），和时间范围、服务器下拉同级
- `stats` 接口同步带 zipcode 参数 → "可疑账户 / 告警事件"卡片与表格数据一致

**不做回填**：2026-04-17 起新数据带 zipcode；历史 9871 行 zipcode 永远 NULL，不在筛选结果中。若未来需要，可仿 `backfill_alert_events_currency.py` 写同款脚本。

### 6.7 可行性分析

| 关注点 | 分析 | 结论 |
|--------|------|------|
| **SQL 对 DB load** | 10min 窗口的开仓查询使用 OPEN_TIME / Timestamp 索引，预估返回数百行，耗时 <10ms。后端单任务每 10min 查一次，Slave 零压力 | ✅ 可行 |
| **Python 滑动窗口** | 按 (server, login, symbol) 分组后每组通常 <20 条。排序 O(N log N) + 滑动 O(N)，多条 rules 重复执行也在亚毫秒级 | ✅ 可行 |
| **后端定时任务** | `asyncio.create_task()` 在 FastAPI startup 启动后台循环。单例 + asyncio.Lock 避免并发。无需 APScheduler 等额外依赖 | ✅ 可行 |
| **多规则** | SQL 执行一次，Python 对同一数据集执行 3-5 条 rules，额外开销忽略不计 | ✅ 可行 |
| **SQLite 历史日志** | 内置模块，单文件，写频率极低。每条记录 ~1-5KB (取决于 alerts 数量)，1000 条 ≈ 1-5MB | ✅ 可行 |
| **前端改动量** | 参数面板从 3 个 input 改为多 rule 卡片式配置 + 新增"查看历史"按钮/抽屉。AG-Grid 列定义调整。改动适中 | ✅ 可行 |

### 6.8 确认答案汇总

| Q | 问题 | 确认结果 |
|---|------|---------|
| Q1 | scan_interval 前端可调？ | ✅ 可调，最小 5min，整数 |
| Q2 | check_interval = scan_interval？ | ✅ 同意，回溯=扫描 |
| Q3 | Rules 上限 | 10 条 |
| Q4 | 持久化 | SQLite |
| Q5 | Log 保留 | 30 天（2026-04-17 调整，配合时间范围视图，详见 §6.6） |
| Q6 | 多 Rule 命中展示 | 每条 Rule 一行 |
| Q7 | equity_per_lot 的 total_lots | 全部未平仓持仓 |
| Q8 | 历史 UI | Drawer (宽版) |
| Q9 | API 路径 | `/burst-open` (新 endpoint) |

### 6.9 架构审查 — 关键问题

#### 🔴 Critical: 扫描窗口边界丢失 (Boundary Problem)

**问题**: 如果 `check_interval` 严格等于 `scan_interval`，批量事件恰好跨越两次扫描的边界时会被**漏检**。

```
scan_interval = 10min

扫描 A (14:00) 回溯范围: [13:50:00, 14:00:00)
扫描 B (14:10) 回溯范围: [14:00:00, 14:10:00)

账户在 13:59:59, 14:00:00, 14:00:01 各开 1 笔 10 手 XAUUSD (3 秒内 3 笔):

→ 扫描 A 只看到 13:59:59 的 1 笔 → 不触发
→ 扫描 B 只看到 14:00:00 和 14:00:01 的 2 笔 → 不触发
→ 真实的 burst 被漏掉!
```

**解决方案**: SQL 回溯窗口加一个 buffer:

```
check_interval = scan_interval_min + max(burst_window_sec across all rules)
```

例如 scan_interval = 10min, 最大 burst_window = 5s → 回溯 10min + 5s = 605s。

多出的 5s 可能产生重复检测 → 用 burst 的 `(server, login, symbol, first_open_time)` 做去重，
如果上一次扫描已经报过同一个 burst（对比 latest_result），则不重复记录。

> 回复:

#### ⚠️ Important: Rule 参数合法性校验

前端和后端都需要校验 Rule 参数的合理范围，防止误配置：

| 参数 | 建议范围 | 理由 |
|------|---------|------|
| `burst_window_sec` | 1 ~ 30 秒 | <1 无意义，>30 接近旧规则的"频繁"而非"批量" |
| `min_order_count` | 2 ~ 50 笔 | <2 没有"密集"概念，>50 不太现实 |
| `min_lots_per_order` | 0.5 ~ 100 手 | <0.5 微型单无风险，>100 极端罕见 |
| `scan_interval_min` | 5 ~ 60 分钟 | <5 对 DB 压力增加，>60 实时性太差 |

> 回复 (范围是否合适？):

#### ⚠️ Important: 服务启动行为

后端启动时需要明确：
1. 从 SQLite 读取上次 config（如首次运行则用代码默认值并写入 SQLite）
2. **立即执行一次扫描**还是等待第一个 scan_interval 后再扫描？

建议: 启动后立即执行一次首扫，不等待。这样部署/重启后不需要等 10 分钟才看到数据。

> 回复:

#### ⚠️ Important: 旧 API `/frequent-open` 的处理

新 API 用 `/burst-open`。旧 `/frequent-open` 端点:
- A) 保留但返回 HTTP 410 Gone + 提示信息（过渡期）
- B) 直接删除

建议 B (内部工具，无外部依赖方)。

> 回复:

#### 💡 Nice-to-have: Rule 展示名称

Q6 确认每条 Rule 一行。前端表格需要标识"命中了哪条 Rule"。两种方案:
- A) 用自动 ID 显示 (Rule 1, Rule 2, Rule 3...)
- B) 允许用户给每条 Rule 起名 (如 "EA大单", "密集中单")

A 最简单，B 可读性更好。建议 v1 用 A，后续按需加 B。

> 回复:

### 6.10 原始 Q&A 存档

以下为原始问答记录，确认结果已汇总到 §6.8。

---

### ~~6.8~~ 原始待确认问题

> 请在每个问题下方填写回复。

**Q1: 扫描间隔 (scan_interval) 是否前端可调？**

默认 10 分钟。是否允许前端用户修改为 5min / 15min / 30min？还是后端写死？

> 回复: 允许前端用户修改, 最小是 5min, 要求输入是整数

**Q2: check_interval 是否始终等于 scan_interval？**

扫描间隔 10min → SQL 回溯 10min，无缝覆盖。是否同意？还是希望 check_interval 独立可调？

> 回复: 同意, 回溯时间和扫描时间一致

**Q3: Rules 上限？**

建议最多 5 条。是否合适？

> 回复: 这个rules上限的理由是什么? 如果rules都是在python检测是否没有影响? 设置limit为10条吧

**Q4: Rules 和 config 持久化方式？**

服务重启后：
- A) 回到代码中的默认值（最简单，重启丢失用户修改）
- B) 从 SQLite 读取上次配置（持久化）

推荐 B。你的偏好？

> 回复: 从sqlite读取上次配置, 我需要持久化

**Q5: 历史 Log 保留策略？**

- A) 保留最近 N 条（如 1000 条 × 每 10min = 约 7 天）
- B) 保留最近 N 天（如 30 天）
- C) 不自动清理，手动管理

推荐 A (1000 条)。你的偏好？

> 回复: 保留7天的数据 

**Q6: 同一账户匹配多条 Rule 时如何展示？**

例如账户 12345 在 XAUUSD 上同时命中 Rule 1 和 Rule 3：
- A) 合并为 1 行，标注 "命中规则: Rule 1, Rule 3"
- B) 分为 2 行（每条 Rule 一行）

推荐 A（减少重复）。你的偏好？

> 回复: 分为2行, 每条rule一行

**Q7: equity_per_lot 中的 total_lots 用哪个值？**

- A) 仅 burst 窗口内的 lots（和触发条件一致的那几笔）
- B) 该账户**所有未平仓持仓**的 total lots（反映真实杠杆利用率）

推荐 B（更有风控参考价值）。你的偏好？

> 回复: 使用B吧

**Q8: "查看历史" UI 形式？**

- A) 侧边抽屉 (Drawer) — 不离开主页面
- B) 弹窗 (Dialog/Modal)
- C) 新页面 / 子路由 `/risk-monitor/history`

推荐 A。你的偏好？

> 回复: 使用drawer, 但是考虑移动端读区, web端的话 drawer要页面大一些

**Q9: API 路径命名？**

旧: `/api/v1/risk-monitor/frequent-open`
新规则建议:
- A) `/api/v1/risk-monitor/burst-open` (新 endpoint, 旧的保留/废弃)
- B) 继续用 `/api/v1/risk-monitor/frequent-open` (原地改造)

推荐 A (干净切换)。你的偏好？

> 回复: 使用方案A
