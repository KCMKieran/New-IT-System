---
id: OPT-0016
title: useGridColumnPersist hardening（6 条 scaling-review 修复打包）
status: done
priority: P2
area: frontend
effort: M
created: 2026-05-18
related: [[OPT-0015]]
---

## 问题

[[OPT-0015]] 刚交付的 `useGridColumnPersist` hook + `ColumnVisibilityMenu` 组件，独立 code reviewer（无前置 context）审视后给出 6 条「随项目变大会爆雷」的 finding。当前 7 个 grid 用得好好的，但项目预计 1 年内新增 10-15 个页面、每页 5-8 tab、grid 总数往 50-100 走，这些隐患**现在不修，将来要批量回头改**——成本会乘以接入点数。

## 背景

源头：独立 code reviewer 的 punch list（详见会话 transcript / 上一条 Claude session 输出）。本 OPT 把 reviewer 标记为「Must address before scaling」+ 部分「Nice-to-have」的 6 条打包修。

涉及文件：
- `frontend/src/hooks/useGridColumnPersist.ts`（hook 本体）
- `frontend/src/components/ColumnVisibilityMenu.tsx`（UI 组件）
- `frontend/src/pages/RiskMonitor.tsx`（3 个 backend-sort tab 需要 compose 升级）
- `frontend/src/App.tsx`（boot 调用 stale-key cleanup）
- `docs/features/grid-column-persist.md` + `.cursor/rules/frontend-ui-conventions.mdc`（删 spread 写法）

## 假设 / 待验证

- [x] AG-Grid v34 的 `setColumnsVisible` 在 `GridApi` 上是公开 typed 方法（不需要 cast）—— claim 后第一步验证
- [x] `applyColumnState` 的事件触发是**同步**的（也就是 `isApplyingRef` 在 try 块里设 true、finally 微任务清掉就够）—— 确认: AG-Grid v34 同步派发列事件
- [x] `gcp:` 命名空间前缀**不影响现有 7 个用户已存的 key**（不改老 key、新增的可选用前缀）—— 决定：保留现有 7 个 key 不动，stale cleanup 走 `*_GRID_STATE_V\d+$` regex + 已注册集合的差集

## 验收标准

- [ ] **Fix #1 — `isApplyingRef` 短路保护**
  - hook 内 `isApplyingRef` ref
  - `onGridReady` 在 `applyColumnState` 前置 true，事件循环结束后用 `queueMicrotask` 清掉
  - `throttledSave` 在 ref=true 时直接 return（避免读完立刻原样写回）
  - 暴露 `isApplying(): boolean` 供 backend-sort 消费者 guard 自己的 `handleSortChanged` 避免初次加载多 1 次 `/alerts` fetch
  - 3 个 backend-sort tab（BurstOpen/QuickOpenClose/QuickProfit）在 compose 里加 `if (!columnPersist.isApplying()) handleSortChanged(e);`

- [ ] **Fix #2 + #6 — typed key 注册表 + boot stale cleanup**
  - hook 文件新增 `export const GRID_STORAGE_KEYS = { ... } as const`，列出所有 7 个 key
  - 导出 `export type GridStorageKey = typeof GRID_STORAGE_KEYS[keyof typeof GRID_STORAGE_KEYS]`
  - `useGridColumnPersist` 签名收紧成 `(storageKey: GridStorageKey)` —— 编译期阻断撞车
  - 导出 `pruneStaleGridKeys()`：遍历 localStorage，对匹配 `/_GRID_STATE_V\d+$/` 但不在 `Object.values(GRID_STORAGE_KEYS)` 集合里的 key 调 `removeItem`
  - `App.tsx` 顶层 `useEffect(() => pruneStaleGridKeys(), [])` 调一次

- [ ] **Fix #3 — schema 校验 + 自愈**
  - `onGridReady` 解析 JSON 后用 type guard 验证每条 entry 至少有 `{colId: string}`
  - 校验失败 → `removeItem(storageKey)` + 不复原（下次 reload 就 clean）
  - 过滤掉 `colId` 不在当前 grid 列里的条目（防止后端 / 业务删列后保留的 dead entry）
  - `applyColumnState` 外 try/catch，throw 也 `removeItem` 自愈

- [ ] **Fix #4 — 移除 `setColumnsVisible` cast**
  - 直接 `api.setColumnsVisible(colIds, visible)` 调用
  - 若 TS 报错说明类型过期，先升级 `ag-grid-community` 类型或在该方法处用 narrow cast，**不允许** `as unknown as {...}`

- [ ] **Fix #5 — 文档删 spread 写法**
  - `docs/features/grid-column-persist.md` §4 Step 3：删 A) Spread 方案，只留 B) Compose
  - `.cursor/rules/frontend-ui-conventions.mdc` 同步更新
  - 加一条 callout：「spread 在以下情况会出错：① 已有自己的 onGridReady ② backend-sort 需要 compose handleSortChanged ③ 用 ref 存 api。99% 的真实 grid 都命中其中一种 → 直接学 compose」

