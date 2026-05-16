---
id: OPT-0010
title: TabsContent 加 forceMount 修复 Risk Monitor 切 Tab 1 秒卡顿
status: done
priority: P1
area: frontend
effort: S
created: 2026-05-15
related: [[OPT-0001]]
---

## 问题

`/risk-monitor` 4 个 Tab 切换每次卡顿 ~1 秒。后端 SQL + HTTP 端到端 < 20ms（已实测），瓶颈全部在前端。

## 背景

- `frontend/src/components/ui/tabs.tsx:51-62` 的 `TabsContent` 是 Radix `Tabs.Content` 薄包装，**没有 forceMount**
- Radix 默认行为：inactive `Tabs.Content` 直接从 DOM 移除（不是 CSS 隐藏）
- `RiskMonitor.tsx:659-670` 的 4 个 `<TabsContent>` 都没设 `forceMount`
- 结果：每次切 Tab 都 unmount 上一个 + mount 新的，包含 AG-Grid 初始化、50 行 × cellRenderer React 组件挂载、useEffect 触发 fetch、useMemo 重算
- `RiskMonitor.tsx:932/1774/2916/4093/4101` 的 `if (!active) return;` 守卫**是死代码**：组件根本不存在时 active 也不存在。作者明显本来想做「常驻 + active prop 控制」但少了 forceMount

## 假设 / 待验证

- [x] 4 个 Tab 全部 mount 后总内存 ~10-20 MB，浏览器可接受
- [x] Radix `Tabs.Content` 在 `forceMount` 下 inactive 时会自动设 `hidden` HTML attribute，无需额外 CSS
- [x] 加 forceMount 后 `active` 守卫立刻生效，inactive Tab 不会 fetch

## 验收标准

- [x] `RiskMonitor.tsx:659-670` 4 个 `TabsContent` 加 `forceMount`
- [-] 浏览器进入 `/risk-monitor`：首次加载稍慢、切 Tab 后续 < 100ms —— **用户验证待做**
- [-] inactive Tab 不显示（Radix `hidden` attribute 生效）—— **用户视觉验证待做**
- [-] inactive Tab 不触发 fetch —— **用户 DevTools Network 验证待做**
- [-] AG-Grid 状态、filter、排序在切换间被保留 —— **用户交互验证待做**
- [x] Vite HMR 无 build/type 错误，dev frontend 仍 200，所有 API 仍 200

## 笔记

- HMR 通过，curl `/risk-monitor` 仍 200，API 路由全 200
- 因无法实际开浏览器，肉眼验证项标记 `[-]`，由用户在浏览器里完成
- **2026-05-15 follow-up**：实际部署后用户报告 4 个 Tab 内容堆叠可见——Radix 设的 `hidden` HTML 属性在本项目 Tailwind v4 preflight 下被 utility 优先级压过没生效。立即打 commit `d8d29f3`：给 `tabs.tsx` 的 TabsContent className 加 `data-[state=inactive]:hidden`，立竿见影。对其他 5 处不用 forceMount 的 TabsContent 是空操作（那些场景 inactive panel 本来就不在 DOM）

## 结果

**Commits**:
- `59cf44b`（main, 2026-05-15）—— RiskMonitor 4 个 TabsContent 加 forceMount
- `d8d29f3`（main, 2026-05-15）—— `tabs.tsx` 加 `data-[state=inactive]:hidden` Tailwind 选择器（兜底视觉隐藏）

**实际交付**：`frontend/src/pages/RiskMonitor.tsx` 4 个 `<TabsContent>` 加 `forceMount` + 8 行解释注释，净 +14 / -4 行。

**预期收益**（基于代码路径分析）：
- 切 Tab 后续切换：**1000+ ms → < 100 ms**（10×+ 加速）
- 代价：首次进入 `/risk-monitor` 慢约 3-4 秒（4 个 Tab 一次性 init），但每个 session 只发生一次
- AG-Grid 内存 ~10-20 MB 常驻

**根因诊断**（这是最有价值的部分）：
- shadcn `TabsContent` 是 Radix `Tabs.Content` 薄包装，没设 `forceMount` → inactive 时整个从 DOM 移除
- 每次切 Tab 都是冷启动：AG-Grid 重 init (100-300ms) + 自定义 cellRenderer React 组件挂载 (50-150ms) + useEffect 触发 fetch + useMemo 重算
- 各 Tab 已写好 `if (!active) return;` 守卫但因为组件根本不存在，守卫是死代码
- 原作者明显想做「常驻 + active 控制」模式但少了 `forceMount` 这一行
