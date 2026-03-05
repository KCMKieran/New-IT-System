# 客户回报率查询 (Client Return Rate)

## 1. 概述

客户回报率查询页面用于分析在指定时间范围内有平仓交易的客户，展示其入金、净值、历史利润及多种回报率指标。帮助风控和运营团队识别高资金效率客户和异常交易客户。

**页面路径**: `/client-return-rate`
**前端文件**: `frontend/src/pages/ClientReturnRate.tsx`
**后端文件**: `backend/app/services/client_return_service.py`
**API 路由**: `backend/app/api/v1/routes/client_return_rate.py`

---

## 2. 页面排布 (UI Layout)

### 2.1 顶部筛选卡片
- **标题**: "客户回报率查询"，副标题说明筛选逻辑
- **日期范围选择器**: 支持自定义日期范围
- **快速时间选择**: 过去 1 小时 / 过去 6 小时 / 今日 / 本周 / 过去 7 天 / 本月 / 过去 30 天
- **客户 ID 搜索**: 精确匹配 client_id
- **查询按钮**: 触发后端查询

### 2.2 统计信息行
- 客户总数统计
- 查询耗时（区分缓存/实时）
- 查询时间（HK 时间 UTC+8，含 AM/PM）
- 缓存状态标签（琥珀色"缓存数据" / 绿色"实时查询"）
- 清除缓存按钮（仅缓存命中时显示）

### 2.3 数据表格 (AG-Grid)
支持排序、分页（20/50/100/200）、文本选择。

---

## 3. 数据列定义

| 列名 | 字段 | 数据来源 | 受时间范围影响 | 计算逻辑 |
|---|---|---|---|---|
| 客户ID | `client_id` | `mt4_users.userId` | 是（筛选维度） | 直接取值，链接到 CRM |
| 历史净入金 | `net_deposit_hist` | `stats_transactions` | 否（全历史） | `SUM(deposit) + SUM(withdrawal + ib_withdrawal)` |
| 区间净入金 | `net_deposit_month` | `stats_transactions` | 是 | 同上，限定时间范围 |
| 现时账户余额 | `equity` | `mt4_users.EQUITY` | 否（实时） | `SUM(EQUITY)`，CEN 账户除以 100 |
| 历史利润 | `profit_hist` | `stats_trading_running_totals` | 否 | `SUM(plClosedHavingActivityRunningTotal)`，CEN 账户除以 100。纯已实现交易利润，不含 IB 佣金/奖金/浮动盈亏 |
| 区间交易利润 | `month_trade_profit` | `stats_trading` / `mt4_trades` | 是 | `SUM(PROFIT + SWAPS + COMMISSION)`，CEN 账户除以 100 |
| 调整后收益率(2K以下)% | `adj_0_2000` | 计算字段 | 否 | 净入金≤0 且平均入金<2K 时：`equity / 2000 × 100` |
| 调整后收益率(2K-5K)% | `adj_2000_5000` | 计算字段 | 否 | 净入金≤0 且 2K≤平均入金<5K 时：`equity / 5000 × 100` |
| 调整后收益率(5K-50K)% | `adj_5000_50000` | 计算字段 | 否 | 净入金≤0 且 5K≤平均入金<50K 时：`equity / 50000 × 100` |
| 调整后收益率(50K以上)% | `adj_50000_plus` | 计算字段 | 否 | 净入金≤0 且平均入金≥50K 时：`equity / 60000 × 100` |
| 正数入金收益率% | `return_non_adjusted` | 计算字段 | 否 | 净入金>0 时：`(equity - net_deposit) / net_deposit × 100` |
| 最近90天入金 | `deposits_90d` | `stats_transactions` | 否（固定90天窗口） | `SUM(deposit)` where `date >= CURDATE() - 90` |
| 负净入金回报率% | `return_neg_adjusted` | 计算字段 | 否 | 净入金≤0 时：`(equity - A) / A × 100`，其中 `A = MAX(deposits_90d, |net_deposit_hist|)` |
| 日均净值 | `avg_daily_equity` | `stats_balances` × `stats_trading` | 否（全历史） | 全历史**活跃天** endingEquity 之和 / 活跃天数。INNER JOIN `stats_trading` 排除休眠尘埃天，排除 equity=0、IB Wallet (sid=2)、demo 账户。仅 `include_avg_equity=true` 时返回 |
| 长期收益率(ROACE)% | `return_on_avg_equity` | 计算字段 | 否 | `profit_hist / avg_daily_equity × 100`。ROACE = Return on Average Capital Employed，衡量每 $1 平均在用资本的累计回报 |

> **注意**: 每个客户只会有"调整后收益率"四列之一有值（按存款区间），或"正数入金收益率"有值，两类互斥。
>
> **ROACE 列**: 日均净值和长期收益率在 UI 中位于"区间交易利润"右侧，分别用淡蓝和淡紫半透明背景高亮。这两列对所有客户通用（不受净入金正负影响）。

---

## 4. 后端架构

### 4.1 两阶段查询 (Two-Phase Query)

