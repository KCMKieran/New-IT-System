---
id: OPT-0001
title: Risk-monitor 四个 tab 切换状态/缓存优化
status: ready
priority: P2
area: frontend
effort: M
created: 2026-05-13
related: [[OPT-0002]] [[OPT-0010]]
---

## 问题

Risk-monitor 页面有 4 个 tab（含 `gap-trade`）。用户观察：从某个 tab 跳到其他页面，再回到该 tab 时，之前的查询条件 / 滚动位置 / 已加载的数据**好像丢失或被重新加载**。但用户自己也不确定，需要先客观验证。

URL 示例：`http://10.6.20.138:5173/risk-monitor?tab=gap-trade&_cb=1778644928655`

## 背景

- 数据源是后端 SQLite（risk-monitor 的 scan_history / alert_events），不是 ClickHouse；查询本身已经很快。
- 已知本仓库其他页面（如 client-return-rate）做过 `sessionStorage` 缓存（参考 `.cursor/skills/smart-commit/SKILL.md` 里的 commit example "前端：默认过去1周，sessionStorage 缓存页面状态"）。
- URL 里的 `_cb` 参数是 cache-buster，提示当前可能存在某种强制刷新逻辑，需要看清楚是有意的还是误用。
- 相关代码大致位置：`frontend/src/pages/RiskMonitor*` 及其下的 tab 组件，可能配合 `apiFetch` / React Router state。

## 假设 / 待验证

- [ ] **现象是否真存在**：切 tab → 切走 → 切回，是否真的丢了 state？区分三种情况：
  - (a) 组件 unmount → remount，state 被丢（React 行为，符合预期但 UX 差）
  - (b) 组件保留但触发了一次 refetch（可能是 useEffect 没正确处理依赖）
  - (c) `_cb` cache-buster 在 query key 里，导致每次进入都被识别为新请求
- [ ] **是否值得做缓存**：SQLite 查询如果本身 < 200ms，前端缓存的价值更多是"保留筛选条件 + 滚动位置"，不是省查询。
- [ ] **缓存的颗粒度**：tab 级别（每个 tab 独立缓存自己的 filter / 数据）vs 页面级别（整页快照）
- [ ] **缓存存储**：`sessionStorage`（关闭 tab 就丢）vs `localStorage`（跨会话保留）vs React Query / SWR 的内存缓存

## 验收标准

- [ ] 客观复现并记录现象（录屏 or 浏览器 devtools 截图 + 网络面板），结论写入笔记
- [ ] 决定要不要做（"不做"也是合法 outcome）；如果做，明确：缓存什么 / 存哪 / TTL / 失效条件
- [ ] 如果做：4 个 tab 表现一致，filter 状态切走再切回保留，不强制 refetch
- [ ] 如果做：保留一个手动刷新入口（按钮 or URL `?refresh=1`），cache-buster `_cb` 的来源被理解或移除

## 笔记

（填充时记得正反观点都写：如果"做了发现没必要"，也写下来作为后续判断依据）

**2026-05-15 更新**：[[OPT-0010]] (`59cf44b`) 已实质解决「**tab → tab 切换**」场景下的状态丢失：给 4 个 `TabsContent` 加 `forceMount`，组件不再 unmount/remount，AG-Grid / filter / 滚动位置全部跨 tab 切换被保留。本 item 剩余 scope 缩窄为：
- **跨页面导航**：从 `/risk-monitor` 切到 `/client-return-rate` 再回来，整页组件仍然 unmount（React Router 默认）→ 4 个 Tab 全部要重新挂载 + 重新 fetch。如果这个场景实际困扰用户，再考虑 `sessionStorage` 缓存
- **`_cb` cache-buster** 来源调查
- 如用户觉得「跨页面回来重 fetch 是合理的」，可直接 drop 本 item

## 结果

_待填_
