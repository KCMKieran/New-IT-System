---
id: OPT-0011
title: Risk-monitor 游标式增量扫描（替代时间窗重叠轮询）
status: done
priority: P1
area: backend
effort: L
created: 2026-05-16
related: [[OPT-0003]] [[OPT-0012]]
---

## 问题

当前 Burst Open / Quick Open-Close / Quick Profit 三类扫描都用"重叠时间窗"拉数据：

```sql
WHERE OPEN_TIME >= NOW() - (scan_interval_min * 60 + 30) SECOND
```

每轮把上一轮已经扫过的数据**再扫一遍**，靠 dedup 兜底（`(rule_id, server, login, symbol, first_open_time)` 去重）。痛点：

1. **MySQL 每轮重复扫**：在 10 min 节拍下，每条数据被扫了 ~1 遍；如果想把 Burst 提到 1 min 节拍，同一条数据会被扫 ~6 遍，MySQL 压力线性放大
2. **节拍提速被卡死**：理论下限是「重叠窗口 ≥ 扫描周期」，否则两次扫描重叠区不够会漏数据；这反过来限制了 Burst Open（业务窗口仅 3 秒）做不到秒级响应
3. **dedup 逻辑膨胀**：每条规则都要维护自己的 seen-set，跨进程重启后还要从 SQLite seed 回来（Quick Profit 已经踩过这个坑，详见 SKILL.md「Restarts seed dedup from SQLite」）

## 背景

- **当前代码**：`backend/app/services/risk_monitor_service.py` 里 `_collect_*` 函数，所有 `WHERE OPEN_TIME >= cutoff` 都是相对当前时间的滑窗
- **MT5 特殊性**：`mt5_deals.Timestamp` 是 Windows FILETIME（不是 Unix），增量游标要分别为 MT4 / MT5 维护
- **现有"游标式"做法可参考**：Gap Trade 已经按 `window_date` 切窗，但那是日级别，不是真正的事件流游标
- **业界对照**：CDC（Change Data Capture）的简化版 —— 我们没法订阅 binlog（broker MySQL 不归我们管），但可以在自己这边维护"上次看到的最大主键 / 时间戳"游标，效果类似

## 假设 / 待验证

- [ ] MT4 `mt4_trades` 是否有 ticket / id 主键可以作为单调游标？还是只能用 `OPEN_TIME`（秒级，可能同秒多笔）
- [ ] 同秒多笔如何处理：用 `(OPEN_TIME, TICKET)` 复合游标？还是 `WHERE OPEN_TIME > last_time OR (OPEN_TIME = last_time AND TICKET > last_ticket)`
- [ ] MT5 FILETIME 的精度（100ns ticks）足够单调吗？同一 microsecond 多笔的概率
- [ ] 当前 dedup 逻辑（`seen_set`）能否在游标方案下完全删掉？或者需要保留作为双保险？
- [ ] 游标存哪：新建 `scan_cursors` 表（`rule_type, server` 主键）？还是塞进 `*_config` 表的 JSON 字段？
- [ ] 游标丢失/损坏的恢复策略：回退到时间窗模式 N 分钟做 warmup？

## 验收标准

- [ ] **设计稿先行**：写一份 `docs/features/risk-monitor-cursor-scan.md`，明确游标 schema、各 rule 的 SQL 改造点、恢复策略
- [ ] 游标持久化到 SQLite（建议新建 `scan_cursors` 表）；每轮扫描 commit 前更新游标
- [ ] 替换 Burst Open + Quick Open-Close 的时间窗 SQL（Quick Profit 因为需要"sliding window 内的全量求和"不能完全游标化，保留时间窗）
- [ ] 单元测试：覆盖「游标空（冷启动）」「游标比 MySQL 还新（数据回滚？）」「同秒多笔」「跨进程重启」四种场景
- [ ] Benchmark：改前 / 改后同样 10 min 节拍下，MySQL 端 query rows scanned 对比，预期 ÷5 以上
- [ ] **不破坏现有 dedup 路径**：保留旧路径作为 fallback，env flag `CURSOR_SCAN_ENABLED` 切换

