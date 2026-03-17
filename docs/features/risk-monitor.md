# Trade Risk Monitor — 交易风控监控

> Feature spec for detecting suspicious trading patterns and alerting the risk team.

## 1. Business Requirements

### Rule System (7 Rules, 3 Tiers)

#### Tier 1 — Must Have (directly impacts company P&L)

| Rule | Name | Detection Logic | Time Window |
|------|------|-----------------|-------------|
| **R4** | 大额敞口预警 | Single client open lots on one symbol > threshold | Snapshot |
| **R5** | 快速盈利提取 | Single client realized profit > $X within 20min | 20min |
| **R2** | 快速开平 | Burst orders (5s, 3+, lots≥5) + hold ≤ 2min | 5s open + 2min hold |

#### Tier 2 — Should Have

| Rule | Name | Detection Logic | Time Window |
|------|------|-----------------|-------------|
| **R1** | 爆发下单 | Single client opens 3+ orders within 5s, each ≥ 5 lots | 5s |
| **R6** | 同向集中度 | All clients net long/short lots on a symbol > threshold (company-level) | Snapshot |
| **R3** | 频繁推仓 | 3+ same-direction orders within 1s, lots ≥ 1 | 1s |

#### Tier 3 — Nice to Have

| Rule | Name | Detection Logic | Time Window |
|------|------|-----------------|-------------|
| **R7** | 高胜率异常 | Win rate > 80% over last N trades with short hold times | Historical |

### Rule Detail

**R4 — Large Exposure Alert** (Tier 1, Highest Priority)

A single client holding excessive lots on one symbol creates unhedged risk for the broker. This is the **most dangerous scenario** — a client quietly holding 200 lots XAUUSD (~$20M notional) won't trigger any pattern-based rule.

Default thresholds (adjustable):

| Symbol | Threshold (lots) | Approx. Notional |
|--------|------------------|------------------|
| XAUUSD | 50 | ~$5M |
| XAGUSD | 200 | ~$2M |
| Default | 100 | Varies |

**R5 — Rapid Profit Extraction** (Tier 1)

Detects clients extracting significant profit within a short window, regardless of order pattern. Catches latency arbitrage and news trading that other rules may miss.

- Window: 20 minutes (matches polling interval × 2)
- Threshold: $5,000 total realized profit (adjustable)
- Data source: recently closed trades

**R2 — Quick Open-Close** (Tier 1, existing, unchanged)

R1 pattern + hold time ≤ 2 minutes. The most precise indicator of latency arbitrage / scalping abuse.

**R1 — Burst Orders** (Tier 2, existing, unchanged)

3+ orders within 5 seconds, each ≥ 5 lots. Detects EA/bot activity and news trading load-up patterns.

**R6 — Directional Concentration** (Tier 2, new)

Company-level risk: all clients combined net position on a symbol. If net exposure exceeds threshold, the broker's unhedged risk is too high. This is not per-client — it's a market-level alert.

- Threshold: 500 net lots (adjustable per symbol)
- Output: symbol, net direction (LONG/SHORT), total lots

**R3 — Rapid Same-Direction Orders** (Tier 2, improved)

Original rule had no lot filter, causing high false positives. Added `lots >= 1` minimum to filter out micro-lot noise.

**R7 — High Win Rate Anomaly** (Tier 3)

Clients with > 80% win rate over N trades AND average hold time < 5 minutes. Indicates systematic exploitation. Requires historical data — implement in a later phase.

### PROFIT Filter: Behavior vs Outcome

> **Design decision**: `PROFIT > 0` is used as a **severity sorting criterion**, NOT a filter.
>
> Risk monitoring should detect **behavioral patterns** regardless of outcome. A client who repeatedly uses latency arbitrage but occasionally loses is still a risk. Waiting until they profit to flag them is too late.
>
> Exception: R5 (Rapid Profit) inherently requires `PROFIT > 0` since it measures profit extraction.

### Monitoring Architecture: Pseudo-Real-Time

| Aspect | Design |
|--------|--------|
| **Update frequency** | Every 10 minutes (frontend polling) |
| **Data window** | Open positions + last 20 min closed trades |
| **No 24h historical scan** | Only scans recent data per poll; historical review via separate on-demand endpoint |
| **Transport** | REST polling, no SSE/WebSocket needed |

---

## 2. Data Source

### Primary: `mt4_live.mt4_trades` (Slave DB, ~18M rows)

