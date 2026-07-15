---
id: OPT-0051
title: 后端测试直连云 DB —— verify.sh 闸门在并行开发下不可用（无 mock / 无 connect timeout）
status: ready
priority: P1
area: backend
effort: M
created: 2026-07-15
related: [[OPT-0041]] [[OPT-0050]]
---

## 背景

`verify.sh` 是项目 canonical 的红/绿验收闸门（tsc + vitest + pytest 硬闸，lint 仅 advisory
因 337 历史 error，见 [[project_verify_gate]]）。2026-07-15 一轮 5-agent 并行开发暴露：
**这道闸门在并行场景下基本不可用**。

### 症状

- **backend pytest 单轮实测 733 秒（12 分 13 秒）**
- 有 `.env` 时**会挂死**（不是慢，是卡住）
- 4 个独立 agent 全部被绊住，每个浪费 10–25 分钟，且**各自独立重新发现同一批 41 个既有失败**

### Root cause

后端测试**直连真实云数据库，没有 mock、没有 connect timeout、没有严格 skip guard**：

- 云 MySQL `4.144.33.170:3306`（实测 `ESTAB`，`wchan=poll_schedule_timeout`，6 分半 CPU 仅 1 秒）
- 云 PostgreSQL `4.190.210.8:5432`（风控V2 案卷层）

测试受 `.env` 门控 —— 有 `.env` 就真连库跑；没有就 skip。这导致一个**恶劣的伪绿**：
`test_risk_cases_api::test_watchlist_end_to_end_with_fixtures` 在 HEAD 上"通过"，其实是
**跳过**了；带 `.env` 跑同样失败。（两个独立 agent 各自在干净 worktree 复现确认。）

### 41 个既有失败早就查明了

`docs/optimization/done.md` 的 OPT-0045 收尾记录写着：

> 41 failed 为 main 既有日期 fixture 炸弹（=OPT-0041 范围）

即：今天四个 agent 各花 12 分钟重新发现的东西，**一个月前就已归档并派工**（[[OPT-0041]]，
已 claim，backlog 标注「闸门修复，最先合」）。这本身说明闸门的**失败噪声掩盖了真信号** ——
没人能从 41 个红里看出第 42 个红是不是自己弄的。

### 并行开发下的次生问题

各 agent 用 `until ! pgrep -f "pytest tests/"; do sleep 5; done` 等待循环判断"pytest 跑完没"，
但该 pattern **匹配全机器的 pytest，包括别的 agent 的** → 互相等待，串行化。
实测同时有 5 个等待循环存活 6–20 分钟。

## 交付内容（建议，实施者可调整）

1. **connect / query timeout**：给测试用的 DB 连接加超时（连不上/跑不完 → 快速失败而非挂死）。
   至少让挂死变成可诊断的红。
2. **收紧 skip guard**：`.env` 存在 ≠ 该跑集成测试。改用显式 marker（如
   `@pytest.mark.integration` + `pytest -m "not integration"` 为默认），让**默认 pytest 不碰网络**。
   这同时消除"没 .env 就伪绿"的陷阱。
3. **verify.sh 分层**：默认只跑离线闸门（tsc + vitest + unit pytest，秒级）；
   集成测试走单独入口（`verify.sh --integration` 或 CI）。让开发者/agent 能拿到快速红绿。
4. **已知失败基线**：给 verify.sh 一个 known-failures 清单或 baseline diff 能力 ——
   让"我有没有引入新失败"变成一个可自动回答的问题，而不是每个人手动开 worktree 对照。
   （2026-07-15 有两个 agent 各自手工开 worktree 跑 pristine HEAD 才敢下结论。）

## AC

- [ ] 默认 `pytest tests/` 不发起任何真实 DB 网络连接（或全部快速超时失败，不挂死）
- [ ] 默认 `verify.sh` 在 60s 内给出红/绿（当前 733s+ 或挂死）
- [ ] 集成测试有独立入口，且 `.env` 缺失时**明确报告 skip 数量**而非静默伪绿
- [ ] verify.sh 能回答"相比 main 基线，本次改动新增了哪些失败"

## 开放问题

1. **与 [[OPT-0041]] 的关系** —— 0041 修的是日期 fixture 腐烂（那 41 个失败的内容）；
   本 OPT 修的是闸门**机制**（为什么这些失败没被及早发现、为什么跑一次要 12 分钟）。
   0041 标注「最先合」，本 OPT 大概率应排在 0041 之后（等测试转绿再改机制，否则改完还是红）。
   **由用户决定顺序。**
2. **mock 还是 testcontainer 还是纯 marker 隔离** —— 方案 2 的 marker 隔离最省事，
   但会让集成测试彻底没人跑。是否需要 CI 侧兜底？

## 注意

2026-07-15 有一批未提交的工作区改动（净赚列 + 三个高危口径修复，5 个 agent 产出，28 个文件）。
**开工前先确认这批改动的去向**。

pytest 的真实基线（两个 agent 在干净 worktree 独立测得，可直接引用）：
```
pristine HEAD:  41 failed, 490 passed
```
失败集中在 `test_burst_open_aggregated`(17) / `test_hedge_open_aggregated`(14) /
`test_leverage_abuse_filter`(3) / `test_rule_martingale_service`(1) / `test_net_profit_sort`
—— 全部 = [[OPT-0041]] 范围。
