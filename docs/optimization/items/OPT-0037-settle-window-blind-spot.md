---
id: OPT-0037
title: event-gated 规则 settle-window「快进快出」漏检盲区（滥用杠杆 + 马丁）
status: done
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

## 方案选型 — **方案 A（提速兜底）已选定**（2026-06-15 用户拍板）

= L1（移 fast tier）+ L2（降 settle）。未采纳 B（MT5 重算，MT4 补不齐）/ C（纯文档）。

## AC（验收标准）

- [x] **L1**：101–110 / 111–120 从 slow 移入 fast tier（`include_*` 改
      `tier in ("all","fast_burst")`）；cache merge 边界改 `_is_fast_tier_rule_id`
      （fast 拥 1-50+101-120 / slow 拥 51-100）；fast tier 下 `scan_interval_min=1`
      使 lookback 缩到 ~180s 减少 overlap 重复读
- [x] **L2**：两条规则 `_SETTLE_SEC` 60 → 30s（仍 > 17s 同步下限，`MODIFY_TIME>=开仓`
      守卫兜底）
- [x] scheduler tier 测试：`test_scheduler_tiers.py` 扩 boundary 断言 +
      fast-tier 成员（leverage/martingale 在 fast 跑、slow 跳）+ 双向 cache 保留
- [x] 文档：risk-monitor skill（架构图 / 两规则行 / SETTLE / scheduler / env flag /
      merge 边界 anti-pattern / Implementation Status）+ 分析文档 §6 决策
- [x] `verify.sh` 绿（backend pytest 242 / tsc / vitest 77；eslint 344 为历史 advisory）

## 结果

**实现**：方案 A 完成（见上 AC 全勾）。`verify.sh` 绿（backend 242→244 / tsc / vitest 77）。

**Stage 1 outsider-review 处理记录**（cold-brief reviewer，6 findings → curate 后 4 真问题）：
- **R3 tier 归属无单一真相源（121+ 未来 band 静默错并）** → **当场修**：`_FAST/_SLOW_TIER_RULE_BANDS`
  常量 + `_MAX_ALLOCATED_RULE_ID` + 派生 `_is_fast_tier_rule_id` + 不变量测试
  `test_tier_ownership_partitions_all_bands`（加新 band 漏分类即 fail）。
- **R4 提速后跳 tick 静默（DEBUG）+ fast job 无 max_instances** → **当场修**：跳过日志
  DEBUG→INFO + 累计 `_fast_tier_skip_count`；fast job 显式 `max_instances=1, coalesce=True`。
- **R1 马丁缺快照新鲜度守卫（settle 30s 放大）** → **拆 [[OPT-0038]]**（马丁 tab 仍隐藏，未面向用户）。
- **R2 catch 窗口在副本延迟尖峰下变窄（360s→150s）** → **拆 [[OPT-0038]]**（自适应 lookback）。
- **R5 dedup seed 用 slow interval** → **live with**：reviewer 自评是安全方向（seed 窗 15min ≫ 180s
  窗口），仅 load-bearing；未来若把 `append_scan_and_events` 改成条件/异步写会破 dedup——靠注释提醒。
- **R6 per-rule「静默 N 小时」无告警** → **live with**：现有 `except Exception` 模式；可观测性增强已记进
  [[OPT-0038]] 可选 AC。
- reviewer 建议「给 event-gated 规则单独锁」**不采纳**——leverage/martingale/burst 共改全局
  `_latest_result` 且走同一 merge，共享 `_scan_lock` 是有意的（拆锁会 race 缓存 merge）。

**未采纳方案**：B（MT5 重算，MT4 无 symbol 表补不齐）/ C（纯文档）。

**部署注意**：prod fast tier 每 60s 会多跑 leverage + martingale（已接受的负载代价）；dev
`BURST_SCAN_ENABLED=false` 不受影响。需 `./deploy.sh` 上线后新扫描才生效。

## 结果

（close 时填）
