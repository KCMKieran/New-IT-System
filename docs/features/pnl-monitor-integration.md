# Demo 盈亏监控 (Preview) 功能集成总结

本文档汇总了 "Demo 盈亏监控 (Preview)" 功能的开发背景、技术实现、文件变动及配置说明。该功能旨在提供一个基于 ClickHouse 数据库的即席查询（Ad-hoc Query）页面，用于分析指定时间范围内的客户交易盈亏情况。

## 1. 功能概述

*   **目标**：快速集成 ClickHouse 数据库，提供实时的客户盈亏分析报表。
*   **特性**：
    *   **即席查询**：用户可选择任意时间范围（如过去1周、1月）。
    *   **直连 ClickHouse**：后端绕过 ETL 流程，直接查询 ClickHouse 聚合数据。
    *   **性能可视化**：前端实时展示 ClickHouse 查询耗时、扫描行数及数据量，验证高性能优势。
    *   **预览版限制**：为了演示目的，查询结束日期被强制锁定（2025-12-27），以匹配数据库中的静态/测试数据。
    *   **UI 一致性**：严格复用现有系统的 AG Grid 表格与 Shadcn UI 风格（斑马纹、深色表头等）。

## 2. 文件清单

### 后端 (Backend - FastAPI)

| 文件路径 | 类型 | 用途 |
| :--- | :--- | :--- |
| `backend/app/services/clickhouse_service.py` | **新增** | 核心服务层。使用 `client.query()` 获取数据及 `summary` 元数据（耗时/扫描量）。 |
| `backend/app/api/v1/routes/client_pnl_analysis.py` | **新增** | API 路由层。返回结构包含 `data` 和 `statistics` 字段。 |
| `backend/app/api/v1/routers.py` | 修改 | 注册新的 API 路由模块。 |
| `backend/requirements.txt` | 修改 | 添加 `clickhouse-connect` 依赖。 |

### 前端 (Frontend - React)

| 文件路径 | 类型 | 用途 |
| :--- | :--- | :--- |
| `frontend/src/pages/ClientPnLAnalysis.tsx` | **新增** | 页面组件。集成 DateRangePicker、Select 互斥筛选、性能统计条及 Shadcn 风格表格。 |
| `frontend/src/components/app-sidebar.tsx` | 修改 | 添加侧边栏菜单项 "Demo 盈亏监控 (Preview)"。 |
| `frontend/src/App.tsx` | 修改 | 注册前端路由 `/client-pnl-analysis`。 |

## 3. 技术架构与数据流

```mermaid
graph LR
    User[用户] -->|1. 选择日期范围/快速筛选| FE[前端页面 (React)]
    FE -->|2. GET /api/v1/client-pnl-analysis/query| BE[后端 API (FastAPI)]
    BE -->|3. 调用 ClickHouseService| Service[Service 层]
    Service -->|4. SQL Query (SSL/8443)| DB[(ClickHouse Cloud)]
    DB -->|5. 返回聚合数据 + Summary| Service
    Service -->|6. 提取 elapsed_ns/read_rows| BE
    BE -->|7. JSON Response (Data + Stats)| FE
    FE -->|8. 渲染表格 & 性能统计条| User
```

## 4. 环境配置

需要在 `backend/.env` 中配置 ClickHouse 连接信息：

```ini
# ClickHouse Database Configuration
CLICKHOUSE_HOST=your-clickhouse-host.clickhouse.cloud
CLICKHOUSE_PORT=8443
CLICKHOUSE_USER=default
CLICKHOUSE_PASSWORD=your_password
CLICKHOUSE_DB=KCM_fxbackoffice
```

**依赖安装**：
确保后端环境安装了驱动：
```bash
pip install clickhouse-connect
```

## 5. 前端逻辑详解

### 页面入口
*   **路由**：`/client-pnl-analysis`
*   **菜单**：Sidebar -> Risk Control -> Demo 盈亏监控 (Preview)

### 交互逻辑
1.  **初始状态**：进入页面时不自动加载数据，显示占位提示。
2.  **Banner 提示**：顶部黄色警告条，提示"快速预览版 — 当前演示数据截止至 2025-12-27"。
3.  **互斥筛选**：
    *   **自定义日期 (Calendar)**：用户选择具体日期范围时，清空快速筛选。
    *   **快速筛选 (Select)**：用户选择"过去1周"等选项时，清空自定义日期，并显示具体日期范围 (如 `2025-12-20 ~ 2025-12-27`)。
    *   **基准时间**：所有快速筛选基于 `2025-12-27` 倒推。
4.  **性能统计**：
    *   查询成功后，在表格上方显示统计条：`⏱️ 0.58s | 📊 Read: 6.35M rows | 💾 363 MB`。
5.  **表格展示**：
    *   **Shadcn 风格**：深色表头 (Light模式) / 浅色表头 (Dark模式)，极浅色斑马纹背景。
    *   **列信息**：Client ID, Name, Trades, Volume, PnL (红/绿), Commission, Swap。
