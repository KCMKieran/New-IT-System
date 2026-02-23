# IB 报表页面设计文档 (IB Report Design)

## 1. 概述
IB 报表主要用于展示各业务组别（Group）及其下属用户的资金往来、交易量及各项成本明细。该页面支持高度定制的筛选条件、月度数据比对以及高性能的表格展示。

## 2. 页面排布 (UI Layout)

页面遵循 `ClientPnLAnalysis.tsx` 的经典布局，分为两大部分：

### 2.1 顶部筛选卡片 (Filter Card)
- **简洁布局**: 移除冗余标签，参考 `ClientPnLAnalysis.tsx` 与 client-return-rate 页保持单行对齐，响应式（小屏纵向、大屏横向）。
- **时间范围筛选 (Date Range)**: 宽度 260px（sm 及以上），高度 h-9，支持选择起始和结束日期；日期格式为 `yyyy-MM-dd`。
- **时间快选 (Quick Range)**: Select 下拉，占位「时间快选」，选项：过去 1 周、过去 1 个月、本月、上月；与 client-return-rate 页面 UI 一致，宽度 `w-full sm:w-[130px]`，选择后自动更新日期并触发查询；若用户从日历选自定义范围则清空时间快选。
- **组别筛选 (Group Filter)**: 宽度 260px，支持从数据库动态加载的组别多选。
- **展示当月数据开关 (Monthly Data Checkbox)**: 
    - 勾选后，表格在展示指定时间段数据的基础上，额外展示该时间段所属月份的全月数据。
- **操作按钮组**:
    - **查询按钮 (Search)**: 宽度 140px，触发后端数据抓取。
    - **图表按钮 (Chart)**: 宽度 140px，预留接口用于展示趋势分析。

### 2.2 下方数据表格 (AG-Grid Table)
- **汇总行 (Totals Row)**: 位于表格顶部（Pinned Top），实时展示所有组别的各项指标总和。
- **双行展示逻辑 (Double-Row Display)**:
    - 每个单元格（Cell）内部展示两行数据：
        - 第一行：选定时间范围（Selected Range）内的统计值。
        - 第二行：当月全月（Monthly Total）的统计值。
    - **排序规则**: 点击表头排序时，系统应**仅以第一行（时间范围内的数据）**作为排序依据。
- **核心列定义 (Columns)**:
    1. **组别 (Group)**
    2. **用户名称 (User Name)**
    3. **时间段 (Time Range)**
    4. **入金 (Deposit USD)**
    5. **出金 (Withdrawal USD)**
    6. **IB 出金 (IB Withdrawal USD)**
    7. **净入金 (Net Deposit USD)**
    8. **平仓交易量 (Closed Volume lots)**
    9. **交易调整 (Trade Adjustments)**: 目前统一设置为 `0`（待团队确定）。
    - *待扩展列*: 佣金 (Commission)、IB 佣金 (IB Commission)、平仓利息 (Swap)、平仓盈亏 (Profit)、当天新开客户、当天新开代理。

---

## 3. 技术专项：IB 报表组别动态管理方案

### 3.1 需求背景
为了解决前端硬编码组别导致的维护困难，并提供实时的组别用户量统计，设计此动态加载与缓存方案。

### 3.2 后端设计 (ClickHouse + Python Cache)
- **数据源**：
    - 组别定义：`"KCM_fxbackoffice"."fxbackoffice_tags"` (categoryId = 6)
    - 用户关联：`"KCM_fxbackoffice"."fxbackoffice_user_tags"`
- **缓存策略**：
    - 使用 Python 内存对象缓存查询结果。
    - **有效期**：7 天。
    - **数据结构**：包含 `tag_id`, `tag_name`, `user_count`, `last_update_time`, `previous_update_time`。
- **性能优化**：通过 `GROUP BY tagId` 一次性完成所有组别的人数统计，避免 N+1 查询。

### 3.3 前端设计 (React + shadcn/ui)
- **交互方式**：
    - Popover 底部新增“查看所有组别”按钮。
    - 弹出 Dialog 展示所有组别的详细列表（名称、人数）。
- **持久化**：
    - “常用组别”存储于浏览器的 `localStorage` 中。
    - 用户可以在 Dialog 中通过“星标”快速切换常用状态。

### 3.4 交互逻辑详解 (Filtering Logic)

