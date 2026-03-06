# Dashboard: 近两日客户平仓净盈亏 (Group) — SQL 与接口设计

## 1. Overview

Dashboard 上的第二个 PnL 卡片，与"近两日客户平仓净盈亏（按国家）"平行，按 `mt4_users.GROUP`（账户组）分组展示盈亏和 IB 佣金，方便运营团队从账户组维度观察盈亏分布。

### 与现有 PnL by Country 卡片的区别

| 维度 | PnL by Country | PnL by Group |
|------|----------------|--------------|
| 分组行 | country → sales_team | **account_group → sales_team** |
| 数据列 | 今日Profit / 今日IB佣金 / 昨日Profit / 昨日IB佣金 | **今日Profit / 今日IB佣金 / 昨日Profit / 昨日IB佣金** |
| IB 佣金数据源 | `stats_ib_commissions` (userId 级) | `stats_ib_commissions_by_login_sid` (loginSid 级，可关联 GROUP) |
| 表头排序 | 无 | **支持点击列标题排序（升序/降序切换）** |
| 后端 API | `GET /api/v1/dashboard/pnl-by-sales-team` | `GET /api/v1/dashboard/pnl-by-group` |

---

## 2. SQL 设计

### 2.1 PnL SQL

基于现有 `SQL_PNL_BY_SALES_TEAM`，将 GROUP BY 维度改为 `(mu.GROUP, tt.team_tag)`。

```sql
SELECT
    by_user.account_group,
    COALESCE(tt.team_tag, 'Unknown') AS sales_team,
    ROUND(SUM(CASE WHEN by_user.dt = CURDATE()
              THEN by_user.pl_usd ELSE 0 END), 2) AS net_pnl_today,
    ROUND(SUM(CASE WHEN by_user.dt = CURDATE() - INTERVAL 1 DAY
              THEN by_user.pl_usd ELSE 0 END), 2) AS net_pnl_yesterday
FROM (
    SELECT st.userId, mu.`GROUP` AS account_group, st.date AS dt,
           SUM(IF(st.currency = 'CEN', st.totalPlClosed / 100.0, st.totalPlClosed)) AS pl_usd
    FROM stats_trading st
    INNER JOIN mt4_users mu ON st.loginSid = mu.loginSid AND mu.userId = st.userId
    INNER JOIN users u ON u.id = st.userId AND COALESCE(u.isEmployee, 0) = 0
    WHERE st.date IN (CURDATE(), CURDATE() - INTERVAL 1 DAY)
      AND st.userId > 0 AND st.tradeCnt > 0
      AND mu.sid IN (1, 5, 6) AND mu.`GROUP` NOT LIKE '%demo%'
    GROUP BY st.userId, mu.`GROUP`, st.date
) AS by_user
LEFT JOIN (...tags subquery...) tt ON by_user.userId = tt.userid
GROUP BY by_user.account_group, tt.team_tag
```

### 2.2 IB Commission SQL

Uses `stats_ib_commissions_by_login_sid` (account-level) instead of `stats_ib_commissions` (user-level), because GROUP is an account-level attribute.

```sql
SELECT
    mu.`GROUP` AS account_group,
    COALESCE(tt.team_tag, 'Unknown') AS sales_team,
    ROUND(SUM(CASE WHEN sicls.date = CURDATE()
              THEN IF(sicls.currency = 'CEN', sicls.commission / 100.0, sicls.commission)
              ELSE 0 END), 2) AS ib_commission_today,
    ROUND(SUM(CASE WHEN sicls.date = CURDATE() - INTERVAL 1 DAY
              THEN IF(sicls.currency = 'CEN', sicls.commission / 100.0, sicls.commission)
              ELSE 0 END), 2) AS ib_commission_yesterday
FROM stats_ib_commissions_by_login_sid sicls
INNER JOIN mt4_users mu ON sicls.fromLoginSid = mu.loginSid
LEFT JOIN (...tags subquery...) tt ON mu.userId = tt.userid
WHERE sicls.date IN (CURDATE(), CURDATE() - INTERVAL 1 DAY)
  AND mu.sid IN (1, 5, 6) AND mu.`GROUP` NOT LIKE '%demo%'
GROUP BY mu.`GROUP`, tt.team_tag
```

### Data filtering (same as PnL by Country)

- Exclude demo accounts: `mu.GROUP NOT LIKE '%demo%'` + `sid IN (1,5,6)`
- Exclude employee accounts: `COALESCE(u.isEmployee, 0) = 0`

---

## 3. API Design

### Endpoint

`GET /api/v1/dashboard/pnl-by-group`

### Response

```json
{
  "items": [
    {
      "account_group": "KCM-USD-A",
      "sales_team": "sh",
      "net_pnl_today": 12345.67,
      "net_pnl_yesterday": -2345.12,
      "ib_commission_today": 500.00,
      "ib_commission_yesterday": 450.00
    }
  ]
}
```

Backend runs two sequential SQL queries (PnL + IB commission), merges by `(account_group, sales_team)` key.

---

## 4. Frontend

### Component

`frontend/src/components/dashboard/Past24hClientPnlByGroup.tsx`

### Table Structure

```
| (expand) | 账户组       | 今日Profit↕ | 今日IB佣金↕ | 昨日Profit↕ | 昨日IB佣金↕ |
|----------|-------------|------------|------------|------------|------------|
| ▶ KCM-USD-A            | $12,345    | $500       | -$2,345    | $450       |
|   └ sh                 | $5,000     | $200       | -$1,200    | $180       |
|   └ szd                | $3,000     | $150       | -$500      | $120       |
| ▶ KCM-CEN-A            | $890       | $50        | $456       | $40        |
```

### Column Sort

All 4 data columns support click-to-sort:
- Click once: ascending (↑)
- Click again: descending (↓)
- Click a different column: switch sort target, reset to ascending
- Default: sort by 昨日Profit ascending
- Sorting applies to both group rows and expanded team sub-rows
- Sort indicator icons: ArrowUp (↑), ArrowDown (↓), ArrowUpDown (inactive)

### Visual Style

- IB commission columns in muted gray (`text-muted-foreground`)
- Profit columns in red/green based on value
- `text-xs` font, fixed-height card (`h-[260px]`), scrollable

---

## 5. File Inventory

| File | Purpose |
|------|---------|
| `backend/app/schemas/dashboard_pnl_group.py` | Pydantic schema (PnL + IB commission fields) |
| `backend/app/services/dashboard_pnl_group_service.py` | Two SQL queries + merge by (group, team) key |
| `backend/app/api/v1/routes/dashboard.py` | Route: `GET /pnl-by-group` |
| `frontend/src/components/dashboard/Past24hClientPnlByGroup.tsx` | Frontend widget with sortable columns |
| `frontend/src/pages/Home.tsx` | Dashboard layout |