6.  **数据展示增强 (v2新增)**：
    *   **服务器映射**：基于 `sid` 映射显示 (1->MT4, 5->MT5, 6->MT4Live2)。
    *   **视觉区分**：币种使用不同颜色 Badge；交易盈亏（浅灰）与净盈亏（浅橙）添加背景色区分。
    *   **CRM 跳转**：点击 Client ID 或 Account 可直接跳转至 CRM 详情页。
    *   **分页控制**：移除后端 1000 条限制，改为前端全量分页（Page Size: 50/100/500），表格高度扩展至 750px。

## 6. 后端逻辑详解

### Service 层 (`clickhouse_service.py`)
*   **连接管理**：使用 `clickhouse_connect`，强制开启 `secure=True` (TLS)。
*   **查询执行**：使用 `client.query()` 替代 `query_df`，以获取 `result.summary` 中的性能元数据。
*   **性能统计**：优先读取 `elapsed_ns` 并转换为秒 (float)，确保高精度显示；同时提取 `read_rows` 和 `read_bytes`。
*   **核心计算逻辑 (v2更新)**：
    *   **CEN 账户标准化**：如果账户币种 (`CURRENCY`) 为 `CEN`，则以下字段在 SQL 层自动除以 100：`lots`, `profit`, `swaps`, `commission` (交易佣金), `net_deposit`。
    *   **IB 佣金特例**：IB 佣金 (`total_ib_cost`) 取自 `ib_processed_tickets.commission` 字段，**不**执行 CEN 除以 100 操作（维持原值）。
    *   **搜索策略**：为了提升大数据量下的性能，搜索功能已优化为仅针对 `Client ID` 和 `Account ID` 的**前缀匹配** (Prefix Match)，移除了 Name 的模糊搜索。

### API 层 (`client_pnl_analysis.py`)
*   **接口**：`GET /query`
*   **响应结构**：
    ```json
    {
      "ok": true,
      "data": [...],
      "statistics": {
        "elapsed": 0.575,
        "rows_read": 128000,
        "bytes_read": 129000000
      },
      "count": 100
    }
    ```

## 7. 数据库 SQL 逻辑摘要

```sql
WITH ib_costs AS (
    SELECT ticketSid, sum(commission) AS total_ib_cost
    FROM fxbackoffice_ib_processed_tickets
    -- ...
    GROUP BY ticketSid
)
SELECT
    t.LOGIN AS account,
    m.userId AS client_id,
    any(m.NAME) AS client_name,
    any(m.sid) AS sid, -- 用于前端映射服务器
    any(m.CURRENCY) AS currency,
    -- ... Group, Zipcode ...

    -- CEN 账户除以 100 逻辑示例
    sumIf(t.lots, t.CMD IN (0, 1)) / if(any(m.CURRENCY) = 'CEN', 100, 1) AS total_volume_lots,
    sumIf(t.PROFIT, t.CMD IN (0, 1)) / if(any(m.CURRENCY) = 'CEN', 100, 1) AS trade_profit_usd,

    -- IB 佣金不除以 100
    COALESCE(sum(ib.total_ib_cost), 0) AS ib_commission_usd
FROM fxbackoffice_mt4_trades AS t
-- ... JOINS ...
WHERE 
    t.CLOSE_TIME >= %(start_date)s 
    AND t.CLOSE_TIME <= %(end_date)s
    AND t.CMD IN (0, 1, 6)
-- ... GROUP BY & ORDER BY ...
```

## 8. V2 迭代更新日志与技术修正 (2025-12-19)

### 后端变更
1.  **SQL 重构**：
    *   拆分 `total_profit_usd` 为 `trade_profit_usd` (纯交易盈亏) + `commission_usd` (交易佣金) + `swap_usd` (库存费)。
    *   新增 `ib_commission_usd` (IB成本)，数据源从 `calculatedCommission` 更正为 `commission`。
    *   新增字段：`sid` (Server ID), `currency`, `group`, `zipcode`, `account`。
2.  **业务逻辑**：
    *   实现 **CEN 账户标准化**：检测 `CURRENCY='CEN'` 时，除 IB 佣金外的金额/手数自动 `/100`。
    *   实现 **净收入计算**：`Broker Net Revenue` = `(交易盈亏_adj * -1) - IB佣金_raw`。
3.  **性能优化**：
    *   移除 `LIMIT 1000` 限制，支持全量数据拉取。
    *   搜索功能优化为 `LIKE '123%'` 前缀匹配，仅限 ID 搜索，移除低效的 Name 模糊匹配。

### 前端变更
1.  **交互升级**：
    *   新增 **客户端分页** (Client-side Pagination)，支持 50/100/500 条/页切换。
    *   新增 **CRM 跳转链接**：点击 Client ID 或 Account 可直接跳转 CRM 系统。
2.  **UI/UX 优化**：
    *   **列顺序调整**：Client -> Account -> Trading -> Financials。
    *   **样式增强**：
        *   交易盈亏列（浅灰底色）、净盈亏列（浅橙底色）。
        *   币种列使用 Badge 区分 (CEN=红, USD=青, USDT=紫)。
        *   IB 佣金列蓝色加粗。
    *   **新列**：前端计算并展示 `Net PnL (w/ Comm)`。
3.  **Bug 修复**：
    *   修复 `ib_commission_usd` 列因数据类型导致的排序失效问题（添加 `comparator`）。
    *   修复分页逻辑引入时的 `useEffect` 引用缺失问题。
