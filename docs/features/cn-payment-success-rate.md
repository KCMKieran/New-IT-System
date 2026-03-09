# CN渠道支付成功率 (CN Payment Channel Success Rate)

> Dashboard left-column widget — monitors CN payment channel deposit success rate in real time.

## 1. Overview

Displays deposit order statistics grouped by PSP (Payment Service Provider) channel for CN region (`psps.cid = 0`). Users can switch between 3h / 6h / 24h time windows.

### Route & Entry Points

| Entry Point | Behavior |
|---|---|
| Dashboard `/` left column | Always visible, auto-loads on mount |

---

## 2. UI Layout

```
┌───────────────────────────────┐
│ 🏷 CN渠道支付成功率    [刷新] │
│    入金订单统计                │
├───────────────────────────────┤
│ [ 3h ] [ 6h ] [ 24h ]        │  ← ToggleGroup (pill style)
│ 🕐 数据获取时间: ...          │
│                               │
│ ┌ 总计 93 笔          72.3% ┐ │  ← Overall summary bar
│ └───────────────────────────┘ │
│                               │
│ ┌─ CNY - P2Pay ─── $5,400 ─┐ │  ← Channel card
│ │ 总数  通过  拒绝  待处理  │ │     - Name + approved total amount
│ │  37    28    6      3     │ │     - 4-col stats grid
│ │ ████████████░░░░░░░░░░░░░ │ │     - Progress bar (green/red/yellow)
│ │ ▸ Top 3 approved          │ │     - Collapsible (default collapsed)
│ └───────────────────────────┘ │
│                               │
│ ┌─ CNY - ChipPay ──────────┐ │
│ │ ...                       │ │
│ └───────────────────────────┘ │
└───────────────────────────────┘

Expanded "Top 3 approved":
┌─────────────────────────────┐
│ ▾ Top 3 approved            │
│  ┌─ bg-muted/50 ──────────┐│
│  │ 订单ID    金额    客户ID ││  ← Column headers
│  │ #1032734  $1,999  160167 ││  ← User ID links to CRM
│  │ #1032523  $1,448  128045 ││
│  │ #1032560  $1,448  128363 ││
│  └─────────────────────────┘│
└─────────────────────────────┘
```

- **Sticky**: `self-start lg:sticky lg:top-4` — stays pinned while right column scrolls
- **Responsive**: Full width on mobile, 1/4 width on `lg+`

---

## 3. API

### `GET /api/v1/dashboard/cn-payment-success-rate`

| Param | Type | Default | Description |
|---|---|---|---|
| `hours` | int (1–24) | `3` | Time window in hours |

### Response

```json
{
  "items": [
    {
      "display_name": "CNY - P2Pay",
      "total": 37,
      "approved": 28,
      "declined": 6,
      "fresh": 3,
      "success_rate": 75.7,
      "approved_amount": 15432.50,
      "top_orders": [
        { "order_id": 1032658, "processed_amount": 1448.29, "from_user_id": 157746 },
        { "order_id": 1032603, "processed_amount": 1299.98, "from_user_id": 158877 },
        { "order_id": 1032468, "processed_amount": 811.04, "from_user_id": 128363 }
      ]
    }
  ],
  "total_orders": 93,
  "total_approved": 48,
  "total_declined": 29,
  "total_fresh": 16,
  "overall_success_rate": 51.6,
  "hours": 3
}
```

---

## 4. SQL

### 4.1 Aggregate stats per channel

```sql
SELECT
    p.displayName                                        AS display_name,
    COUNT(*)                                             AS total,
    SUM(t.status = 'approved')                           AS approved,
    SUM(t.status = 'declined')                           AS declined,
    SUM(t.status = 'fresh')                              AS fresh,
    SUM(IF(t.status = 'approved', t.processedAmount, 0)) AS approved_amount
FROM transactions t
INNER JOIN psps p ON p.id = t.pspId
WHERE t.type = 'deposit'
  AND p.cid = 0
  AND t.createdAt >= NOW() - INTERVAL %s HOUR
GROUP BY p.displayName
ORDER BY total DESC
```

### 4.2 Top 3 approved orders per channel

```sql
SELECT display_name, order_id, processed_amount, from_user_id
FROM (
    SELECT
        p.displayName                  AS display_name,
        t.id                           AS order_id,
        t.processedAmount              AS processed_amount,
        t.fromUserId                   AS from_user_id,
        ROW_NUMBER() OVER (
            PARTITION BY p.displayName
            ORDER BY t.processedAmount DESC
        ) AS rn
    FROM transactions t
    INNER JOIN psps p ON p.id = t.pspId
    WHERE t.type = 'deposit'
      AND p.cid = 0
      AND t.status = 'approved'
      AND t.createdAt >= NOW() - INTERVAL %s HOUR
) ranked
WHERE rn <= 3
ORDER BY display_name, rn
```

