# 长期收益率 ROACE (Return on Average Capital Employed)

## 1. 背景与问题

作为 CFD 券商，传统收益率公式在**净入金为负**（出金 > 入金）时失效：

```
传统收益率 = (净值 - 净入金) / 净入金 × 100%
                                    ↑ 负数或接近零 → 公式崩溃
```

现有补救方案：
- **正数入金收益率** (`return_non_adjusted`)：仅净入金 > 0 时可用
- **负数入金收益率** (`return_neg_adjusted`)：用 `MAX(90天入金, |净入金|)` 做分母，有一定人为性
- **分桶调整收益率** (`adj_xxx`)：按入金规模用固定基准值做分母

这些方案各有适用场景，但缺乏一个**对所有客户通用**的收益率指标。

---

## 2. 指标定义

| 指标 | 字段名 | 含义 |
|------|--------|------|
| **日均净值** | `avg_daily_equity` | 客户全历史**活跃天**（有交易或资金变动）日终净值的平均值，代表"平均在用资本" |
| **长期收益率 ROACE** | `return_on_avg_equity` | 每投入 $1 平均资本，累计赚了多少 |

> **ROACE** = Return on Average Capital Employed，金融行业通用的资本回报率指标。

---

## 3. 计算公式

### 日均净值

```
avg_daily_equity = SUM(每日 endingEquity) / 活跃资金天数
```

- 数据来源：`fxbackoffice.stats_balances`（每日日终快照）× `stats_trading`（活跃天过滤）
- 通过 `mt4_users` JOIN，取 `sid IN (1, 5, 6)` 的交易账户
- **INNER JOIN `stats_trading`**：只计算有交易活动或资金变动的天数（`stats_trading` 仅在有事件的日期产生记录），自动排除长期休眠的"尘埃余额"天
- 排除 IB Wallet（sid=2），避免佣金钱包金额扭曲
- 排除 `endingEquity = 0` 的天数
- 排除 demo 账户（`GROUP NOT LIKE '%demo%'`）
- CEN（美分）账户自动除以 100
- 全历史无日期限制，每个客户用自己完整的数据

### 长期收益率

```
return_on_avg_equity = profit_hist / avg_daily_equity × 100%
```

- `profit_hist`：已实现交易盈亏（来自 `stats_trading_running_totals.plClosedHavingActivityRunningTotal`）
- 不含 IB 佣金、奖金、浮动盈亏

---

## 4. 计算举例

### 例一：正常客户（30 天）

| 时段 | 操作 | 账户净值 |
|------|------|---------|
| Day 1-10 | 存入 $10,000 | ~$10,000 |
| Day 11-20 | 取出 $8,000 | ~$2,000 |
| Day 21-30 | 存入 $5,000 | ~$7,000 |

- 历史总利润 = $3,000
- 日均净值 = (10K×10 + 2K×10 + 7K×10) / 30 = **$6,333**
- ROACE = $3,000 / $6,333 × 100% = **47.4%**

与其他方法对比：

| 方法 | 分母 | 收益率 | 评价 |
|------|------|--------|------|
| 传统（净入金） | $7,000 | 42.9% | 可用但不反映资金时间分布 |
| 累计入金法 | $15,000 | 20.0% | 被重复存取撑大，系统性偏低 |
| **ROACE（日均净值）** | **$6,333** | **47.4%** | 真实反映平均在用资本 |

### 例二：净入金为负的客户

| 时段 | 操作 | 净值 |
|------|------|------|
| Day 1-10 | 存 $5,000 | ~$5,000 |
| Day 11-30 | 取 $6,000（含利润出金）| ~$1,000 |

- 净入金 = -$1,000（传统公式失效）
- 日均净值 = (5K×10 + 1K×20) / 30 = **$2,333**
- ROACE 可正常计算 ✅

---

## 5. 设计决策

