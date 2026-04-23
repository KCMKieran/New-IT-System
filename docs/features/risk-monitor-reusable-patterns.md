# Risk Monitor — Reusable Patterns Reference

> Code-level templates for adding new risk detection rules.
> Companion to `.cursor/skills/risk-monitor/SKILL.md` § "Adding a New Rule".
>
> **Status**: 2026-04-21 created during burst-open pattern extraction.

---

## §1 AlertEvent Base Fields Contract

> Field definitions and Rule ID ranges: see `.cursor/skills/risk-monitor/SKILL.md` § Data Model and § Adding a New Rule.
> Pydantic source of truth: `backend/app/schemas/risk_monitor.py`

Every detection function must return dicts with all `AlertEvent` base fields.
Fields set to `None` initially will be filled by `_enrich_account_info()` after detection.
Key enriched fields: `equity`, `balance`, `equity_per_lot`, `total_open_lots`, `leverage`, `group`, `currency`, `zipcode`.

---

## §2 Broker Timezone SQL Helper

**Module**: `backend/app/core/sql_helpers.py`

```python
from ..core.sql_helpers import broker_time_to_utc_iso, FILETIME_EPOCH_OFFSET, FILETIME_TICKS_PER_SEC
```

### MT4 SELECT template

```python
open_time_col = broker_time_to_utc_iso("t.OPEN_TIME", "open_time")
sql = f"""
    SELECT
        '{server_label}' AS server,
        t.LOGIN AS login,
        t.SYMBOL AS symbol,
        CASE WHEN t.CMD = 0 THEN 'Buy' ELSE 'Sell' END AS direction,
        t.VOLUME / 100 AS lots,
        {open_time_col},
        ...
    FROM {db_name}.mt4_trades t
    INNER JOIN {db_name}.mt4_users u ON t.LOGIN = u.LOGIN
    WHERE t.OPEN_TIME >= DATE_SUB(NOW(), INTERVAL %s SECOND)
      AND t.CMD IN (0, 1)
      AND t.LOGIN NOT LIKE '7%%'
      AND u.`GROUP` NOT LIKE '%%demo%%'
      AND u.`GROUP` NOT LIKE '%%test%%'
"""
```

### MT5 SELECT template

```python
open_time_col = broker_time_to_utc_iso("d.Time", "open_time")
sql = f"""
    SELECT
        'MT5' AS server,
        d.Login AS login,
        d.Symbol AS symbol,
        CASE WHEN d.Action = 0 THEN 'Buy' ELSE 'Sell' END AS direction,
        d.Volume / 10000 AS lots,
        {open_time_col},
        d.PositionID AS position_id
    FROM mt5_live.mt5_deals d
    WHERE d.Timestamp >= (UNIX_TIMESTAMP(DATE_SUB(NOW(), INTERVAL %s SECOND))
                          + {FILETIME_EPOCH_OFFSET}) * {FILETIME_TICKS_PER_SEC}
      AND d.Entry = 0
      AND d.Action IN (0, 1)
"""
```

### For CLOSE_TIME (future rules like Quick Open-Close)

```python
close_time_col = broker_time_to_utc_iso("t.CLOSE_TIME", "close_time")
```

The WHERE clause stays in broker-local time:
```sql
WHERE t.CLOSE_TIME >= DATE_SUB(NOW(), INTERVAL %s SECOND)
  AND t.CLOSE_TIME != '1970-01-01 00:00:00'
```

---

## §3 Account Enrichment

**Module**: `backend/app/services/account_enrichment.py`

```python
from .account_enrichment import get_account_info_map, apply_cen_conversion, round_or_none
```

### Batch CRM lookup

```python
account_info_map = get_account_info_map(conn, alerts)
# Returns: {"5-67035933": {"currency": "CEN", "zipcode": "111 90"}, ...}
```

### CEN conversion

```python
from ..core.sql_helpers import SID_MAP

for alert in alerts:
    sid = SID_MAP.get(alert["server"])
    loginsid = f"{sid}-{alert['login']}" if sid else None
    info = account_info_map.get(loginsid, {}) if loginsid else {}
    currency = info.get("currency") or "USD"
    alert["currency"] = currency
    alert["zipcode"] = info.get("zipcode")

    # Divide CEN equity/balance by 100 → USD
    apply_cen_conversion(alert, currency)
```

### Key rules

- Missing loginsid defaults to `"USD"` — never divides by 100 on unknown accounts
- `zipcode` empty string normalised to `None` inside `get_account_info_map`
- Lots are NOT affected by CEN — contract sizes are identical

---

## §4 Frontend TimeRange Pattern

Source: `RiskMonitor.tsx` lines 131–253 + 460–475

### Range presets

