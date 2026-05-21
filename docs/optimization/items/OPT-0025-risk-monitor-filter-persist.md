---
id: OPT-0025
title: Risk-monitor 工具栏过滤器持久化（rangePreset / ruleFilter / serverFilter / onlySharedIp）
status: done
priority: P2
area: frontend
effort: S
created: 2026-05-21
claimed: 2026-05-21
completed: 2026-05-21
branch: opt/risk-monitor-filter-persist
related: [[OPT-0015]]
---

## 问题

`/risk-monitor` 5 个 tab（burst-open / quick-open-close / quick-profit /
hedge-open / gap-trade）的**工具栏过滤器**目前每次刷新 / 重开浏览器都被
重置成默认值。痛点：

- 风控分析师固定看 **4h**，CS 看 **7d**，每次进页面都被拉回 4h
- gap-trade 反洗钱分析师几乎永远开「**只看同 IP**」，每次默认关回去等于强制多点一下
- 某些岗位只盯特定 server（MT5 团队 vs MT4 团队）或单条 rule

[[OPT-0015]] 已经把**列设置**（显隐 / 顺序 / 宽度 / pinned）做了 localStorage
持久化，但**过滤器选择**没覆盖。这条补完。

## 背景

### 现有持久化资产（不要重做）

- [`useGridColumnPersist`](../../../frontend/src/hooks/useGridColumnPersist.ts)
  hook（OPT-0015 沉淀，323 行）+ `<ColumnVisibilityMenu>` / `<ColumnVisibilityInline>`
  组件 — **本 OPT 不动**
- `RISK_MONITOR_ACTIVE_TAB_V1`（last-active tab）和
  `RISK_MONITOR_HEDGE_OPEN_AGGREGATED_V1`（聚合 toggle）已经在 RiskMonitor.tsx
  里直接 `localStorage.getItem/setItem` 走起 — **本 OPT 不动**

### 当前过滤器分布（5 个 tab）

```
RiskMonitor.tsx 里 useState 的过滤器（grep useState | grep filter|range|server|login|zip 已经数过）：

burst-open (BurstOpenTab @ ~969):     rangePreset / customRange / ruleFilter / serverFilter / loginQuery / zipcodeQuery
quick-open-close (QOC @ ~1873):       同上
quick-profit (QP @ ~3235):            同上
hedge-open (HedgeOpenTab @ ~4312):    同上 + aggregated（已持久化）
gap-trade (GapTradeTab @ ~5662):      rangePreset(GapTradeDayRange) / customRange / serverFilter / onlySharedIp(未确认)
```

### 持久化 vs 不持久化的判断

| 状态 | 是否持久化 | 理由 |
|---|---|---|
| `rangePreset`（4h/1h/1d/7d/30d；gap-trade today/yesterday/3d/...） | ✅ 是 | 岗位偏好 |
| `customRange`（绝对日期区间） | ❌ 否 | 持久化会导致下次打开停在旧区间、误以为「数据没更新」；而且会盖掉 rangePreset 的自动刷新 |
| `ruleFilter`（"all" / 某条 rule） | ✅ 是 | 用户常只盯某条规则；但默认 "all" 也合理 |
| `serverFilter`（all / MT4_Live / MT4_Live2 / MT5） | ✅ 是 | MT5 / MT4 团队分工不同 |
| `loginInput` / `zipcodeInput` | ❌ 否 | **调查具体客户**的临时输入，持久化会出现「关浏览器再打开还停在某个 login 上、以为今天没新单」的事故 |
| `onlySharedIp`（Gap Trade「只看同 IP」） | ✅ 是 | 反洗钱分析师几乎永远开着 |

**核心标准**：「用户偏好」（怎么看数据）持久化；「调查上下文」（在追哪个客户）不持久化。

## 验收标准

### Hook 抽象

- [ ] 新增 `frontend/src/hooks/useFilterPersist.ts`（≤ 80 行，跟 `useGridColumnPersist.ts` 同目录）
- [ ] 签名大致：`useFilterPersist<T>(storageKey, defaultValue, { schemaVersion?: number, debounceMs?: number }) → [T, (next: T) => void, () => void /* reset */]`
- [ ] 序列化 JSON `{ v: <schemaVersion>, value: T }`；`v` 不匹配时直接返回 default（schema 自愈，向 OPT-0016 看齐）
- [ ] try/catch 包 localStorage 调用（隐私模式 / 禁用 storage 不炸页面）

