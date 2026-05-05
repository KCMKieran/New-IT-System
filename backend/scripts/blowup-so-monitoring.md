# 爆仓 / 强平（SO）监测说明

本文档说明如何在 `fxbackoffice` 库中监测 **Stop Out（强平）** 与 **爆仓/高风险账户（业务口径）**，并提供可直接修改日期与窗口后执行的 SQL 示例。

相关脚本入口：`blowup_audit_window.py`（当前「爆仓审计」第一步为 **亏损单 + 当前负余额**，与本文 **SO 全量** 口径不同，见下文）。

---

## 一、两种口径（不要混为一条 SQL 的「唯一真相」）

| 口径 | 含义 | 主要数据依据 | 典型用途 |
|------|------|----------------|----------|
| **A. Stop Out（强平 / SO）** | 保证金不足，由**服务器自动平仓** | `mt4_trades.COMMENT` 出现 **`[so…`**、**`so:`**、**`cso:`** 等服务器侧形态 | 统计强平笔数、涉及账户数、按 `sid` 对比 |
| **B. 爆仓/高风险（业务常称爆仓）** | 如：窗口内 **亏损单** 且 **`mt4_users` 当前 `BALANCE < 0`** | `mt4_trades` + `mt4_users` | 找仍处于严重亏损/穿仓状态的账户 |

**注意：**

- SO 发生后客户可能**入金**，`BALANCE` 不再为负 → 口径 B 会**漏掉**「曾强平」。
- 负余额也未必每笔成交都带 `so:` 备注 → 口径 A 与 B **互补**，建议**并行出数**。

---

## 二、环境与表约定

| 项 | 说明 |
|----|------|
| 库 | `fxbackoffice`（与 `blowup_audit_window.py` 使用的 `MYSQL_DATABASE_FXBACKOFFICE` 一致） |
| 主表 | `mt4_trades`（明细）、`mt4_users`（账户当前资金等） |
| 三服 | `sid IN (1, 5, 6)` — MT4 / MT5 / CEN（与内部编号一致即可） |
| 方向 | `CMD IN (0, 1)`（买卖） |
| 删除 | `(isDeleted = 0 OR isDeleted IS NULL)` |
| 时间 | `closeDate` + `CLOSE_TIME` 为**库内 MT 时间**；跨日查询时 `closeDate IN (...)` 须包含窗口内所有日期 |

**Python / pymysql：** 在代码里拼接 `LIKE` 时，`%` 需写成 `%%`，否则可能触发 `format` 类错误。

---

## 三、SO 判定（推荐生产固定规则）

**不要使用** `COMMENT LIKE '%so%'` — 会误匹配如 `VIEWSON` 中的子串 `so`。

**推荐使用以下前缀组合（三 sid 通用）：**

```sql
AND (
  COMMENT LIKE '[so%'
  OR TRIM(COMMENT) LIKE 'so:%'
  OR TRIM(COMMENT) LIKE 'cso:%'
)
```

