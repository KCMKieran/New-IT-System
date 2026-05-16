---
id: OPT-0006
title: Client Return Rate ROACE 子查询预计算到 SQLite 快照
status: done
priority: P2
area: backend
effort: L
created: 2026-05-14
related: [[OPT-0005]]
---

## 问题

`client_return_service.py` Phase 2 里 ROACE 子查询每次 web 请求都跑：

```sql
LEFT JOIN (
  SELECT mu2.userId, SUM(...endingEquity...) / COUNT(DISTINCT sb.date) AS avg_daily_equity
  FROM mt4_users mu2
  INNER JOIN stats_balances sb ON sb.loginsid = mu2.loginsid       -- 19.4M 行
  INNER JOIN stats_trading st2 ON st2.loginSid = mu2.loginsid AND st2.date = sb.date
  WHERE mu2.userId IN (...上千个 id...)
    AND mu2.sid IN (1, 5, 6)
    AND mu2.`GROUP` NOT LIKE '%demo%'
    AND sb.endingEquity > 0
  GROUP BY mu2.userId
) AS ade ...
```

- 1500 客户实测 **2.0s**，占 Phase 2 总耗时约 60%
- 索引已是最优（EXPLAIN: userId range + IDX_ACCDATE ref + PRIMARY eq_ref），没有 SQL 层进一步压榨空间
- 24/12 个月日期 cutoff 实测节省 4%-25%，且会引入语义偏差（profit_hist 是全历史、divisor 改成限定窗口对老客户失真）—— **不可行**
- 平台客户基数还在涨，3 年后客户翻倍时这条查询会到 4-8s

## 背景

- ROACE = `profit_hist / avg_daily_equity × 100`，其中 `avg_daily_equity` 是全历史"活跃天 endingEquity 之和 ÷ 活跃天数"
- `avg_daily_equity` 数据变化非常慢：客户的"全历史日均净值"基本上每天只多一天的新数据，移动平均稳定
- 现有项目已有 APScheduler 框架（`backend/app/core/scheduler.py`、`burst_open_scheduler.py`、`login_ip_scheduler.py`）和成熟的 SQLite-as-cache 模式（`client_return_export_db.py`、`login_ip_db.py`）
- 现有 docs/features/client-return-rate.md 第 4.1 节明确说"`include_avg_equity=true` 时返回，全量页传 true"

## 假设 / 待验证

- [x] 全客户一次性算（无 IN 过滤）的 SQL 跑得动 —— 估算 1500 → 50k 线性外推 ~67s，可接受为夜间任务
- [x] SQLite 单表 ~50k 行规模无性能问题（项目已有先例：login_ip_db 等都几十万行）
- [ ] dev 环境 backend `data/` 目录可写（实测看 client_return_export.db 已经在用）
- [ ] APScheduler 启用条件不冲突现有 scheduler

## 验收标准

- [ ] 新增 `backend/app/core/client_roace_db.py`：SQLite schema + 批量 upsert + bulk read
- [ ] 新增 `backend/app/services/client_roace_refresh_service.py`：跑 MySQL 全客户 ROACE 查询、批量写 SQLite
- [ ] 新增 `backend/app/core/client_roace_scheduler.py`：APScheduler cron job 每天 06:00 HKT 跑一次刷新，env 开关默认 false
- [ ] 修改 `client_return_service.py`：Phase 2 SQL 删除 ROACE LEFT JOIN（`avg_equity_select` + `avg_equity_join` 完全移除），改为 fetch 后从 SQLite 批量查 attach 到每行
- [ ] 修改 `app/main.py`：lifespan 加 `init_client_roace_db()` + `start_client_roace_scheduler()`
- [ ] 新增 admin endpoint `POST /api/v1/client-return-rate/roace/refresh`：手动触发刷新（用于 backfill + 调试）
- [ ] 修改 `docs/features/client-return-rate.md`：更新 4.1 / 4.4 节说明新数据流向
- [ ] dev 环境冒烟通过：手动触发一次刷新 → API 查询返回 avg_daily_equity / return_on_avg_equity 正确
- [ ] 回归：默认 7 天查询 cache miss 耗时从 ~3.4s 降到 ~1s 以下
- [ ] 缓存命中保持原 50ms 速度

## 笔记

### 架构

```
        ┌──────────────────────────┐
        │  APScheduler 06:00 HKT  │
        │  client_roace_scheduler  │
        └─────────────┬────────────┘
                      │ trigger
                      ▼
        ┌──────────────────────────┐         ┌──────────────────────┐
        │  refresh_service         │ ←─SQL─→ │  MySQL slave         │
        │  - 跑全客户 ROACE 查询    │         │  fxbackoffice        │
        │  - 批量 upsert SQLite    │         │  stats_balances etc. │
        └─────────────┬────────────┘         └──────────────────────┘
                      │ upsert
                      ▼
        ┌──────────────────────────┐
        │  SQLite                  │
        │  client_roace.db         │
        │  roace_snapshot 表       │
        └─────────────┬────────────┘
                      │ bulk_get(user_ids)
                      ▼
        ┌──────────────────────────┐
        │  Web API 请求            │
        │  Phase 2 SQL（无 ROACE） │
        │  + Python 侧 attach      │
        └──────────────────────────┘
```

