# 已完成的优化

Append-only 日志。最新的写在最上面。
查看任何一条的完整背景，打开对应的 `items/OPT-NNNN-*.md`。

## 已完成

| 日期 | ID | Commit | 标题 |
|------|----|--------|------|
| 2026-05-18 | [OPT-0017](./items/OPT-0017-risk-monitor-group-column.md) | `e5e102c` (merge of `e2b21ae`) | Risk Monitor 各 Tab 添加账户组列（Tab 2/3 后端 `get_account_info_map` extend；Tab 4 三段 grid 用现有 `l_groupsid` / `client_groupsid`）|
| 2026-05-18 | [OPT-0016](./items/OPT-0016-grid-persist-hardening.md) | `8cce6f9` (merge of `55b2cf7`) | useGridColumnPersist hardening：6 条 scaling-review 修复（typed key 注册表 + applyColumnState 事件循环短路 + schema 自愈 + stale key cleanup + cast 移除 + 文档 compose-only + a11y label）|
| 2026-05-18 | [OPT-0015](./items/OPT-0015-customizable-grid-columns.md) | `68838f9` (merge of `891560b` + `a458588` + `43de83f`) | RiskMonitor / ClientReturnRate 列自定义 + localStorage 持久化（`useGridColumnPersist` hook + `<ColumnVisibilityMenu>` 组件，7 个 grid 接入，dark-mode 对比度优化）|
| 2026-05-17 | [OPT-0013](./items/OPT-0013-risk-monitor-sse-alerts.md) | `b687cfc` + nginx `edcd3a3` | Risk-monitor 用 SSE 推送告警到前端（含 nginx ?api_key= + buffering 修复，prod live SSE_ENABLED=true）|
| 2026-05-17 | [OPT-0012](./items/OPT-0012-scheduler-fast-slow-tier.md) | `9fb5cca` | Risk-monitor scheduler 拆 fast / slow / daily tier（env-gated，prod 默认 OFF 保留）|
| 2026-05-17 | [OPT-0011](./items/OPT-0011-risk-monitor-cursor-scan.md) | `4ee87ae` | Risk-monitor 游标式增量扫描（env-gated，prod 默认 OFF 保留）|
| 2026-05-16 | [OPT-0014](./items/OPT-0014-sqlite-wal-mode.md) | `526af46` | Risk-monitor SQLite 启用 WAL 模式 + 4 个调优 PRAGMA（synchronous=NORMAL / busy_timeout=5s / cache=64MB / temp_store=MEMORY）|
| 2026-05-15 | [OPT-0010](./items/OPT-0010-tabs-content-forcemount.md) | `59cf44b` + `d8d29f3` | TabsContent forceMount 修切 Tab 1s 卡顿（含 Tailwind hidden 兜底）|
| 2026-05-15 | [OPT-0009](./items/OPT-0009-alert-events-rule-scanned-index.md) | `7a9089b` | 加 (rule_id, scanned_at DESC) 索引（Gap SO 17ms→0.02ms, 865×）|
| 2026-05-15 | [OPT-0008](./items/OPT-0008-alert-events-split-detail-tables.md) | `4fd0aa2` | 拆 alert_events 60 列宽表为 1 + 4 张 detail 表（顺手关问题 8 + 2 个孤儿列） |
| 2026-05-15 | [OPT-0007](./items/OPT-0007-scan-history-dual-write.md) | `27a0b00` | 删除 scan_history.alerts/rules_config 双写冗余（DB 203MB→92MB） |
| 2026-05-14 | [OPT-0006](./items/OPT-0006-m2-roace-precompute.md) | `38b77a4` | Client Return Rate ROACE 子查询预计算到 SQLite 快照 |
| 2026-05-14 | [OPT-0005](./items/OPT-0005-m1-phase2-temp-table.md) | `8d1d1c2` | Client Return Rate Phase 2 主表替代 N 行 UNION ALL |

## 已放弃

| 日期 | ID | 标题 | 原因 |
|------|----|------|------|
| _暂无_ | | | |
