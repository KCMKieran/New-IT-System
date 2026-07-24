# 优化清单（Backlog）

> 单一入口。每条 item 的细节都在 [`items/`](./items/) 里。
> 动 item 之前，**先把它的 row 移到 WIP**，填 branch 和 claim 日期。完整流程见 [`README.md`](./README.md)。

## 🔨 进行中（WIP）

> 其他 session：除非是自己的 claim，不要碰这里的行。
> 表是空的就好，所有人都可以从 Ready 里挑。

| ID | Branch | Claim 日期 | 备注 |
|----|--------|-----------|------|
| [OPT-0041](./items/OPT-0041-test-seed-date-rot.md) | opt/test-seed-date-rot | 2026-07-06 | 闸门修复，最先合 |
| [OPT-0002](./items/OPT-0002-browser-cache-pattern-doc.md) | opt/browser-cache-pattern-doc | 2026-07-06 | 与 0041 并行（docs-only） |
| [OPT-0028](./items/OPT-0028-risk-monitor-aggregator-hardening.md) | opt/aggregator-hardening | 2026-07-06 | 等 0041 merge 后开工（依赖测试转绿） |
| [OPT-0057](./items/OPT-0057-risk-watchlist-copy-rewrite.md) | opt/risk-watchlist-copy-rewrite | 2026-07-25 | 阻塞：待用户在 items/OPT-0057-...-copy-review.md 填新文案后实施 |
## ✅ 待领取（Ready）

> AC 已经在 item 文件里定义好了。按 priority + effort + 你的当前心智状态挑一个。

| ID | 优先级 | 区域 | 工作量 | 标题 |
|----|--------|------|--------|------|
| [OPT-0001](./items/OPT-0001-risk-monitor-tab-cache.md) | P2 | frontend | M | Risk-monitor 四个 tab 切换状态/缓存优化 |
| [OPT-0003](./items/OPT-0003-risk-monitor-sqlite-perf.md) | P1 | db | L | Risk-monitor SQLite 数据增长后的性能方案 |
| [OPT-0044](./items/OPT-0044-alert-mail-hardening.md) | P2 | backend | M | 告警邮件中心 hardening：发送移出扫描锁 / digest 渲染上限 / digest UNIQUE 认领 / 注册表 dataclass 化 |
| [OPT-0048](./items/OPT-0048-case-engine-hardening.md) | P1 | mixed | M | 风控V2 hardening 包：case sync 幂等化（冷审头号项）+ 0046-F3 去重 UNIQUE + 快照 catch-up + 前端 stale-guard/截断护栏（8 项，Phase B 接源前必做前 3） |
| [OPT-0049](./items/OPT-0049-case-metrics-trades-leg-perf.md) | P1 | db | S | case_metrics 交易腿改走 stats_trading_running_totals：生涯全量扫 26.96s → 0.02s（1235x，已对账 0.001% 差异）；开工前需拍板 orders_all 去留 |
| [OPT-0050](./items/OPT-0050-baseline-test-prod-pollution.md) | P1 | backend | S | 基线幂等测试污染 prod PG（940 行假快照）+ 随 roster 增长挂死（178→940 后 ≥3.6 分钟）；与 0041 关系待定 |
| [OPT-0051](./items/OPT-0051-verify-gate-live-db-coupling.md) | P1 | backend | M | 后端测试直连云 DB：verify.sh 单轮 733s 且有 .env 会挂死，41 个既有失败掩盖真信号；建议排在 0041 之后 |
| [OPT-0053](./items/OPT-0053-scheduler-tier-test-flake.md) | P1 | backend | S | verify.sh 硬闸有 flaky 测试：scheduler_tiers 的 fast_burst 保留槽位断言 clean HEAD 实测 1/8 轮随机红；疑 daemon 线程 + 模块级 `_latest_result` 竞态。与 0051 叠加后闸门实际已失效 |

## 💡 想法（Ideas）—— 还不能直接 claim

> 太模糊 / 太大 / AC 不清楚。不要 claim —— 先升级到 Ready（流程见 [README.md §B](./README.md#b-idea--ready你或-claudescope-阶段)）。

| ID | 区域 | 标题 | 备注 |
|----|------|------|------|
| [OPT-0004](./items/OPT-0004-risk-monitor-arch.md) | mixed | Risk-monitor 架构/框架重构 | 太宽，需要先拆 3~5 个子任务（路由分层 / service 抽象 / 配置集中 / 任务调度 …） |
| [OPT-0018](./items/OPT-0018-cache-layer-audit.md) | mixed | 全链路缓存审计与硬化（HTTP / 应用 / Redis / DB） | 已扫描出 5 条真问题（Redis 无 maxmemory、匿名 volume、PnL/IB hit rate 异常等），audit 完成后拆 3-5 个子 OPT |
| [OPT-0020](./items/OPT-0020-client-return-rate-risk-signals.md) | mixed | Client Return Rate 加 4 个风控判断列（过夜 / ~~USDT~~ / Sharpe / Consistency） | 剩余 7 列分两 Drop 上线（USDT 已拆到 OPT-0022），复用 OPT-0006 的夜间预计算 SQLite 模式；claim 前需用户审 AC、回答 4 个开放问题 + 先跑过夜 SQL 的预飞行实测 |
| [OPT-0052](./items/OPT-0052-ibdata-trading-net-deposit-split.md) | mixed | IBData / IB Report 净入金拆出「交易净入金」（不含 ib withdrawal） | 2026-07-15 口径审计剩余项（RiskMonitor/ClientReturn/IBData 三个高危已修）；全站 7/9 实现都含 ibw 且有业务确认背书，更像"更精细视角"而非修 bug —— 是否值得做待用户判断 |
