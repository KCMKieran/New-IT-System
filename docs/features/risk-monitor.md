# Trade Risk Monitor — 交易风控监控

> Feature spec for detecting suspicious trading patterns and alerting the risk team.

## 1. Business Requirements

### Monitoring Rules

| Rule | Description | Detection Logic | Time Window |
|------|-------------|-----------------|-------------|
| **R1** | 单一客户5秒内开3+张、每张≥5手 | GROUP BY loginSid → sliding 5s window, `lots >= 5`, `COUNT >= 3` | 5s |
| **R2** | R1 + 开平仓时间≤2分钟 | R1 hit + `CLOSE_TIME - OPEN_TIME <= 120s` | 5s open + 2min hold |
| **R3** | 1秒内同方向开3+张（频繁推仓） | GROUP BY loginSid + CMD → sliding 1s window, `COUNT >= 3` | 1s |

**Common filter**: Only flag clients with `PROFIT > 0` (profitable trades).

### Two Monitoring Layers

| Layer | Data Source | Scope |
|-------|-------------|-------|
| **Historical** (past 24h) | Closed + open trades where `openDate >= yesterday` | R1, R2, R3 all applicable |
| **Real-time** (open positions) | Open trades where `CLOSE_TIME = '1970-01-01'` | R1, R3 only (R2 needs close time) |

### Update Frequency

~10 minutes (frontend polling). No SSE/WebSocket needed.

---

## 2. Data Source

### Table: `fxbackoffice.mt4_trades` (~55M rows)

Full schema in `database-context/mysql-schemas.md`.

**Key columns for this feature:**

| Column | Type | Indexed | Notes |
|--------|------|---------|-------|
| `loginSid` | varchar | ✅ `loginSid`, `(loginSid, closeDate)` | Composite `{SID}-{LOGIN}` |
| `OPEN_TIME` | datetime | ❌ (no direct index) | Use `openDate` for range scans |
| `openDate` | date (generated) | ✅ `IDX_OPEN_DATE` | `CAST(OPEN_TIME AS DATE)` |
| `CLOSE_TIME` | datetime | ❌ | Open positions = `'1970-01-01 00:00:00'` |
| `closeDate` | date (generated) | ✅ `INDEX_CLOSEDATE` | `CAST(CLOSE_TIME AS DATE)` |
| `CMD` | int | ❌ | 0=Buy, 1=Sell |
| `lots` | decimal (virtual) | ❌ | `VOLUME / 100` |
| `PROFIT` | double | ❌ | Raw P&L |
| `totalProfit` | decimal (virtual) | ❌ | `PROFIT + SWAPS + COMMISSION` |

### Performance Strategy

**Do NOT do sliding window detection in SQL** (self-join on 55M rows is too slow).

Instead:
1. **SQL**: Pull candidate trades using indexed columns (`openDate`) → small dataset (thousands of rows)
2. **Python**: In-memory sliding window detection → microsecond-level processing

### SQL Query (single query, uses index)

```sql
-- Historical: past 24h trades, uses IDX_OPEN_DATE index
SELECT TICKET, loginSid, sid, LOGIN, SYMBOL, CMD,
       lots, OPEN_TIME, CLOSE_TIME, PROFIT, totalProfit
FROM fxbackoffice.mt4_trades
WHERE openDate >= DATE_SUB(CURDATE(), INTERVAL 1 DAY)
  AND CMD IN (0, 1)
ORDER BY loginSid, OPEN_TIME

-- Real-time: open positions only, uses INDEX_CLOSEDATE index
SELECT TICKET, loginSid, sid, LOGIN, SYMBOL, CMD,
       lots, OPEN_TIME, CLOSE_TIME, PROFIT, totalProfit
FROM fxbackoffice.mt4_trades
WHERE closeDate = '1970-01-01'
  AND CMD IN (0, 1)
ORDER BY loginSid, OPEN_TIME
```

**Avoid `OR` on date columns** — it breaks index usage. Use `UNION ALL` if combining both.

---

## 3. Backend Architecture

### Data Flow

```
Frontend polls every 10 min
        │
        ▼
   GET /api/v1/risk-monitor/scan?hours=24
        │
        ▼
   SQL: Pull trades from past N hours (indexed openDate scan)
        │
        ▼
   Python in-memory detection:
   ├── R1: group by loginSid → sliding 5s window → lots>=5, count>=3
   ├── R2: R1 hits + (CLOSE_TIME - OPEN_TIME) <= 120s
   └── R3: group by loginSid+CMD → sliding 1s window → count>=3
        │
        ▼
   Filter: only keep PROFIT > 0
        │
        ▼
   Return alert list (+ optional: email on new alerts)
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
| `/api/v1/risk-monitor/scan` | GET | Scan past N hours, return alert list |
| `/api/v1/risk-monitor/realtime` | GET | Scan open positions only |

**Query params**: `hours` (default 24), `login` (optional, filter specific account).

Both endpoints share the same Python detection logic, differing only in SQL WHERE clause.

### Python Detection Logic

```python
from itertools import groupby
from datetime import timedelta