### localStorage key 命名（5 个，沿用 `_V1` 后缀）

- [ ] `RISK_MONITOR_BURST_OPEN_FILTERS_V1`
- [ ] `RISK_MONITOR_QUICK_OPEN_CLOSE_FILTERS_V1`
- [ ] `RISK_MONITOR_QUICK_PROFIT_FILTERS_V1`
- [ ] `RISK_MONITOR_HEDGE_OPEN_FILTERS_V1`
- [ ] `RISK_MONITOR_GAP_TRADE_FILTERS_V1`

每个 key 是 JSON object：`{ v: 1, rangePreset, ruleFilter?, serverFilter, onlySharedIp? }`
（字段按 tab 实际情况裁剪 — gap-trade 没 ruleFilter）

### 行为

- [ ] `customRange` / `loginInput` / `zipcodeInput` 不持久化（**每次都从空值开始**）
- [ ] 用户选 `customRange` 后**清掉**该 tab 的 rangePreset 持久化（避免下次进来 preset 与 customRange 不一致）
- [ ] 每个 tab 工具栏右侧加一个小按钮 **「重置过滤」**（icon 用 RotateCcw），只清当前 tab 的 filter key — 不动列设置
- [ ] 关浏览器 → 重开 → 5 个 tab 的 rangePreset / ruleFilter / serverFilter / onlySharedIp 完全恢复
- [ ] 切 tab 互不污染

### 测试

- [ ] `frontend/src/hooks/useFilterPersist.test.ts`（vitest） — 默认值 / 读 / 写 / version 不匹配 / 损坏 JSON / localStorage throw

### 文档

- [ ] 在 [`docs/features/grid-column-persist.md`](../../features/grid-column-persist.md) 同目录加 `risk-monitor-filter-persist.md` 或在原文件追加章节
- [ ] CLAUDE.md「Key conventions」段提一句「新 tab 加过滤器时走 `useFilterPersist`」

## 假设 / 待验证

- [x] `ruleFilter` 持久化是否默认开启 → 是（推荐方案，"重置过滤"按钮兜底）
- [ ] gap-trade 是否有 `onlySharedIp` 状态 — 需要 claim 时再 grep 确认（CLAUDE.md / SKILL.md 都提到这个工具栏按钮，但当前 useState 列表里没扫到）
- [ ] 是否要给 `customRange` 也加 TTL 持久化（如「24h 内回来仍然记得」）？倾向 **No** — 引入 TTL 复杂度大、收益小，首版不加
- [ ] 「重置过滤」按钮的视觉位置：跟「列设置」并排还是单独一行？claim 时实施定

## 笔记

- 纯前端单文件改动（`RiskMonitor.tsx` + 新 hook + 测试），无后端 / SQL
- 5 处复用同一份 hook，过了 OPT-0015「3 次以上 copy-paste / 5 次以上必抽 hook」的阈值
- 跟 [[OPT-0001]]（tab 切换缓存）零冲突 — OPT-0001 缓存「数据」，本 OPT 持久化「过滤器选择」；如果 OPT-0001 先做，本 OPT 落地时只需要从 hook 读一行
- 跟 [[OPT-0002]]（浏览器缓存模式归纳文档）正好可以联动 — 本 OPT 产出的 hook 是 OPT-0002 要纳入的样例之一

### 反观点 / 风险

- **过度持久化 footgun**：用户 A 把 serverFilter 改成只看 MT5，用户 B 接班看同一台机器以为数据全（其实被过滤了）。**缓解**：(a) 工具栏的 ruleFilter / serverFilter 永远显式渲染当前值（不要折叠隐藏），(b) 「重置过滤」按钮 prominent 可见
- **schema 漂移**：未来加新 filter 字段时不要破坏老 key — `_V1` 后缀 + version field 解决，不匹配回退到 default
- **写入频率**：filter 变化频率比列拖拽低得多（每次点击 1 次），不需要 throttle / debounce

