# IB Report 与 Client PnL Analysis 页面分析及统一建议

> 分析范围：前后端实现、ClickHouse 数据源差异、统一到 KCM_fxbackoffice 的方案、前端 UI 一致性建议。**本文档仅做分析，不涉及代码修改。**

---

## 1. 如何查看 KCM_fxbackoffice 下的所有表

在已配置好 **KCM_fxbackoffice_prod** 的 ClickHouse 客户端中，对 database **KCM_fxbackoffice** 执行：

```sql
-- 方法一：列出当前库所有表（先 USE database 或带库名）
SHOW TABLES FROM KCM_fxbackoffice;

-- 方法二：从系统表查（可同时看引擎、行数等）
SELECT name, engine, total_rows, total_bytes
FROM system.tables
WHERE database = 'KCM_fxbackoffice'
ORDER BY name;

-- 方法三：仅表名列表（便于复制对比）
SELECT name FROM system.tables WHERE database = 'KCM_fxbackoffice' ORDER BY name;
```

**建议**：用方法二跑一次，把结果（表名 + engine）保存下来，便于和后文「依赖表清单」做对比，确认 CDC 后是否都已在 KCM_fxbackoffice 中存在。

---

## 2. 两个页面对应的前后端入口

| 页面 | 前端路由 | 前端组件 | 后端路由前缀 | 主要 Service 方法 |
|------|----------|----------|--------------|-------------------|
| IB Report | `/ib-report` | `IBReport.tsx` | `/api/v1/ib-report` | `get_ib_groups`, `get_ib_report_data` |
| Client PnL Analysis | `/client-pnl-analysis` | `ClientPnLAnalysis.tsx` | `/api/v1/client-pnl-analysis` | `get_pnl_analysis` |

---

## 3. 后端 ClickHouse 使用现状

### 3.1 连接与库配置（`clickhouse_service.py`）

- **默认连接（use_prod=False）**
  - Host/User/Password：`CLICKHOUSE_HOST` / `CLICKHOUSE_USER` / `CLICKHOUSE_PASSWORD`
  - **Database**：`CLICKHOUSE_DB`，未设置时默认 **`Fxbo_Trades`**
  - 用于：client_return 等
- **生产连接（use_prod=True）**
  - Host/User/Password：`CLICKHOUSE_prod_HOST` / `CLICKHOUSE_prod_USER` / `CLICKHOUSE_prod_PASSWORD`
  - **Database**：代码写死 **`KCM_fxbackoffice`**（CDC 目标库）
  - 用于：IB Report、Client PnL Analysis

### 3.2 各接口实际使用的连接与库（当前）

| 接口 | Service 方法 | 使用的连接 | 实际 Database |
|------|--------------|------------|----------------|
| `GET /api/v1/ib-report/groups` | `get_ib_groups()` | `get_client(use_prod=True)` | **KCM_fxbackoffice** |
| `POST /api/v1/ib-report/search` | `get_ib_report_data()` | `get_client(use_prod=True)` | **KCM_fxbackoffice** |
| `GET /api/v1/client-pnl-analysis/query` | `get_pnl_analysis()` | `get_client(use_prod=True)` | **KCM_fxbackoffice** |
| client_return_rate | — | **已迁移至 MySQL** (`fxbackoffice` slave via pymysql) | **fxbackoffice** (MySQL) |

结论：**IB Report 与 Client PnL Analysis 使用 prod 连接（KCM_fxbackoffice）。** 部署时需配置 `CLICKHOUSE_prod_*` 指向 CDC 所在集群。

---

## 4. 两套 SQL 依赖的表清单（用于对照 CDC 后库内是否有表）

### 4.1 IB Report（当前已用 KCM_fxbackoffice）

以下表需在 **KCM_fxbackoffice** 中存在：

| 表名 | 用途 |
|------|------|
| `fxbackoffice_tags` | 组别/标签定义（categoryId=6 为 IB 组） |
| `fxbackoffice_user_tags` | 用户–标签映射 |
| `fxbackoffice_transactions` | 入金/出金/IB 出金（deposit, withdrawal, ib withdrawal） |
| `fxbackoffice_mt4_trades` | MT4 成交（手数、盈亏、佣金、swap） |
| `fxbackoffice_mt4_users` | MT4 账户与 userId 等 |
| `fxbackoffice_stats_ib_commissions_by_login_sid` | IB 佣金按 login_sid 预聚合 |

### 4.2 Client PnL Analysis（已统一到 KCM_fxbackoffice）

以下表需在 **KCM_fxbackoffice** 中存在：