### 4.3 Index usage

| Filter | Index Used |
|---|---|
| `t.createdAt >= ...` | `createdAt` (KEY) |
| `t.pspId` → JOIN psps | `IDX_EAA81A4C79EF21FD` (pspId) |
| `psps` table (134 rows) | Full scan OK — tiny table |

---

## 5. CN Channel Identification

CN channels are identified via `psps.cid = 0`, **not** by `fromLoginSid` (which identifies the client's server, not the payment channel).

| Approach | Meaning | Used? |
|---|---|---|
| `psps.cid = 0` | PSP channel is configured for CN | **Yes** |
| `fromLoginSid LIKE '1-%'` | Client is on CN server (sid=1) | No — a CN client could use a non-CN channel |

### Known CN channels (as of 2026-03)

| displayName | handler | Typical volume |
|---|---|---|
| CNY - P2Pay | pass2pay | High |
| CNY - Mpay | MPay | Medium |
| CNY - Exlink CNY Alipay | cup | Medium |
| CNY - Exlink CNY P2P | cup | Medium |
| CNY - ChipPay | chippay_order | Medium |
| Fast Deposit China (Jpay-USDT-TRC20) | jpay | Low |
| Skrill(CN Client) | skrill | Low |

Note: `handler = 'cup'` maps to two different displayNames (Exlink Alipay / P2P), which is why we group by `displayName` instead of `handler`.

---

## 6. Database Tables

| Table | Database | Purpose |
|---|---|---|
| `transactions` | fxbackoffice | Deposit/withdrawal records; filtered by `type='deposit'`, `createdAt`, `pspId` |
| `psps` | fxbackoffice | PSP configuration; `cid=0` = CN, `displayName` for grouping |

Key columns from `transactions`: `id`, `status`, `type`, `processedAmount`, `createdAt`, `fromUserId`, `pspId`.

Full schema: `.cursor/skills/database-context/mysql-schemas.md` → sections `transactions` and `psps`.

---

## 7. File Inventory

| File | Purpose |
|---|---|
| `frontend/src/components/dashboard/CnPaymentSuccessRate.tsx` | Widget component (Card with ToggleGroup, channel cards, collapsible top orders) |
| `frontend/src/pages/Home.tsx` | Dashboard page — imports CnPaymentSuccessRate in left column |
| `backend/app/api/v1/routes/dashboard.py` | API route: `GET /cn-payment-success-rate` |
| `backend/app/services/cn_payment_service.py` | SQL queries: stats aggregation + top 3 window function |
| `backend/app/schemas/cn_payment.py` | Pydantic models: `TopOrder`, `CnPaymentChannelRow`, `CnPaymentSuccessRateResponse` |

---

## 8. Frontend Details

### State

| State | Type | Default | Purpose |
|---|---|---|---|
| `data` | `ApiResponse \| null` | `null` | API response |
| `loading` | `boolean` | `true` | Loading indicator |
| `error` | `string \| null` | `null` | Error message |
| `fetchedAt` | `Date \| null` | `null` | Last fetch timestamp |
| `hours` | `string` | `"3"` | Selected time window |
| `expandedChannel` | `string \| null` | `null` | Which channel's top orders are expanded |

### Interactions

| Action | Behavior |
|---|---|
| Page mount | Auto-fetch with default 3h window |
| Toggle 3h/6h/24h | `hours` state changes → `useCallback` dep triggers refetch |
| Click "刷新" | Manual refetch with current `hours` |
| Click "Top 3 approved" | Toggle expand/collapse; only one channel expanded at a time |
| Click user ID | Opens CRM in new tab: `https://mt4.kohleglobal.com/crm/users/{userId}` |

### UI Conventions (matches other dashboard cards)

| Element | Style |
|---|---|
| Refresh button | `size="sm" variant="outline" className="h-7 gap-1 text-xs"` + text "刷新" |
| Timestamp | `text-xs text-muted-foreground` + Clock icon + `toLocaleString("zh-CN", { hour12: false })` |
| Success rate color | ≥80% green, ≥50% yellow, <50% red |
| Top orders background | `bg-muted/50 rounded-md` to visually separate from stats |
| Collapse arrow | `ChevronRight` with `rotate-90` transition on expand |

---

## 9. Future Enhancements

- [ ] Auto-refresh with configurable interval (e.g., every 5 minutes)
- [ ] Redis cache with short TTL (30–60s) to reduce DB load under concurrent users
- [ ] Alert/notification when success rate drops below threshold
- [ ] Click channel name to see full order list (link to dedicated page)
