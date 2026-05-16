---
id: OPT-0007
title: 删除 scan_history.alerts/rules_config 双写冗余
status: done
priority: P1
area: db
effort: S
created: 2026-05-15
related: [[OPT-0003]]
---

## 问题

`backend/data/risk_monitor.db` 200 MB 中约 50% 是 `scan_history` 的两个胖 JSON 字段（`alerts` 90 MB + `rules_config` 7 MB），它们是 `alert_events` 关系表的冗余副本（早期版本的遗留结构）。每 5 min 一次 scheduler 扫描都要重序列化写一遍，浪费写吞吐和 IO 锁。

## 背景

- 来源：`risk-monitor-db-analysis.html`（项目根目录，2026-05-14 产物）问题 1
- 父 item：[[OPT-0003]] —— 该分析报告本身回答了 OPT-0003 的「先 benchmark 再优化」AC
- `scan_history.alerts` 在生产路径上 0 处被读，仅 `_backfill_alert_events_if_needed()` 一次性启动函数和 `scripts/export_risk_monitor_alerts.py` 还在读
- `rules_config` 完全无人读取
- 部署的 SQLite 是 3.45.1（支持 `ALTER TABLE DROP COLUMN`，需 ≥ 3.35）

## 假设 / 待验证

- [x] 全仓 grep 确认无其他 reader
- [x] CSV 导出脚本能改写为读 `alert_events JOIN scan_history` 且 CSV 表头不变

## 验收标准

- [x] schema 去掉 `scan_history.alerts` + `rules_config`，保留 6 个轻量元数据列
- [x] `append_scan_and_events()` 不再写这两个字段
- [x] 新增幂等迁移函数（PRAGMA 校验）
- [x] 删除已死的 `_backfill_alert_events_if_needed()`
- [x] 迁移成功后自动 VACUUM 回收 page
- [x] CSV 导出脚本改读 `alert_events`，输出字段语义不变
- [x] dev 实测文件大小骤降 ≥ 40%

## 笔记

- dev 容器是 hot-reload，编辑过程中会触发 `init_risk_monitor_db()` 多次重跑——本次实操中 hot-reload 在 VACUUM 代码加好**之前**就跑完了迁移，导致自动 VACUUM 跳过。手工补跑 0.9 s 即可（prod 冷启动场景不会出现这个时序问题）。
- prod 部署后没看到 `Dropped... legacy columns` 日志是预期：dev 与 prod 共享同一个 host volume，DB 文件已先在 dev 那侧迁移，prod 只是幂等回退。
- 两条 `append_scan_and_events()` 调用路径（burst+QOC+QP / gap-trade）都通过 `/scan-now` + `trigger_gap_trade_scan_now()` 实测写入成功。

## 结果

**Commit**: `27a0b00`（main, 2026-05-15）

**实际交付**：
- `backend/app/core/risk_monitor_db.py` 改 schema、改 writer、加迁移函数 `_migrate_drop_scan_history_legacy_columns`、删 `_backfill_alert_events_if_needed`、迁移后自动 VACUUM
- `backend/app/core/burst_open_scheduler.py` 两处调用去掉 `rules_config` 入参
- `backend/scripts/export_risk_monitor_alerts.py` 改读 `alert_events JOIN scan_history`，CSV 表头/字段语义零改动

**实测收益**：
- DB 文件大小 203 MB → **92 MB**（-55%，比原预估的 110 MB 还多省 18 MB）
- 每次 scheduler 写入字节数减半，写事务时间下降，与前端读路径的 IO 锁竞争同步消失
- 净代码量：3 文件 +83 / -123 行

**Follow-up（来自 HTML 分析报告的剩余 7 个问题，按推荐优先级）**：
- 问题 4 · 开 WAL（5 行改动，消除写阻塞读）—— 可 file 成 OPT-0008
- 问题 3 · 加 `(rule_id, scanned_at)` 索引 —— 可 file 成 OPT-0009
- 问题 2 · 拆 `alert_events` 为 4 张 detail 表 —— 中等工作量，可 file 成 OPT-0010
- 问题 5/6/7/8 · 较小或观感性，按需 file
