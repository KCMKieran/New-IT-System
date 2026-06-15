---
id: OPT-0038
title: event-gated 规则 fast-tier 硬化（马丁快照新鲜度守卫 + 自适应 lookback）
status: done
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

**交付（branch `opt/event-gated-tier-hardening`，2 commit）：**

- **R1（达成，但实现优于原 AC）** — 马丁快照新鲜度守卫。原 AC 要求镜像滥用杠杆的
  `mt4_users.MODIFY_TIME >= 开仓时间`。实现初版照此做了，但 Stage 1 outsider-review #1 指出
  **马丁读的是 `mt4_trades`/`mt5_positions`，跟 MODIFY_TIME 不是同一张表**——MODIFY_TIME 在
  保证金重算时跳、与成交行复制独立，守卫可能在残缺 ladder 上放行。改为**对实际读的表直接自检**：
  要求快照 ladder 自身最新腿 `latest_open_dt >= gate_latest_open_dt`（事件门控检测到的开仓）。
  更正确、且省掉 `_query_modify_time_map` 一次查询，并消掉 NULL/future-clock/query-throws 三个
  失败模式。`rule_martingale_detect` 前置过滤 + INFO log。
- **R2（达成）** — fast-tier 自适应 lookback。调度器记 `_event_gated_last_scan_at`，
  `lookback = (now − last_success) + 120s buffer`，capped 1800s；两条 scan 加
  `lookback_override_sec` 参，scan-now('all')/slow 保持固定窗口。Stage 1 review #2 指出原实现
  **时间戳无条件推进、outage 期间会漏扫**——改为 `event_gated_ok` 标志门控，任一 scan 抛异常则
  不推进，下一 tick 自动加宽补齐。
- **测试** +14（马丁 R1 ladder 自检 4 / R2 override 2；杠杆 R2 override 2；调度器自适应+
  outage 安全 6）。`verify.sh` 绿，backend 257 passed。

**与 AC 偏差：** R1 用 ladder 自检替代 AC 字面的 MODIFY_TIME 代理（review 驱动，更正确）。
R3（per-rule 静默告警端点）仍标"可选"，只落了 log 级观测，未做端点 → 见 follow-up。

**Stage 1 outsider-review 处理记录（独立后台线程冷审）：**
- #1 残缺 ladder silent-miss → **当场修**（commit f29493e，ladder 自检）。
- #2 outage 时间戳推进漏扫 → **当场修**（commit f29493e，成功才推进 + 注释修正 review #7）。
- #4 NULL/future MODIFY_TIME / query-throws → 被 #1 重构**吸收**（不再依赖 MODIFY_TIME）。

**Follow-up（live-with，未修）：**
- **三时钟偏移（review #2/#3）**：gate 下界用 MySQL `NOW()`，settle/adaptive 用 app 时钟。
  **pre-existing**（固定窗口同款查询），非本 OPT 引入；常态 NTP 秒级偏移被 120s buffer 吸收。
  根治需把 gate 下界改成 app 侧绝对时间戳传入（`OPEN_TIME >= %s`）——独立改动，未来按需立单。
- **检测器静默可观测（review #5 = AC R3 可选项）**：stale-skip 速率 / lookback 撞 1800s cap
  次数 / "N tick 零告警"心跳，目前只有 log，无聚合指标/告警端点。未来观测类 OPT 收口。