Direct MT4 server database with near-real-time data. Preferred over `fxbackoffice.mt4_trades` for risk monitoring due to lower latency (no CRM sync delay).

**Why `mt4_live` over `fxbackoffice`:**

| Aspect | `fxbackoffice.mt4_trades` | `mt4_live.mt4_trades` |
|--------|--------------------------|----------------------|
| **Data freshness** | CRM sync delay (minutes) | Near real-time |
| **Row count** | ~55M (all SIDs merged) | ~18M (MT4 Live only) |
| **Account ID** | `loginSid` (VARCHAR, "SID-LOGIN") | `LOGIN` (INT) |
| **Lots** | `lots` (virtual column) | `VOLUME / 100` (manual calc) |
| **Total profit** | `totalProfit` (virtual column) | `PROFIT + SWAPS + COMMISSION` |
| **Date indexes** | `openDate`, `closeDate` (generated) | `OPEN_TIME`, `CLOSE_TIME` (native) |
| **Server coverage** | All SIDs (1, 5, 6) | SID 1 (MT4 Live) only |

**Limitation**: `mt4_live` only contains SID 1 data. MT4 Live2 (SID 6) and MT5 (SID 5) require separate database connections if needed.

### Table Schema: `mt4_live.mt4_trades`

| Column | Type | Indexed | Notes |
|--------|------|---------|-------|
| `TICKET` | int | ✅ PK | Trade ticket number |
| `LOGIN` | int | ✅ `INDEX_LOGIN` | MT4 login number |
| `SYMBOL` | char(16) | ❌ | Trading symbol |
| `DIGITS` | int | ❌ | Price decimal digits |
| `CMD` | int | ✅ `INDEX_CMD` | 0=Buy, 1=Sell |
| `VOLUME` | int | ❌ | Raw volume (÷100 = lots) |
| `OPEN_TIME` | datetime | ✅ `INDEX_OPENTIME` | Trade open time |
| `OPEN_PRICE` | double | ❌ | Entry price |
| `SL` | double | ❌ | Stop loss |
| `TP` | double | ❌ | Take profit |
| `CLOSE_TIME` | datetime | ✅ `INDEX_CLOSETIME` | Close time; `'1970-01-01'` = open |
| `COMMISSION` | double | ❌ | Commission charged |
| `SWAPS` | double | ❌ | Swap charges |
| `CLOSE_PRICE` | double | ❌ | Exit price |
| `PROFIT` | double | ❌ | Raw P&L |
| `COMMENT` | char(32) | ❌ | Trade comment |
| `TIMESTAMP` | int | ✅ `INDEX_STAMP` | Unix timestamp |
| `MAGIC` | int | ❌ | EA magic number |

### Performance: Verified on Slave DB

| Query | Index Used | Rows | Time |
|-------|-----------|------|------|
| Open positions (`CLOSE_TIME = '1970-01-01'`) | `INDEX_CLOSETIME` | ~3,400 | 4ms |
| Last 24h trades (`OPEN_TIME >= -24h`) | `INDEX_OPENTIME` | ~30,000 | 36ms |
| Last 20min trades (estimated) | `INDEX_OPENTIME` | ~400 | <5ms |

**Total polling load: ~10ms per 10 min cycle** — negligible impact on slave DB.

### Performance Strategy

**Do NOT do sliding window detection in SQL** (self-join on millions of rows is too slow).

Instead:
1. **SQL**: Pull candidate trades using indexed columns → small dataset (thousands of rows)
2. **Python**: In-memory sliding window detection → microsecond-level processing

### SQL Queries (use `UNION ALL`, not `OR`, to preserve index usage)

```sql
-- Query 1: Open positions (R1, R3, R4, R6)
-- Uses INDEX_CLOSETIME, ~3400 rows, 4ms
SELECT TICKET, LOGIN, SYMBOL, CMD,
       VOLUME / 100 AS lots,
       OPEN_TIME, CLOSE_TIME,
       PROFIT, SWAPS, COMMISSION,
       PROFIT + SWAPS + COMMISSION AS total_profit
FROM mt4_live.mt4_trades
WHERE CLOSE_TIME = '1970-01-01 00:00:00'
  AND CMD IN (0, 1)
ORDER BY LOGIN, OPEN_TIME;

-- Query 2: Recently closed trades (R2, R5)
-- Uses INDEX_CLOSETIME, ~400 rows, <5ms
SELECT TICKET, LOGIN, SYMBOL, CMD,
       VOLUME / 100 AS lots,
       OPEN_TIME, CLOSE_TIME,
       TIMESTAMPDIFF(SECOND, OPEN_TIME, CLOSE_TIME) AS hold_seconds,
       PROFIT, SWAPS, COMMISSION,
       PROFIT + SWAPS + COMMISSION AS total_profit
FROM mt4_live.mt4_trades
WHERE CLOSE_TIME >= DATE_SUB(NOW(), INTERVAL 20 MINUTE)
  AND CLOSE_TIME != '1970-01-01 00:00:00'
  AND CMD IN (0, 1)
ORDER BY LOGIN, CLOSE_TIME;
```

