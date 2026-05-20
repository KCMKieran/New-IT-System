---
id: OPT-0023
title: Risk-monitor header 化繁为简：合并「列设置」「立即扫描」进设置抽屉
status: wip
priority: P2
area: frontend
effort: M
created: 2026-05-20
---

## 问题

`/risk-monitor` 5 个 tab 的右上角按钮越来越多：

| Tab | 当前按钮 | 数量 |
|---|---|---|
| burst-open | 导出CSV / 规则配置 / 列设置 / 立即扫描 | 4 |
| quick-open-close | 导出CSV / 规则配置 / 列设置 / 立即扫描 | 4 |
| quick-profit | 导出CSV / 规则配置 / 列设置 / 立即扫描 | 4 |
| hedge-open | 导出CSV / 规则配置 / 列设置 / 立即扫描 / 聚合 | 5 |
| gap-trade | 导出CSV / 规则配置 / 列设置×3 | 5 |

随着新规则 / 新 tab 持续加入，header 横向溢出风险越来越大；
按钮多了用户也分不清主次（规则配置 vs 列设置 vs 立即扫描 都是配置类操作）。

## 背景

- 单文件 `frontend/src/pages/RiskMonitor.tsx`（~7090 行），5 个 tab 各自有独立的 `*ConfigDrawer`：
  - `BurstConfigDrawer`（`规则配置` 抽屉）
  - `QuickConfigDrawer`（`快开快平规则配置`）
  - `QuickProfitConfigDrawer`（`快速获利规则配置`）
  - `HedgeConfigDrawer`（`对冲刷单规则配置`）
  - `GapConfigDrawer`（`Gap Trade 规则配置`）
- 列设置：`ColumnVisibilityMenu`（`frontend/src/components/ColumnVisibilityMenu.tsx`）+ `useGridColumnPersist` hook（localStorage 持久化，立即生效）
- 立即扫描：调后端 `/api/risk-monitor/scan-now`，立即触发一次扫描；高频操作
- 导出CSV、hedge 的「聚合」按钮：按用户决定**保留在 header 外面**

## 验收标准

### Header 简化
- [ ] burst-open / quick-open-close / gap-trade：header 只剩 `导出CSV` + `设置`（2 个按钮）
- [ ] quick-profit：header 留 `导出CSV` + `刷新浮动盈亏`（tab-specific，类似 hedge 聚合）+ `设置`（3 个按钮）
- [ ] hedge-open：header 留 `导出CSV` + `聚合` + `设置`（3 个按钮）
- [ ] 「规则配置」按钮文案 → 「设置」（Settings icon 保留）

### 设置抽屉（5 个 drawer 都升级）
- [ ] 抽屉标题统一改为「设置」（不再是 "规则配置" / "快开快平规则配置" 等）
- [ ] 抽屉自上而下 3 段：
  1. **启用规则** — 既有规则编辑 UI（扫描间隔 + 规则列表 + 添加/删除）。底部「保存配置」按钮**仅对该段生效**（语义清楚）。
  2. **列设置** — **内联 checkbox 列表**（不再用 DropdownMenu 二次弹出）。即时存 localStorage，复用 `useGridColumnPersist`。
     - gap-trade 抽屉特殊：分 3 段折叠分组（客户对汇总 / 逐笔明细 / Gap Trade 表格）
  3. **立即扫描** — 抽屉底部的 action 块，点击立即触发扫描；扫描中按钮 disabled + spinner
     - gap-trade 抽屉**不放**该段（gap-trade 是每日刷新，没有 on-demand 扫描）

### 行为不变
- [ ] 列可见性持久化逻辑（`useGridColumnPersist`）行为不变，localStorage key 不变 → 老用户切换后保留原选择
- [ ] 立即扫描的扫描中状态、SSE 触发、结果提示都不变
- [ ] 规则保存语义不变（POST 后端 + 成功 toast）
- [ ] 导出CSV、hedge 聚合按钮位置 / 行为不变

### 一致性
- [ ] 5 个抽屉的段落顺序、视觉密度、CTA 位置一致

## 假设 / 待验证

- [x] 「立即扫描」进抽屉（用户已确认，接受多 1 次点击的代价换 header 干净）
- [x] 「列设置」抽屉内用内联 checkbox（用户已确认，比 DropdownMenu 嵌套更扁平）
- [ ] gap-trade 3 张表的列设置在抽屉里是「3 段并列」还是「3 段可折叠」？倾向并列（用户切换 tab 内子表时方便），实施时确认

## 笔记

- 不涉及后端，纯 frontend，单文件改动
- 主要风险：5 个 drawer 一致性 + gap-trade 多列设置抽屉布局
- 期望长期收益：未来加 tab / 加 toggle 都进抽屉，header 永远只有 2-3 个核心 action

## 结果

<!-- done 时填 -->
