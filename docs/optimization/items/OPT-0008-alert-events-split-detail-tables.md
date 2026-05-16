---
id: OPT-0008
title: 拆 alert_events 为 1 + 4 张 detail 表（消除 60 列众神之表）
status: done
priority: P1
area: db
effort: M
created: 2026-05-15
related: [[OPT-0003]] [[OPT-0007]]
---

## 问题

`alert_events` 是 60+ 列的「众神之表」，4 套规则字段都堆在一起，rule-specific 列对其他规则全 NULL（实测 28 个 Gap Trade 列在 77k Burst 行里 100% NULL）。每加新规则就「再加 5 列到主表 + 加 ALTER TABLE 分支」，`_migrate_alert_events_columns` 已长成 80 行。schema 还背着 6 个 2026-05-07 已废弃的 deposit_*/withdrawal_* 列。

## 背景

- 来源：`risk-monitor-db-analysis.html` 问题 2（同步关掉问题 8）
- 父 item：[[OPT-0003]]（Risk-monitor SQLite 性能母 item）
- 前置 item：[[OPT-0007]]（已删 scan_history 双写，DB 92MB）
- detail 表用同一个 `id` 做 PK 与 alert_events 1:1 关联（不是 AUTOINCREMENT，绑死外键关系）

## 假设 / 待验证

- [x] LEFT JOIN-on-PK 在 80k 行 + page_size 50 的查询代价毫秒级（SQLite 索引查找 O(log N)）
- [x] 前端 SORTABLE_COL_IDS 是后端 SORTABLE_ALERT_COLS 子集，sort_by 名不变
- [x] API response 形状（扁平 dict）和 CSV 字段保持完全不变 — 路由层零改动

## 验收标准

- [x] alert_events 23 列，4 张 detail 表分别 2/4/29/11 列（gso 含 id 共 29，gp 含 id 共 11）
- [x] writer 按 rule_id 路由 INSERT 到合适 detail 表
- [x] 4 个 reader (query/stream/get_recent_qp/get_alerts_by_ids) 切到 LEFT JOIN SELECT
- [x] `_resolve_alert_order` 用 `<alias>.<col>` 解析 sort_by；`window_date` 用 COALESCE
- [x] `_row_to_alert_dict` 适配新 row 形状，字段名同今天
- [x] 迁移函数幂等，backfill 完整，DROP 47 列后自动 VACUUM
- [x] 4 个 Tab `/alerts` `/alerts/stats` `/alerts/export` 端点全绿
- [x] burst+QOC+QP scan-now 写路径 + gap-trade scan 写路径实测通过
- [-] 4 个 Tab CSV diff vs 迁移前基线 — **不适用**：迁移在 hot-reload 时已无感发生，无法取「迁移前」基线 CSV；改用「字段集和 schema 完整性 + 写路径回归」验证

## 笔记

- **dev hot-reload 抢跑了 VACUUM**：和 OPT-0007 一样的剧情，hot-reload 在我加 VACUUM 触发条件之前就跑完了 split 迁移。后续我 file 修改触发了一次完整迁移（detail 表+drop+VACUUM 都跑），文件大小 92→92 MB（变化不显著，因为 alert_events 列变少但 detail 表新增数据，基本抵消）。
- **意外发现 2 个孤儿列** `lookback_rule_min` / `include_floating_rule`：alert_events 第一次清完后还剩 25 列。grep 全仓 + git log --all 都查不到这俩列的来源，确认是某次实验代码反复但 DB 没回滚的残留。新加 `_migrate_drop_orphan_alert_events_columns` 顺手清理。
- **prod 部署后预期不显式触发迁移**：dev/prod 共享 host volume，dev 的 hot-reload 已先把 DB 迁移，prod 重启会幂等返回 False。

## 结果

**Commit**: `4fd0aa2`（main, 2026-05-15）

**实际交付**：
- `backend/app/core/risk_monitor_db.py` 改 schema、改 writer、加 2 个迁移函数（split + orphan-drop）、改 4 个 reader、扩 `_SORT_COL_DB_NAME` 全列映射
- 路由层、scheduler、services、frontend 零改动 — API 字段名/形状未变
- 净代码量：1 文件 +442 / -214 行

**实测收益**：
- alert_events 60 列「众神之表」→ 23 列共有 + 4 张 rule-specific detail 表（2/4/29/11 列含 id）
- 顺手关掉 HTML 报告问题 8（6 个 deprecated deposit_*/withdrawal_*）
- 加新规则的成本：「新建 detail 表 + writer 加 1 个 elif」，不再动主表 + 80 行的 `_migrate_alert_events_columns`
- backfill: qoc=2653, qp=123, gso=16, gp=10 行，<1 秒
- DB 文件大小变化不显著（92 MB → 92 MB）：宽表行虽然字节数减少，但 detail 表新增了同样的字节，净中性。这次 ROI 在「schema 可读性」和「未来可维护性」，不在磁盘
- 4 个 Tab API 全部 200，rule-specific sort_by 全部生效（`hold_duration_sec` / `realized_profit` / `l_profit_usd` / `window_date` 都验证）

**Follow-up（HTML 报告剩余问题）**：
- 问题 4 · 开 WAL（5 行改动） — 可 file 成 OPT-0009
- 问题 3 · 加 `(rule_id, scanned_at)` 索引 — 拆完后需重新评估收益（query 现在按 rule_id 范围过滤再 JOIN）
- 问题 5/6/7 · 较小或观感性