def detect_all_rules(trades: list[dict]) -> list[dict]:
    """
    In-memory sliding window detection.
    trades: pre-sorted by (loginSid, OPEN_TIME), filtered to CMD IN (0,1).
    """
    alerts = []

    trades.sort(key=lambda t: (t["loginSid"], t["OPEN_TIME"]))

    for login, group in groupby(trades, key=lambda t: t["loginSid"]):
        orders = list(group)

        # --- R1: 5s window, lots >= 5, count >= 3, profit > 0 ---
        big_orders = [o for o in orders if o["lots"] >= 5]
        for i, anchor in enumerate(big_orders):
            window_end = anchor["OPEN_TIME"] + timedelta(seconds=5)
            window = [o for o in big_orders[i:] if o["OPEN_TIME"] <= window_end]
            if len(window) >= 3:
                profitable = [o for o in window if o["PROFIT"] and o["PROFIT"] > 0]
                if profitable:
                    alerts.append({
                        "rule": "R1", "loginSid": login,
                        "window_start": anchor["OPEN_TIME"],
                        "orders": window,
                    })

                    # --- R2: R1 + hold time <= 2 min ---
                    quick_close = [
                        o for o in window
                        if o["CLOSE_TIME"] and o["CLOSE_TIME"].year > 1970
                        and (o["CLOSE_TIME"] - o["OPEN_TIME"]).total_seconds() <= 120
                        and o["PROFIT"] > 0
                    ]
                    if len(quick_close) >= 3:
                        alerts.append({
                            "rule": "R2", "loginSid": login,
                            "window_start": anchor["OPEN_TIME"],
                            "orders": quick_close,
                        })

        # --- R3: 1s window, same direction, count >= 3, profit > 0 ---
        for cmd in [0, 1]:
            same_dir = [o for o in orders if o["CMD"] == cmd]
            for i, anchor in enumerate(same_dir):
                window_end = anchor["OPEN_TIME"] + timedelta(seconds=1)
                window = [o for o in same_dir[i:] if o["OPEN_TIME"] <= window_end]
                if len(window) >= 3:
                    profitable = [o for o in window if o["PROFIT"] and o["PROFIT"] > 0]
                    if profitable:
                        alerts.append({
                            "rule": "R3", "loginSid": login,
                            "direction": "Buy" if cmd == 0 else "Sell",
                            "window_start": anchor["OPEN_TIME"],
                            "orders": window,
                        })

    return deduplicate(alerts)
```

### Email Alerting

Deduplication via Redis to avoid repeated notifications:

```python
key = f"risk_alert:{alert['loginSid']}:{alert['rule']}:{alert['window_start']}"
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
┌─────────────────────────────────────────────────────────┐
│  交易风控监控              上次更新: 14:32  [🔄 刷新]     │
│                            自动刷新: 每10分钟            │
├─────────────────────────────────────────────────────────┤
│  ┌────────┐  ┌────────┐  ┌────────┐                    │
│  │ R1: 12 │  │ R2: 3  │  │ R3: 8  │   Stat cards       │
│  └────────┘  └────────┘  └────────┘                    │
├─────────────────────────────────────────────────────────┤
│  [实时(未平仓)]  [历史(24h)]          Tab switch          │
├─────────────────────────────────────────────────────────┤
│  AG-Grid table                                          │
│  Columns: Rule | Account | Direction | Window Time |    │
│           Order Count | Symbols | Lots Detail |         │
│           Hold Time | Profit                            │
│                                                         │
│  Supports: sort, filter, CSV export                     │
└─────────────────────────────────────────────────────────┘
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
| **Phase 1** | Backend detection engine (`risk_monitor_service.py`) + API | 1 day |
| **Phase 2** | Frontend page (table + stat cards + auto-refresh) | 1-2 days |
| **Phase 3** | Dashboard widget integration (`SuspiciousClients.tsx`) | 0.5 day |
| **Phase 4** | Email alerting + Redis dedup | 0.5 day |

**Total: ~4 days**

---

## 6. Exploratory SQL (for manual analysis)

### Trades with lots > 5 (past week)

```sql
SELECT TICKET, loginSid, sid, SYMBOL, CMD, lots,
       OPEN_TIME, CLOSE_TIME, PROFIT, totalProfit
FROM fxbackoffice.mt4_trades
WHERE openDate >= DATE_SUB(CURDATE(), INTERVAL 7 DAY)
  AND CMD IN (0, 1)
  AND lots > 5
ORDER BY OPEN_TIME DESC
LIMIT 5000;
```

### Same-second order clusters (past week)

```sql
SELECT loginSid, sid,
       DATE_FORMAT(OPEN_TIME, '%Y-%m-%d %H:%i:%s') AS open_second,
       COUNT(*) AS orders_in_1sec,
       GROUP_CONCAT(TICKET ORDER BY OPEN_TIME) AS tickets,
       GROUP_CONCAT(SYMBOL ORDER BY OPEN_TIME) AS symbols,
       GROUP_CONCAT(lots ORDER BY OPEN_TIME) AS lots_list,
       GROUP_CONCAT(CMD ORDER BY OPEN_TIME) AS cmds
FROM fxbackoffice.mt4_trades
WHERE openDate >= DATE_SUB(CURDATE(), INTERVAL 7 DAY)
  AND CMD IN (0, 1)
GROUP BY loginSid, sid, DATE_FORMAT(OPEN_TIME, '%Y-%m-%d %H:%i:%s')
HAVING orders_in_1sec >= 3
ORDER BY open_second DESC
LIMIT 500;
```

### Check specific account (e.g. 67034699)

```sql
SELECT loginSid, sid, SYMBOL, CMD, lots,
       OPEN_TIME, CLOSE_TIME, PROFIT
FROM fxbackoffice.mt4_trades
WHERE loginSid LIKE '%67034699%'
  AND openDate >= DATE_SUB(CURDATE(), INTERVAL 7 DAY)
  AND CMD IN (0, 1)
ORDER BY OPEN_TIME DESC;
```
