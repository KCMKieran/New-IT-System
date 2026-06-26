---
id: OPT-0039
title: Risk-monitor 聚合视图切换丢失列持久化 + 计算列(净赚/佣金试算)排序失效
status: done
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

**完成 2026-06-26**（branch `opt/risk-monitor-aggregate-toggle-state-loss`，2 commit：`0922326` 实现 + `ff6441b` 冷审修复）。

### 实际交付 vs AC

- ✅ **Bug 1（淨賺真排序）**：后端 `net_profit` 加入 `SORTABLE_ALERT_COLS` + `_SORT_COL_DB_NAME` 映射到派生 `(ae.equity - ae.net_deposit_hist)`。SQLite 把 NULL 当最低值，恰好对齐前端 comparator 的 `null → -1`（ASC nulls-first / DESC nulls-last），无需额外 NULLS FIRST/LAST。equity/net_deposit_hist 均已存 USD，无 CEN 二次除。前端 `SORTABLE_COL_IDS` 加 `"net_profit"`。核对：5 个账户级 tab 的 `handleSortChanged` 共用同一 `SORTABLE_COL_IDS` Set，一处改全受益。pytest 覆盖（见下）。
- ✅ **佣金试算列**：`estCommissionColDef()` 改 `sortable:false` 并删除已无意义的 comparator（D03 纯前端，服务端分页下无法排序，避免假性排序）。
- ✅ **Bug 2（聚合 toggle 保列偏好）**：burst-open + hedge-open 两处三元各加稳定 `key="agg"/"detail"`，强制 React 卸载/重挂 → `onGridReady` 重跑 → 正确 persist hook 加载/保存。聚合视图自身偏好也因此首次能持久化（原先 onGridReady 不触发，聚合偏好也存不住）。
- ✅ **gap-trade 核对**：**不受影响**——三表（clientPairAgg / soAbAlerts / gapAlerts）各在独立 `<div>` JSX 位置、各自 persist hook（`clientPairPersist`/`soAbPersist`/`gapPersist`），纵向堆叠非同位三元，本就独立挂载，`onGridReady` 各自正常触发。
- ✅ `tsc` + `vitest`（97）绿；`pytest` net_profit 套件 9 测试全过。31 个 `test_burst_open_aggregated`/`test_hedge_open_aggregated` 失败为 **main pre-existing**（aggregated endpoint 在该 sandbox 返回 `[]`，环境/数据依赖，非本 OPT；已在 main 上独立复现确认）。
- ✅ 回归：未触发聚合的其他 5 个账户级 tab 排序/持久化逻辑未动（whitelist 是增量加项）。

### Stage 1 冷审 finding 处理记录（独立后台 reviewer，3 条全「当场修」commit `ff6441b`）

- **#1（当场修）**：key-remount 后被销毁 grid 的 `gridApiRef` 仍指向已销毁 api（`useGridColumnPersist` 卸载时不清空 ref），`useRefreshRemarkColumn` 改备注时对死 grid `refreshCells` → console error spam（v34 不抛错）。在唯一 consumer 加 `if (ref.current && !ref.current.isDestroyed())` 守卫。
- **#2（当场修）**：前后端排序白名单无防漂移耦合（加可排序计算列要改 3 处，漏后端→静默 fallback `scanned_at`=本 OPT 刚修的假性排序复现）。加 anti-drift 测试 `test_sortable_alert_cols_all_have_db_mapping`（断言每个 `SORTABLE_ALERT_COLS` 成员有 `_SORT_COL_DB_NAME` 映射）；并补 equity-NULL、相同 net_profit 的 `id DESC` 跨页 tiebreaker、ASC 跨页 NULL-first 三条测试。
- **#3（当场修）**：net_profit 保留 client comparator 而 est_commission 删了——加注释说明 net_profit 是服务端可排序列（comparator 仅当页防御性一致排序），est_commission 是纯前端不可排序（comparator 会是假性排序），消除两 colDef 左右矛盾观感。

### Follow-up（live with，留 paper trail）