#### 1. 快捷选择器 (Popover Dropdown)
- **展示内容**：显示“常用组别”与“当前已选中组别”的**并集**。这意味着任何在全量弹窗中勾选的组别，都会自动出现在快捷菜单中。
- **视觉标识**：
    - **金星图标**：标识该组别为“常用”，通过点击组别名旁的星标切换。
    - **蓝色高亮**：标识该组别当前已被选中参与报表计算。
- **按钮逻辑**：
    - **清空**：一键清空所有已选组别（`selectedGroups = []`）。
    - **全选常用**：快速选中所有被标记为“常用”的组别。
    - **查看所有组别**：打开全量详情弹窗。

#### 2. 全量详情弹窗 (Dialog Overview)
- **实时同步**：弹窗内的选择状态与主页面报表状态实时联动。在弹窗中勾选 `CheckSquare`，报表数据会同步变化。
- **元数据展示**：
    - **MT Server Time**：显示数据最后一次从 MetaTrader 服务器同步的时间（数据源更新时间）。
    - **数据状态**：显示“数据更新于：时间 (上一次：时间)”，若无历史记录则显示 N/A。
- **搜索过滤**：支持对 60+ 个组别进行前端实时文本检索。
- **大小写兼容**：所有匹配逻辑（选中、收藏、过滤）均采用 `toLowerCase()` 处理，自动兼容数据库与前端可能存在的大小写差异（如 `HZL` vs `hzl`）。

---

## 4. 后端数据聚合方案 (Backend Data Aggregation)

### 4.1 核心设计理念
为实现高性能的“老板视角”汇总报表，采用以下策略：
1.  **组别为核心**：不再查询成千上万的用户明细，而是将数据聚合到“组别 (Group)”维度。
2.  **单次扫描 (One-Pass Scan)**：在一个 SQL 查询中利用 `sumIf` 同时计算“选定时间范围 (Range)”和“当月全月 (Month)”的数据，避免二次查询。
3.  **分层聚合 (Layered Aggregation)**：使用 CTE (`WITH` 子句) 分别处理资金流水、交易统计和 IB 佣金，最后进行 Join。

### 4.2 关键业务逻辑映射
| 业务指标 | 数据源表 | 过滤/计算逻辑 | 备注 |
| :--- | :--- | :--- | :--- |
| **入金/出金** | `fxbackoffice_transactions` | `status='approved'`, `type IN ('deposit', 'withdrawal')` | 美分账户需 `/100` |
| **IB 出金** | `fxbackoffice_transactions` | `type = 'ib withdrawal'` | 负数表示出金 |
| **净入金** | (计算字段) | `Deposit + Withdrawal + IB_Withdrawal` | 均为代数和相加 |
| **交易量** | `fxbackoffice_mt4_trades` | `lots` 字段, `CMD IN (0, 1)` | 美分账户需 `/100` |
| **交易盈亏** | `fxbackoffice_mt4_trades` | `PROFIT + SWAPS + COMMISSION` | 客户净盈亏视角 |
| **IB 佣金** | `fxbackoffice_stats_ib_commissions_by_login_sid` | `fromLoginSid` 关联 `mt4_users` | 使用预聚合表提速 |

### 4.3 生产环境 SQL 示例 (ClickHouse)
此 SQL 已通过验证，解决了 Compound ID (`SID-LOGIN`) 关联问题及类型匹配问题。

