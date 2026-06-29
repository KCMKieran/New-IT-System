---
id: OPT-0041
title: 修复 backend pytest date-rot —— 测试种子日期硬编码超过 30 天保留窗导致闸门长期红
status: ready
priority: P1
area: tests
effort: S
created: 2026-06-29
related: [[OPT-0040]] [[OPT-0014]] [[OPT-0007]]
---

## 问题

`./verify.sh` 的 backend pytest 硬闸门当前在 `main` 上**全局标红**（OPT-0040 close 时实测 40 failed / 281 passed）。这 40 个失败**与功能代码无关**，是**测试时效腐烂（date-rot）**：多个测试把种子数据的 `scanned_at` / `window_start` / `window_end` **硬编码**在 `2026-05` 附近，而系统当前日期已是 `2026-06-29`，相差 > 30 天。

`risk_monitor_db.py` 的 `_RETENTION_DAYS = 30`（`backend/app/core/risk_monitor_db.py:149`），`append_scan_and_events()` 在每次写入时执行 30 天保留清理（`DELETE ... WHERE scanned_at < datetime('now','-30 days')`）。测试 seed 一写进去就被立即清掉 → 查询返回 0 行 → 断言失败。

**影响**：闸门红 = verify.sh 无法认证任何 merge（OPT-0040 就是在「slice 全绿、但闸门因预存在 date-rot 红」的情况下靠对照实验人工判断后 merge 的）。这把红会随时间永久存在并逐步污染更多测试。

## 背景 / 涉及文件

确认带硬编码 `2026-04/05` 种子日期的测试文件（`grep -lrE "2026-0[45]" backend/tests/`）：
- `backend/tests/test_net_profit_sort.py`
- `backend/tests/test_hedge_open_aggregated.py`
- `backend/tests/test_burst_open_aggregated.py`
- `backend/tests/test_leverage_abuse_filter.py`
- `backend/tests/test_burst_open_scheduler_prev_alerts.py`（疑似）
- `backend/tests/test_scheduler_tiers.py`（疑似）

实际失败集以 `cd backend && python -m pytest -q` 跑一遍为准（OPT-0040 worker 报的失败集是前 4 个）。

根因代码：
- `backend/app/core/risk_monitor_db.py:149` `_RETENTION_DAYS = 30`
- `append_scan_and_events()`（约 :1620-1745）写入路径里的 30 天 purge
- 启动期 purge 在 :635-641

## 修复方向（执行时定）

把这些测试的种子时间从**固定字符串**改成**相对 now**，使其落在 30 天保留窗内。推荐做法（择一，保持测试语义不变）：
1. 在测试里用 `datetime.now(timezone.utc) - timedelta(...)` 动态生成 `scanned_at` / `window_*`，格式化成现有的 `"%Y-%m-%dT%H:%M:%SZ"`；或
2. 提供一个共享 fixture / helper（如 `recent_iso(minutes_ago)`）集中产出近 now 的时间戳，所有受影响测试改用它（避免下次再腐烂）；或
3. 若某些测试本意是验证保留清理边界，则显式 monkeypatch `_RETENTION_DAYS` 或冻结时间（freezegun / 注入 now），让意图清晰。

⚠ 不要为了让测试通过去**改动功能代码或调大 `_RETENTION_DAYS`** —— 30 天保留是生产约定（[[OPT-0007]] / [[OPT-0014]] 语境），问题在测试硬编码日期，不在保留策略。

## 验收标准（AC）

1. `cd backend && python -m pytest -q` —— 所有因 seed-date-rot 失败的用例转绿；无新增失败。
2. `./verify.sh` 三闸门（tsc + vitest + pytest）全绿。
3. 修法对时间稳健：用相对 now 的种子（或冻结时间），保证未来任意日期跑都不再腐烂（可在 PR 描述里说明为什么不会再 rot）。
4. 不改 `_RETENTION_DAYS`，不改任何被测功能代码的行为。

## 结果

（实施后填写）
