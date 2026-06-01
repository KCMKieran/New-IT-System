# 已完成的优化

Append-only 日志。最新的写在最上面。
查看任何一条的完整背景，打开对应的 `items/OPT-NNNN-*.md`。

> **行长约束**：`标题` 列 = **一句话**（一行能扫完），重型细节进 item 文件的 **结果** 段——这是可扫描索引，不是日志正文。
> **Commit 列**：新条目 merge SHA 写盘前不可知，填 `—`，用 `git log --grep="OPT-NNNN" --merges` 找回；早期手填的真 SHA 保留。

## 已完成

| 日期 | ID | Commit | 标题 |
|------|----|--------|------|
| 2026-05-28 | [OPT-0030 P2](./items/OPT-0030-risk-monitor-leverage-abuse-tab.md) | — | **滥用杠杆 Phase 2（event-gated）**：检测内核 snapshot→event-gated（只看最近开仓账户在开仓那一刻的 margin level，剔除"开仓正常、后来亏损漂移到 MC"的误报——B-book 那是公司在赚的亏损户）。复用 burst/hedge opens 查询(overlap)+SETTLE 60s+`MODIFY_TIME>=开仓时间`守卫(UTC 同框)+dedup(rule,server,login,open_time);弃用 streak/grace/冷却。前端 3 条 rules(200/150/125)不动，streak_min 废弃。Stage 1 review 8 finding：#3 重启 dedup seed + #9 streak_min KeyError 守卫当场修；#4 再开仓重报"保持"(用户拍板);#1/#2 跳-tick 漏开仓 live with(所有 window 规则共有,杠杆缓冲更大);#5/#6/#7/#8 live with。教训:MARGIN_LEVEL 一个指标承载"仓位大/亏损"两语义→换评估时机=换指标 |
| 2026-05-28 | [OPT-0030](./items/OPT-0030-risk-monitor-leverage-abuse-tab.md) | — | Risk-monitor 第 6 个 tab「滥用杠杆」(rule_id 101-110) — **首条 snapshot-scan 规则**：扫 `fxbackoffice.mt4_users.MARGIN_LEVEL` 现成列（预飞行发现 → §4.2 设计稿 symbol_contract/required_margin 三难点全消失，effort L→M），不走 mt4_trades/cursor。D1 瞬时(<105.3%) + D2 持续(<125% 连续 3 次)；新表 `account_leverage_streak`(D2 跨扫描计数) + `alert_leverage_abuse_detail`。无 severity(用户决策)/无聚合/有立即扫描。Stage 1 outsider-review 8 条 finding：A(streak 抗抖动 miss_count 宽限窗 + 新鲜度 max(15,interval+5)) + B(config save 清 streak) + C(snapshot SQL 防御 LIMIT) 当场修；#4 跨事务/#6 占位契约(reviewer QP-dedup 看错)/#7 时钟/#8 前端色带 live with。15 单测 |
| 2026-05-22 | [OPT-0027](./items/OPT-0027-risk-monitor-burst-aggregated-view.md) | — | Risk-monitor 批量下单 tab 加聚合视图（按账户折叠，复用 OPT-0021 hedge-open 模式）— 后端 CTE ranked→agg→JOIN latest（`aggregate_burst_open_by_login` + `/burst-open/alerts/aggregated`）+ 19 集成测试；前端工具栏加琥珀/翠绿 toggle、独立 `aggColumnPersist` key、独立 aggSort 状态；聚合视图 13 列（去掉 buy/sell 拆分）；总手数为普通 sum 无双向语义；Stage 1 outsider-review 12 条 finding：F8 + F12 当场修（聚合模式 disable 导出 CSV + helper 注释 + 引用 OPT-0028 backlog）、F1 实测无效回滚、F5/F7/F9/F10/F11 live with、F2 + F3 + F4 + F6 拆到 [[OPT-0028]] hardening |
| 2026-05-21 | [OPT-0026](./items/OPT-0026-docs-portal.md) | — | 文档中心 sidebar 入口 + MkDocs Material 自托管 — Configuration 组首项「文档中心 / Docs」external link 开新窗到 `/docs/`；mkdocs-material:9.7.6 容器 ro mount `docs/` + `mkdocs.yml`，Nginx `proxy_pass http://mkdocs:8000`（**无** trailing slash 保留前缀避免 302 循环）；超 fence + Mermaid + admonition + 中英搜索；docker healthcheck 用 127.0.0.1（image 内 localhost=::1 但 mkdocs bind IPv4）；NavDocuments 加 external 字段 + forwardRef NavLink；firstItems 3→4 保 Reports 可见；`.gitignore` 单文件 unignore docs-portal.md。Stage 1 outsider-review 11 条 finding：F1 (gitignored docs 被 portal 渲染) live with、F2 + F3 当场修（pin tag + healthcheck + ops doc trailing-slash 矛盾）、F4 + F6 拆到 hardening OPT。手机/iPad 实测留给用户 |
| 2026-05-21 | [OPT-0025](./items/OPT-0025-risk-monitor-filter-persist.md) | — | Risk-monitor 5 个 tab 工具栏过滤器持久化（rangePreset / ruleFilter / serverFilter / sharedIpOnly）；新增 `useFilterPersist` hook + 纯函数 helpers（envelope `{v, value}` schema 自愈 + localStorage throw 兜底）+ 18 项 vitest 测试；`customRange` 路径靠 `skipFields: ["rangePreset"]` 保留 storage 中上一次真 preset；遵守「用户偏好持久化、调查上下文（login/zipcode/customRange）不持久化」标准；用户拍板不加「重置过滤」按钮（保持 OPT-0023 化繁为简） |
| 2026-05-21 | [OPT-0024](./items/OPT-0024-risk-monitor-est-commission.md) | — | Risk-monitor 4 个 tab 加「佣金试算」列 — **Phase 1：CN（CID=0）D03 公式**：新建 `lib/commission.ts` 复刻 D03 公式（External + Internal + Dark Points），7 处列注入，非 CN 显示 `—`；用 InfoHeader（ℹ 图标 + shadcn Tooltip）；顺手修 AG-Grid headerTooltip 即时显示（`enableBrowserTooltips: true`）；引入 vitest 测试框架 + commission.test.ts 30 项测试。**Multi-phase OPT**：Phase 2（global / CID=1）触发时 reopen 本 OPT 加新分支，**不开新 OPT** —— 详情见 item 文末「§ 后续扩展」段 + 文档顶部「⚠ Multi-phase」段 |
| 2026-05-20 | [OPT-0023](./items/OPT-0023-risk-monitor-unified-settings.md) | — | Risk-monitor header 化繁为简：5 个 tab 的「列设置」+「立即扫描」收进设置抽屉（rename「规则配置」→「设置」），导出 CSV + hedge 聚合 + QP 刷新浮动盈亏 留在 header；新增 ColumnVisibilityInline 组件 + UnifiedSettingsExtras 共享段；Stage 1 outsider-review 修了 3 条（save semantics 提示 / a11y group / hedge 聚合 caption）|
| 2026-05-20 | [OPT-0022](./items/OPT-0022-client-return-usdt-tag.md) | — | Client Return Rate 加 USDT 标记列 + 入金渠道筛选（从 OPT-0020 拆出，与 OPT-0023 一起 merge）|
| 2026-05-19 | [OPT-0021](./items/OPT-0021-risk-monitor-hedge-wash-tab.md) | — | Risk Monitor 新增「对冲刷单」Tab（rule_id 91-100，单账户 buy+sell 严格 1:1 + 0.01 lot EPS）— 抓 wash trading via lock-position；同时引入 per-rule name 字段（fund-flow 模式）+ page-style-conventions §9 多 tab 页面布局规范 |
| 2026-05-19 | [OPT-0019](./items/OPT-0019-redis-maxmemory-policy.md) | — (merge of `61b2666`) | Redis 加 maxmemory 256mb + allkeys-lru（OPT-0018 sub-OPT；prod + dev compose 同步；需要手动 `docker compose up -d redis-prod` 才会生效）|
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