```sql
WITH
    -- [1] 参数定义 (由后端动态注入)
    toDateTime64(%(r_start)s, 6) AS r_start,
    toDateTime64(%(r_end)s, 6) AS r_end,
    toDateTime64(%(m_start)s, 6) AS m_start,
    toDateTime64(%(m_end)s, 6) AS m_end,
    toDate32(%(r_start)s) AS r_date_start,
    toDate32(%(r_end)s) AS r_date_end,
    toDate32(%(m_start)s) AS m_date_start,
    toDate32(%(m_end)s) AS m_date_end,
    %(target_groups)s AS target_groups, -- e.g. ['HZL']

    -- [2] 组别映射: 找到目标组别下的所有 User ID
    group_mapping AS (
        SELECT
            t.tag AS group_name,
            ut.userId AS user_id
        FROM "KCM_fxbackoffice"."fxbackoffice_tags" t
        JOIN "KCM_fxbackoffice"."fxbackoffice_user_tags" ut ON t.id = ut.tagId
        WHERE t.categoryId = 6
          AND has(arrayMap(x -> lower(x), target_groups), lower(t.tag))
    ),

    -- [3] 资金统计: Transactions 表
    money_stats AS (
        SELECT
            gm.group_name,
            -- Range Stats
            sumIf(if(upper(tr.processedCurrency) = 'CEN', tr.processedAmount / 100, tr.processedAmount), tr.type = 'deposit' AND tr.processedAt BETWEEN r_start AND r_end) AS deposit_range,
            sumIf(if(upper(tr.processedCurrency) = 'CEN', tr.processedAmount / 100, tr.processedAmount), tr.type = 'withdrawal' AND tr.processedAt BETWEEN r_start AND r_end) AS withdrawal_range,
            sumIf(if(upper(tr.processedCurrency) = 'CEN', tr.processedAmount / 100, tr.processedAmount), tr.type = 'ib withdrawal' AND tr.processedAt BETWEEN r_start AND r_end) AS ib_withdrawal_range,
            -- Month Stats
            sumIf(if(upper(tr.processedCurrency) = 'CEN', tr.processedAmount / 100, tr.processedAmount), tr.type = 'deposit' AND tr.processedAt BETWEEN m_start AND m_end) AS deposit_month,
            sumIf(if(upper(tr.processedCurrency) = 'CEN', tr.processedAmount / 100, tr.processedAmount), tr.type = 'withdrawal' AND tr.processedAt BETWEEN m_start AND m_end) AS withdrawal_month,
            sumIf(if(upper(tr.processedCurrency) = 'CEN', tr.processedAmount / 100, tr.processedAmount), tr.type = 'ib withdrawal' AND tr.processedAt BETWEEN m_start AND m_end) AS ib_withdrawal_month
        FROM "KCM_fxbackoffice"."fxbackoffice_transactions" tr
        INNER JOIN group_mapping gm ON tr.fromUserId = gm.user_id
        WHERE tr.status = 'approved' 
          AND tr.type IN ('deposit', 'withdrawal', 'ib withdrawal')
          AND tr.processedAt >= m_start
        GROUP BY gm.group_name
    ),

    -- [4] 交易统计: MT4 Trades 表
    trade_stats AS (
        SELECT
            gm.group_name,
            -- Range Stats
            sumIf(t.lots / if(mu.CURRENCY = 'CEN', 100, 1), t.CLOSE_TIME BETWEEN r_start AND r_end) AS volume_range,
            sumIf((t.PROFIT + t.SWAPS + t.COMMISSION) / if(mu.CURRENCY = 'CEN', 100, 1), t.CLOSE_TIME BETWEEN r_start AND r_end) AS net_profit_range,
            sumIf(t.COMMISSION / if(mu.CURRENCY = 'CEN', 100, 1), t.CLOSE_TIME BETWEEN r_start AND r_end) AS commission_range,
            -- Month Stats
            sumIf(t.lots / if(mu.CURRENCY = 'CEN', 100, 1), t.CLOSE_TIME BETWEEN m_start AND m_end) AS volume_month,
            sumIf((t.PROFIT + t.SWAPS + t.COMMISSION) / if(mu.CURRENCY = 'CEN', 100, 1), t.CLOSE_TIME BETWEEN m_start AND m_end) AS net_profit_month,
            sumIf(t.COMMISSION / if(mu.CURRENCY = 'CEN', 100, 1), t.CLOSE_TIME BETWEEN m_start AND m_end) AS commission_month
        FROM "KCM_fxbackoffice"."fxbackoffice_mt4_trades" t
        INNER JOIN "KCM_fxbackoffice"."fxbackoffice_mt4_users" mu ON t.LOGIN = mu.LOGIN
        INNER JOIN group_mapping gm ON mu.userId = gm.user_id
        WHERE t.CMD IN (0, 1) AND t.CLOSE_TIME >= m_start
        GROUP BY gm.group_name
    ),

    -- [5] IB佣金统计: Stats 预聚合表
    ib_commission_stats AS (
        SELECT
            gm.group_name,
            -- Range Stats
            sumIf(st.commission / if(upper(st.currency) = 'CEN', 100, 1), st.date BETWEEN r_date_start AND r_date_end) AS ib_commission_range,
            -- Month Stats
            sumIf(st.commission / if(upper(st.currency) = 'CEN', 100, 1), st.date BETWEEN m_date_start AND m_date_end) AS ib_commission_month
        FROM "KCM_fxbackoffice"."fxbackoffice_stats_ib_commissions_by_login_sid" st
        -- Fix: Split SID-LOGIN format (e.g., '1-8522845') and match with mu.LOGIN
        INNER JOIN "KCM_fxbackoffice"."fxbackoffice_mt4_users" mu 
            ON splitByChar('-', st.fromLoginSid)[2] = toString(mu.LOGIN)
        INNER JOIN group_mapping gm ON mu.userId = gm.user_id
        WHERE st.date >= m_date_start
        GROUP BY gm.group_name
    )

-- [6] 最终输出 (Result Set)
SELECT
    coalesce(m.group_name, t.group_name, i.group_name) AS group,
    
    round(coalesce(m.deposit_range, 0), 2) AS deposit_range,
    round(coalesce(m.deposit_month, 0), 2) AS deposit_month,
    
    round(coalesce(m.withdrawal_range, 0), 2) AS withdrawal_range,
    round(coalesce(m.withdrawal_month, 0), 2) AS withdrawal_month,
    
    round(coalesce(m.ib_withdrawal_range, 0), 2) AS ib_withdrawal_range,
    round(coalesce(m.ib_withdrawal_month, 0), 2) AS ib_withdrawal_month,
    
    -- Net Deposit = D + W + IBW (Arithmetic Sum)
    round(coalesce(m.deposit_range, 0) + coalesce(m.withdrawal_range, 0) + coalesce(m.ib_withdrawal_range, 0), 2) AS net_deposit_range,
    round(coalesce(m.deposit_month, 0) + coalesce(m.withdrawal_month, 0) + coalesce(m.ib_withdrawal_month, 0), 2) AS net_deposit_month,
    
    round(coalesce(t.volume_range, 0), 2) AS volume_range,
    round(coalesce(t.volume_month, 0), 2) AS volume_month,
    
    round(coalesce(t.net_profit_range, 0), 2) AS profit_range,
    round(coalesce(t.net_profit_month, 0), 2) AS profit_month,
    
    round(coalesce(t.commission_range, 0), 2) AS commission_range,
    round(coalesce(t.commission_month, 0), 2) AS commission_month,
    
    round(coalesce(i.ib_commission_range, 0), 2) AS ib_commission_range,
    round(coalesce(i.ib_commission_month, 0), 2) AS ib_commission_month

FROM money_stats m
FULL OUTER JOIN trade_stats t ON m.group_name = t.group_name
FULL OUTER JOIN ib_commission_stats i ON coalesce(m.group_name, t.group_name) = i.group_name
ORDER BY deposit_range DESC
```