### Account Exclusion

```sql
-- Exclude test/demo accounts (applied via NOT EXISTS subquery)
AND LOGIN NOT LIKE '7%'
AND NOT EXISTS (
    SELECT 1 FROM mt4_live.mt4_users u
    WHERE u.LOGIN = t.LOGIN
      AND (u.`NAME` LIKE '%test%'
           OR u.`GROUP` LIKE '%demo%'
           OR u.`GROUP` LIKE '%test%')
)
```

---

## 3. Backend Architecture

### Data Flow

```
Every 10 minutes (frontend polling)
    │
    ├─ Query 1: Open positions (~3400 rows, 4ms)
    │   → R4: Large exposure detection
    │   → R6: Directional concentration detection
    │   → R1: Burst order pattern detection
    │   → R3: Rapid same-direction detection
    │
    ├─ Query 2: Last 20min closed trades (~400 rows, <5ms)
    │   → R2: Quick open-close detection
    │   → R5: Rapid profit extraction detection
    │
    ▼
Python in-memory detection engine
    │
    ├─ Deduplicate: Compare with Redis (previous scan results)
    │   Only keep new alerts
    │
    ├─ Sort by severity: PROFIT as ranking criterion (not filter)
    │
    └─ Output: API response → Frontend display / Email notification
```

### File Structure

```
backend/app/
├── services/risk_monitor_service.py   # SQL query + Python detection engine
├── schemas/risk_monitor.py            # Pydantic request/response models
└── api/v1/routes/risk_monitor.py      # API endpoints
```

### API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/v1/risk-monitor/scan` | GET | Pseudo-real-time scan (open + recent 20min) |

**Query params**: `login` (optional, filter specific account).

### Python Detection Logic