| 表名 | 用途 | 备注 |
|------|------|------|
| `fxbackoffice_ib_processed_tickets` | 按 ticket 汇总的 IB 成本（commission） | 已在表清单中 |
| `fxbackoffice_mt4_trades` / `fxbackoffice_mt4_users` / `fxbackoffice_users` | 交易、用户、CRM | 已在表清单中 |
| `ib_downline_net_deposit_agg` | IB 旗下净入金汇总（sumMerge） | **当前表清单中无此表**；可用 `CLICKHOUSE_IB_NET_DEPOSIT_AGG_TABLE` 指定实际表名，或置空则 JOIN 跳过、ib_net_deposit 为 0 |

**建议**：若库中有同名或等价表，在 .env 中设置 `CLICKHOUSE_IB_NET_DEPOSIT_AGG_TABLE=实际表名`；若无，可不设置（或设为空），报表照常、仅 IB 净入金列为 0。

---

## 5. 其他使用 ClickHouse 的模块（统一 DB 时需一并考虑）

- **client_return_service.py**：**已迁移至 MySQL**（`fxbackoffice` slave），不再使用 ClickHouse。采用两阶段查询：Phase 1 从 `mt4_trades` 获取活跃客户，Phase 2 用 `stats_transactions` 查净入金。

---

## 6. 统一后端 DB 到 KCM_fxbackoffice 的建议（仅方案，不改代码）

### 6.1 目标

- 两个页面（及依赖 ClickHouse 的其他接口）都从 **KCM_fxbackoffice** 读数据，与 CDC pipeline（KCM_fxbackoffice_prod / KCM_fxbackoffice）一致。

### 6.2 方案 A：仅把 Client PnL 改为 prod 连接（最小改动）

- 在 `get_pnl_analysis()` 中改为使用 `get_client(use_prod=True)`，与 IB Report 一致。
- **前提**：第 4.2 节所列 5 张表在 KCM_fxbackoffice 中均存在且结构兼容（尤其 `ib_downline_net_deposit_agg` 的 sumMerge 用法）。
- **优点**：改点少，风险集中在一处。  
- **注意**：client_return 已迁移至 MySQL slave，不再使用 Fxbo_Trades。

### 6.3 方案 B：默认连接即指向 KCM_fxbackoffice（推荐中长期）

- 环境变量：设置 `CLICKHOUSE_DB=KCM_fxbackoffice`，或让默认连接使用的 host 直接指向 CDC 所在集群（与 prod 同库或同实例均可）。
- 代码：逐步把 `get_client(use_prod=True)` 改为 `get_client()`，最终只保留一套连接配置，避免「默认 / prod 两套库」长期并存。
- **前提**：所有当前读 Fxbo_Trades 的 SQL（Client PnL 等）在 KCM_fxbackoffice 中都有对应表且数据正确。（client_return 已迁移至 MySQL，不受影响。）

### 6.4 实施前必做

1. 用第 1 节 SQL 导出 **KCM_fxbackoffice** 全表列表（建议带 engine/行数）。
2. 核对第 4.1、4.2 节表是否都存在；若存在但名不同，列出一张「旧表名 → 新表名」映射表。
3. 对关键表（如 `fxbackoffice_mt4_trades`、`fxbackoffice_users`、`ib_downline_net_deposit_agg`）在 KCM_fxbackoffice 中做一次抽样查询，确认字段与现有 SQL 兼容（尤其是时间字段、金额、Merge 聚合列）。
4. 确认 CDC 延迟可接受（例如报表按日/按小时即可，则分钟级延迟一般足够）。

---

## 7. 前端 UI 一致性建议（仅分析与建议）

### 7.1 共同点（可视为已有基础）

- 都使用：日期范围选择（DateRange + Popover）、AG Grid 表格、筛选/列显控制（DropdownMenu + Checkbox）、本地存储（grid state、设置）。
- 都偏向「分析/报表」场景，而非强交互编辑。

### 7.2 差异与可统一点

| 维度 | IB Report | Client PnL Analysis | 一致性建议 |
|------|-----------|----------------------|------------|
| 页面标题/副标题 | 有标题和说明 | 有标题、副标题、数据来源说明 | 统一「页面标题 + 简短说明」的排版和层级 |
| 日期选择位置与样式 | 日期在卡片内左侧 | 日期在卡片内 | 统一为同一侧（如都左上）和同一组件样式 |
| 主要操作按钮 | 查询、导出等 | 查询、重置等 | 统一「主按钮」「次按钮」顺序与命名（如都「查询」+「重置」） |
| 表格「合计行」 | 有 pinned 合计行 | 有合计行 | 统一 pinned 方式与合计行样式（颜色/加粗） |
| 列显示/隐藏 | DropdownMenu 多选 | 同类 | 可统一为同一组件或同一交互文案（如「列显示」） |
| 主题/深色模式 | 使用 theme | 使用 theme + i18n | 两页都接入 i18n，避免一处硬编码中文 |
| 错误/空状态 | 需确认 | 有 503/空数据提示 | 统一错误文案与空状态样式（如同一 Empty 组件） |
| 加载状态 | 需确认 | 有 loading | 统一 loading 样式（如同一 Spinner/骨架） |

