---
id: OPT-0039
title: Risk-monitor 聚合视图切换丢失列持久化 + 计算列(净赚/佣金试算)排序失效
status: wip
priority: P1
area: mixed
effort: M
created: 2026-06-26
related: [[OPT-0027]] [[OPT-0028]] [[OPT-0015]] [[OPT-0016]]
---

## 问题

risk-monitor 批量下单 tab 的「聚合」功能有两个用户可感知缺陷，且都是**跨 tab 的模式级问题**（hedge-open、gap-trade 同款结构）：

1. **未开聚合时，按「淨賺 (USD)」列排序结果不对。** 点列头出现排序箭头，但表格数据实际是按 `scanned_at` 排的——排序「看起来生效了但其实没生效」。同一问题也存在于「佣金试算」列（同为前端计算列）。
2. **点「聚合」开 → 再取消聚合，明细视图之前存的列偏好（显示/隐藏/列序/列宽）全没了，回到默认。**

用户原话：「没打开聚合功能时，净赚的排序有问题；点击聚合后，再取消聚合，column 持久化，之前存储的用户偏好就没有了。可能其他页面的聚合功能都是没有优化好。」

## 背景

文件全部在 `frontend/src/pages/RiskMonitor.tsx` + `frontend/src/hooks/useGridColumnPersist.ts`。

### Bug 1 根因 —— 计算列被标 sortable 但无法服务端排序

- `netProfitColDef()`（`RiskMonitor.tsx:1056`）定义「淨賺」列：`sortable: true`、有 `valueGetter`（`equity − net_deposit_hist`，纯前端计算）、有 `comparator`、稳定 `colId: "net_profit"`。
- 但 `net_profit` **不在** `SORTABLE_COL_IDS` 白名单（`RiskMonitor.tsx:299-336`）里。
- 表格走**服务端排序 + 服务端分页**：`handleSortChanged`（`RiskMonitor.tsx:2073`）把当前排序列发给后端 `sort_by`。`active.colId && SORTABLE_COL_IDS.has(active.colId)` 不命中 → fallback 到 `"scanned_at"`（`:2076-2078`）。
- 结果：点「淨賺」列头 → AG-Grid 在列头画出排序箭头 → 但后端收到的是 `sort_by=scanned_at` → 返回按扫描时间排的页 → 列头箭头在「淨賺」、数据却按 scanned_at。**排序假性生效**。
- `net_profit` 是 `equity − net_deposit_hist`，两个都是真实后端列，**理论上后端可排**（但当前后端 `SORTABLE_ALERT_COLS` 没有它）。
- `estCommissionColDef()`（`RiskMonitor.tsx:1134`）同样 `sortable: true` 且不在白名单——但「佣金试算」是 **D03 纯前端逻辑（`@/lib/commission`，CN-only）**，后端**无法**计算/排序。两列性质不同，修法要分开（见下）。

### Bug 2 根因 —— React 复用同一 AgGridReact 实例（ternary 同型同位、无 key）

- 渲染处（`RiskMonitor.tsx:2613`）：
  ```tsx
  {aggregated ? (
    <AgGridReact<BurstOpenAggregatedRow> ...aggColumnPersist... />  // 2615
  ) : (
    <AgGridReact<AlertEvent> ...columnPersist... />                 // 2647
  )}
  ```
- 两个分支是**同一组件类型 `AgGridReact`、在 JSX 树同一位置、都没有 `key` prop**。React 协调按 (type, position) 判等 → **复用同一组件实例**，只更新 props（`columnDefs` / `rowData` / 事件 handler 全换掉），**不卸载/重挂**。
- 后果链：
  1. `onGridReady` 只在真正初始化时触发一次。切换聚合时它**不会重跑** → 切到聚合视图后 `aggColumnPersist` 的 `applyColumnState` 永不执行、其 `gridApiRef` 永不被赋值（saveState 因 `api` null 直接 return，聚合视图偏好也存不进去）。
  2. `columnDefs` prop 一换（明细↔聚合），AG-Grid 按新 colDef 把列状态**重置回默认**（order/visibility/width）。用户在 mount 时 applyColumnState 恢复的自定义被冲掉。
  3. 切回明细时 `onGridReady` 同样不重跑 → 明细保存的状态不会被重新 apply；而此时 `columnPersist.gridApiRef` 仍是旧的（step 1 时设过），列重置过程中任一列事件触发 `throttledSave` → 读到**默认**列状态 → **覆盖写** `RISK_MONITOR_BURST_OPEN_GRID_STATE_V1` → 用户偏好在 localStorage 里被销毁。
- `useGridColumnPersist`（`useGridColumnPersist.ts`）本身设计是对的（每 grid 独立 key、loadValidState 防 ghost、mergeMissingColumns、isApplying 守卫）——bug 在**调用方让两个 grid 共用了一个 React 实例**，使 hook 的 mount-time 加载/卸载语义失效。