- [ ] **Fix #7 — a11y DropdownMenuLabel 用 `label` prop**
  - 把 `<DropdownMenuLabel>显示列</DropdownMenuLabel>` 改成 `<DropdownMenuLabel>{label}</DropdownMenuLabel>`
  - 测试：Gap Trade 3 张表用屏幕阅读器能听到不同的 section 名

- [ ] 全部跑通 `npx tsc -b --noEmit` 无新错（baseline 已知 pre-existing 错不算）
- [ ] 用户浏览器验证：现有 7 个 grid 行为**完全不变**（列设置 / 拖拽 / 排序持久化 / dark mode 都正常）
- [ ] 用户在 DevTools 清掉某个 grid 的 localStorage entry → 注入坏 JSON → 刷新页面 → 应该自愈，grid 正常显示默认列状态

## 笔记

**reviewer 标记但本 OPT 不做的项**（live with）：
- `bump()` 模式 → 当前无跨 grid 联动需求，重构成本 > 收益
- Unit test 基建 → 项目目前没前端 test，单这个 hook 起框架不合算；等整体测试规划
- `as ColDef<unknown>[]` 在 callsite → 纯类型层面 smell，行为正确

**为什么 #2 选「不改老 key」**：
- 改成 `gcp:` 前缀会让所有用户的现有 saved state 失效
- TS 注册表已经把撞车在编译期挡住，runtime cleanup 用 regex + Set 差集足够安全
- 后续真要重命名 key（V1→V2），直接改 `GRID_STORAGE_KEYS` 值，stale cleanup 会顺手清掉老 entry

**为什么 backend-sort consumer 要主动 guard**：
- hook 的 `isApplyingRef` 只能短路 hook 内部的 `throttledSave`
- 消费者自己的 `handleSortChanged` 在 compose 链里，hook 没法拦
- 暴露 `isApplying()` 让 consumer 选择性 guard，是最干净的边界

## 结果

**合并到 main**：`8cce6f9` (2026-05-18)，单一 feature commit `55b2cf7` 承载 6 项修复 + 4 文件改动（hook +218/-36，组件 a11y 标签、RiskMonitor 3 处 sort compose 升级、App.tsx boot cleanup）。

**实际交付 vs 原 AC**：

| Fix | 状态 | 备注 |
|---|---|---|
| #1 `isApplyingRef` + `isApplying()` + 3 个 backend-sort tab compose | ✅ | 用户在浏览器 Network 面板**实测验证**：以前会发 2 个 `/alerts`（一次默认 sort + 一次 restored sort），现在只 1 个直接带 `sort_by=order_count&sort_order=desc` |
| #2 typed `GRID_STORAGE_KEYS` 注册表 + `GridStorageKey` union | ✅ | TS 编译期撞 key 防护。注册表搬进代码（之前只在 gitignored 文档里——独立 reviewer 指出对 fresh clone 不可见） |
| #3 schema validation + 自愈 | ✅ | `loadValidState()` 校验每条 entry 必须 `{colId: string}`，过滤当前 grid 不存在的 ghost colId，applyColumnState 抛 → `removeItem` |
| #4 移除 `setColumnsVisible` cast | ✅ | AG-Grid v34 已公开 typed，cast 不必要 |
| #5 文档删 spread 写法 | ✅ | `grid-column-persist.md` + `ag-grid-integration.md` + `frontend-ui-conventions.mdc` 全部改成 compose-only，配 callout 解释 spread 99% 都会出错 |
| #6 `pruneStaleGridKeys()` boot 清理 | ✅ | App.tsx mount 时调一次，regex `/_GRID_STATE_V\d+$/` + 注册表差集移除孤儿 |
| #7 `DropdownMenuLabel` 用 dynamic label | ✅ | Gap Trade 3 张表 a11y heading 各异，不再三遍「显示列」 |

**额外做了（超出原 AC）**：

- 文档 §9「已接入清单」改为指向代码常量而非维护重复列表（避免文档/代码漂移）—— §1 步骤 1 直接告诉开发者"去 hook 文件加常量"
- 文档 §7「持久化存活范围」表格补充 OPT-0016 修复后的自愈行为
- commit message + OPT 笔记里把 reviewer 的具体术语（"applyColumnState event loop"、"ghost colId"）记下来，方便将来 grep 历史

**未做（明确推迟，已在 OPT 笔记中说明）**：

- `bump()` re-render → AG-Grid 事件订阅模式（reviewer #8）：当前无跨 grid 联动需求，重构成本 > 收益
- Vitest 单测基建（reviewer #9）：等项目整体前端测试规划
- `as ColDef<unknown>[]` 类型清理（reviewer #10）：纯 smell，行为正确

**用户浏览器验证**：✅ Fix #1 关键场景验证通过（只发 1 次 alerts），其他靠 typecheck + production build 通过 + 代码 review。Fix #3 / #6 用户跳过手工测试，依赖代码正确性。

**Follow-up**：无新 OPT。本 hardening pass 完整闭环。下一波若 reviewer 再有发现可单独再开。