**Phase 1**: 获取在时间范围内有平仓交易的 client_id 列表及区间交易利润。
- **快速路径（默认）**: 使用 `stats_trading` 预聚合表，字段 `totalPlClosed`（= PROFIT + SWAPS + COMMISSION），按 `(userId, date)` 索引查询，<1s 完成
- **慢速回退（sub-day）**: 选择"过去 1/6 小时"等 sub-day 模式时，回退到 `mt4_trades` 原始表，使用 VIRTUAL 列 `totalProfit`（需要 `CLOSE_TIME` 精确过滤）
- `stats_trading` 字段映射：`totalPlClosed` = SUM(PROFIT+SWAPS+COMMISSION)，`totalProfit` = 仅 SUM(PROFIT)

**Phase 2**: 对 Phase 1 筛出的 client_id，通过 LEFT JOIN 获取：
- `eq`: `mt4_users` 表的 EQUITY 汇总（sid IN 1,5,6）
- `th`: `stats_transactions` 全历史入金/出金（sid IN 1,2,5,6，含 IB Wallet）
- `txm`: `stats_transactions` 所选时间范围内入金/出金（sid IN 1,2,5,6）
- `dep90`: `stats_transactions` 近 90 天入金（sid IN 1,2,5,6）
- `rt`: `stats_trading_running_totals` 全历史累计已实现交易利润（按 userId 聚合，CEN 除以 100）
- `ade`（可选）: `stats_balances` INNER JOIN `stats_trading` 全历史活跃天日均 equity，排除休眠尘埃天、IB Wallet (sid=2)、demo 账户。仅当 `include_avg_equity=true` 时加入查询

### 4.2 时区处理

`mt4_trades.CLOSE_TIME` 使用 MT4 服务器时间：
- **冬令时**: UTC+2
- **夏令时**: UTC+3

前端传 HK 时间 (UTC+8)，后端通过 `MT4_TZ_OFFSET_HOURS` 常量转换。

> **TODO**: 夏令时切换时需更新 `MT4_TZ_OFFSET_HOURS` 从 6 改为 5。

### 4.3 数据库连接

使用独立配置 `MYSQL_HOST_PRIMARY`（`.env` 中设置），未设置时自动 fallback 到 `MYSQL_HOST`。用于支持临时切换主库/从库而不影响其他页面。

### 4.4 缓存策略

| 层级 | 机制 | TTL | 清除方式 |
|---|---|---|---|
| Redis | 按查询参数 MD5 做 key | 3 小时 | `DELETE /api/v1/client-return-rate/cache` |
| 前端 sessionStorage | 保存最后一次查询结果 | 3 小时 | 页面清除缓存按钮 / 手动清 |

---

## 5. API 接口

### `GET /api/v1/client-return-rate/query`

| 参数 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `page` | int | 1 | 页码 |
| `page_size` | int | 50 | 每页数量（最大 10000） |
| `sort_by` | string | `month_trade_profit` | 排序字段 |
| `sort_order` | string | `desc` | 排序方向 |
| `search` | string | null | 按 client_id 精确搜索 |
| `deposit_bucket` | string | null | 按存款区间过滤 |
| `month_start` | string | 7天前 | 开始日期 (YYYY-MM-DD) |
| `month_end` | string | 今天 | 结束日期 (YYYY-MM-DD) |
| `close_time_start` | string | null | 精确时间过滤 (YYYY-MM-DD HH:MM:SS，HK 时间) |
| `include_avg_equity` | bool | false | 是否计算日均净值和 ROACE 收益率（需额外查询 stats_balances，较重）。全量页传 true，Dashboard 不传（默认 false）以保持轻量 |

### `DELETE /api/v1/client-return-rate/cache`

清除所有 `app:client_return:cache:*` Redis 缓存。

---

## 6. 数据过滤规则

- **排除 demo 账户**: `mt4_users.GROUP NOT LIKE '%demo%'`（Phase 1 mt4_trades 路径及 Phase 2 各子查询）
- **排除 employee 账户**: Phase 1 两条路径均 `INNER JOIN users u ON u.id = <userId> AND COALESCE(u.isEmployee, 0) = 0`，仅保留非员工客户（`users.isEmployee = 0` 或 `NULL`）
- Phase 1 交易利润: `sid IN (1, 5, 6)`（stats_trading 路径无 sid 列，依赖 Phase 2 过滤；mt4_trades 路径在 JOIN 时已过滤 demo）
- Phase 2 equity: `sid IN (1, 5, 6)`
- Phase 2 入金/出金: `sid IN (1, 2, 5, 6)`（含 sid=2 IB Wallet，用于计入 `ib withdrawal`）
- 仅限已平仓买卖单: `CMD IN (0, 1)`（仅 mt4_trades fallback 路径使用）
- 仅限有效客户: `userId > 0`
- CEN（美分）账户金额自动除以 100
- **ROACE 日均净值**: `stats_balances` INNER JOIN `stats_trading`（仅活跃天），通过 `mt4_users` JOIN，`sid IN (1, 5, 6)` 排除 IB Wallet，`endingEquity > 0` 排除空仓天数，全历史无日期限制

---

## 7. 超时保护

| 配置 | 值 | 说明 |
|------|---|------|
| MySQL `read_timeout` | 30s | 单次 SQL 执行超过 30s 自动断开 |
| MySQL `connect_timeout` | 10s | 连接建立超时 |
| 后端异常处理 | HTTP 504 | 捕获 `OperationalError(2013)` 返回 504 + 中文提示 |
| 前端 UI | 红色 banner | 显示"查询超时，请缩小时间范围后重试"，可关闭 |
