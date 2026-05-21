---
id: OPT-0025
title: Risk-monitor 工具栏过滤器持久化（rangePreset / ruleFilter / serverFilter / onlySharedIp）
status: ready
priority: P2
area: frontend
effort: S
created: 2026-05-21
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
