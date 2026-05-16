# 优化追踪器（Optimization Tracker）

记录 **想优化什么**、**进行中是什么**、**已完成什么** 的唯一来源。

为单人开发者 **多个 Claude CLI 并行运行** 的场景设计 —— 下面的 claim 纪律确保两个 session 不会同时干同一件事。

## 文件结构

| 文件 | 作用 |
|---|---|
| `README.md` | 本文 —— 流程和字段规范 |
| `backlog.md` | 索引：WIP / Ready / Ideas 三张表。每次都从这里看起。|
| `done.md` | 已完成日志，append-only |
| `items/OPT-NNNN-<slug>.md` | 每个值得记录的 item 一个文件（细节都在这里）|

## 字段规范

| 字段 | 取值 |
|---|---|
| **ID** | `OPT-NNNN` —— 4 位单调递增，永不复用。下一个 = 现有最大值 + 1。 |
| **status** | `idea`（想法）→ `ready`（可干）→ `wip`（进行中）→ `done`（完成） / `dropped`（放弃）|
| **priority** | `P0` 立刻（阻塞/线上事故）· `P1` 本周 · `P2` 本月 · `P3` 有空再说 |
| **area** | `frontend` / `backend` / `db` / `infra` / `docs` / `mixed` |
| **effort** | `S` < 2 小时 · `M` 半天 · `L` 1 天 · `XL` 多天 |

> 字段名（`status`/`priority`/...）和取值（`ready`/`P1`/...）保留英文，因为 skill 的 bash 命令会 grep 它们；中文只用在标题、描述、章节里。

### 状态流转

```
idea ──scope──▶ ready ──claim──▶ wip ──finish──▶ done
  │                │              │
  └────drop────────┴──────────────┴──▶ dropped
```

- **idea**：粗想法，还不能 claim。在 backlog.md 的 Ideas 表里；item 文件可有可无。
- **ready**：scope 清晰、AC 已定义、item 文件存在。任何 Claude session 都可以拿来做。
- **wip**：已 claim。row 在 backlog.md 的 WIP 表，带 branch 名和 claim 日期。
- **done**：已合到 main。row 追加到 done.md，从 backlog.md 删掉。
- **dropped**：决定不做。原因写在 item 文件的 **结果** 段。

## 工作流

### A. 新增 idea（你，随手）

1. 打开 backlog.md，看现有最大的 OPT-NNNN，用下一个号。
2. 在 **Ideas** 表加一行 —— 标题 + 一句话备注。
3. 如果能写超过 2 行背景 → 用下面的模板新建 `items/OPT-NNNN-<slug>.md`。
4. Commit：`docs(opt): file OPT-NNNN <slug>`

### B. idea → ready（你或 Claude，scope 阶段）

1. 确保 item 文件存在且 **验收标准** 已填完整。
2. backlog.md 里把行从 **Ideas** 移到 **Ready**，填 `priority`、`effort`。
3. 改 item 文件 frontmatter：`status: ready`。
4. Commit：`docs(opt): scope OPT-NNNN`

### C. Claim 并开始（Claude session）

1. **先读 backlog.md 的 WIP 表**。目标 ID 已在表里 → 换一个。
2. 把行从 **Ready** 移到 **WIP**。填 `branch`（如 `opt/sqlite-perf`）和 `claimed` 日期。
3. 改 item 文件：`status: wip`。
4. **写代码前立刻 commit**：`chore(opt): claim OPT-NNNN`。这样其他 session `git pull` 能看到 claim。
5. `git checkout -b opt/<slug>`
6. 在 branch 上干活。

### D. 完成（Claude session，合 main 之后）

1. branch 合到 main。
2. backlog.md 里把行从 **WIP** 删掉。
3. done.md 追加：`| YYYY-MM-DD | OPT-NNNN | <commit SHA> | <标题> |`
4. 改 item 文件：`status: done`，填 **结果**（实际交付什么、和 AC 的偏差、留下的 follow-up）。
5. Commit：`docs(opt): close OPT-NNNN`

### E. 放弃

1. 改 item 文件：`status: dropped`，**结果** 段写原因。
2. done.md 的 "已放弃" 段追加：`| YYYY-MM-DD | OPT-NNNN | <标题> | <一句话原因> |`
3. backlog.md 里把行从所在表删掉。
4. Commit：`docs(opt): drop OPT-NNNN`

## 并行 session 安全

整个系统的核心规则只有一条：**动 item 之前，row 必须先以你的 branch 进 WIP。**

- 两个 Claude session 同时启动：都读 backlog.md，都看到 WIP 是空的，都想 claim OPT-0003。
  - 缓解：claim 是一次很小的 commit，**写任何代码之前**就做。第二个 session 在自己 claim commit 之前 `git pull` 会看到冲突，立刻中止。
- 后台 session：不会自动 pull。你（人）每次启动新 session 时记得让它先 `git pull` 再重读 WIP 再 claim。

如果还是冲突了，第二个 session 把 row 移回 Ready，挑别的 item 做。

## 什么时候可以不建 item 文件

非常小的活（< 30 分钟、AC 一眼看穿）可以直接内联进 backlog.md 的 Ready 表（标题写长一点），不建 items/ 文件。**但默认建文件**，因为未来的你 / 并行的 Claude 需要背景。

## item 文件模板

```markdown
---
id: OPT-NNNN
title: <简短标题>
status: idea | ready | wip | done | dropped
priority: P0 | P1 | P2 | P3
area: frontend | backend | db | infra | docs | mixed
effort: S | M | L | XL
created: YYYY-MM-DD
related: [[OPT-NNNN]]   # 可选，链接到相关 item
---

## 问题
<症状或痛点，如果是用户可感知的就写用户角度>

## 背景
<相关文件、当前实现、为什么现在要做>

## 假设 / 待验证
- [ ] <设计方案之前要先确认的事>

## 验收标准
- [ ] <可衡量 / 可验证的产出>

## 笔记
<探索过程随手记 —— 正反观点都写下来>

## 结果
<done/dropped 时填：commit SHA、实际交付了什么、和 AC 的偏差、follow-up>
```