```python
from itertools import groupby
from datetime import timedelta
from collections import defaultdict


def detect_all(open_trades: list[dict], closed_trades: list[dict]) -> list[dict]:
    """
    In-memory detection engine. Runs all rules against two datasets:
    - open_trades: current open positions (from Query 1)
    - closed_trades: recently closed trades (from Query 2)
    """
    alerts = []

    # === R4: Large Exposure (per client per symbol) ===
    exposure = defaultdict(lambda: defaultdict(float))
    for t in open_trades:
        exposure[t["LOGIN"]][t["SYMBOL"]] += t["lots"]

    EXPOSURE_THRESHOLDS = {"XAUUSD": 50, "XAGUSD": 200, "DEFAULT": 100}
    for login, symbols in exposure.items():
        for symbol, total_lots in symbols.items():
            threshold = EXPOSURE_THRESHOLDS.get(
                symbol, EXPOSURE_THRESHOLDS["DEFAULT"]
            )
            if total_lots >= threshold:
                alerts.append({
                    "rule": "R4", "LOGIN": login, "symbol": symbol,
                    "total_lots": total_lots, "threshold": threshold,
                    "severity": "HIGH",
                })

    # === R5: Rapid Profit (per client, 20min window) ===
    profit_by_login = defaultdict(float)
    for t in closed_trades:
        if t["total_profit"] > 0:
            profit_by_login[t["LOGIN"]] += t["total_profit"]

    PROFIT_THRESHOLD = 5000  # USD
    for login, total in profit_by_login.items():
        if total >= PROFIT_THRESHOLD:
            alerts.append({
                "rule": "R5", "LOGIN": login,
                "profit_20min": total, "severity": "HIGH",
            })

    # === R1 / R2: Burst Orders + Quick Close ===
    open_trades.sort(key=lambda t: (t["LOGIN"], t["OPEN_TIME"]))

    # R1 from open positions
    for login, group in groupby(open_trades, key=lambda t: t["LOGIN"]):
        orders = list(group)
        big = [o for o in orders if o["lots"] >= 5]
        for i, anchor in enumerate(big):
            end = anchor["OPEN_TIME"] + timedelta(seconds=5)
            window = [o for o in big[i:] if o["OPEN_TIME"] <= end]
            if len(window) >= 3:
                alerts.append({
                    "rule": "R1", "LOGIN": login,
                    "window_start": anchor["OPEN_TIME"],
                    "orders": window,
                })

    # R2 from closed trades (needs CLOSE_TIME)
    closed_trades.sort(key=lambda t: (t["LOGIN"], t["OPEN_TIME"]))
    for login, group in groupby(closed_trades, key=lambda t: t["LOGIN"]):
        orders = list(group)
        big = [o for o in orders if o["lots"] >= 5]
        for i, anchor in enumerate(big):
            end = anchor["OPEN_TIME"] + timedelta(seconds=5)
            window = [o for o in big[i:] if o["OPEN_TIME"] <= end]
            if len(window) >= 3:
                quick = [
                    o for o in window
                    if o["hold_seconds"] and o["hold_seconds"] <= 120
                ]
                if len(quick) >= 3:
                    alerts.append({
                        "rule": "R2", "LOGIN": login,
                        "window_start": anchor["OPEN_TIME"],
                        "orders": quick,
                    })

    # === R3: Rapid Same-Direction (lots >= 1 filter) ===
    for login, group in groupby(open_trades, key=lambda t: t["LOGIN"]):
        orders = list(group)
        for cmd in [0, 1]:
            same = [o for o in orders if o["CMD"] == cmd and o["lots"] >= 1]
            for i, anchor in enumerate(same):
                end = anchor["OPEN_TIME"] + timedelta(seconds=1)
                window = [o for o in same[i:] if o["OPEN_TIME"] <= end]
                if len(window) >= 3:
                    alerts.append({
                        "rule": "R3", "LOGIN": login,
                        "direction": "Buy" if cmd == 0 else "Sell",
                        "window_start": anchor["OPEN_TIME"],
                        "orders": window,
                    })

    # === R6: Market-Level Directional Concentration ===
    net_by_symbol = defaultdict(float)
    for t in open_trades:
        sign = 1 if t["CMD"] == 0 else -1
        net_by_symbol[t["SYMBOL"]] += sign * t["lots"]

    CONCENTRATION_THRESHOLD = 500  # net lots
    for symbol, net in net_by_symbol.items():
        if abs(net) >= CONCENTRATION_THRESHOLD:
            alerts.append({
                "rule": "R6", "symbol": symbol,
                "net_lots": net,
                "direction": "LONG" if net > 0 else "SHORT",
                "severity": "MEDIUM",
            })

    return deduplicate(alerts)
```

### Email Alerting

Deduplication via Redis to avoid repeated notifications:

```python
key = f"risk_alert:{alert['LOGIN']}:{alert['rule']}:{alert['window_start']}"
if not redis.exists(key):
    send_email(alert)
    redis.set(key, 1, ex=3600)  # no repeat within 1 hour
```

Email via Python `smtplib`. Config in `.env`:

```env
SMTP_HOST=smtp.example.com
SMTP_PORT=587
SMTP_USER=alert@example.com
SMTP_PASSWORD=xxx
RISK_TEAM_EMAIL=risk@example.com
```

---

## 4. Frontend Design

### Page: `/risk-monitor`

Auto-refresh every 10 minutes + manual refresh button.

```
┌─────────────────────────────────────────────────────────────────────────┐
│  交易风控监控                       上次更新: 14:32  [🔄 刷新]           │
│                                     自动刷新: 每10分钟                  │
├─────────────────────────────────────────────────────────────────────────┤
│  ┌────────┐  ┌────────┐  ┌────────┐  ┌────────┐  ┌────────┐           │
│  │ R4: 2  │  │ R5: 1  │  │ R2: 3  │  │ R1: 12 │  │ R6: 1  │  Cards   │
│  │ 大额   │  │ 快利   │  │ 快平   │  │ 爆发   │  │ 集中   │           │
│  └────────┘  └────────┘  └────────┘  └────────┘  └────────┘           │
├─────────────────────────────────────────────────────────────────────────┤
│  AG-Grid table                                                         │
│  Columns: Severity | Rule | Account | Symbol | Direction |             │
│           Lots (Detail) | Window Time | Hold Time | Profit             │
│                                                                        │
│  Supports: sort, filter, CSV export                                    │
│  Color coding: HIGH = red row bg, MEDIUM = orange                      │
└─────────────────────────────────────────────────────────────────────────┘
```