- **CEN 单位**（冷审 #4）：后端注释断言 equity/net_deposit_hist 存 USD 非 cents，未实测核对真实 CEN 户。**排序方向两种存法都对**（前后端同字段），仅显示值若实为 cents 会差 100×——但 淨賺 列显示是 OPT-0027 引入、非本 OPT 新增。值得未来拿真实 CEN 告警行一行核。
- **SQLite 派生表达式排序无索引**（冷审 #5）：`ORDER BY (equity - net_deposit_hist)` 走全过滤扫描+排序，OFFSET 深分页同样退化。当前 30 天保留 ~20k 行封顶，可接受；若保留期增长再加生成列/索引。
- **死代码 `gridRef`**（冷审 #7）：`RiskMonitor.tsx:1635` 声明赋值未读，nitpick，未清。
- **31 个 aggregated 测试在 main 上红**：与本 OPT 无关，但发现 burst/hedge aggregated 测试套件目前在 main 红（环境数据依赖 vs 真回归未定），值得单独 file 一个 item 排查。

### 文档跟进

按笔记建议，应在 risk-monitor / grid-column-persist 文档「Adding a Column」段补一句：**前端计算列若要可排序，要么后端支持该派生列排序并加进两边白名单（+ anti-drift 测试已落地），要么显式 `sortable:false`，不能只给 comparator**（否则服务端分页下假性排序）。

---

## § 后续扩展 / Phase 2（reopen 2026-06-26）—— 取消排序态显形（方案 B）

> reopen 理由：同主题（OPT-0039 排序/聚合切换体验）的后续 phase，非正交目标 → reopen 不开新单（用户拍板 2026-06-26）。branch `opt/risk-monitor-aggregate-toggle-state-loss-phase2`。

### 问题（Phase 1 上线后用户实测发现）

账户级 tab 点列头排序，循环是 **desc → asc → 取消（null）**（`sortingOrder={["desc","asc",null]}`，8 处）。但表走**服务端排序+分页**，后端永远要 `ORDER BY`，所以「取消」态在 `handleSortChanged` 里 fallback 到默认列（账户级 = `scanned_at`，聚合 = `total_lots`/`total_count`）。

**症状**：点到第三下（取消），AG-Grid 清掉当前列箭头，数据跳回默认列倒序——但**默认列的箭头不会自动显形**（`handleSortChanged` 只更新前端 server 状态 `sortBy/sortOrder`，不把箭头重新画回默认列）。用户看到「全部无箭头 + 数据莫名跳动」，感觉「只有 asc/desc 两态，没有取消态」。这套行为早于 OPT-0039 就存在，Phase 1 让 net_profit 可排序后把它暴露出来。

### 方案 B（用户选定）—— 取消时让默认列箭头显形

取消排序（第三下、无 active 排序列）时，**程序化把默认列的 ▼（desc）箭头重新 apply 回去**，让用户明确看到「现在按 <默认列> 倒序」，而不是无箭头 limbo。三态全部可读：`<列> ▼ → <列> ▲ → 默认列 ▼`。

### 涉及的 8 个 onSortChanged（每个用它自己**现有的 fallback 列**作为要显形的默认列）

| grid | 行（Phase 1 后，会漂移，按 colId/结构定位）| fallback 默认列 |
|---|---|---|
| burst-open 明细 | `handleSortChanged` ~`:2093` | `scanned_at` |
| burst-open 聚合 | inline onSortChanged ~`:2654`（`BURST_AGG_SORTABLE_COL_IDS`）| `total_lots` |
| 快开快平 | ~`:3089` 附近 | `scanned_at` |
| 快速获利 | ~`:4536` 附近 | `scanned_at` |
| 对冲 明细 | ~`:5622` 附近 | `scanned_at` |
| 对冲 聚合 | ~`:6196`（`HEDGE_AGG_SORTABLE_COL_IDS`）| `total_lots`（核对）|
| 滥用杠杆 | ~`:6885` 附近 | `scanned_at` |
| 马丁 | ~`:7899` 附近 | `scanned_at` |

> 用 `grep -n "getColumnState().find" frontend/src/pages/RiskMonitor.tsx` 定位全部 handler，逐个核对它现有的 fallback 列字符串（`: "scanned_at"` / `: "total_lots"` / `: "total_count"`），那个就是要 apply 回去的默认列。

