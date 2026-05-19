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

> 设计原则：**单一 OPT 在 main 上的 commit 数最少化**，把 file/claim/close 三个流程节点压成尽量少的 git 事件，留出空间给真正的代码 commit。

### A. 新增 idea（仅 file，留作 backlog）

适用：只记一笔，未来再决定做不做。

1. 打开 backlog.md，看现有最大的 OPT-NNNN，用下一个号。
2. 在 **Ideas** 表加一行 —— 标题 + 一句话备注。
3. 如果能写超过 2 行背景 → 用下面的模板新建 `items/OPT-NNNN-<slug>.md`。
4. Commit：`docs(opt): file OPT-NNNN <slug>`

### A+. file 后立刻开始干（file + claim 合并）

适用：**OPT 的主流路径**。新想法立刻就要做，没必要分两步。

1. 同 A 步骤 1–3，但 item frontmatter 直接写 `status: wip`，backlog.md 把行直接加进 **WIP** 表（跳过 Ready）。
2. **写代码前立刻 commit**：`chore(opt): file+claim OPT-NNNN <slug>`。
3. `git checkout -b opt/<slug>`，开干。

> 跟原来"先 docs(opt): file 再 chore(opt): claim"两条 commit 比，这里合并成一条。砍掉的那条对未来的 reader 没价值（中间没有他人参与的 window），但给 main 历史挪出了空间。

### B. idea → ready（你或 Claude，scope 阶段）

1. 确保 item 文件存在且 **验收标准** 已填完整。
2. backlog.md 里把行从 **Ideas** 移到 **Ready**，填 `priority`、`effort`。
3. 改 item 文件 frontmatter：`status: ready`。
4. Commit：`docs(opt): scope OPT-NNNN`

### C. Claim 并开始（item 已在 Ideas/Ready，现在拿来做）

1. **先读 backlog.md 的 WIP 表**。目标 ID 已在表里 → 换一个。
2. 把行从 **Ideas / Ready** 移到 **WIP**。填 `branch`（如 `opt/sqlite-perf`）和 `claimed` 日期。
3. 改 item 文件：`status: wip`。
4. **写代码前立刻 commit**：`chore(opt): claim OPT-NNNN`。这样其他 session `git pull` 能看到 claim。
5. `git checkout -b opt/<slug>`
6. 在 branch 上干活。

> 如果是**全新的 idea + 立刻就要做**，走 A+ 一步完成，不要拆成 A 然后 C。

### D. 完成（3 stage：review hook → merge+close → lesson hook）

> **核心变化**：动作 4 不再是单步 close，而是 3 stage 流程，**两端各加一个用户决策 hook**：
> - 前置 hook（Stage 1）：merge 前问要不要 outsider-review
> - 收尾 hook（Stage 3）：close 后问要不要生成技术课件
> - 中间（Stage 2）：合并 + close 一次性 commit，不再产出独立 `docs(opt): close`

前置：opt branch 上代码全 committed，**还没 merge 到 main**。

#### Stage 1 — outsider-review hook（merge 前）

**总是问用户**（不基于 effort 隐式默认）。两个选项：
- Yes → 跑 [`outsider-review`](../../.cursor/skills/outsider-review/SKILL.md)，对每条 finding 单独问处理方式：当场修 / 立新 hardening OPT / live with（记 follow-up）
- No → 直接进 Stage 2

#### Stage 2 — Merge + close

```bash
git checkout main && git pull
git merge --no-commit --no-ff opt/<slug>
# 此刻 working tree 已 staged 了 branch 所有改动，但 merge 还没落盘
```

编辑追踪器文件：
- backlog.md：把行从 **WIP** 删掉。
- done.md 追加：`| YYYY-MM-DD | OPT-NNNN | — | <标题> |`（SHA 列留 dash —— merge SHA 在 commit 写盘前不存在，未来用 `git log --grep="OPT-NNNN" --merges` 找回来）。
- item 文件：`status: done`，填 **结果**（实际交付什么、和 AC 的偏差、Stage 1 review finding 处理记录、follow-up）。

把 docs 改动 stage 然后一次 commit：

```bash
git add docs/optimization/
git commit -m "Merge branch 'opt/<slug>' — OPT-NNNN closed: <一句话总结>

<close body：实际交付、与 AC 偏差、follow-up、Stage 1 review 处理记录>

Co-Authored-By: ..."
```

效果：单个 OPT 在 main 历史的 commit 总数从 5 条（file → claim → feat → merge → close）降到 **3 条**（file+claim → feat → merge）。流程税 -40%，零信息损失。

#### Stage 3 — 课件生成 hook（close 后）

**总是问用户，但 Claude 先给推荐**（yes / no + 一句话理由）。判断标准：

| 信号 | 推荐 |
|---|---|
| 引入了未在 [learn-from-opt 索引](../../.cursor/skills/learn-from-opt/SKILL.md) 里讲过的新技术概念 | ✅ Yes |
| 是现有技术的新组合或反直觉用法 | 🤔 看用户想不想存档 |
| 纯加列 / 改 UI / 改文案 / 修小 bug | ❌ No |

如果 yes：按 [`learn-from-opt`](../../.cursor/skills/learn-from-opt/SKILL.md) 的 7 段骨架生成 `docs/lessons/lesson-opt-NNNN-<slug>.md`，在索引表 append 一行，**留在本地不 commit**（`.gitignore` 排除了 `docs/lessons/` 和 `.cursor/`，课件是纯本地教学资产，跟工作区走 —— 不要 `git add -f` 强推）。

如果 no：流程结束。未来想补课件，用户说「补一个 OPT-NNNN 的课件」即可走补课路径。

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
