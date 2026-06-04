---
id: OPT-0034
title: AG-Grid 全局约定收口（skills/rules/docs 去重）+ 表头视觉优化（table-like 边框 + 字号缩小）
status: done
priority: P3
area: frontend
effort: S
created: 2026-06-04
claimed: 2026-06-04
completed: 2026-06-04
branch: opt/ag-grid-conventions-consolidation
related: [[OPT-0015]] [[OPT-0016]] [[OPT-0025]]
---

## 问题

两件事，主题都是「AG-Grid 的约定散落各处、各页各写一份」：

1. **文档/规则漂移**：AG-Grid 的样式与持久化约定散在 5 个地方互相抄
   （`ag-grid-style` skill、`ui-pitfalls.mdc`、`frontend-ui-conventions.mdc`、
   `docs/features/grid-column-persist.md`、`CLAUDE.md`）。同一条规则
   （`theme:"legacy"` 3 处、斑马纹 rgba 2 处、`colId` 2 处、persist compose 2 处）
   重复出现 → 改一处忘改另一处就漂移。更糟的是 `frontend-ui-conventions.mdc` 里
   还留着一段**已被项目明令禁止的内联 `localStorage` 列状态持久化示例**，等于在教
   坏写法。

2. **表头样式不统一 + 视觉诉求**：表头样式一半走共享 hook
   （`gridTheme.ts`，RiskMonitor + fund-flow），一半每页内联各写一份
   （ClientReturnRate / IBReport / ClientPnLAnalysis / ReturnRateSummary /
   login-ip 等）。用户两个具体诉求：
   - 给每个 title 加 table-like 边框（亮色黑底→白线，暗色白底→黑线，含 title 之间）
   - 表头字体偏大（Quartz 默认 14px），想全局缩小让密集表更紧凑

## 背景

### 现有资产（不重做）

- 持久化内核 [`useGridColumnPersist`](../../../frontend/src/hooks/useGridColumnPersist.ts)
  （[[OPT-0015]]）+ `useFilterPersist`（[[OPT-0025]]）+ `<ColumnVisibilityMenu>` — **不动**。
- 共享主题 hook [`gridTheme.ts`](../../../frontend/src/pages/cs/fund-flow/gridTheme.ts)
  返回 `--ag-*` CSS 变量；已有 `--ag-header-column-separator-*`（title 之间竖线）。
- 全局 CSS 入口 `frontend/src/index.css`（已有 `.ag-paging-panel` 全局覆盖区）；
  `main.tsx` 导入顺序：`index.css` → `ag-grid.css` → `ag-theme-quartz.css`
  （即 index.css 在主题 css **之前**，全局覆盖需 `!important`）。
- 全项目 grid 容器 className 统一为 `ag-theme-quartz` / `ag-theme-quartz-dark`
  → 一条全局 CSS 即可覆盖所有表，无论它用 hook 还是内联。

### 关键事实（量过）

- Quartz 默认 `--ag-font-size: 14px`；`--ag-header-height` / `--ag-row-height`
  由它 `calc()` 派生（所以**不能**直接改 `--ag-font-size` 来缩表头字，会连带
  body 字号 + 行高一起变）。
- 自定义表头 `InfoHeader` 的标题 span 复用了 `.ag-header-cell-text` class
  （`info-header.tsx:79`），未写死字号 → 一个 `.ag-header-cell-text` 选择器即可
  同时覆盖默认表头 + InfoHeader + group 表头。

## 验收标准

- [x] 每条 AG-Grid 知识只有一个权威源（SoT）；其余位置降级为一行指针，无重复正文。
- [x] 保留 glob 触发的「绊线」机制：`ui-pitfalls.mdc` 仍有 3 条 AG-Grid 硬绊线
      （legacy theme / rgba / colId），但瘦身成 symptom + 一行 fix + 指针。
- [x] 删除 `frontend-ui-conventions.mdc` 里已废弃的内联 localStorage 持久化示例。
- [x] 所有交叉指针的 section 号、相对路径实测无断链。
- [x] 全局表头边框：亮色白线 / 暗色黑线，每个 title 框成 table 格子，覆盖所有 grid。
- [x] 全局表头字号缩小，**只动表头文字**，body 字号与行高/表头高不变；覆盖 InfoHeader。

