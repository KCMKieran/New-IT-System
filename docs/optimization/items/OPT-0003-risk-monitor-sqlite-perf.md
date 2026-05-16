---
id: OPT-0003
title: Risk-monitor SQLite 数据增长后的性能方案
status: ready
priority: P1
area: db
effort: L
created: 2026-05-13
related: [[OPT-0004]] [[OPT-0007]] [[OPT-0008]] [[OPT-0009]] [[OPT-0014]]
---

## 问题

Risk-monitor 当前用 SQLite 存 scan_history（每次扫描的批次元信息）+ alert_events（每条告警），保留 30 天。担心数据规模增长后查询/写入变慢 —— 但当前并没有客观的"慢"证据，需要先量化现状。

## 背景

- 当前实现：APScheduler 周期扫描 → SQLite，30 天 retention（来源：`.cursor/skills/risk-monitor/SKILL.md`）
- SQLite 在单写者 + 中等 QPS 下表现良好，瓶颈通常在：
  - 并发写（多个 scheduler 任务同时写同一个 .db）
  - 大表全扫（缺索引）
  - WAL 模式没开（默认是 DELETE journal）
  - 单文件超过 ~10GB 后维护成本上升
- 候选迁移目标（按代价递增）：纯索引/PRAGMA 调优 < 分表/按月切 < PostgreSQL < ClickHouse
- 项目已有 ClickHouse + MySQL + PostgreSQL + Redis 基础设施，迁移目标已就位

## 假设 / 待验证

- [ ] 当前 alert_events / scan_history 的实际行数、磁盘大小？每天增长多少？
- [ ] 当前最慢的查询是什么？是 UI 的列表查询，还是 scheduler 自己的去重查询？
- [ ] 是否已开 WAL 模式？(`PRAGMA journal_mode;`)
- [ ] 索引覆盖情况：常用的 `WHERE server, login, symbol` 是否有复合索引？
- [ ] 30 天 retention 是否真的在执行？（有清理任务吗？）

## 验收标准

- [ ] **先 benchmark，再优化**：用 EXPLAIN QUERY PLAN + 真实查询时间，列出 top-3 慢操作
- [ ] 估算 6 个月后的数据规模（当前增长率 × 6 × 30 天 ÷ 现有 retention 比例）
- [ ] 产出 3 档方案，附预估收益和工作量：
  - **低代价**：PRAGMA 调优（WAL / synchronous=NORMAL / cache_size）+ 补索引 + 确保 retention 任务跑着
  - **中代价**：按月分表 / 按 server 分库 / 异步批量写
  - **高代价**：迁移到 PostgreSQL 或 ClickHouse（评估迁移成本 + 改后端 services 代码）
- [ ] 给出明确推荐（不是"做了就改"），用户决策后再做实现
- [ ] 实施后回归：相同查询的 p50 / p95 时间，写入结果段

## 笔记

**2026-05-14**：完成 benchmark + 分析，产物 `risk-monitor-db-analysis.html`（项目根目录）。涵盖：
- 当前规模：210 MB / scan_history 16k 行 / alert_events 80k 行 / 30 天 retention 已在跑
- 列出 8 个具体问题，按 high / med / low 给出推荐优先级表
- 等价于本 item 「3 档方案 + 推荐」AC 的交付物

**剩余子问题**（按推荐优先级，可独立 file 成 OPT-NNNN）：
1. ✅ 问题 1 · scan_history.alerts/rules_config 双写 → [[OPT-0007]] done @ 2026-05-15
2. ✅ 问题 2 · 拆 `alert_events` 为 1+4 张 detail 表 → [[OPT-0008]] done @ 2026-05-15
3. ✅ 问题 3 · 加 `(rule_id, scanned_at)` 索引 → [[OPT-0009]] done @ 2026-05-15（实际收益面与原诊断不同，详见 item）
4. ✅ 问题 8 · 6 个废弃 deposit_*/withdrawal_* 死列 → 顺手在 OPT-0008 一并清掉
5. → 问题 4 · 开 WAL → 已提取为独立 item [[OPT-0014]]（2026-05-16）
6. ⏳ 问题 5 · `scan_batch_id` 半成品 FK
7. ⏳ 问题 6 · `window_date` 字典序比较风险
8. ⏳ 问题 7 · 配置 + 告警数据共存（架构观感，不必动）

## 结果

_待填（等剩余子问题全部完成或显式 drop 后再 close）_