### SQLite schema

```sql
CREATE TABLE IF NOT EXISTS roace_snapshot (
    user_id          INTEGER PRIMARY KEY,
    avg_daily_equity REAL NOT NULL,
    active_days      INTEGER NOT NULL,
    refreshed_at     TEXT NOT NULL  -- ISO8601 HKT
);

CREATE TABLE IF NOT EXISTS roace_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
-- meta keys: last_full_refresh_at, last_refresh_duration_ms, last_refresh_rows
```

### 边界处理

- SQLite 里没有该客户记录（新客户、ETL 还没跑过）：`avg_daily_equity = None`、`return_on_avg_equity = None`（与现状语义一致：原 SQL 也是 LEFT JOIN，无数据则 NULL）
- SQLite 文件不存在：`init_client_roace_db` 在 startup 时创建空表，所有客户 ROACE 都 None 直到第一次刷新
- 第一次部署：管理员手动调一次 `POST /roace/refresh` 做 backfill，或等 06:00 HKT 自动触发

### env 开关

- `CLIENT_ROACE_SCHEDULER_ENABLED` — 默认 false（dev 不跑），prod compose 设 true
- `CLIENT_ROACE_REFRESH_HOUR` / `CLIENT_ROACE_REFRESH_MINUTE` — 默认 06:00 HKT

## 结果

**Commit**: `38b77a4 perf(client-return-rate): ROACE 子查询预计算到 SQLite 快照`

### 交付清单 vs AC

- [x] `backend/app/core/client_roace_db.py` — schema (roace_snapshot + roace_meta) + `bulk_get_roace` + `upsert_roace_batch` + meta 读写 + size 查询
- [x] `backend/app/services/client_roace_refresh_service.py` — `refresh_all_clients()`，read_timeout=600s，每 2000 行批量 upsert
- [x] `backend/app/core/client_roace_scheduler.py` — APScheduler cron HKT 06:00；threading.Lock 防 cron + 手动并发；env 开关默认 false
- [x] `client_return_service.py` — `_build_phase2_sql` 内 `avg_equity_select`/`avg_equity_join` 都置空（保留参数和占位符以备回滚）；service 入口 fetch 后 `bulk_get_roace` + 计算 ROACE 加到行上
- [x] `app/main.py` lifespan 加 `init_client_roace_db()` + `start_client_roace_scheduler()` / `stop_*`
- [x] `config.py` 加 3 个 env: `CLIENT_ROACE_SCHEDULER_ENABLED` / `CLIENT_ROACE_REFRESH_HOUR` / `CLIENT_ROACE_REFRESH_MINUTE`
- [x] admin endpoint `POST /api/v1/client-return-rate/roace/refresh` —— 同步阻塞返回 summary
- [x] `docs/features/client-return-rate.md` 第 4.1 节标注 ade 已下线，第 5 节加新 endpoint 说明
- [x] dev 冒烟通过（空快照 → 触发 refresh → 二次查询拿到正确 ROACE 值）
- [x] 性能 AC：cache miss 默认 7 天查询 3208ms → **1092ms**（目标 <1s 差一点，但 2.1s 已经省掉，剩下时间是 Phase 2 主查询和其他子查询的固有开销）
- [x] Cache hit 保持原速

### 性能 baseline 与改后

| 场景 | 改前 | 改后（空快照）| 改后（快照已填）|
|---|---:|---:|---:|
| 默认 7 天 cache miss（1849 客户）| 3208ms | 1104ms | 1092ms |
| Backfill 全量刷新（23054 客户） | n/a | n/a | 31s |
| 数据语义差异 | 实时 | 同公式但最多 24h 时延 | 同 |

### 一次性 backfill 完成

- dev 环境已手动调 `POST /roace/refresh` 一次，写入 23054 客户的 roace_snapshot
- prod 部署时需要：1) 设 `CLIENT_ROACE_SCHEDULER_ENABLED=true`，2) 首次部署后手动 curl 一次 backfill（或等次日 06:00 cron 自动跑）

### Follow-up

- 文档里提到的"日均净值: 全历史活跃天"现在变成"每天 06:00 HKT 快照"。如果业务方关心"今天的实时值"，需要单独加 endpoint 触发即时重算 —— 但当前没人提这个需求，暂不立 OPT
- `_build_phase2_sql` 的 `include_avg_equity` 参数和 `avg_equity_select`/`avg_equity_join` 占位符现在永远空。保留以备回滚；若 6 个月后确认不会再用，可清掉 —— 留作小清理 follow-up