- **`[so%`**：覆盖 `[so at xx%]、`[so 0.00%/…` 等括号形态（MT4/MT5 常见）。
- **`so:%`**：覆盖 `so: xx%/yy/zz` 等（CEN 上亦有样本）。
- **`cso:%`**：预留信用/相关强平写法；若无数据可保留，成本低。

---

## 四、SQL 示例

将示例中的日期、时间范围替换为你的监测窗口。以下为单日示例 `2026-05-04`；跨日请扩展 `closeDate IN ('YYYY-MM-DD', ...)`。

### 4.1 SO — 三服汇总（笔数、账户数）

```sql
SELECT
  COUNT(*) AS so_rows,
  COUNT(DISTINCT loginSid) AS so_accounts
FROM fxbackoffice.mt4_trades
WHERE closeDate IN ('2026-05-04')
  AND CLOSE_TIME >= '2026-05-04 00:00:00'
  AND CLOSE_TIME <  '2026-05-04 23:59:59'
  AND sid IN (1, 5, 6)
  AND CMD IN (0, 1)
  AND (isDeleted = 0 OR isDeleted IS NULL)
  AND (
    COMMENT LIKE '[so%'
    OR TRIM(COMMENT) LIKE 'so:%'
    OR TRIM(COMMENT) LIKE 'cso:%'
  );
```

### 4.2 SO — 按 `sid` 分布

```sql
SELECT
  sid,
  COUNT(*) AS so_rows,
  COUNT(DISTINCT loginSid) AS so_accounts
FROM fxbackoffice.mt4_trades
WHERE closeDate IN ('2026-05-04')
  AND CLOSE_TIME >= '2026-05-04 00:00:00'
  AND CLOSE_TIME <  '2026-05-04 23:59:59'
  AND sid IN (1, 5, 6)
  AND CMD IN (0, 1)
  AND (isDeleted = 0 OR isDeleted IS NULL)
  AND (
    COMMENT LIKE '[so%'
    OR TRIM(COMMENT) LIKE 'so:%'
    OR TRIM(COMMENT) LIKE 'cso:%'
  )
GROUP BY sid
ORDER BY sid;
```

### 4.3 SO — 明细（抽查 / 导出）

```sql
SELECT
  TICKET,
  loginSid,
  sid,
  SYMBOL,
  CMD,
  VOLUME,
  OPEN_TIME,
  CLOSE_TIME,
  totalProfit,
  COMMENT
FROM fxbackoffice.mt4_trades
WHERE closeDate IN ('2026-05-04')
  AND CLOSE_TIME >= '2026-05-04 00:00:00'
  AND CLOSE_TIME <  '2026-05-04 23:59:59'
  AND sid IN (1, 5, 6)
  AND CMD IN (0, 1)
  AND (isDeleted = 0 OR isDeleted IS NULL)
  AND (
    COMMENT LIKE '[so%'
    OR TRIM(COMMENT) LIKE 'so:%'
    OR TRIM(COMMENT) LIKE 'cso:%'
  )
ORDER BY CLOSE_TIME DESC, sid, loginSid
LIMIT 500;
```

### 4.4 SO — 单账户集中度（高频强平户）

```sql
SELECT
  loginSid,
  sid,
  COUNT(*) AS so_rows,
  MIN(CLOSE_TIME) AS first_so,
  MAX(CLOSE_TIME) AS last_so,
  SUM(totalProfit) AS sum_profit_so_trades
FROM fxbackoffice.mt4_trades
WHERE closeDate IN ('2026-05-04')
  AND CLOSE_TIME >= '2026-05-04 00:00:00'
  AND CLOSE_TIME <  '2026-05-04 23:59:59'
  AND sid IN (1, 5, 6)
  AND CMD IN (0, 1)
  AND (isDeleted = 0 OR isDeleted IS NULL)
  AND (
    COMMENT LIKE '[so%'
    OR TRIM(COMMENT) LIKE 'so:%'
    OR TRIM(COMMENT) LIKE 'cso:%'
  )
GROUP BY loginSid, sid
ORDER BY so_rows DESC
LIMIT 50;
```

### 4.5 爆仓/高风险（近似）— 窗口内亏损单 + 当前负余额

与 `blowup_audit_window.py` 第一步思路接近。`BALANCE` 为**当前快照**，补款后会变。

```sql
SELECT
  L.loginSid,
  L.sid,
  U.userid,
  U.NAME,
  U.groupsid,
  U.BALANCE,
  COUNT(*) AS loss_orders,
  SUM(L.totalProfit) AS total_loss
FROM fxbackoffice.mt4_trades L
JOIN fxbackoffice.mt4_users U
  ON U.loginsid = L.loginSid
WHERE L.closeDate IN ('2026-05-04')
  AND L.CLOSE_TIME >= '2026-05-04 00:00:00'
  AND L.CLOSE_TIME <  '2026-05-04 23:59:59'
  AND L.sid IN (1, 5, 6)
  AND L.CMD IN (0, 1)
  AND (L.isDeleted = 0 OR L.isDeleted IS NULL)
  AND L.totalProfit < 0
  AND U.BALANCE < 0
GROUP BY L.loginSid, L.sid, U.userid, U.NAME, U.groupsid, U.BALANCE
ORDER BY total_loss ASC
LIMIT 200;
```

如需排除 demo/test，可追加与 `blowup_audit_window.py` 中 `demo_filter_sql` 一致的条件（按 `groupsid` / `NAME`）。

### 4.6 联合视角 — 窗口内存在 SO 且当前仍为负余额的账户

用于风控「重点名单」。若 `mt4_users` 无 `sid` 列，可删除 `u.sid` 或按实际表结构调整。

```sql
SELECT DISTINCT u.loginsid AS loginSid
FROM fxbackoffice.mt4_users u
WHERE u.BALANCE < 0
  AND EXISTS (
    SELECT 1
    FROM fxbackoffice.mt4_trades t
    WHERE t.loginSid = u.loginsid
      AND t.closeDate IN ('2026-05-04')
      AND t.CLOSE_TIME >= '2026-05-04 00:00:00'
      AND t.CLOSE_TIME <  '2026-05-04 23:59:59'
      AND t.sid IN (1, 5, 6)
      AND t.CMD IN (0, 1)
      AND (t.isDeleted = 0 OR t.isDeleted IS NULL)
      AND (
        t.COMMENT LIKE '[so%'
        OR TRIM(t.COMMENT) LIKE 'so:%'
        OR TRIM(t.COMMENT) LIKE 'cso:%'
      )
  );
```

### 4.7 巡检 — 发现新 COMMENT 形态（仅人工用，勿作生产 SO 统计）

以下使用宽泛 `%so%` **仅用于巡检**，发现新前缀后应回到第三节规则并更新文档。

```sql
SELECT DISTINCT LEFT(TRIM(COMMENT), 20) AS p20, COUNT(*) AS cnt
FROM fxbackoffice.mt4_trades
WHERE closeDate IN ('2026-05-04')
  AND sid IN (1, 5, 6)
  AND CMD IN (0, 1)
  AND (isDeleted = 0 OR isDeleted IS NULL)
GROUP BY p20
HAVING p20 LIKE '%so%' OR p20 LIKE '%SO%'
ORDER BY cnt DESC
LIMIT 100;
```

---

## 五、CEN（sid=6）与金额单位

- CEN 上 `COMMENT` 中 **SL/TP/EA** 备注很多，SO 识别更依赖 **`[so%` / `so:`** 前缀，不要用 `%so%` 做生产统计。
- Cent 账户的 `totalProfit` / `BALANCE` 可能与美元账户**单位不同**；若要做「损失金额」汇总，须与现有报表约定一致（例如是否换算为 USD），与 **SO 笔数统计** 可分开处理。

---

## 六、局限与运维建议

1. **COMMENT 依赖服务器/插件版本**：大版本或插件变更后应用 **4.7** 巡检，必要时在第三节增加新的 `OR` 分支并记录样例。  
2. **`BALANCE` 为当前值**：不能单独还原「历史是否曾爆仓」，需与 SO 或内部快照结合。  
3. **日常建议固定三张表**：SO 汇总（4.1）、按 sid（4.2）、SO + 当前负余额交集（4.6）；深度分析再加 4.4、4.5。

---

## 七、相关文件

| 文件 | 说明 |
|------|------|
| `backend/scripts/blowup_audit_window.py` | 爆仓审计脚本（Excel + 可选邮件）。`--audit-mode balance_loss`（默认）= 亏损+当前负余额；`so` / `both` = COMMENT 强平口径（见第三节），与本文 SO 规则一致 |
| `backend/scripts/blowup-so-monitoring.md` | 本文档（SO 与 SQL 监测口径） |