---

## 5. 待办事项：结束 Mock 阶段 (Next Steps)
目前报表主体数据处于 Mock 阶段（读取本地 `ib_report_mock.csv`），后续需执行以下步骤实现生产切换：

1.  **后端 SQL 补全**：在 `clickhouse_service.py` 中编写真实的报表聚合 SQL，替代现有的模拟逻辑。
2.  **API 联调**：将前端 `handleSearch` 函数中的 `fetch` 地址由 `.csv` 路径更改为正式的后端 API 接口。
3.  **参数传递**：确保前端将 `date_range` (开始/结束日期) 和 `selectedGroups` (已选组别列表) 作为请求参数发送至后端。

## 6. 开发路线图 (Roadmap)
- [x] 前端：构建筛选卡片 UI（已参考 ClientPnLAnalysis 优化，保持简洁）。
- [x] 前端：集成 AG-Grid 并配置汇总行（Pinned Top Row）。
- [x] 前端：实现双行展示逻辑（Selected Range vs Monthly Total）。
- [x] 前端：实现动态组别加载与“查看所有组别”弹窗。
- [x] **后端：完成 ClickHouse 聚合 SQL 验证 (Group-Level Aggregation)。**
- [ ] 后端：在 Python 服务层封装 SQL 查询接口。
- [ ] 联调：前后端 API 对接。
- [ ] 联调：测试大数据量下的排序与筛选性能。
- [ ] 扩展：增加预留的趋势图表功能。
