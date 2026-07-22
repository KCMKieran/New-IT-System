---
id: OPT-0054
title: open-positions endpoint 加 TTL 缓存 + singleflight
status: wip
priority: P2
area: backend
effort: S
created: 2026-07-22
related: [[OPT-0047]]
---

## 问题

`GET /api/v1/risk-cases/open-positions`（risk-watchlist「当前持仓客户」视图的数据源）
**每请求现算、无任何缓存**：每个打开该页的浏览器 tab 各自 60s 轮询一次，每次触发一发
~1.2s 的 5-CTE PG 全量聚合。但底层数据源 `kcm.active_positions_snapshot` 本身**每 60s
才刷新一次**——N 个人开着页面 = 同一份数据被重复算 N 次，PG 负载 ×N，纯浪费。

**实测（2026-07-22 钱路改造当天）**：`statistics.query_time_ms ≈ 1226`、748 行、
gzip 后 ~35KB/响应。

## 背景

- 路由：[`backend/app/api/v1/routes/risk_cases.py:96-125`](../../../backend/app/api/v1/routes/risk_cases.py)
  （`open_positions()`——每请求直接调 service，`statistics.from_cache` 从不置真，
  走的是 `WatchlistStatistics` 的默认 `False`，见
  [`backend/app/schemas/risk_cases.py:80`](../../../backend/app/schemas/risk_cases.py)）。
- Service：[`backend/app/services/risk_cases_service.py:254-416`](../../../backend/app/services/risk_cases_service.py)
  —— `_OPEN_POSITIONS_SQL`（L254，2026-07-22 起 5 个 CTE：per_symbol/hedge/pos_users/cash/
  closed_pl/rebate/acct 钱路聚合）+ `query_open_positions()`（L389，返回
  `(rows, newest_snapshot_iso)`）。
- 前端：[`frontend/src/pages/OpenPositionsPanel.tsx`](../../../frontend/src/pages/OpenPositionsPanel.tsx)
  每 tab 60s 自动轮询。
- 现有基建（直接复用，不新造轮子）：
  - [`backend/app/core/singleflight.py`](../../../backend/app/core/singleflight.py)
    `SingleFlight.do(key, fn)` —— 并发同 key 只执行一次，其余等待共享结果。
  - 项目 Redis TTL 惯例已成型（PnL 30min / IB 10min / Return Rate 3h，见 CLAUDE.md
    Concurrency 条目）——本 endpoint 只是把 TTL 缩到快照节奏。

## 方案要点

1. **TTL 缓存，30–60s**（对齐上游 60s 快照节奏）。**Redis 为主选**（对齐项目既有
   Redis TTL 缓存惯例，多 worker/多进程天然共享）；进程内存缓存为备选（实现更省，
   但多 worker 各存一份、命中率打折——实施时按部署形态拍板）。
2. **cache miss 走 singleflight**：复用 `core/singleflight.py` 合并并发 miss——
   缓存过期瞬间多个 tab 同时打进来时，只有 1 发打到 PG。
3. **`statistics.from_cache` 置真值**：schema 字段已存在（现在恒为 False），命中缓存时
   返回 `from_cache: true`，`query_time_ms` 如实反映（命中时应为个位数 ms）。
4. **效果**：无论多少 viewer 开着页面，PG 每 60s 最多被打 1 次 `_OPEN_POSITIONS_SQL`。

## 非目标

- **服务端分页**：当前千级行（~748 行 / ~35KB gzip）一页全量 + 客户端排序筛选没问题。
  只有将来该视图改吃 KCM 全量底座（几万 userId）才需要，届时**另 file 新 OPT**
  （kcm-risk-pipeline skill「改动时的注意点」已有此预警）。

## 假设 / 待验证

- [ ] 缓存序列化格式：rows 已是 plain dict + ISO 字符串，JSON 序列化应无障碍（`float`/
      `None`/`str`/`int` only）——实施时确认无 Decimal/datetime 漏网。
- [ ] TTL 具体值（30s vs 60s）：60s 与快照同步但最坏可见 ~2 分钟旧数据（快照 60s + 缓存
      60s）；30s 折中。实施时拍板，倾向 30s。
- [ ] Redis 不可用时的降级姿态：应 fail-open 直查 PG（与 case 层 fail-open 惯例一致），
      不许把 endpoint 打挂。

## 验收标准

- [ ] 两个并发/相继（TTL 内）请求，第二个响应 `statistics.from_cache: true` 且
      `query_time_ms` 显著小于直查（<100ms）。
- [ ] 缓存过期瞬间的并发请求只触发 1 次 PG 查询（singleflight 生效，日志或计数可证）。
- [ ] `snapshot_at` 随缓存一起存取，命中缓存时仍返回正确的快照时间（不许丢）。
- [ ] Redis down 时 endpoint 仍可用（fail-open 直查），不 5xx。
- [ ] 既有 route-mock 测试（`backend/tests/test_risk_cases_api.py` 两个 open-positions
      测试）不破；新增缓存命中/miss 路径测试。

## 笔记

- 数据源节奏是本方案的天然上界：`kcm.active_positions_snapshot` 每 60s 由 KCM 管道刷新，
  缓存 TTL ≤60s 时理论新鲜度损失可忽略（用户本来就在看最多 60s 旧的快照）。
- 钱路 CTE（cash/closed_pl/rebate）大多是 T-1 / ≤10min 级新鲜度，更没有理由每请求重算。
- 不缓存的唯一论据是「当前只有个位数 viewer」——但该视图是 risk-watchlist 默认视图，
  viewer 数只会涨；1.2s×N 的 PG 压力与 KCM 管道写入共享同一台 PG。

## 结果

<done/dropped 时填>