| 设计点 | 决策 | 理由 |
|--------|------|------|
| 数据源 | `stats_balances` 每日快照 | 系统已有日终 equity 记录，无需额外采集 |
| 时间范围 | 全历史 | 每个客户用自己完整历史，自动归一化，无需统一时段 |
| 排除 equity=0 天 | 是 | 避免空仓期拉低分母，只衡量有资金时的效率 |
| JOIN stats_trading 过滤 | 是 | 排除长期休眠"尘埃余额"天（如账户留 $2.83 数月无交易），只计有交易/资金变动的活跃天 |
| 排除 IB Wallet (sid=2) | 是 | IB 佣金钱包金额大，会扭曲真实交易资本 |
| 利润口径 | `profit_hist`（已实现交易盈亏） | 不含 IB 佣金、奖金、浮动盈亏，纯交易利润 |
| 按需启用 | `include_avg_equity=true` | Dashboard 高频刷新不需要此指标，全量页按需开启 |

---

## 6. 优缺点

### 优点
- **万能分母**：净入金为正、零、负都能计算，无需分桶补救
- **抗操纵**：不受频繁存取影响（反复存取不会撑大分母）
- **横向可比**：不同时长、不同操作模式的客户可直接比较
- **行业通用**：ROACE 是基金/券商通用指标，合规和管理层容易理解
- **零额外成本**：利用已有的 `stats_balances` 日终快照

### 缺点/局限
- **新客户数据少**：刚开户 1-2 天的客户，日均净值样本少，收益率波动大（会随时间趋稳）
- **查询较重**：需扫描全历史快照并 JOIN stats_trading，首次查询较慢（后续命中 Redis 缓存则无感）
- **不含浮动盈亏**：分子 `profit_hist` 只算已平仓利润，未含当前持仓浮盈浮亏
- **依赖 stats_trading 完整性**：若 stats_trading 某天漏记录，该天的 equity 不计入平均值

---

## 7. 与现有指标的关系

ROACE 不替代现有指标，而是**补充**：

```
现有指标                                新增指标
├─ 正数入金收益率%  → 净入金>0 时用       │
├─ 负数入金收益率%  → 净入金≤0 时用       │
├─ 分桶调整收益率%  → 按入金规模分层       │
│                                       │
└──────── 各有适用场景 ─────────── 长期收益率 ROACE% → 所有客户通用
```

---

## 8. 技术实现

### 涉及文件

| 层 | 文件 | 改动 |
|---|---|---|
| Schema | `backend/app/schemas/client_return_rate.py` | 新增 `avg_daily_equity`、`return_on_avg_equity` 字段 |
| Service | `backend/app/services/client_return_service.py` | `_build_phase2_sql` 条件构建 stats_balances + stats_trading INNER JOIN |
| Route | `backend/app/api/v1/routes/client_return_rate.py` | 新增 `include_avg_equity` query 参数 |
| Frontend | `frontend/src/pages/ClientReturnRate.tsx` | 新增两列（淡蓝/淡紫背景），位于"区间交易利润"右侧 |

### API 调用

```
GET /api/v1/client-return-rate/query?include_avg_equity=true&...
```

- Dashboard（`ReturnRateSummary.tsx`）不传此参数 → 默认 false → 不触发 stats_balances 查询
- 全量页（`ClientReturnRate.tsx`）传 `include_avg_equity=true` → 返回 ROACE 数据

### SQL 核心逻辑

```sql
SELECT
    mu2.userId AS client_id,
    SUM(IF(sb.currency = 'CEN', sb.endingEquity / 100.0, sb.endingEquity))
        / COUNT(DISTINCT sb.date) AS avg_daily_equity
FROM mt4_users mu2
INNER JOIN stats_balances sb ON sb.loginsid = mu2.loginsid
INNER JOIN stats_trading st2 ON st2.loginSid = mu2.loginsid AND st2.date = sb.date
WHERE mu2.userId IN ({client_id_list})
  AND mu2.sid IN (1, 5, 6)
  AND mu2.`GROUP` NOT LIKE '%demo%'
  AND sb.endingEquity > 0
GROUP BY mu2.userId
```

> **为什么 JOIN stats_trading？** `stats_trading` 仅在有交易活动或资金变动的日期产生记录。
> 通过 INNER JOIN，长期休眠期（如账户残留 $2.83 长达数月无任何操作）自动被排除，
> 避免大量低余额天拉低 avg_daily_equity 导致 ROACE 虚高。