```tsx
type RangePresetKey = "1h" | "4h" | "1d" | "7d" | "30d" | "custom";

const RANGE_PRESETS: { key: RangePresetKey; label: string; hours: number | null }[] = [
  { key: "1h", label: "最近 1 小时", hours: 1 },
  { key: "4h", label: "最近 4 小时", hours: 4 },
  { key: "1d", label: "最近 1 天", hours: 24 },
  { key: "7d", label: "最近 7 天", hours: 24 * 7 },
  { key: "30d", label: "最近 30 天", hours: 24 * 30 },
  { key: "custom", label: "自定义范围", hours: null },
];
```

### buildRangeIso — compute (since, until) from selector state

```tsx
function buildRangeIso(
  preset: RangePresetKey,
  custom: DateRange | undefined,
): { since: string; until: string } | null {
  if (preset === "custom") {
    if (!custom?.from) return null;
    const from = new Date(custom.from);
    from.setHours(0, 0, 0, 0);
    const to = custom.to ? new Date(custom.to) : new Date(custom.from);
    to.setHours(23, 59, 59, 999);
    return { since: from.toISOString(), until: to.toISOString() };
  }
  const hours = RANGE_PRESETS.find((p) => p.key === preset)?.hours ?? 4;
  const until = new Date();
  const since = new Date(until.getTime() - hours * 3600 * 1000);
  return { since: since.toISOString(), until: until.toISOString() };
}
```

### Auto-refresh pattern

Relative ranges refresh every 30s; absolute (custom) ranges don't:

```tsx
useEffect(() => {
  if (!active) return;
  const controller = new AbortController();
  fetchAlerts(controller.signal);

  if (rangePreset !== "custom") {
    const timer = setInterval(() => fetchAlerts(), 30_000);
    return () => { controller.abort(); clearInterval(timer); };
  }
  return () => controller.abort();
}, [fetchAlerts, active, rangePreset]);
```

### Calendar constraint

Align the date picker disabled range with backend retention (30 days):
```tsx
<Calendar
  disabled={{ before: new Date(Date.now() - 30 * 24 * 3600 * 1000) }}
/>
```

---

## §5 Frontend SummaryCards Pattern

Source: `RiskMonitor.tsx` lines 1107–1136

```tsx
function SummaryCard({
  label, description, value, dotColor, textColor,
}: {
  label: string;
  description?: string;
  value: number;
  dotColor: string;   // e.g. "bg-red-500"
  textColor: string;  // e.g. "text-red-600 dark:text-red-400"
}) { ... }
```

Color semantics:

| Meaning | dotColor | textColor |
|---------|----------|-----------|
| Suspicious accounts | `bg-red-500` | `text-red-600 dark:text-red-400` |
| Alert events | `bg-amber-500` | `text-amber-600 dark:text-amber-400` |
| Info / neutral | `bg-blue-500` | `text-blue-600 dark:text-blue-400` |

Usage:
```tsx
<div className="grid grid-cols-2 gap-3">
  <SummaryCard label="可疑账户" value={stats.suspicious_count}
    dotColor="bg-red-500" textColor="text-red-600 dark:text-red-400" />
  <SummaryCard label="告警事件" value={stats.event_count}
    dotColor="bg-amber-500" textColor="text-amber-600 dark:text-amber-400" />
</div>
```

---

## §6 AG-Grid Alert Table Config

See `.cursor/skills/ag-grid-style/SKILL.md` for full theme/style guide.

### Base columns (shared by all rules)

These columns appear in every rule's table. Copy as-is, then append
rule-specific columns after `symbol`.

```tsx
const baseColumns: ColDef[] = [
  { headerName: "规则",       field: "rule_label",  width: 90, pinned: "left" },
  { headerName: "被发现时间", field: "scanned_at",   width: 165, sort: "desc",
    valueFormatter: (p) => fmtTime(p.value) },
  { headerName: "服务器",     field: "server",       width: 110 },
  { headerName: "Zipcode",    field: "zipcode",      width: 120 },
  { headerName: "账户",       field: "login",        width: 110, cellRenderer: LoginCell },
  { headerName: "币种",       field: "currency",     width: 80 },
  { headerName: "品种",       field: "symbol",       width: 110 },
  // ... rule-specific columns here ...
  { headerName: "净值 (USD)",      field: "equity",          width: 130 },
  { headerName: "每手净值 (USD)",  field: "equity_per_lot",  width: 120 },
  { headerName: "总持仓手数",      field: "total_open_lots", width: 120 },
  { headerName: "杠杆",           field: "leverage",        width: 80 },
  { headerName: "账户组",         field: "group",           width: 150 },
];
```

### LoginCell — CRM link renderer

```tsx
function crmLink(login: number, server?: string) {
  let prefix = "1";
  if (server === "MT5") prefix = "5";
  else if (server === "MT4_Live2") prefix = "6";
  return `https://mt4.kohleglobal.com/crm/accounts/${prefix}-${login}`;
}