## 笔记

**与 OPT-0012 的关系**：OPT-0011 是地基。没有游标，OPT-0012 的 fast tier（60s 节拍）会让 MySQL 每条数据被扫 ~10 遍，反向放大压力。所以执行顺序必须 0011 → 0012。

**为什么 Quick Profit 不能完全游标化**：它的本质是「过去 30 min 窗口内的 P&L 求和」，是聚合而非事件。可以用游标拉**新成交**，但已成交的旧数据仍要参与求和 —— 所以 Quick Profit 改造收益小，可以暂不做。

**与 OPT-0003 的关系**：OPT-0003 关注 SQLite 端的查询/写入性能；OPT-0011 关注 MySQL 端的查询负担。两边互补，不冲突。

**反对意见**（先记下来防止只听一面之词）：
- 「现在没出过事，为什么要改？」—— 当前 MySQL 没瓶颈是因为 10 min 节拍下数据量小。问题是想加速到 1 min 时硬撞墙。如果业务永远不需要秒级响应，这个 item 可以 drop
- 「dedup 已经能正确工作了」—— 是的，但 dedup 是事后补救，游标是源头解决。两套机制并存增加维护成本

## 结果

**Commit**: `4ee87ae` (impl) + `c59e2f8` (claim) on `feat/risk-monitor-realtime`,
merged to main as `ceb21c4` on 2026-05-17.

**实际交付**：
- 新表 `scan_cursors (rule_type, server, cursor_time, cursor_id, updated_at)` 加进 `risk_monitor_db.py` 主 schema（`IF NOT EXISTS`，prod 启动自动 migrate）
- 助手 `get_scan_cursor / update_scan_cursor / reset_scan_cursor`，upsert WHERE 子句拒绝 HWM 回退
- MT4/MT5 query 助手新增可选 `cursor_time / cursor_id` 参数：开启 cursor 时 SQL 改为 `WHERE (col > %s OR (col = %s AND id > %s))`；关闭时走旧 `WHERE col >= DATE_SUB(NOW(), INTERVAL ... SECOND)` 路径完全不变
- MT5 cursor_time 用 20 字符 zero-pad 的 FILETIME（SQLite TEXT 字典序匹配数字序；MySQL 强转回 BIGINT）
- HWM 计算函数 `_compute_cursor_hwm` (burst) + `_compute_hwm_mt4 / _compute_hwm_mt5` (quick OC)
- env flag `CURSOR_SCAN_ENABLED`（默认 false，且只有 `true` 字面量触发）
- Quick Profit 未游标化（聚合规则不适合 strict-greater-than）

**测试**：17 个 cursor 单元测试（test_scan_cursors.py）+ 全量回归 84/84 通过

**Prod 状态**（2026-05-17 上线 / `719eb66` 当晚开 flag）：
- 代码已 merge + deploy；schema 自动 migrate 完成
- `CURSOR_SCAN_ENABLED=true` 已写进 `docker-compose.prod.yml` + 容器 restart
- Dev 也开了同 flag（`backend/docker-compose.dev.yml`），但 dev `BURST_SCAN_ENABLED=false`，所以 dev 端是 no-op（scheduler 根本不跑，cursor 不会被读到），开着只是为了让 dev SSE/UI 路径完整可观测

**周末上线的玄机**（719eb66 commit message 备忘）：选周末跑 stage-2/3 是因为外汇市场 Sat 05:00 → Mon 05:00 HKT 休市 —— cold-start cursor 整周末维持空值，到周一首单时才被自然 seed，零数据风险；周一开盘前若有任何不对，flip 回 false + `./deploy.sh` 没有 row 被写入。

**Follow-up**：
- 上 flag 后观察 MySQL `rows scanned` 应该 ÷5 以上
- 是 OPT-0012 fast tier 的前置条件（fast tier 60s 节拍若无 cursor 会 ×10 MySQL 压力）