### 项目级范围（印证用户「其他页面也没优化好」）

同款「同型同位无 key 三元」聚合切换存在于：
- **批量下单（burst-open）**：`RiskMonitor.tsx:2613`（agg 2615 / detail 2647）。
- **对冲刷单（hedge-open）**：`RiskMonitor.tsx:6182`（agg）/ `6214`（detail），结构与 burst 一致。
- **gap-trade**：`9812 / 9846 / 9882` 三表——需确认是否也是「同位条件切换」共用实例（gap 是多表纵向堆叠，可能不是同一坑，执行时核对）。

经 `grep "key=" RiskMonitor.tsx` 确认：上述 AgGridReact 渲染点**均无 `key` prop**。

## 假设 / 待验证

- [ ] **Bug 1 修法决策（需用户拍板，见下「开放问题」）**：淨賺列是「加后端排序支持」还是「改 sortable:false」？佣金试算列（纯前端）基本只能 `sortable:false` 或接受「仅当前页排序」。
- [ ] 复现 Bug 2：批量下单 tab 自定义列序 → 开聚合 → 取消聚合 → 确认偏好丢失（在 dev `http://10.6.20.138:5173/risk-monitor` 实测）。
- [ ] 验证 `key` prop 方案：给两个分支各加 `key="agg"` / `key="detail"` 强制 React 卸载/重挂 → `onGridReady` 重跑 → 正确 persist key 加载/保存。确认不会引入闪烁/重复 fetch（rowData 由各自 state 驱动，应无额外请求）。
- [ ] 核对 gap-trade 三表是否同坑（结构不同，可能本就独立 mount）。
- [ ] 确认修 key 方案后，聚合视图自身的列偏好也能正确持久化（当前因 onGridReady 不触发，聚合视图偏好其实也存不住——一并验证）。

## 验收标准

- [ ] **Bug 1**：点「淨賺」列头排序，数据真正按淨賺排序（跨页一致），列头箭头与实际数据序一致。佣金试算列按拍板方案处理（可排 / 不可排但不再假性生效）。
- [ ] **Bug 2**：批量下单 tab——自定义列（隐藏/列序/列宽）→ 开聚合 → 取消聚合 → 明细列偏好**完整保留**。
- [ ] 聚合视图自身的列偏好也能跨「切走再切回」保留。
- [ ] 同样修复 **hedge-open** tab（同款聚合切换）。gap-trade 经核对后按需修或记录「不受影响」。
- [ ] `tsc` + `vitest` 绿（`cd frontend && npm test`）。若改后端排序则 `pytest` 绿。
- [ ] 回归：未触发聚合的其他 5 个账户级 tab 排序/持久化不受影响。

## 笔记

- Bug 2 的「同型 ternary 复用实例」是经典 React+AG-Grid 坑。最小修法 = 给两个分支加稳定 `key`；更彻底 = 抽成两个子组件。优先 `key`（改动小、风险低），但要确认 remount 不带来体验回退。
- Bug 1 的 `net_profit`/`est_commission` 在 OPT-0027 加聚合视图 + 淨賺列（2026-06-03）之后引入，当时只接了前端 comparator，漏了「服务端排序白名单」这一层——这是「计算列 + 服务端排序」的语义冲突。修复后应在 SKILL.md「Adding a Column」段补一句：**前端计算列若要可排序，要么后端支持该派生列排序并加进两边白名单，要么显式 `sortable:false`，不能只给 comparator**（否则服务端分页下假性排序）。
- 与 OPT-0028（聚合视图 SQL 性能 + 语义硬化，backend）正交：本 OPT 是**前端状态硬化**。两者可独立做。

## 决策（用户 2026-06-26 拍板，开放问题已关闭）

1. **淨賺列排序 → 加后端排序支持**：后端 `SORTABLE_ALERT_COLS`（`risk_monitor_db.py` / 相关 service）加 `net_profit`，按派生表达式 `equity − net_deposit_hist` 排序（注意 CEN/null 语义要与前端 comparator 一致：null 排最前/最后需对齐当前 `comparator` 的 `null → -1`）；前端 `SORTABLE_COL_IDS`（`RiskMonitor.tsx:299`）加 `"net_profit"`。淨賺真正可跨页排序。**需 pytest 覆盖新排序列。**
2. **佣金试算列 → `sortable:false`**：`estCommissionColDef()`（`RiskMonitor.tsx:1134`）改 `sortable:false`（D03 纯前端逻辑，后端无法排序，避免假性排序）。不要发服务端 sort_by。

> ⚠ 注意：`net_profit` 列出现在哪些账户级 tab，就要确认那些 tab 的后端 `/alerts` 排序都认这个派生列（burst-open / 快开快平 / 快速获利 / 对冲 / 滥用杠杆 共用同一套排序白名单逻辑，核对是否一处改全受益）。聚合视图（burst-agg / hedge-agg）无 equity，不涉及。

## 结果
<done 时填>
