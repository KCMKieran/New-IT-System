---
id: OPT-0038
title: event-gated 规则 fast-tier 硬化（马丁快照新鲜度守卫 + 自适应 lookback）
status: wip
priority: P2
area: backend
effort: M
created: 2026-06-15
related: [[OPT-0037]], [[OPT-0033]], [[OPT-0030]]
---

## 问题

[[OPT-0037]] 把滥用杠杆 + 马丁两条 event-gated 规则移到 fast tier（60s）+ settle 60→30s。
其 Stage 1 outsider-review 发现两条**真问题**，因属较重改动且马丁前端 tab 仍隐藏（未面向
用户），拆到本 hardening OPT 跟进（OPT-0037 当时直接进 Stage 2）。

### R1 — 马丁缺「快照新鲜度」守卫（settle 30s 放大）

滥用杠杆 `rule_leverage_abuse_detect` 有 `mdt < a["last_open_dt"]: continue` 守卫——
保证金快照没追上开仓就跳过、靠 overlap 下轮重试。**马丁 `rule_martingale_detect` 没有
等价守卫**：直接读 `_query_mt4/mt5_open_positions` 当前持仓，若持仓行还没从副本同步过来，
就按**残缺快照**算 `add_count` / `floating_pnl` → 静默错判（少报或误判方向），无 skip、
无 log、无重试信号。OPT-0037 把 settle 降到 30s 提高了读到未同步快照的概率，放大了这个
**OPT-0033 起就存在**的缺口。

### R2 — fast-tier catch 窗口在副本延迟尖峰下变窄（OPT-0037 引入）

OPT-0037 把 lookback 从 ~420s（slow 5min）缩到 ~180s（fast 1min cadence + buffer）。
配 settle 30s，单 tick processable 窗口 = 开仓 30s–180s old = 150s 宽。**含义**：若某笔
开仓的快照在 ~150s 内没同步（高负载副本延迟尖峰），该开仓**老化出窗口、永不重查 →
静默漏报**。旧配置容忍 ~360s，新配置只容忍 ~150s——常态延迟（17–60s）无碍，但负载尖峰
下韧性下降。叠加锁竞争跳 tick（[[OPT-0037]] R4 已加可观测）会进一步吃掉余量。

## AC

- [ ] **R1**：给马丁持仓快照加新鲜度守卫——评估某 `(server,login,symbol,direction)` 组前，
      要求该账户 `mt4_users.MODIFY_TIME >= 该组最新开仓时间`（镜像滥用杠杆的 `modify_dt`
      守卫；归 UTC 比较）。没追上就跳过、靠 overlap 重试。+ 单测（未同步快照不误判）。
- [ ] **R2**：fast-tier 自适应 lookback——记 `last_successful_scan_at`，
      `lookback_sec = (now - last_success) + buffer` 取代固定 `1*60+120`，跳过的 tick 由
      下一 tick 自动补窗，消除「跳 tick / 副本延迟 → 开仓老化出窗口」的漏报。+ 单测。
- [ ] **（可选，R3 观测延伸）** 每规则 `last_emitted_at` / `consecutive_failures` 指标，
      让「某检测器静默 N 小时」可告警（当前 success 与 total-failure 下游无法区分）。
- [ ] `verify.sh` 绿。

## 背景

完整 review 记录见 [[OPT-0037]] 结果段 + `docs/analysis/risk-monitor-settle-window-blind-spot.md`。
R3（tier 单一真相源 + 守卫测试）、R4（跳 tick 可观测 + `max_instances`/`coalesce`）已在
OPT-0037 当场修。R5（dedup seed 用 slow interval，方向安全）/ R6（per-rule 静默告警）
OPT-0037 live-with。

## 结果

（claim + 做完时填）
