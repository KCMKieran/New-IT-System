# 优化清单（Backlog）

> 单一入口。每条 item 的细节都在 [`items/`](./items/) 里。
> 动 item 之前，**先把它的 row 移到 WIP**，填 branch 和 claim 日期。完整流程见 [`README.md`](./README.md)。

## 🔨 进行中（WIP）

> 其他 session：除非是自己的 claim，不要碰这里的行。
> 表是空的就好，所有人都可以从 Ready 里挑。

| ID | Branch | Claim 日期 | 备注 |
|----|--------|-----------|------|
| [OPT-0030](./items/OPT-0030-risk-monitor-leverage-abuse-tab.md) | opt/risk-monitor-leverage-abuse-tab | 2026-05-28 | 滥用杠杆 tab — 第 6 个检测规则；risk-monitor 首条 snapshot-scan 规则 |

## ✅ 待领取（Ready）

> AC 已经在 item 文件里定义好了。按 priority + effort + 你的当前心智状态挑一个。

| ID | 优先级 | 区域 | 工作量 | 标题 |
|----|--------|------|--------|------|
| [OPT-0001](./items/OPT-0001-risk-monitor-tab-cache.md) | P2 | frontend | M | Risk-monitor 四个 tab 切换状态/缓存优化 |
| [OPT-0002](./items/OPT-0002-browser-cache-pattern-doc.md) | P2 | docs | S | 现有浏览器缓存模式归纳成复用文档 |
| [OPT-0003](./items/OPT-0003-risk-monitor-sqlite-perf.md) | P1 | db | L | Risk-monitor SQLite 数据增长后的性能方案 |
| [OPT-0028](./items/OPT-0028-risk-monitor-aggregator-hardening.md) | P2 | mixed | M | Risk-monitor 聚合视图硬化（SQL 性能 + 语义确定性，同时覆盖 burst + hedge） |

## 💡 想法（Ideas）—— 还不能直接 claim

> 太模糊 / 太大 / AC 不清楚。不要 claim —— 先升级到 Ready（流程见 [README.md §B](./README.md#b-idea--ready你或-claudescope-阶段)）。

| ID | 区域 | 标题 | 备注 |
|----|------|------|------|
| [OPT-0004](./items/OPT-0004-risk-monitor-arch.md) | mixed | Risk-monitor 架构/框架重构 | 太宽，需要先拆 3~5 个子任务（路由分层 / service 抽象 / 配置集中 / 任务调度 …） |
| [OPT-0018](./items/OPT-0018-cache-layer-audit.md) | mixed | 全链路缓存审计与硬化（HTTP / 应用 / Redis / DB） | 已扫描出 5 条真问题（Redis 无 maxmemory、匿名 volume、PnL/IB hit rate 异常等），audit 完成后拆 3-5 个子 OPT |
| [OPT-0020](./items/OPT-0020-client-return-rate-risk-signals.md) | mixed | Client Return Rate 加 4 个风控判断列（过夜 / ~~USDT~~ / Sharpe / Consistency） | 剩余 7 列分两 Drop 上线（USDT 已拆到 OPT-0022），复用 OPT-0006 的夜间预计算 SQLite 模式；claim 前需用户审 AC、回答 4 个开放问题 + 先跑过夜 SQL 的预飞行实测 |
| [OPT-0029](./items/OPT-0029-risk-monitor-view-presets.md) | mixed | Risk-monitor 视图模板服务端化（团队共享 view presets + 轻量自报身份） | 老板要全队统一 risk-monitor 显示设置；localStorage 无法共享/跨机。范围刻意收窄=只做服务端模板+自报身份，不含认证/RBAC/行为日志。claim 前定 3 件事：表放 SQLite vs PG、产品形态（显式模板 vs 静默同步，倾向前者）、UI 3 key 进不进模板 |