### Auto-Refresh Pattern

```tsx
useEffect(() => {
  const controller = new AbortController();
  fetchData(controller.signal);

  const interval = setInterval(() => {
    fetchData(controller.signal);
  }, 10 * 60 * 1000); // 10 minutes

  return () => {
    controller.abort();
    clearInterval(interval);
  };
}, []);
```

### Dashboard Widget

Existing `SuspiciousClients.tsx` (currently placeholder) → show latest N alerts summary with link to `/risk-monitor`.

---

## 5. Development Plan

| Phase | Scope | Estimate |
|-------|-------|----------|
| **Phase 1** | Backend detection engine (`risk_monitor_service.py`) + API — implement R4, R5, R2, R1 | 1-2 days |
| **Phase 2** | Frontend page (table + stat cards + auto-refresh) | 1-2 days |
| **Phase 3** | Add R3, R6 detection + dashboard widget (`SuspiciousClients.tsx`) | 1 day |
| **Phase 4** | Email alerting + Redis dedup | 0.5 day |
| **Phase 5** | Threshold tuning (run 1 week, analyze false positive rate, adjust) | Ongoing |

**Total: ~5 days + 1 week tuning**

---

## 6. Risk Rule Design Rationale

### Why these rules? (CFD Broker Risk Perspective)

**The biggest risks for a CFD broker:**

1. **Unhedged large exposure** — A single client holding 200 lots XAUUSD means the broker is exposed to ~$20M of market risk if not hedged. This is why **R4 is the highest priority** rule.

2. **Latency arbitrage** — Clients exploit price feed delays (especially during news events like NFP, FOMC) to guarantee profits. They open large positions during price gaps and close quickly. **R2 + R5** catch this pattern from two angles: R2 via order pattern, R5 via profit outcome.

3. **EA/Bot abuse** — Automated strategies that exploit market microstructure. **R1 + R3** detect the mechanical ordering patterns that EAs produce.

4. **Concentration risk** — If all clients are long XAUUSD and gold drops, the broker's total exposure could be catastrophic. **R6** monitors this at the market level, not per-client.

### Why not filter on PROFIT > 0?

| Approach | Pros | Cons |
|----------|------|------|
| **Filter** (PROFIT > 0 required) | Fewer alerts, less noise | Misses pattern before it becomes profitable; too late to act |
| **Sort** (PROFIT as severity rank) | Catches behavior early; profitable trades ranked higher | More alerts initially; needs threshold tuning |

**Decision**: Use PROFIT as a **sorting/severity signal**, not a filter. Exception: R5 inherently requires profit > 0.

### Symbol Risk Weights (for future enhancement)

| Symbol | Approx. Contract Value per Lot | Volatility |
|--------|-------------------------------|------------|
| XAUUSD | ~$100,000 | High |
| XAGUSD | ~$10,000 | Very High |
| EURUSD | ~$100,000 | Low |
| GBPUSD | ~$100,000 | Medium |
| US30 | ~$10 × index | Medium |

Future: weight lots by contract value for normalized exposure comparison.

---

## 7. Exploratory SQL (for manual analysis on mt4_live)

### Verify data freshness

```sql
-- Check latest data timestamp (uses INDEX_OPENTIME reverse scan, <5ms)
SELECT OPEN_TIME, CLOSE_TIME, TICKET, LOGIN, SYMBOL
FROM mt4_live.mt4_trades
ORDER BY OPEN_TIME DESC
LIMIT 1;
```

### Open positions count

```sql
-- Uses INDEX_CLOSETIME, ~4ms
SELECT COUNT(*) AS open_positions
FROM mt4_live.mt4_trades
WHERE CLOSE_TIME = '1970-01-01 00:00:00';
```

### R1 candidates: large-lot clusters

```sql
SELECT
    t.LOGIN,
    DATE_FORMAT(t.OPEN_TIME, '%Y-%m-%d %H:%i:%s') AS open_second,
    COUNT(*) AS big_orders_in_window,
    GROUP_CONCAT(t.TICKET ORDER BY t.OPEN_TIME) AS tickets,
    GROUP_CONCAT(t.SYMBOL ORDER BY t.OPEN_TIME) AS symbols,
    GROUP_CONCAT(t.VOLUME / 100 ORDER BY t.OPEN_TIME) AS lots_list,
    SUM(t.PROFIT) AS total_profit
FROM mt4_live.mt4_trades t
WHERE t.OPEN_TIME >= DATE_SUB(NOW(), INTERVAL 24 HOUR)
  AND t.CMD IN (0, 1)
  AND t.VOLUME / 100 >= 5
  AND t.LOGIN NOT LIKE '7%'
GROUP BY t.LOGIN, DATE_FORMAT(t.OPEN_TIME, '%Y-%m-%d %H:%i:%s')
HAVING big_orders_in_window >= 2
ORDER BY open_second DESC
LIMIT 200;
```