## 结果

实际交付（与 AC 基本一致，**1 处用户拍板修改**）：

**核心**

- `frontend/src/hooks/useFilterPersist.helpers.ts`（73 行）—— 纯函数 `readFilterState` / `writeFilterState` / `mergeFilterState`，envelope `{ v, value }` + schema 自愈（v 不匹配回退 default）+ localStorage throw 兜底（隐私模式不炸）。零 React 依赖
- `frontend/src/hooks/useFilterPersist.ts`（44 行）—— 薄 React 包装，useEffect 写入，`skipFields` 选项屏蔽某些字段（专为 customRange 场景：`rangePreset === "custom"` 时不写 rangePreset 持久位，storage 保留上一次真 preset）
- `frontend/src/hooks/useFilterPersist.test.ts` —— 18 项 vitest 测试覆盖 read/write/merge/version 不匹配/损坏 JSON/localStorage throw 全场景
- `frontend/src/pages/RiskMonitor.tsx` —— 5 个 tab 接入，每 tab 一个 `RISK_MONITOR_<TAB>_FILTERS_V1` key + typed defaults。文件顶部添加 `StandardTabFilters` / `GapTradeFilters` 类型 + `DEFAULT_STANDARD_FILTERS` / `DEFAULT_GAP_TRADE_FILTERS` 常量

**文档**

- `CLAUDE.md` —— Key conventions 加「工具栏过滤器持久化」一条
- `docs/features/grid-column-persist.md` —— 加 §13「工具栏过滤器持久化（OPT-0025，同模式不同对象）」整段（持久化 vs 不持久化判断 / 一句话总览 / customRange 特殊处理 / 已用 key 列表 / 测试模式 / 反观点）
- `.cursor/skills/risk-monitor/SKILL.md` —— Step 5「Build the Frontend Tab」加「Filter persistence (必做 · OPT-0025)」段
- 这 3 个 doc 文件都是 **gitignored 本地资产**（`.gitignore` line 46 / 57 / 23），不进 commit

**与 AC 偏差（用户 claim 时拍板）**

- ❌ **不加「重置过滤」按钮**（AC 原本要求 icon RotateCcw 贴工具栏）—— 用户决定保持 OPT-0023 化繁为简方向，footgun 缓解靠「工具栏过滤器永远显式渲染当前值（不折叠）」+ 「用户手动改回默认值」。如果未来频次高再单独 file follow-up

**测试 / 验证**

- `npm test` 一行验证：48/48 通过（30 commission + 18 useFilterPersist），163ms
- `npx tsc --noEmit` 干净，无新 lint 错误（baseline 3 个 pre-existing 错误：1 个 unused var + 2 个 `any`，与本 OPT 无关）
- **未做浏览器手动验证** —— 留给用户验收。改动是 standard React useState init + useEffect 模式，纯函数已经测试覆盖，hook 是薄包装

**实施时遇到的（值得追踪）**

- 并发 session 把我的 branch 移到了独立 worktree `/opt/myproject/New-IT-System-opt0025`，stash 救援 + 不同分支切换发生数次。**好用：worktree 隔离 + stash 保活** 。这次反映出**多 session 共享单仓时需要主动用 `git worktree add`** 切隔离，否则各自 `git checkout` 会互相覆盖工作树（已在 [git worktree 知识] 中记忆）
- **gitignore 边界**：CLAUDE.md / docs/** / .cursor 都是 local-only assets，文档侧改动不进 commit。Memory feedback `feedback_lessons_and_cursor_local_only.md` 已经记录过

**Stage 1 outsider-review**：用户选 No（5 个 tab 改动 standard 模式 + 18 项测试覆盖纯函数 + 不动后端 / SQL / SSE）

**Follow-up（值得追踪）**

- 用户浏览器实际验证后如发现某些 tab 默认持久化体感不佳（如 ruleFilter 持久化导致漏告警），可单独 file follow-up「ruleFilter 默认不持久化」或「加重置按钮」
- 跟 [[OPT-0001]]（tab 切换缓存）零冲突，本 OPT 产出的 hook 是 OPT-0001 实施时可以读的