### 7.3 建议的 UI 统一原则（实施时再改代码）

1. **布局**：筛选区（日期 + 业务筛选）在上，表格在下；筛选区用同一 Card 或同一栅格规范。
2. **表格**：统一 AG Grid 主题（如 alpine）、表头样式、数字列右对齐、金额小数位与千分位格式。
3. **国际化**：Client PnL 已用 `useI18n`；IB Report 建议也接入同一 i18n key 命名（如 `pages.ibReport.*`），便于两页文案风格一致。
4. **无障碍与提示**：两页都提供「数据时间范围说明」（如「数据来自 KCM_fxbackoffice，约延迟 X 分钟」），避免用户误以为实时库。

---

## 8. 小结

| 项目 | 结论 |
|------|------|
| 查看 KCM_fxbackoffice 所有表 | 使用 `SHOW TABLES FROM KCM_fxbackoffice` 或 `system.tables WHERE database = 'KCM_fxbackoffice'`，建议导出表名+engine 做对照 |
| 当前库使用 | IB Report、Client PnL 使用 **prod**（KCM_fxbackoffice）；client_return 已迁移至 **MySQL** slave（fxbackoffice） |
| 统一后端 | `ib_downline_net_deposit_agg` 可通过 `CLICKHOUSE_IB_NET_DEPOSIT_AGG_TABLE` 指定表名或置空跳过 |
| 前端一致 | IB Report 快捷日期与 client-return-rate 一致：Select 下拉「时间快选」（过去 1 周/1 个月/本月/上月），响应式布局 |

---

## 9. Redis 缓存分析

后端与 ClickHouse 相关的 Redis 缓存集中在 **`clickhouse_service`** 与 **`client_return_service`**，用于减轻重复查询压力并与 SingleFlight 配合防击穿。

### 9.1 Key 规范与用途

| Key 前缀 | 用途 | 生成方式 | TTL |
|----------|------|----------|-----|
| `app:pnl:cache:{md5}` | Client PnL Analysis 查询结果 | `pnl_v1_{start_date}_{end_date}_{search}` 的 MD5 | **1800s (30min)** |
| `app:ib_report:cache:{md5}` | IB Report 报表数据 | `ib_report_v1_{r_start}_{r_end}_{m_start}_{m_end}_{sorted_groups}` 的 MD5 | **600s (10min)** |
| `app:client_return:cache:{md5}` | 客户回报率查询（MySQL） | `client_return_v2_{month_start}_{month_end}_{search}_{deposit_bucket}_{sort_by}_{sort_order}_{page}_{page_size}` 的 MD5 | **1800s (30min)** |

- 命中时：接口直接返回缓存 JSON，并在 `statistics.from_cache` 中标记为 `true`（PnL / client_return）。
- 未命中：执行数据库查询（PnL 用 ClickHouse，client_return 用 MySQL），结果序列化后 `setex(key, ttl, json)` 写入 Redis。

### 9.2 非 Redis 缓存（内存）

| 缓存 | 位置 | 内容 | 有效期 |
|------|------|------|--------|
| IB 组别列表 | `ClickHouseService._group_cache` | `get_ib_groups()` 的完整返回（group_list、last_update_time 等） | **7 天**（内存，进程内） |

- 超过 7 天后下一次请求会重新查 ClickHouse 并刷新内存缓存。
- 与 SingleFlight 合用：同一时刻多请求只打一次 ClickHouse，其余等待共享结果。

### 9.3 SingleFlight 与缓存的关系

- **PnL**：同一 `cache_key` 的并发请求先经 SingleFlight 合并，只有第一个执行 ClickHouse 查询；查完后写 Redis，后续相同请求可直接 Redis 命中。
- **IB groups**：用 SingleFlight key `"ib_groups"`，多请求同时到达时只放行一次查库，结果写入内存缓存。
- **IB report**：无 SingleFlight，仅 Redis；相同日期范围+组别会共享 10 分钟缓存。

### 9.4 运维与排查

- **查看某类缓存 key**（示例）：  
  `redis-cli KEYS "app:pnl:cache:*"`、`KEYS "app:ib_report:cache:*"`、`KEYS "app:client_return:cache:*"`  
  生产慎用 `KEYS`，可用 `SCAN` 迭代。
- **清理 PnL 缓存**（便于看最新数据）：  
  `redis-cli KEYS "app:pnl:cache:*" | xargs redis-cli DEL`（或按需 SCAN+DEL）。
- **清理 IB report 缓存**：  
  `redis-cli KEYS "app:ib_report:cache:*" | xargs redis-cli DEL`
- **日志**：命中会打 `Redis cache hit for ...`，写入会打 `Redis cache saved ...`，便于确认是否走缓存。
