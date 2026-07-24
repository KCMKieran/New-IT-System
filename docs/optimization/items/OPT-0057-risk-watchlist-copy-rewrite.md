---
id: OPT-0057
title: risk-watchlist 全页文案/tooltip 重写 —— 说明横幅 + 列头 tooltip + 下拉/提示统一口径措辞
status: wip
priority: P2
area: frontend
created: 2026-07-25
effort: M
---

## 问题

`/risk-watchlist`（全量客户 · 交易状态视图）现有的**注释文字 / tooltip / 说明文案**用户不满意：
措辞密、口径术语堆叠、长短不一、部分表述可读性差。需要逐条重写，**只改文案、不改口径与逻辑**。

> 注：本项破例进 tracker —— tracker 约定通常把「改文案」排除在 OPT 之外（走普通 feat 分支），
> 用户 2026-07-25 拍板本次破例 file 成 OPT，走完整 3-stage close。

## 背景

页面在该 URL 实际渲染的是 **全量客户 · 交易状态视图**，组件是
[`frontend/src/pages/ActivityClientsPanel.tsx`](../../../frontend/src/pages/ActivityClientsPanel.tsx)
（2126 行）。外层 [`frontend/src/pages/RiskWatchlist.tsx`](../../../frontend/src/pages/RiskWatchlist.tsx)
里的旧「返佣观察清单」roster 视图已于 2026-07-24 退役、UI 不可达，**不在本次范围内**
（其 tooltip 若也要改需单独说明）。

工具栏还引用了 [`frontend/src/components/CrmTagsFilter.tsx`](../../../frontend/src/components/CrmTagsFilter.tsx)。

**全部待改文案已逐条盘出**，存于同目录的可编辑清单：
[`OPT-0057-risk-watchlist-copy-review.md`](./OPT-0057-risk-watchlist-copy-review.md)
—— 每条有「当前」+「改后 ✍️」空块，编号（A/B/C/D/E/F/G/H/I/J）对应页面区域。

文案在源码中的位置（供 worker 定位，行号以当前 main 为准，改前先 grep 校准）：

| 清单区 | 内容 | 源文件 · 大致位置 |
|---|---|---|
| A1–A3 | 顶部说明横幅 3 段 | `ActivityClientsPanel.tsx` ~1845–1887（JSX） |
| B1 | 搜索框 placeholder | `ActivityClientsPanel.tsx` ~1896 |
| B2–B6 | 下拉标题 / note / 全选行 | `FilterMultiSelect` 调用处 ~1930–1969；`note` prop |
| C1–C9 | 交易状态 badge 显示名 | `STATUS_META` 常量 ~176–233（`label` 字段） |
| D1–D7 | 国家下拉显示名 | `COUNTRY_META` 常量 ~293–301 |
| E1–E5 | CRM属性 label + hint | `CRM_META` 常量 ~320–326 |
| F1–F5 | CRM Tags 下拉文案 | `CrmTagsFilter.tsx` ~60–121 |
| G1–G19 | 列头 ℹ️ tooltip（InfoHeader） | `columnDefs` 内 `headerComponentParams.tooltip` |
| H1–H9 | 列头普通 tooltip（headerTooltip 字符串） | `columnDefs` 内 `headerTooltip` |
| I1–I6 | 空状态 / 错误 / 统计条提示 | `emptyFilterMsg` ~1833–1840、fetch catch、统计条 JSX |
| J1–J43 | 列名（headerName，选填） | `columnDefs` 各列 `headerName` |

**只改字符串字面量**，不动：`colId` / `field` / API 参数 / `STATUS_META` 等的 `code` /
后端 code、口径公式、格式化函数、颜色规则、列宽。tooltip 里的 `\n` 换行按新文案需要保留。

## 假设 / 待验证

- [ ] **阻塞项**：用户需先在 `OPT-0057-risk-watchlist-copy-review.md` 里填写各条 `改后 ✍️`
      新文案（留空 = 该条保持不变）。填完再实施。
- [ ] 列名（J 区）是否也要改？默认「保持不变」，只有用户在 J 表填了才改。
- [ ] 改动仅涉及显示文案，不触发口径/文档同步；如某条 tooltip 的口径**表述**变了但
      口径本身没变，不需要改后端/docs。若发现用户想改的其实是口径 → 停下澄清，不当文案改。

## 验收标准

- [ ] 用户在清单里填了新文案的每一条，都已回填进对应源码字符串
- [ ] 留空的条目**原样未动**
- [ ] `code` / `colId` / `field` / API 参数 / 口径公式 / 颜色规则 一律未改
- [ ] `cd frontend && npx tsc --noEmit` 通过；`npm test` 通过
- [ ] 页面在浏览器实测：横幅、各列头 tooltip、下拉、空状态提示文字渲染正常，无 `\n` 丢失/多余转义

## 笔记

- 清单是 2026-07-25 从源码逐字盘出的，含 ~100 条字符串。
- 交易状态显示名（C 区）同时出现在：状态列 badge、工具栏「交易状态」下拉、横幅瀑布顺序 —— 改 `STATUS_META.label` 一处即全页统一，无需多处改。
- CRM属性/国家同理，改常量 `label` 即可。

## 结果

<done 时填>
