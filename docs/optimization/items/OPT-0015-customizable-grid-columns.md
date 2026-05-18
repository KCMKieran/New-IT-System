---
id: OPT-0015
title: RiskMonitor / ClientReturnRate 列自定义 + localStorage 持久化
status: ready
priority: P2
area: frontend
effort: M
created: 2026-05-18
related: [[OPT-0002]]
---

## 问题

`/risk-monitor`（4 个 tab：批量下单 / 快开快平 / 快速获利 / Gap Trade）和 `/client-return-rate` 的 AG-Grid 表格列**没有用户自定义入口**：列的顺序和显示/隐藏是硬编码的。不同岗位用户关心的列不同（风控只看 equity/leverage/order_count，CS 更关心 client_name/country），现在被迫横向滚动 + 肉眼找列，体感差。需求：

1. 每个表格可显示/隐藏列、拖拽重排
2. 设置**持久化在浏览器**（关浏览器再开依然记得）
3. 提供"重置默认"
4. **每个 tab 独立**记忆（4 个 tab 列定义完全不同）

## 背景

**代码库里已有成熟实现 + 文档**，本次工作 = 把它复用到这两个页面：

| 现有页 | 文件 | 模式 | localStorage key | 备注 |
|---|---|---|---|---|
| ClientPnLAnalysis | `frontend/src/pages/ClientPnLAnalysis.tsx` | **V1（推荐）** | `CLIENT_PNL_ANALYSIS_GRID_STATE_V1` | 单 source of truth = AG Grid Column State；300ms throttle；onGridReady 时 applyColumnState；Settings2 + DropdownMenu UI。**文档**：`docs/features/client-pnl-column-toggle.md` |
| IBReport | `frontend/src/pages/IBReport.tsx` | V1 | `GRID_STATE_STORAGE_KEY` | 同 V1 |
| ClientPnLMonitor（隐藏） | `frontend/src/pages/ClientPnLMonitor.tsx` | V2 | `client_pnl_grid_state` | App.tsx line 29 `[HIDDEN]` 注释掉。V2 多维护一个 React state `columnVisibility` —— **双 source of truth 有不一致风险，本 item 不采用** |

V1 关键事件 / API：
- `onGridReady` → 从 localStorage 读 → `applyColumnState({ state, applyOrder: true })`
- 监听 `onColumnMoved` / `onColumnVisible` / `onColumnPinned` / `onColumnResized(finished=true)`
- → 300ms throttle → `getColumnState()` → `localStorage.setItem(key, JSON.stringify(state))`
- UI：顶部按钮 → DropdownMenu → 每列一个 `DropdownMenuCheckboxItem` + "全选" / "重置"
- 计算列必须显式带 `colId`（valueGetter-only 没有 field 时持久化会乱）

**目标页结构**：

- **RiskMonitor.tsx**（`frontend/src/pages/RiskMonitor.tsx:676`）：4 个 tab 是 4 个独立子组件（`BurstOpenTab` / `QuickOpenCloseTab` / `QuickProfitTab` / `GapTradeTab`）。**总共 6 个 AG-Grid 实例**（不是 4 个）：
  - Burst Open ~22 列（`:1129-1283`）—— 1 个 grid
  - Quick Open-Close ~16 列（`:1936-2058`）—— 1 个 grid
  - Quick Profit ~18 列（`:3144-3276`）—— 1 个 grid
  - **Gap Trade 一个 tab 里纵向堆 3 张表**（`:4850/4878/4908`，常驻共存无切换）：
    - `clientPairColumns`：爆仓 AB 仓配对 · 客户对汇总（聚合视图）
    - `soAbColumns`：rule_id 71 (SO+AB) 逐笔明细
    - `gapColumns`：rule_id 81 (Gap) 超额获利明细
- **ClientReturnRate.tsx**（`frontend/src/pages/ClientReturnRate.tsx`）：单一表格，~20 列，无 tab

**合计 7 个 grid 需要列持久化。**

## 假设 / 待验证

- [x] **每个 grid 独立 key**：tab 内列定义完全不同，独立 key 最干净。**结论：7 个独立 key**
- [x] **Gap Trade tab 内部 rule 71/81 怎么算**：✅ 已确认看代码 `:4850/4878/4908`——3 张表（client-pair 聚合 + SO+AB 明细 + Gap 明细）**纵向堆叠常驻共存，无 sub-tab 切换**，各自一个 key
- [ ] **计算列 colId 全覆盖**：grep 7 处 columnDefs，列出所有只写 `valueGetter` / 没有 `field` 的列，必须补 `colId`。否则用户保存后再刷新会列错位
- [ ] **是否抽公共 hook `useGridColumnPersist(storageKey)`**：7 处复用度高，但 [[OPT-0002]]（浏览器缓存模式文档）已 ready 准备统一规范。两个选择：
  - (a) 本 item 内联复制粘贴 5 份（快），等 OPT-0002 之后再抽
  - (b) 本 item 直接产出 hook，OPT-0002 把它写进文档
  - 倾向 **(b)** —— 5 份复制粘贴维护成本高，hook 30 行内能写完
