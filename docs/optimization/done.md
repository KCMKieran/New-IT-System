# 已完成的优化

Append-only 日志。最新的写在最上面。
查看任何一条的完整背景，打开对应的 `items/OPT-NNNN-*.md`。

## 已完成

| 日期 | ID | Commit | 标题 |
|------|----|--------|------|
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
