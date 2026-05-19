---
id: OPT-0012
title: Risk-monitor scheduler 拆 fast / slow / daily tier
status: done
priority: P2
area: backend
effort: M
created: 2026-05-16
related: [[OPT-0004]] [[OPT-0011]]
---

## 问题

当前 `backend/app/core/burst_open_scheduler.py` 一个 `_run_scan()` 串联跑 4 条规则：

```
Burst Open  +  Quick Open-Close  +  Quick Profit   →  同一节拍 (scan_interval_min, 默认 10 min)
Gap Trade   →  已独立 cron（Mon-Sat HKT 07:20）
```

业务需求实际上**节拍差异巨大**：

| 规则 | 业务窗口 | 真正需要的延迟 | 现在节拍 |
|---|---|---|---|
| Burst Open | 3 秒内 N 单 | ≤ 60s（越快越能及时干预） | 10 min ❌ 慢了 |
| Quick Open-Close | 短持开平 | 1-2 min | 10 min ⚠ 凑合 |
| Quick Profit | 30 min 累积 | 5-10 min | 10 min ✅ 合理 |

但因为三条规则被绑死在同一 `_run_scan()` 里，**想给 Burst 提速就必须给所有规则都提速** → MySQL 受不了，浮动 P&L 也跟着被反复重算。

## 背景

- 现有反模式（SKILL.md §Anti-Patterns）：**No separate scheduler job** — 因为当时担心多 scheduler 写 SQLite 互踩。这条本意是好的（防止 dev + prod 双写），但被解读得过严了，实际上**同一进程内多个 job** 是没问题的（APScheduler 本来就支持）
- 现有铁律必须保留：**只有一个进程能写 SQLite**（dev compose 已经用 `BURST_SCAN_ENABLED=false` + `GAP_TRADE_SCAN_ENABLED=false` 关掉了 dev 写入）
- Gap Trade 已经独立成自己的 cron job（每天 HKT 07:20），证明同进程多 job 这个模式是可行的

## 假设 / 待验证

- [ ] APScheduler 在同一 BackgroundScheduler 实例里跑两个 IntervalTrigger job（60s / 600s），共享同一个 SQLite 连接池，是否会有写锁冲突？（开 WAL 后应该没问题，见 OPT-0003 问题 4）
- [ ] Burst Open 提到 60s 节拍后，浮动 P&L enrich（要查 MT 服务器实时 equity/balance）的负载是否扛得住？或者 fast tier 跳过 enrich，留到 slow tier 补
- [ ] dedup 跨 tier 协作：fast tier 写的 alert 进 SQLite 后，slow tier 的 prev-pool 要不要也 seed 这部分？（应该要，避免重复发）
- [ ] env flag 命名：`BURST_FAST_TIER_ENABLED` 还是复用现有 `BURST_SCAN_ENABLED`？

## 验收标准

- [ ] **不阻塞条件**：OPT-0011（游标扫描）必须先完成。没有游标，fast tier 60s 节拍会让 MySQL 重复扫数据 10×
- [ ] 拆 scheduler：同一进程内两个 job
  - **fast tier**：60s，只跑 Burst Open（最需要时效性的规则）
  - **slow tier**：原 scan_interval_min（10 min），跑 Quick Open-Close + Quick Profit
  - **daily tier**：保持不变（Gap Trade）
- [ ] dev compose 加 `BURST_FAST_TIER_ENABLED=false`，prod 默认 `true`
- [ ] dedup 链路：fast tier 写的 burst alerts 必须能被 slow tier 的下一轮 seen-set 看到（共享 SQLite 即可，但要测试验证）
- [ ] Benchmark：fast tier 上线后 MySQL 端 burst-open 查询 QPS、单次查询 rows scanned、p95 latency 三项指标
- [ ] 回归测试：现有 `test_burst_open_scheduler_*.py` 全过

## 笔记

**与 OPT-0004 的关系**：OPT-0004「架构重构」列了候选方向「任务调度」，本 item 就是把那个抽象方向具体化为一个可执行子任务。如果本 item 完成，OPT-0004 可以删掉「任务调度」这个候选方向。

**为什么不一次拆成完全 plugin 化（Strategy 模式）**：roadmap §4.4 说"规则越多越值得拆"——但当前只有 4 条规则，且未来一年最多再加 1-2 条（Martingale / Scale-In），plugin 化是过度工程。本 item 只做最小拆分（fast/slow 两层），不引入新抽象。

**反对意见**：
- 「Burst Open 真的需要 60s 吗？」—— 业务上：burst 是 3 秒窗口的事件，10 min 后才发现，dealing desk 已经没法干预。提到 60s 才能让风控真正"准实时"
- 「拆两个 scheduler 增加复杂度」—— 对，但 APScheduler 同实例多 job 是它的原生用法，不是新概念

**严格防线**：本 item 完成后**绝不允许**再增加第 4 个 scheduler 实例。后续如果还需要更精细的节拍（比如 30s tier），必须复用 fast tier 然后在 detector 层 throttle，而不是再加 scheduler。

## 结果

**Commit**: `9fb5cca` (impl) + `c59e2f8` (claim) on `feat/risk-monitor-realtime`,
merged to main as `ceb21c4` on 2026-05-17.

**实际交付**：
- `_run_scan(tier='all'|'fast_burst'|'slow')` — tier 参数控制哪些 detector 运行
- 新 job `BURST_FAST_JOB_ID = "burst_open_fast_tier"`，60s 节拍，只跑 burst（`_locked_fast_burst_scan`）
- 旧 job 在 fast tier 开启时自动切到 `tier='slow'`（跳过 burst）；关闭时保持 `tier='all'` 行为完全不变
- `_latest_result.alerts` 缓存按 rule_id 分段合并（1-50 / 51+），切 tier 不丢另一边的告警快照
- env flag `BURST_FAST_TIER_ENABLED`（默认 false）+ 严格 `true` 字面量判定
- 两个 tier 共享 `_scan_lock`（避免分割 `_latest_result` mutation 复杂度，~1/10min 命中冲突可接受）
- BURST_SCAN_ENABLED=false 仍是 master kill-switch（连 scheduler 都不起）

**测试**：14 个 scheduler tier 测试（test_scheduler_tiers.py）覆盖 env flag、单/双 job 装配、tier dispatch、跨 tier 缓存保留、lock 隔离

**Prod 状态**（2026-05-17 上线 / `719eb66` 当晚开 flag — 与 [[OPT-0011]] 同批）：
- 代码已 merge + deploy
- `BURST_FAST_TIER_ENABLED=true` 已写进 `docker-compose.prod.yml` + 容器 restart
- 实际跑双 job：60s fast tier 只跑 burst，原 10min slow tier 跑 quick OC + quick profit
- 日志格式：fast tick → `Scan complete [fast_burst]: ...`，slow tick → `Scan complete [slow]: ...`
- Dev 端 `BURST_FAST_TIER_ENABLED` **故意不设**（dev compose 注释明写："Do NOT enable BURST_FAST_TIER_ENABLED here — dev shares SQLite with prod"），防止 dev + prod 两个 scheduler 同时写 `alert_events`

**周末上线的玄机**（719eb66 commit message 备忘）：选周末跑是因为外汇市场 Sat 05:00 → Mon 05:00 HKT 休市 —— fast tier 60s 节拍整周末扫到 0 单，不会写 SQLite，但 scheduler 持续跑可以验证 SSE 推送链路（前端 "N 次推送" 计数每 60s 跳 1）。

**Follow-up**：
- 周一开盘后观察 fast tier 真实流量下的 MySQL 压力 + SQLite 写并发
