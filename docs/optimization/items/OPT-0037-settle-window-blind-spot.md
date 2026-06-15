---
id: OPT-0037
title: event-gated 规则 settle-window「快进快出」漏检盲区（滥用杠杆 + 马丁）
status: wip
priority: P1
area: backend
effort: M
created: 2026-06-15
related: [[OPT-0030]], [[OPT-0033]], [[OPT-0011]], [[OPT-0012]]
---

## 问题

两条 **event-gated** 规则——**滥用杠杆（rule 101–110，[[OPT-0030]] Phase 2）** 和
**马丁策略（rule 111–120，[[OPT-0033]]）**——检测到开仓后要读账户**当前状态快照**
（保证金水平 / 当前持仓）。仓位若在 **SETTLE 窗口（60s）内被平掉**，等规则去读时账户
已空仓（`MARGIN=0`/`MARGIN_LEVEL=0`/无持仓行）→ 被当「空仓」跳过 → **永不报警**。

**触发案例**：测试号 `5-60000017` 上周五（2026-06-12）用 1–1.5 手黄金做 9s–40s 快进
快出，终端保证金使用率冲到 ~98%，但「滥用杠杆」tab 一条没出。白名单/demo 过滤经实测
**工作正常**，漏检根因是本盲区。

完整分析：[`docs/analysis/risk-monitor-settle-window-blind-spot.md`](../../analysis/risk-monitor-settle-window-blind-spot.md)（gitignored，本地）。

## 背景 / 关键事实

- **机制**：snapshot 范式 + 两层时间窗叠加。层 A = SETTLE 60s（`_SETTLE_SEC`）；
  层 B = slow-tier 扫描节奏 ~5min。「持仓 < 同步延迟」的超短仓 snapshot 天然抓不到。
- **严重度（prod 实测，近 7 天真实 MT5 已平仓 278,531 笔）**：持仓 **<60s 占 19%**
  （结构性不可见）、**<5min 占 50%**（受扫描节奏影响）。主要贡献是层 B（扫描节奏），
  不是层 A（settle）。
- **受影响仅这 2 条**；其余 6 条（burst / 快开快平 / 快速获利 / gap×2 / hedge）走持久化
  成交行，**不受影响**。马丁更严重：整条 ladder 窗口内全平 → 该组凭空消失 → 整条漏报。
- **修复杠杆**（详见分析文档 §5）：
  - **L1 提速扫描**（移 fast tier 60s）：盲区 ~50%→~20%，代价 MySQL 常驻负载↑
  - **L2 降 settle**（60→~30s）：小改善，逼近 17s 同步下限
  - **L3 保证金重算**：MT5 可行（`mt5_symbols`+`mt5_positions.ContractSize/RateMargin`），
    **MT4 不可行**（无 symbol/margin 配置表）；正是 OPT-0030「三难点」有意绕开的路
  - **L4 接受+文档+测试硬化**：零负载，维持现状盲区

## 待决策（方案选型 — 见分析文档 §6）

> ⚠ **本 OPT 的 AC 取决于方案选型，需用户/业务拍板后定稿**。三种取向成本/覆盖/风险差异大：
> - **方案 A（提速兜底）** = L1+L2：盲区砍到物理下限，代价常驻负载
> - **方案 B（MT5 重算）** = L3：MT5 抓全、MT4 仍盲，工程重
> - **方案 C（接受+文档+测试硬化）** = L4：不动检测、零负载，明确边界

## AC（验收标准 — 方案选定后补全）

- [ ] **（全方案共有）** 把 §4 盲区边界写进 risk-monitor skill（滥用杠杆 + 马丁两节）+
      测试验证指引（「验证 event-gated 规则时持仓须 >90s，且别在两次扫描间开平」）
- [ ] **（方案 A）** 101–110 / 111–120 移入 fast tier；settle 调至 ~30s；dedup 吃 overlap
      重复；新增/扩展 scheduler tier 测试；实测盲区下降
- [ ] **（方案 B）** MT5 路径从 `mt5_symbols`+成交价重算开仓时 margin level；MT4 标注
      不支持；单测覆盖重算公式（含 CEN / 不同合约规模）
- [ ] **（方案 C）** 仅文档 + 测试硬化 + 可选 L2 降 settle
- [ ] `verify.sh` 绿（tsc + vitest + pytest 硬闸）

## 结果

（close 时填）
