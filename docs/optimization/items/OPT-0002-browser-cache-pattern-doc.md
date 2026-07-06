---
id: OPT-0002
title: 现有浏览器缓存模式归纳成复用文档
status: wip
priority: P2
area: docs
effort: S
created: 2026-05-13
related: [[OPT-0001]]
---

## 问题

项目里已有 ≥1 个页面做过浏览器侧的状态/数据缓存（已知线索：client-return-rate 用 `sessionStorage`，见 `smart-commit/SKILL.md` 的 commit example）。但是：
- 没有统一文档说明用了什么方案、为什么这样选
- 后续新页面（包括 OPT-0001 可能要做的 risk-monitor）想复用时只能去 grep 代码
- 多种缓存方式如果各做各的，会变成项目里 3~4 种不一致的实现

## 背景

待审计的候选页面（开干时再 grep 确认）：
- `client-return-rate`：sessionStorage 缓存页面状态（已知）
- `ib-report`：可能有缓存（按已知的 10min Redis TTL 推断，前端可能也缓存）
- `pnl-*`：可能有缓存（已知 30min Redis TTL）
- 其他：grep `sessionStorage` / `localStorage` / `useQuery` / `swr` 在 `frontend/src/`

关联约定（已存在）：
- 后端 Redis 有 feature-specific TTL（PnL 30min, IB 10min, Return Rate 3h）—— 前端缓存策略应该和这个匹配，不要 over-cache
- React 18 StrictMode + `AbortController` 模式（`CLAUDE.md` 已规定）

## 假设 / 待验证

- [ ] 现有缓存到底有几种实现？grep 之后才知道（1 种 = 直接抽工具函数；2~3 种 = 选一种推为标准，迁移其他）
- [ ] 缓存的颗粒度统一选哪个：纯 filter 状态 / filter + 数据 / filter + 数据 + 滚动位置
- [ ] 是否引入 React Query 或 SWR？还是保持轻量手写 hook？（取决于现有依赖和复杂度）

## 验收标准

- [ ] 完成 grep 审计，列出当前所有用到客户端缓存的页面和它们的实现方式
- [ ] 产出文档：`docs/frontend/browser-cache-pattern.md`，至少包含：
  - 什么场景需要 / 不需要前端缓存（决策树）
  - 推荐的存储方式（sessionStorage / localStorage / 内存）选择标准
  - 推荐的 key 命名规范（避免不同页面冲突）
  - 失效策略（TTL / 手动刷新入口 / URL 参数失效）
  - 一份可复制的最小示例 hook（≤ 50 行）
- [ ] 在 `CLAUDE.md` 或 `.cursor/rules/frontend-ui-conventions.mdc` 里加一行引用
- [ ] 如果存在不一致实现，**只标注**不强制迁移（迁移单独开 item）

## 笔记

（填充时记录：哪个页面用了什么、为什么这样选、踩过什么坑）

## 结果

_待填_
