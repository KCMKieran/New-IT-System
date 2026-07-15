---
id: OPT-0050
title: 基线幂等测试污染 prod PG（940 假行）+ 随 roster 增长挂死
status: ready
priority: P1
area: backend
effort: S
created: 2026-07-15
related: [[OPT-0041]] [[OPT-0049]] [[OPT-0051]]
---

## 背景

`backend/tests/test_case_metrics_service.py::test_baseline_rerun_same_day_does_not_duplicate_rows`
（AC5，验证同日重跑不产生重复行）有两个问题，2026-07-15 实测确认。

### 问题 1：往 prod PG 写 940 行假数据，且不清理

测试用 `metric_date = date(2026, 1, 2)`（注释称 "fixed past date, never collides with real runs"）
调 `cms.run_daily_baseline(metric_date=md)`。但 `run_daily_baseline` 第一步是
`SELECT user_id, country FROM risk_cases`（`case_metrics_service.py:325`）拉**整个 roster**，
然后给**全部客户**写快照。

而 `finally` 块只清理它自己造的那**一个** fixture uid：

```python
cur.execute("DELETE FROM case_metrics_daily WHERE user_id = %s", (uid,))  # uid = 990_000_601
```

→ 另外 939 个**真实客户**的行留在库里。实测：

```
metric_date     行数   floating_pl   equity   profit_all   rebate_all
  2026-01-02    940       940          940       940         940      ← 假的
  2026-07-13    160         0          160       160         160
  2026-07-14    178         0          178       178         178
  2026-07-15    938         0          938       938         938
```

行数不会增长（`ON CONFLICT (user_id, metric_date)` 覆盖），但：
- 这是一个**从未发生过的历史快照**，日期 2026-01-02
- /risk-watchlist 的案卷卡**会展示快照历史**（`docs/features/risk-watchlist.md:159`），假数据会出现在 UI
- 对**约 3 个没有当前快照的客户**，这批假数据成为他们的"最新快照"
- 更隐蔽：该表的 `floating_pl` 列**只有这 940 行假数据有值**（真实快照全 NULL），
  因为该列是 2026-07-15 新加的、真实 job 还没跑过 —— 假数据比真数据"更完整"

### 问题 2：随 roster 增长无限变慢，现已挂死

因为跑的是全 roster，且交易腿是生涯全量扫描（见 [[OPT-0049]]），而**整个 job 要跑两遍**
（测的就是重跑幂等）：

```
940 客户 → 按 300 分块 = 4 块 × 生涯扫描 26.96s/块 × 2 轮 ≈ 3.6 分钟起
```

实测该测试进程：`ESTAB → 4.144.33.170:3306`（云 MySQL），`wchan=poll_schedule_timeout`，
**6 分半内 CPU 只用了 1 秒** —— 纯网络等待。

**关键**：测试注释写着 "this runs over the whole (**small**) roster — unknown userIds produce
empty MySQL aggregates, so the pull stays cheap"。这个假设**当初成立**（roster 接近 0），
是 rule 122「宽网观察」（commit `e068f52`, 2026-07-14）把 roster 从 178 撑到 940 之后
**悄悄毒化**的 —— 没人改过这个测试一行代码。

2026-07-15 一轮 5-agent 并行开发中，**4 个 agent 全被它绊住**，每个独立浪费 10–25 分钟。

## 交付内容

1. **测试隔离 roster**：让 `run_daily_baseline` 只跑测试自己造的 fixture 客户，不碰真实
   roster。可选做法：(a) 给 `run_daily_baseline` 加可选 `roster` 参数（测试传 `[uid]`）；
   (b) 测试内 monkeypatch roster 查询；(c) 用独立的测试 PG schema。**方案 (a) 最干净且对
   生产代码侵入小**，但需确认不破坏现有调用点。
2. **清理已有的 940 行假数据**：`DELETE FROM case_metrics_daily WHERE metric_date = '2026-01-02'`
   —— 执行前先确认该日期无其他用途（见开放问题）。
3. **cleanup 收紧**：`finally` 块按 `metric_date` 清理而非只按 uid（防御性，即使 roster 隔离了）。

## AC

- [ ] 该测试不再对真实 roster 的客户写任何行
- [ ] 该测试耗时 < 10s（当前 ≥ 3.6 分钟，实测挂到 6 分半仍未完成）
- [ ] `case_metrics_daily` 中 `metric_date = '2026-01-02'` 的行被清理干净
- [ ] 测试仍能验证原 AC5 语义（同日重跑不产生重复行）

## 开放问题（需用户决策）

1. **那 940 行假快照的清理时机** —— 主线程已确认是测试产物（`created 2026-07-13`，
   2026-07-15 被 agent 的测试运行刷新了 `floating_pl`）。是 prod PG 的真实数据，
   删除前需用户确认 `metric_date='2026-01-02'` 无其他用途。
2. **是否并入 [[OPT-0041]]** —— 0041（`opt/test-seed-date-rot`，已 claim，worktree 存在，
   backlog 标注「闸门修复，最先合」）负责的是**日期 fixture 腐烂**（41 个既有失败）。
   本 OPT 是 **roster 增长 + prod 污染**，root cause 不同但同属"测试闸门可信度"。
   0041 已被 claim，**不擅自并入** —— 由用户决定是合并还是并行。

## 注意

- 2026-07-15 有一批未提交的工作区改动（净赚列 + 三个高危口径修复，5 个 agent 产出，
  28 个文件），`test_case_metrics_service.py` 在其中（有 agent 修过 2 个 positional-arg
  测试 + 加了 4 个新测试）。**开工前先确认这批改动的去向**。
- 该测试挂死的**表象**曾被误判为"连不上 MySQL"。实测澄清：**连接是 ESTABLISHED 的**
  （`4.144.33.170:3306`），是**查询本身跑不完**。别按"网络不通"的方向排查。