### R3 candidates: same-second same-direction clusters

```sql
SELECT
    t.LOGIN, t.CMD,
    CASE WHEN t.CMD = 0 THEN 'Buy' ELSE 'Sell' END AS direction,
    DATE_FORMAT(t.OPEN_TIME, '%Y-%m-%d %H:%i:%s') AS open_second,
    COUNT(*) AS orders_in_1sec,
    GROUP_CONCAT(t.TICKET ORDER BY t.OPEN_TIME) AS tickets,
    GROUP_CONCAT(t.SYMBOL ORDER BY t.OPEN_TIME) AS symbols,
    GROUP_CONCAT(t.VOLUME / 100 ORDER BY t.OPEN_TIME) AS lots_list,
    SUM(t.PROFIT) AS total_profit
FROM mt4_live.mt4_trades t
WHERE t.OPEN_TIME >= DATE_SUB(NOW(), INTERVAL 24 HOUR)
  AND t.CMD IN (0, 1)
  AND t.LOGIN NOT LIKE '7%'
GROUP BY t.LOGIN, t.CMD, DATE_FORMAT(t.OPEN_TIME, '%Y-%m-%d %H:%i:%s')
HAVING orders_in_1sec >= 3
ORDER BY open_second DESC
LIMIT 200;
```

### R2 candidates: quick close with profit

```sql
SELECT
    t.TICKET, t.LOGIN, t.SYMBOL, t.CMD,
    t.VOLUME / 100 AS lots,
    t.OPEN_TIME, t.CLOSE_TIME,
    TIMESTAMPDIFF(SECOND, t.OPEN_TIME, t.CLOSE_TIME) AS hold_seconds,
    t.PROFIT,
    t.PROFIT + t.SWAPS + t.COMMISSION AS total_profit
FROM mt4_live.mt4_trades t
WHERE t.OPEN_TIME >= DATE_SUB(NOW(), INTERVAL 24 HOUR)
  AND t.CMD IN (0, 1)
  AND t.CLOSE_TIME != '1970-01-01 00:00:00'
  AND TIMESTAMPDIFF(SECOND, t.OPEN_TIME, t.CLOSE_TIME) <= 120
  AND t.PROFIT > 0
  AND t.VOLUME / 100 >= 5
  AND t.LOGIN NOT LIKE '7%'
ORDER BY t.LOGIN, t.OPEN_TIME
LIMIT 500;
```

### Check specific account

```sql
SELECT
    t.TICKET, t.LOGIN, t.SYMBOL,
    CASE WHEN t.CMD = 0 THEN 'Buy' ELSE 'Sell' END AS direction,
    t.VOLUME / 100 AS lots,
    t.OPEN_TIME, t.CLOSE_TIME,
    CASE
        WHEN t.CLOSE_TIME = '1970-01-01 00:00:00' THEN 'OPEN'
        ELSE CONCAT(TIMESTAMPDIFF(SECOND, t.OPEN_TIME, t.CLOSE_TIME), 's')
    END AS hold_time,
    t.PROFIT
FROM mt4_live.mt4_trades t
WHERE t.LOGIN = 67034699
  AND t.OPEN_TIME >= DATE_SUB(NOW(), INTERVAL 7 DAY)
  AND t.CMD IN (0, 1)
ORDER BY t.OPEN_TIME DESC;
```

### Compare data freshness: mt4_live vs fxbackoffice

```sql
SELECT
    'mt4_live' AS source,
    MAX(OPEN_TIME) AS latest_open_time,
    COUNT(*) AS open_position_count
FROM mt4_live.mt4_trades
WHERE CLOSE_TIME = '1970-01-01 00:00:00'
  AND CMD IN (0, 1)

UNION ALL

SELECT
    'fxbackoffice' AS source,
    MAX(OPEN_TIME) AS latest_open_time,
    COUNT(*) AS open_position_count
FROM fxbackoffice.mt4_trades
WHERE closeDate = '1970-01-01'
  AND CMD IN (0, 1)
  AND sid = 1;
```
