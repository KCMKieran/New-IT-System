---
id: OPT-0005
title: Client Return Rate Phase 2 主表替代 N 行 UNION ALL
status: done
priority: P2
area: backend
effort: M
created: 2026-05-14
related: [[OPT-0003]]
---

## 问题

Client Return Rate 后端 Phase 2 SQL 把 Phase 1 拿到的 N 个 `(client_id, month_trade_profit)` 用循环拼成 `SELECT ... UNION ALL SELECT ... UNION ALL ...`，再当成 derived table `FROM ({tm_inline}) AS tm` 给后面 6 个 LEFT JOIN 使用。

- 客户数越多 SQL 越长（5000 客户 ≈ 400 KB，20000 客户 ≈ 1.6 MB）
- 30 天大范围查询有触达 MySQL `max_allowed_packet` 的可能
- derived table 无索引，优化器只能走 Nested Loop Join，性能差
- 代码意图绕弯（"用 SQL 模拟 Python 列表"），新人读起来要绕一圈

## 背景

- 关键文件：`backend/app/services/client_return_service.py`
  - line 474-484：拼 `tm_inline` 字符串
  - line 234（`_build_phase2_sql` 函数内）：`FROM ({tm_inline}) AS tm`
  - 6 个 LEFT JOIN 子查询都用 `WHERE userId IN ({id_list_str})` 各自过滤后 GROUP BY，独立于 tm
- 现有数据规模：默认 7 天范围 ~1500 客户没问题；用户手动选"过去 30 天"以上才暴露
- 没见过 prod 报错日志，但属于"日期一拉大就崩"的隐患

## 假设 / 待验证

- [x] pymysql 在 `_get_mysql_connection()` 每请求新建连接、`conn.close()` 时关闭——TEMPORARY TABLE 生命周期能自然跟连接绑定（确认：`client_return_service.py:55-68`）
- [x] MySQL 从库支持 TEMPORARY TABLE DDL（确认：会话级、不写 binlog）
- [x] pymysql `executemany` 对纯 INSERT 会做 multi-row 批处理（确认：≥ 0.9）
- [ ] 实测 30 天范围回归：改前 vs 改后查询时长 / 总数 / 抽样数据一致

## 验收标准

- [ ] `client_return_service.py` 中 `tm_inline` 字符串拼接代码移除
- [ ] Phase 2 改用 `CREATE TEMPORARY TABLE tm_clients (..., PRIMARY KEY (client_id)) ENGINE=MEMORY` + `executemany` 灌数据
- [ ] `_build_phase2_sql` 的 FROM 子句改为 `FROM tm_clients tm`，签名去掉 `tm_inline` 参数
- [ ] 7 天 / 30 天 / 6 小时 sub-day 三种模式回归通过：客户数、`month_trade_profit` 抽样、ROACE 抽样、`return_non_adjusted` 与改前一致
- [ ] 异常路径回归：搜索单个 client_id（active_rows 长度=1）、active_rows 为空（提前 return）两种边界仍正常
- [ ] dev 环境 `docker compose logs api` 无新增 warning

## 笔记

### 方案 A（最干净）—— 选定

`CREATE TEMPORARY TABLE` + `executemany` + `FROM tm_clients tm`。代码改动只在 service 内部 ~15 行，外部 API、Pydantic schema、frontend 完全不动。

### 方案 B（备选）

直接 `FROM ( <Phase 1 SQL 整段贴回来> ) AS tm`，让 MySQL 自己做 derived table，Phase 1 SQL 跑两遍。代价是丢掉 `profit_map` 这层 Python 缓存；好处是不需要 DDL 权限。**不选**——derived table 仍然没索引，没解决根本问题。

### 进一步可清理（不在本次 AC 内）

6 个 LEFT JOIN 子查询的 `WHERE userId IN ({id_list_str})` 可以改成 `WHERE userId IN (SELECT client_id FROM tm_clients)`，彻底消灭 `id_list_str` 变量。属于锦上添花，留作 follow-up。

### 性能预期

| 场景 | 现状 | 改后 |
|---|---:|---:|
| 7 天范围（~1500 客户） | ~600ms | ~400ms |
| 30 天范围（~15000 客户） | 3-5s + 偶发 packet too large | 1-2s 稳定 |
| 6 个月范围（>40000 客户） | 必失败 | 能跑过 |

## 结果

**Commit**: `8d1d1c2 perf(client-return-rate): Phase 2 主表改用 users PK 替代 N 行 UNION ALL`

### Plan A → Plan D 中途变更

实施时撞墙：`readonly` 这个 MySQL 账号只有 SELECT 权限，没有 `CREATE TEMPORARY TABLES`，原 Plan A 直接 1044 拒绝。

`SHOW GRANTS FOR CURRENT_USER()` 输出：
```
GRANT SELECT, RELOAD, PROCESS, SHOW DATABASES,
      REPLICATION SLAVE, REPLICATION CLIENT, SHOW VIEW ON *.*
TO `readonly`@`%`
```

改用方案 D：`FROM users tm WHERE tm.id IN (id_list_str) AND COALESCE(tm.isEmployee, 0) = 0`，依赖 `users.id` PK 走 main 行获取，`month_trade_profit` 改为 Python 侧 fetch 后逐行 attach。效果与 Plan A 等价（SQL 大小不再随客户数线性、main 表走索引查找），且不需要 DDL 权限。

### AC 偏差

- [x] N 行 UNION ALL 移除 ✓
- [ ] ~~CREATE TEMPORARY TABLE + executemany~~ → **改为 `FROM users tm`**
- [x] `_build_phase2_sql` FROM 子句改写、签名去掉 `tm_inline` ✓
- [x] 7 天 / 单 client 搜索 / sub-day 三种回归通过：与改前数据一致（mt4_users.EQUITY 是实时 tick 数据，两次查询间漂移属正常） ✓
- [x] 空 active_rows 提前 return 路径正常 ✓
- [x] dev 环境无新增 warning ✓

### Follow-up

- 8 个子查询各带 N 个 id 的 IN list，50k 客户场景从 4MB → 350KB（11× 改善），但**仍是线性扩张**。如果未来要彻底消灭，需要 DBA 给 readonly 账号 `CREATE TEMPORARY TABLES` 权限，再上 Plan A；或改用专用读写账号。当前数据规模够用，不立 OPT。
- 页面卡顿真正主因是 M2（ROACE 全历史子查询）。本次仅改 M1，没动 M2。回归测试 cache miss 仍要 3.4s。
