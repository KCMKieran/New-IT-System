---
id: OPT-0013
title: Risk-monitor 用 SSE 推送告警到前端（替代轮询）
status: idea
priority: P2
area: mixed
effort: M
created: 2026-05-16
related: [[OPT-0011]] [[OPT-0012]]
---

## 问题

`/risk-monitor` 页面当前用 `setInterval` 按 `scan_interval_min` 节拍调 `GET /alerts`：

```
后端扫完写 SQLite 这一刻  →  前端下一次 setInterval 才会去 fetch  →  最坏延迟 ≈ 2 × scan_interval
                              （后端 10 min + 前端 10 min = 20 min）
```

即使后端节拍提到 60s（OPT-0012），前端如果还是 60s 轮询，最坏端到端延迟仍是 ~2 min。**前端这一层轮询是真正的延迟瓶颈**。

副作用：每个打开页面的用户都在每 N 分钟全量重拉 `/alerts`（默认 last 4h，可能几百-几千行），后端 + SQLite 都被 hammer。

## 背景

- **现有实现位置**：`frontend/src/pages/RiskMonitor.tsx` 里每个 Tab 有自己的 auto-refresh useEffect
- **现有"准推送"机制**：无 —— 完全是轮询架构
- **关键现实**：业务上看告警的是风控 / dealing desk 同事，通常 1-3 人同时在线，规模不大 → 不需要 Redis pubsub / 消息队列，单进程 in-memory 订阅就够
- **SSE vs WebSocket**：本场景是**单向**（后端 → 前端），SSE 完爆 WebSocket：
  - 走 HTTP，能穿透公司代理 / Cloudflare Tunnel
  - 浏览器原生 `EventSource` 自动重连
  - FastAPI 一行 `sse-starlette.EventSourceResponse` 就能写
  - 不用维护双向状态机

## 假设 / 待验证

- [ ] FastAPI 在 Cloudflare Tunnel 后面跑 SSE，长连接是否会被中间设施 30s/60s 切断？需要测试（可能要加 keepalive ping）
- [ ] 同时有 N 个用户打开页面 → N 个 SSE 长连接，对单 worker FastAPI 的并发上限是多少？（uvicorn 默认 1 worker、async 处理，应该轻松 100+ 连接，但要测）
- [ ] 前端：当 SSE 连接断开时，是 fallback 到原轮询，还是只显示连接状态让用户手动重连？
- [ ] 浏览器 tab 切到后台后，`EventSource` 仍持有连接吗？需不需要 `visibilitychange` 监听做断/连？
- [ ] 推什么粒度：完整 alert dict？还是只推「有新告警」让前端自己去 fetch？前者省一次 fetch，后者更解耦

## 验收标准

- [ ] 后端新增 `GET /risk-monitor/alerts/stream` SSE endpoint
  - 每条扫描结果（`append_scan_and_events` 之后）向所有订阅者推送 `{ rule_id, scanned_at, count }` 通知（轻量，不含完整 alert）
  - 订阅时可指定 tab 过滤（`?tabs=burst,quick_profit`），减少无关推送
  - keepalive：每 15s 推一个 `:ping` 注释，防代理切断
- [ ] 前端 `RiskMonitor.tsx`：每个 Tab 用 `useEventSource` hook
  - SSE 收到通知 → 增量 fetch（基于 since=last_scanned_at）→ prepend 新告警到表头
  - 加一个"实时连接"状态指示（顶部小圆点：绿=已连接 / 黄=重连中 / 灰=不可用 fallback 轮询）
- [ ] 现有 `setInterval` 轮询作为 fallback 保留（SSE 失败时自动接管），不删旧代码
- [ ] **不破坏**手动切时间范围 / 改 filter 的现有路径 —— 这些走的还是 REST `GET /alerts`，SSE 只负责"有新东西了"通知
- [ ] 压测：模拟 10 个并发用户开页面，10 min 内总 MySQL 查询数对比改前/改后

## 笔记

**与 OPT-0011 / OPT-0012 的关系**：三者协作让"看起来实时"成立。但 OPT-0013 可以**独立先做**（在现有 10 min 节拍下也有意义 —— 至少前端那 10 min 轮询延迟就归零了）。优先级仍排在 OPT-0011/0012 后，因为后两者收益更基础。

**为什么不选 WebSocket**：单向场景 + Cloudflare Tunnel + 单人开发维护 = 选越简单的越好。SSE 就是这个最简单的选项。如果未来真要双向（比如前端点"暂停某规则"实时下发），再升级。

**为什么不用 Redis pubsub**：当前是单 backend 进程，in-memory `asyncio.Queue` 完全够用。多 worker / 多进程时才需要 Redis —— 但项目用 Redis 已经有别的用途，未来要切换也是一个 import 的事。

**反对意见**：
- 「轮询 10 min 够用，为什么要做这个？」—— 反例：burst 告警 11:00 触发，11:01 dealing desk 想知道，但页面下次 fetch 是 11:10。这段 10 min 内可能已经被客户走掉。SSE 让这 10 min 归零
- 「SSE 长连接会不会有内存泄漏」—— 会，所以必须有 timeout + 心跳清理；这是常规防护

**优雅退化原则**：SSE 是**增强**，不是**替换**。所有功能在 SSE 失败时仍能用（只是变慢）。这意味着可以放心上线，不会引入新的单点。

## 结果

_待填_
