# 优化清单（Backlog）

> 单一入口。每条 item 的细节都在 [`items/`](./items/) 里。
> 动 item 之前，**先把它的 row 移到 WIP**，填 branch 和 claim 日期。完整流程见 [`README.md`](./README.md)。

## 🔨 进行中（WIP）

> 其他 session：除非是自己的 claim，不要碰这里的行。
> 表是空的就好，所有人都可以从 Ready 里挑。

| ID | Branch | Claim 日期 | 备注 |
|----|--------|-----------|------|
| _空_ | | | |

## ✅ 待领取（Ready）

> AC 已经在 item 文件里定义好了。按 priority + effort + 你的当前心智状态挑一个。

| ID | 优先级 | 区域 | 工作量 | 标题 |
|----|--------|------|--------|------|
| [OPT-0001](./items/OPT-0001-risk-monitor-tab-cache.md) | P2 | frontend | M | Risk-monitor 四个 tab 切换状态/缓存优化 |
| [OPT-0002](./items/OPT-0002-browser-cache-pattern-doc.md) | P2 | docs | S | 现有浏览器缓存模式归纳成复用文档 |
| [OPT-0003](./items/OPT-0003-risk-monitor-sqlite-perf.md) | P1 | db | L | Risk-monitor SQLite 数据增长后的性能方案 |
| [OPT-0017](./items/OPT-0017-risk-monitor-group-column.md) | P2 | mixed | S | Risk Monitor 各 Tab 添加账户组（Group）列 |

## 💡 想法（Ideas）—— 还不能直接 claim

> 太模糊 / 太大 / AC 不清楚。不要 claim —— 先升级到 Ready（流程见 [README.md §B](./README.md#b-idea--ready你或-claudescope-阶段)）。

| ID | 区域 | 标题 | 备注 |
|----|------|------|------|
| [OPT-0004](./items/OPT-0004-risk-monitor-arch.md) | mixed | Risk-monitor 架构/框架重构 | 太宽，需要先拆 3~5 个子任务（路由分层 / service 抽象 / 配置集中 / 任务调度 …） |