function LoginCell(params: { value: number; data?: AlertEvent }) {
  if (!params.value) return null;
  return (
    <a href={crmLink(params.value, params.data?.server)}
       target="_blank" rel="noopener noreferrer"
       className="text-blue-600 hover:underline dark:text-blue-400 font-medium"
       onClick={(e) => e.stopPropagation()}>
      {params.value}
    </a>
  );
}
```

---

## §7 CSV Export

### Frontend AG-Grid export (quick, ≤1000 rows)

```tsx
const handleExportCsv = () => {
  if (!gridApiRef.current || !effectiveRange) return;
  const stamp = `${fmtFilenameStamp(effectiveRange.since)}_to_${fmtFilenameStamp(effectiveRange.until)}`;
  gridApiRef.current.exportDataAsCsv({
    fileName: `risk-monitor-{ruleName}_${stamp}.csv`,
    allColumns: true,
    processCellCallback: (params) => {
      const colId = params.column.getColId();
      if (colId === "scanned_at") return fmtTime(params.value);
      // Add rule-specific column transformations here
      return params.value ?? "";
    },
  });
};
```

### Filename timestamp helper (HKT)

```tsx
function fmtFilenameStamp(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "unknown";
  return new Intl.DateTimeFormat("sv-SE", {
    timeZone: "Asia/Hong_Kong",
    year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", hourCycle: "h23",
  }).format(d).replace(" ", "_").replace(":", "-");
}
```

### Backend task-style export (future, for >1000 rows)

Reference implementation: `backend/app/services/client_return_export_service.py`

Pattern:
```
POST /alerts/export/tasks      → create async export task
GET  /alerts/export/tasks/{id} → poll status
GET  /alerts/export/tasks/{id}/download → download CSV
```

---

## §8 API Contract Template

### GET /alerts — time-range query

Standard parameters (all rules share the same endpoint, differentiated by `rule_id`):

| Param | Type | Default | Notes |
|-------|------|---------|-------|
| `since` | ISO8601 UTC | now - 4h | Lower bound (inclusive) |
| `until` | ISO8601 UTC | now | Upper bound (exclusive) |
| `server` | str? | — | Equality filter |
| `login` | int? | — | Equality filter |
| `symbol` | str? | — | Equality filter |
| `rule_id` | int? | — | Filter by specific rule |
| `zipcode` | str? | — | Backend LIKE `%x%` substring match |
| `limit` | int | 200 | Max 1000 |
| `offset` | int | 0 | Pagination |

Response: `{ entries: AlertEvent[], total: int, since: str, until: str }`

### GET /alerts/stats — summary aggregates

Same `since` / `until` / `zipcode` params. Returns:
`{ suspicious_count: int, event_count: int, servers: str[] }`

### Default window helper (backend)

```python
def _default_since_until(since, until) -> tuple[str, str]:
    now = datetime.now(timezone.utc).replace(microsecond=0)
    until_dt = _parse_iso_utc(until) if until else now
    since_dt = _parse_iso_utc(since) if since else until_dt - timedelta(hours=4)
    return since_dt.isoformat(), until_dt.isoformat()
```

---

## §9 Config Drawer Pattern

Source: `RiskMonitor.tsx` `ConfigDrawer` component

Key features:
- **Direction**: `direction={isMobile ? "bottom" : "right"}` — adapts to screen size
- **Width**: `w-[480px] max-w-[90vw]` on desktop
- **Dynamic rules**: `addRule()` / `removeRule()` with max limit
- **Save → reschedule**: `POST /config` → `reschedule_burst(interval_min)`

Template for new rule config:

```tsx
function {Name}ConfigDrawer({ open, onOpenChange, config, setConfig, onSave, saving }) {
  // Same drawer shell as BurstOpenTab's ConfigDrawer
  // Replace the rule fields (burst_window_sec etc.) with the new rule's parameters
}
```

---

## §10 Cross-Scan Dedup

Prevents the same pattern from being reported on consecutive scans when the
check_interval overlaps.

**Dedup key**: `(rule_id, server, login, symbol, first_open_time)`

```python
if previous_alerts:
    prev_keys = {
        (a["rule_id"], a["server"], a["login"], a["symbol"], a["first_open"])
        for a in previous_alerts
    }
    alerts = [
        a for a in alerts
        if (a["rule_id"], a["server"], a["login"], a["symbol"], a["first_open"])
        not in prev_keys
    ]
```

The `previous_alerts` list comes from `_latest_result` in `burst_open_scheduler.py`.
When adding a new rule, its alerts should be included in the same `_latest_result`
so subsequent scans can dedup against them.

---

## Maintenance

When adding a new section or updating templates:
1. Keep this file under 500 lines
2. Match section numbering (§N) with references in `.cursor/skills/risk-monitor/SKILL.md` § "Adding a New Rule"
3. Update the SKILL.md if section structure changes
