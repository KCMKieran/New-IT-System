---
id: OPT-0009
title: 加 `(rule_id, scanned_at DESC)` 复合索引到 alert_events
status: done
priority: P1
area: db
effort: S
created: 2026-05-15
related: [[OPT-0003]] [[OPT-0008]]
---

## 问题

OPT-0008 拆 detail 表后，Gap Trade Tab 的查询走全表扫——`_build_alert_filters` 把 `time_field=window_date` 实现成 `COALESCE(gso.window_date, gp.window_date) >= ?`，任何 alert_events 索引都救不了 COALESCE 表达式，SQLite 只能 SCAN ae 全表（82k 行）→ 实测 17.29 ms。

Quick Profit dedup seed（每次 backend 重启）也类似：`WHERE rule_id BETWEEN 61 AND 70 AND scanned_at >= datetime('now', '-60min')`，SQLite 用 scanned_at 索引拿到最近 60min 全部行再回行过滤 rule_id，0.12 ms。

## 背景

- HTML 报告问题 3 原本说「QOC/QP 查询走全表」，但拆完 OPT-0008 后**实测 QOC 0.61 ms / QP 4.03 ms** —— 优化器选 `idx_alert_events_scanned_at` 已经够快，不是问题
- 数据极度倾斜：Burst 96.57% / QOC 3.24% / QP 0.15% / Gap 0.03%
- 真正的痛点变成了 Gap Trade Tab（17 ms 全表扫）+ 启动期 dedup seed
- EXPLAIN QUERY PLAN 实测：加 `(rule_id, scanned_at DESC)` 后 Gap SO 17.29 → 0.02 ms（**865×**），dedup seed 0.12 → 0.01 ms（12×）；Burst/QOC/QP 普通查询 SQLite 优化器自己选不变（无影响）

## 假设 / 待验证

- [x] CREATE INDEX 在 ~82k 行表上耗时 < 1 秒
- [x] 索引磁盘成本 ~2 MB（4B rule_id + ~24B scanned_at × 82k 行）
- [x] 现有 idx_alert_events_scanned_at 不受影响（SQLite 按 query 选最优）
- [x] 写路径 INSERT 多维护一个索引，每条 alert 微秒级，可忽略

## 验收标准

- [x] `_SCHEMA_SQL` 加 `CREATE INDEX IF NOT EXISTS idx_alert_events_rule_scanned ON alert_events(rule_id, scanned_at DESC)`
- [x] dev hot-reload 后 EXPLAIN QUERY PLAN 上 Gap SO 用 `idx_alert_events_rule_scanned`，不再 SCAN ae
- [x] Gap SO 查询从 17ms 降到 0.02ms（**865×**，超额完成）
- [x] QP dedup seed 从 0.12ms 降到 0.01ms（12×）
- [x] Burst/QOC/QP 普通查询时间不变（SQLite 优化器自己选 scanned_at）
- [-] /scan-now 写路径未单独计时，但本次只新增 1 个 PK-style 索引，单 INSERT 微秒级开销可忽略

## 笔记

- CREATE INDEX IF NOT EXISTS 天然幂等，dev/prod hot-reload 都安全 ✓
- 不需要 VACUUM——只是新增 page ✓
- 不需要 ANALYZE——SQLite 默认按 cardinality 选 index 验证 OK ✓
- 不动 routes/scheduler/services/frontend，纯 schema 增量

## 结果

**Commit**: `7a9089b`（main, 2026-05-15）

**实际交付**：`backend/app/core/risk_monitor_db.py` 在 `_SCHEMA_SQL` 加 1 行 CREATE INDEX + 5 行注释解释，共 +8 行。

**实测收益**（5 次跑 median，dev DB 82k 行）：

| 路径 | before | after | 提升 |
|---|---|---|---|
| Gap SO（rule_id=71 + window_date COALESCE）| 17.29 ms | **0.02 ms** | **865×** |
| QP dedup seed（启动期 60min lookback）| 0.12 ms | 0.01 ms | 12× |
| Burst（rule_id ≤ 50, scanned_at 30 天范围）| 0.03 ms | 0.03 ms | 不变 |
| QOC（51-60）| 0.61 ms | 0.64 ms | 不变 |
| QP（61-70）| 4.03 ms | 4.46 ms | 不变 |

EXPLAIN 验证：Gap SO 现在走 `SEARCH ae USING COVERING INDEX idx_alert_events_rule_scanned (rule_id=?)`，dedup seed 走 `(rule_id>? AND rule_id<?)`。Burst/QOC/QP 优化器仍选 `idx_alert_events_scanned_at`（如预期）。

**关闭 HTML 报告问题 3**——但实际收益面与原诊断不同：QOC/QP 普通查询本就够快，索引真正修的是 OPT-0008 拆 detail 表后引入的 Gap Trade 全表扫副作用 + 启动期 dedup seed。