### 索引依赖

- `stats_balances.IDX_ACCOUNT (loginSid)` — 已存在，通过 mt4_users JOIN 间接查询
- `stats_trading.IDX_ACCDATE (loginSid, date)` — 已存在，用于 INNER JOIN 匹配活跃天
- 无需新增索引

---

## 9. 单客户验证 SQL

修改第一行 `@client_id` 即可验证任意客户：

```sql
-- ========== 只需改这一行 ==========
SET @client_id = 130130;

-- 1) 该客户的 MT4 账号
SELECT loginsid, LOGIN, CURRENCY, sid, `GROUP`
FROM mt4_users
WHERE userId = @client_id
  AND sid IN (1, 5, 6)
  AND `GROUP` NOT LIKE '%demo%';

-- 2) 每日 ending equity（仅活跃天，JOIN stats_trading 过滤休眠期）
SELECT sb.date, sb.loginsid, sb.currency, sb.endingEquity
FROM mt4_users mu
INNER JOIN stats_balances sb ON sb.loginsid = mu.loginsid
INNER JOIN stats_trading st ON st.loginSid = mu.loginsid AND st.date = sb.date
WHERE mu.userId = @client_id
  AND mu.sid IN (1, 5, 6)
  AND mu.`GROUP` NOT LIKE '%demo%'
  AND sb.endingEquity > 0
ORDER BY sb.date DESC;

-- 3) 平均每日 equity（仅活跃天）
SELECT
    mu.userId AS client_id,
    COUNT(DISTINCT sb.date) AS equity_days,
    ROUND(SUM(IF(sb.currency = 'CEN', sb.endingEquity / 100.0, sb.endingEquity)), 2) AS total_equity_sum,
    ROUND(
        SUM(IF(sb.currency = 'CEN', sb.endingEquity / 100.0, sb.endingEquity))
        / COUNT(DISTINCT sb.date),
        2
    ) AS avg_daily_equity
FROM mt4_users mu
INNER JOIN stats_balances sb ON sb.loginsid = mu.loginsid
INNER JOIN stats_trading st ON st.loginSid = mu.loginsid AND st.date = sb.date
WHERE mu.userId = @client_id
  AND mu.sid IN (1, 5, 6)
  AND mu.`GROUP` NOT LIKE '%demo%'
  AND sb.endingEquity > 0
GROUP BY mu.userId;

-- 4) 历史盈亏 (该表已预处理 CEN，无需再 /100)
SELECT
    userId AS client_id,
    ROUND(SUM(plClosedHavingActivityRunningTotal), 2) AS profit_hist
FROM stats_trading_running_totals
WHERE userId = @client_id
GROUP BY userId;

-- 5) 最终: 平均权益回报率
SELECT
    ade.client_id,
    ade.equity_days,
    ade.avg_daily_equity,
    rt.profit_hist,
    ROUND(rt.profit_hist / ade.avg_daily_equity * 100, 2) AS return_on_avg_equity
FROM (
    SELECT
        mu.userId AS client_id,
        COUNT(DISTINCT sb.date) AS equity_days,
        SUM(IF(sb.currency = 'CEN', sb.endingEquity / 100.0, sb.endingEquity))
            / COUNT(DISTINCT sb.date) AS avg_daily_equity
    FROM mt4_users mu
    INNER JOIN stats_balances sb ON sb.loginsid = mu.loginsid
    INNER JOIN stats_trading st ON st.loginSid = mu.loginsid AND st.date = sb.date
    WHERE mu.userId = @client_id
      AND mu.sid IN (1, 5, 6)
      AND mu.`GROUP` NOT LIKE '%demo%'
      AND sb.endingEquity > 0
    GROUP BY mu.userId
) ade
JOIN (
    SELECT
        userId AS client_id,
        SUM(plClosedHavingActivityRunningTotal) AS profit_hist
    FROM stats_trading_running_totals
    WHERE userId = @client_id
    GROUP BY userId
) rt ON ade.client_id = rt.client_id;
```