### 实施要点

1. **触发条件**：handler 里 `active = getColumnState().find(c => c.sort)` 为 `undefined`（即取消态）时，才 apply 默认列箭头。`active` 存在（用户正常 asc/desc）时不动。
2. **apply 方式**：`e.api.applyColumnState({ state: [{ colId: <fallback>, sort: "desc" }], defaultState: { sort: null } })`，把默认列设 desc、其余清空。
3. **防循环/防回声**：`applyColumnState` 会再触发一次 `onSortChanged`。因为 fallback 列（`scanned_at`/`total_lots`）**都在各自白名单里**，回声事件里 `active` = 默认列（已找到）→ 走「active 存在」分支 → **不再** apply → 自然终止（最多多一次 no-op 事件，`sortBy` 值不变不会重复 fetch）。
   - 聚合 handler 已有 `if (!aggColumnPersist.isApplying())` 守卫——apply 默认列时要走该 hook 的 apply 包装（或临时置 isApplying），避免回声事件重入。明细 handler 无此守卫，靠「fallback 列在白名单 → 回声走 active 分支」自然终止即可；若仍担心，加一个 `isReapplyingRef` 本地守卫。
4. **持久化**：apply 默认列 sort 会被 `useGridColumnPersist` 的 throttledSave 记下（sort 也在持久化范围）——这是**期望行为**（取消→默认被记住），无需特殊处理。
5. **不要**改 `sortingOrder`（保持三态），不要改后端（纯前端 UX）。

### Phase 2 验收标准

- [ ] 任一账户级 tab：列 ▼ → ▲ → 第三下，**默认列（发现时间）显出 ▼ 箭头**，数据按发现时间倒序，用户能明确看到当前排序态。
- [ ] 聚合视图同理：第三下显出默认列（total_lots）▼。
- [ ] 无排序循环 / 无重复 fetch（apply 回声事件不触发二次请求）。
- [ ] 8 个 grid 全覆盖（含 hedge 聚合的 total_lots 核对）。
- [ ] 持久化不回退：取消态被正确记住，刷新后仍是默认列倒序。
- [ ] `tsc` + `vitest` 绿（`./verify.sh`）。纯前端无 pytest 影响。
- [ ] 回归：正常 asc/desc 排序、Phase 1 的 net_profit 服务端排序、聚合 toggle 列偏好保留都不受影响。

### Phase 2 结果（完成 2026-06-26，commit `055afc2`）

- ✅ 8 个 `onSortChanged` handler 全覆盖（6 账户级明细 + burst-agg + hedge-agg）。取消态（`active === undefined`）时 `applyColumnState({ state: [{ colId: <fallback>, sort: "desc" }], defaultState: { sort: null } })` 把默认列 ▼ 显形。
- ✅ fallback 列：账户级 = `scanned_at`（∈ `SORTABLE_COL_IDS`），burst-agg + hedge-agg = `total_lots`（分别 ∈ `BURST_AGG_SORTABLE_COL_IDS` / `HEDGE_AGG_SORTABLE_COL_IDS`，hedge 的 total_lots 已核对）。
- ✅ 防循环：回声事件因默认列在白名单 → 走「active 存在」分支 → 不再 apply → 自然终止；`sortBy` 值不变不重复 fetch。聚合 handler 的 apply 放在既有 `if (!aggColumnPersist.isApplying())` 守卫内。
- ✅ 未改 `sortingOrder`（保持三态）、未改后端。Phase 1（net_profit 服务端排序 / est_commission sortable:false / 聚合 toggle key）均未动，diff 为 handler 体内增量。
- ✅ `tsc` + `vitest`（97）绿。纯前端 1 文件 +100，pytest 不受影响。
- ⏳ **需人在 dev 点验**（worker 连不上 `10.6.20.138:5173`）：账户级三态 ▼→▲→`scanned_at` ▼；聚合三态第三下 `total_lots` ▼；取消态刷新后仍是默认列倒序（持久化 round-trip）。