## 笔记

### 文档收口（SoT 分配）

| 主题 | 唯一权威源 |
|---|---|
| theme / 表头变量 / rgba / 防截断 / 配色 / 渲染器 | `ag-grid-style` skill |
| 列 + 过滤器持久化（深度） | `docs/features/grid-column-persist.md` |
| 3 条硬绊线 | `ui-pitfalls.mdc`（指针式） |

- skill 新增 §13「列/过滤器持久化」（5 步速查 + 判断标准 + 指向 doc），原 §13 checklist
  顺延 §14 并补 1 勾项。
- `ui-pitfalls.mdc` §0/§1 砍掉与 skill 重复的长解释；§2 colId 保留；§3 ToggleGroup
  非 AG-Grid，不动。
- `frontend-ui-conventions.mdc`「Column Toggle+Persist」段、整个「AG Grid Integration」
  段（含废弃内联示例）collapse 成指针。

### 表头 CSS（全部落在 `index.css`，全局）

- **边框踩坑**：先试 `.ag-header` 整体外框 → 不可见（被 `.ag-root-wrapper` 自身外框
  盖住）。改为给每个 `.ag-header-cell` / `.ag-header-group-cell` 加边框（内嵌、可靠）。
- **粗细**：1px 实线，但相邻两格各 1px → title 之间视觉约 2px。改用
  `rgba(.,.,.,0.45)` 降对比让线看起来更细更轻（避开标准屏 0.5px 不可靠）。透明度是
  唯一旋钮。
- **字号**：`.ag-header-cell-text` / `.ag-header-group-text` 设 `12px !important`
  （从 14px）；不碰 `--ag-font-size`，故行高/表头高/body 字号不变。
- 三条规则都需 `!important`（index.css 导入早于主题 css）。

## 结果

**实际交付**：

- **文档去重**（均为 gitignored 本地资产：`.cursor/**` + `docs/**` 除 optimization）：
  - `ag-grid-style/SKILL.md`：+§13 持久化节、checklist 顺延 §14 + 补勾项。
  - `ui-pitfalls.mdc`：§0/§1 瘦身为绊线 + 顶部加「本文件只是绊线」声明（→ 85 行）。
  - `frontend-ui-conventions.mdc`：砍两段 AG-Grid 重复（含废弃内联持久化反例，~109 行）。
  - `CLAUDE.md`：核对 3 条 must-remember 指针已收口到 SoT、无矛盾 → 未改。
  - 自查：skill §1/§3/§6/§7 与 checklist「Section 13」引用、3 条相对路径实测可达。
- **表头视觉**（`frontend/src/index.css`，全局、tracked）：
  - 每个表头格 table-like 边框：亮色 `rgba(255,255,255,0.45)` / 暗色
    `rgba(0,0,0,0.45)`，1px。
  - 表头字号 14px → 12px，仅 `.ag-header-cell-text`（同时覆盖 InfoHeader）。

**与 AC 偏差**：无。

**备注**：除 `index.css` 是 git tracked 外，其余 `.cursor/**` 文档改动是本地资产
（项目 `.gitignore` 排除），不进 git 历史。

**follow-up（本 OPT 不含，建议各自独立 file）**：

- **列宽 autoSize（原 Q2，已明确推迟）**：现象是列宽不按内容分配（数字列过宽、
  时间戳列过窄导致换行）。方案已讨论清楚：`onFirstDataRendered` 时「**有持久化
  state → 用用户保存的宽度；否则 → `autoSizeAllColumns()`**」，优先级
  `用户拖过的宽度 > autoSize > min/max 栏杆`。最 DRY 的落地是焊进
  `useGridColumnPersist` hook（影响 RiskMonitor 8–11 表 + fund-flow + ClientReturnRate，
  改动面大）。**值得单独 file 一条 OPT**。
- 表头边框透明度 / 字号若团队觉得需要再微调（纯 CSS 常量，非 OPT 级）。
- 把这两条全局 CSS 约定补一句进 `ag-grid-style` skill 表头节，避免未来有人在某页
  内联覆盖（小事，可随手做）。