- [ ] **是否要"列搜索"**：列数最大 22（Burst Open），不需要搜索，纯 checkbox list 够用
- [ ] **是否允许"重置默认"按钮**：ClientPnLAnalysis 已有，推荐继承

## 验收标准

- [ ] **RiskMonitor 4 个 tab** 各自顶部加"列设置"按钮（`Settings2` icon + DropdownMenu），列出该 tab/section 所有列；列可勾选显示/隐藏；可在网格内拖拽重排
- [ ] **Gap Trade tab 3 张表各有独立"列设置"入口**（3 张表列定义完全不同，不能共用一个菜单）
- [ ] **ClientReturnRate** 顶部加同款"列设置"按钮
- [ ] localStorage key 命名（共 7 个）：
  - `RISK_MONITOR_BURST_OPEN_GRID_STATE_V1`
  - `RISK_MONITOR_QUICK_OPEN_CLOSE_GRID_STATE_V1`
  - `RISK_MONITOR_QUICK_PROFIT_GRID_STATE_V1`
  - `RISK_MONITOR_GAP_TRADE_CLIENT_PAIR_GRID_STATE_V1`
  - `RISK_MONITOR_GAP_TRADE_SO_AB_GRID_STATE_V1`
  - `RISK_MONITOR_GAP_TRADE_GAP_GRID_STATE_V1`
  - `CLIENT_RETURN_RATE_GRID_STATE_V1`
- [ ] 关浏览器 → 重开 → 7 处列设置完全恢复（顺序 + 显隐 + 宽度 + pinned）
- [ ] 每处提供"重置"按钮：清 localStorage + `resetColumnState()`
- [ ] 所有 valueGetter-only 计算列都有显式稳定的 `colId`（grep 全部 7 处 columnDefs 确认）
- [ ] 300ms throttle 节流 localStorage 写入（`onColumnResized` 仅 `finished===true` 时写）
- [ ] **切 tab 不影响其他 tab/section 的设置**（每个 grid 自己读自己 key，互不污染）
- [ ] **Gap Trade 3 张表互不影响**：调 A1 客户对汇总的列不会污染 A2 SO+AB 明细 / B Gap 明细
- [ ] 切走再回来（依赖已合的 [[OPT-0010]] forceMount）列设置不丢
- [ ] 不引入第二份 React state；以 AG Grid 内部 state 为唯一 source of truth（避免 ClientPnLMonitor V2 的坑）

## 笔记

**推荐落地路径（claim 后参考）**：

1. **先 grep**：列出 5 处 columnDefs 里所有 valueGetter-only 列，补 `colId`（这一步如果不做，后续持久化全废）
2. **抽公共 hook**（推荐 (b) 方案）：
   ```ts
   // frontend/src/hooks/useGridColumnPersist.ts
   export function useGridColumnPersist(storageKey: string) {
     // returns { onGridReady, onColumnMoved, onColumnVisible, onColumnPinned, onColumnResized, gridApiRef, resetState, columnState }
   }
   ```
3. **抽公共组件** `<ColumnVisibilityMenu gridApi columnDefs onReset />`（30 行内），照搬 ClientPnLAnalysis.tsx:1334-1417 的 DropdownMenu 结构
4. 5 处接入（每处 < 10 行 diff）
5. 写迁移文档：`docs/frontend/grid-column-persist.md`（顺手把 ClientPnLAnalysis / IBReport / ClientPnLMonitor 老的内联实现列为"待迁移"，但**不在本 item 迁移**，单独开 OPT）

**风险 / 反观点**：
- **抽 hook 的反观点**：3 个老页（ClientPnLAnalysis / IBReport / ClientPnLMonitor）现在是内联各写一份，本 item 抽 hook 之后会出现"两种实现并存"。缓解：本 item 不动老页，只做新增；老页迁移单独开 item（成本可控、风险隔离）
- **gap-trade tab 的复杂度**：确认是 3 张常驻表（client-pair / SO+AB / Gap），各自一套 columnDefs。3 张表的"列设置"入口要分别放在各自表头上方，UI 上要标清楚哪个按钮对应哪个表——别让用户点了上面表的按钮以为是改下面的
- **UI 密度**：Gap Trade tab 已经有时间筛选 + 服务器筛选 + 共享 IP 开关 + 配置 + 导出按钮，再加 3 个"列设置"会拥挤。Claim 时设计上可考虑：放在每张表 `<h3>` 标题行的右侧，icon-only 按钮
- **localStorage 容量**：每个 key 几 KB，7 个 key < 70 KB，远低于 5 MB 上限。不担心

**与 [[OPT-0002]] 的协调**：
- OPT-0002 是"浏览器缓存模式归纳"文档，本 item 产出的 hook 正好是 OPT-0002 要纳入的样例之一。建议本 item 先做（产出实物），OPT-0002 后做（归纳文档时把本 hook 写进去）。两者**不互相阻塞**

## 结果

_待填_
