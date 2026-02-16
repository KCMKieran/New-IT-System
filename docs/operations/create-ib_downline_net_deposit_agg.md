# 在 KCM_fxbackoffice 中创建 ib_downline_net_deposit_agg

在 **KCM_fxbackoffice** 库下执行以下 SQL（按顺序）。连接使用 **prod** 集群：`ny8bvfks7d.ap-southeast-1.aws.clickhouse.cloud`，数据库选 **KCM_fxbackoffice**。

---

## 0. 确认依赖表及列名（可选）

若执行时报“列不存在”，先检查列名是否与下面一致（CDC 可能用小写或不同名）：

```sql
DESCRIBE TABLE fxbackoffice_stats_transactions;
DESCRIBE TABLE fxbackoffice_ib_tree_with_self;
```

需要对应关系：

- **fxbackoffice_stats_transactions**：`userId`（或 `user_id`）、`amount`、`currency`、`type`
- **fxbackoffice_ib_tree_with_self**：`ibId`、`referralId`（或 `referral_id`）

若列名不同，把下面 SQL 里对应列名替换成你库里的名字。

---

## 1. 建表

```sql
-- IB 下级净入金汇总表 (AggregatingMergeTree，查询时用 sumMerge)
CREATE TABLE IF NOT EXISTS ib_downline_net_deposit_agg
(
  ibId UInt64,
  net_deposit AggregateFunction(sum, Decimal(18, 4))
) ENGINE = AggregatingMergeTree() ORDER BY ibId;
```

---

## 2. 建物化视图（新数据自动写入）

```sql
CREATE MATERIALIZED VIEW IF NOT EXISTS mv_ib_downline_net_deposit_agg
TO ib_downline_net_deposit_agg
AS SELECT
  toUInt64(tree.ibId) AS ibId,
  sumState(toDecimal64(tx.amount / if(tx.currency = 'CEN', 100.0, 1.0), 4)) AS net_deposit
FROM fxbackoffice_stats_transactions AS tx
INNER JOIN fxbackoffice_ib_tree_with_self AS tree
  ON toUInt64(tx.userId) = toUInt64(tree.referralId)
WHERE tx.type IN ('deposit', 'withdrawal', 'ib withdrawal')
GROUP BY ibId;
```

若列名为小写（如 `user_id`、`referral_id`），改为例如：

```sql
  ON toUInt64(tx.user_id) = toUInt64(tree.referral_id)
```

---

## 3. 历史数据回填（一次性）

物化视图只对**之后**插入的数据生效，已有数据需手动回填：

```sql
INSERT INTO ib_downline_net_deposit_agg
SELECT
  toUInt64(tree.ibId) AS ibId,
  sumState(toDecimal64(tx.amount / if(tx.currency = 'CEN', 100.0, 1.0), 4)) AS net_deposit
FROM fxbackoffice_stats_transactions AS tx
INNER JOIN fxbackoffice_ib_tree_with_self AS tree
  ON toUInt64(tx.userId) = toUInt64(tree.referralId)
WHERE tx.type IN ('deposit', 'withdrawal', 'ib withdrawal')
GROUP BY ibId;
```

（列名若不同，同样改成 `user_id` / `referral_id` 等。）

---

## 4. 校验

```sql
SELECT ibId, sumMerge(net_deposit) AS net_deposit_usd
FROM ib_downline_net_deposit_agg
GROUP BY ibId
LIMIT 10;
```

有结果且无报错即表示表与回填正常，Client PnL 页面的「IB 净入金」会从该表取数。
