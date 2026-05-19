---
id: OPT-0018
title: 全链路缓存审计与硬化（HTTP / 应用 / Redis / DB）
status: idea
priority: P2
area: mixed
effort: M
created: 2026-05-19
related: [[OPT-0001]] [[OPT-0002]] [[OPT-0006]] [[OPT-0014]]
---

## 问题

项目现在跨 4 层（浏览器 / 前端应用 / 后端 Redis + 进程内 / DB）都有缓存，但**没人系统看过一遍**：哪几层是真在工作的、哪几层是空白、哪些 TTL / 容量 / 失效策略经不起 prod 流量翻倍。

本 OPT 的产出是一份 audit + 把发现的硬骨头拆成可独立执行的子 OPT，**不在本 OPT 里直接修代码**（除非是 1 行就能加的护栏）。

## 背景

### 范围说明（2026-05-19 用户校准）

**ClickHouse 当前低活**：PnL Analysis、IB Financial Report 这两个走 ClickHouse 的页面用户暂时几乎不使用。因此涉及 ClickHouse 的缓存层（PnL 30min Redis、IB 10min Redis、IB Groups 7-day 内存缓存、`singleflight` ClickHouse 去重）**不是本 audit 的优先级**。

留在 scope 内的活跃路径：
- Client Return Rate（**MySQL → Redis 3h**，日志日均 20+ 命中）—— 唯一被频繁打到的缓存路径
- Risk-monitor 系列（SQLite WAL + 内存 `_latest_result`）
- ROACE 快照（SQLite，OPT-0006）
- Login IP 分析（SQLite + `@lru_cache`）
- 前端 / Nginx / HTTP 全局策略

未来如果 ClickHouse 路径被弃用 / 简化，IB Groups TTL、PnL/IB cache、singleflight 这些条款可能整体下线，**单独开 OPT 评估，不在本 audit 内决定**。

### 已知工作良好的层（不动）

| 层 | 实现 | 文件 |
|---|---|---|
| 静态资源 | Vite hashed filename + Nginx `expires 1y` + `Cache-Control: public, immutable` | `frontend/nginx.conf:36-40` |
| index.html | `no-cache, no-store, must-revalidate` 防旧 chunk 404 | `frontend/nginx.conf:19-25` |
| SSE | `Cache-Control: no-cache` + `proxy_buffering off` | `frontend/nginx.conf:48,61` |
| SQLite risk_monitor.db | `WAL` + `cache_size=-64000` + `synchronous=NORMAL` | `backend/app/core/risk_monitor_db.py:106-110`（OPT-0014） |
| Client ROACE snapshot | 预聚合表消除 19M 行 JOIN | `backend/app/core/client_roace_db.py`（OPT-0006） |
| Client Return Redis 3h | 生产日志频繁命中（每日 20+ hit） | `backend/app/services/client_return_service.py:593-595` |

### 已发现的 5 条真问题（按风险排序）

#### 🔴 1. Redis 缺 maxmemory + noeviction 默认 → OOM 时静默故障

证据：
- `docker-compose.prod.yml:74-77` 整个 redis-prod 块只有 4 行（image / container_name / restart），**无 `command:` 覆盖**，无 `--maxmemory`，无 `--maxmemory-policy`
- `redis-cli INFO memory` 实测：`maxmemory_human=0B`，`maxmemory_policy=noeviction`
- 当前 `used_memory=1.09M / peak=5.60M`，还远，但是没护栏

后果：当任何 service 大量写 key（比如未来给 risk-monitor 加 alert dedup cache），写到内存满 → 后续 `SET` 直接报错 → 缓存层退化但 endpoint 仍返回结果（路径都有 fallback 到 ClickHouse），监控发现不了。

#### 🔴 2. Redis 数据盘是匿名 docker volume，AOF 关闭

证据：
- `docker inspect new-it-redis-prod` 显示 Mount 是匿名 volume `3e87db3c91a...`，不在 compose 里显式声明
- `INFO persistence`: `aof_enabled=0`，仅默认 RDB（每 N 秒 / M 次变更触发 BGSAVE）
- `rdb_last_save_time=1779082125` → 2026-05-18 13:28 HKT（约 24h 前的快照）

后果：
- `docker compose down -v` 一行把缓存清空。这本身可接受（缓存层），但**会导致每次升级后冷启动期 ClickHouse 流量峰值**，没有量化
- 没有显式 volume → 未来运维不知道这个 mount 存在，容易误删

#### 🟡 3. 前端无 React Query / SWR，每次切页全量重打后端

证据：
- `frontend/package.json` 无 `@tanstack/react-query` / `swr`
- 仅 AG-Grid 列状态走 `useGridColumnPersist` localStorage 持久化
- `login-ip-search-cache.ts` 用 sessionStorage，**单页面单实现**，无复用模式（这块 OPT-0002 已覆盖）

后果：浏览器无 cross-tab 网络去重；用户在客户端来回切 tab / 翻页时，前端每次都打到后端 → 哪怕 Redis 命中，也消耗了 nginx + FastAPI + Redis round-trip。中等用户量没事，但 scaling 时这是第一个瓶颈。

#### 🟡 4. API 路由零 HTTP cache header（除 SSE / 静态）

证据：grep `Cache-Control` / `ETag` / `Last-Modified` 在 `backend/app/api/v1/routes/` 全部 endpoint 只命中 SSE stream（`risk_monitor.py:210`）。

后果：即使数据 5 分钟内根本不变，浏览器仍然每次完整重发请求 + 服务端完整重新走一遍。配合 #3 影响放大。

#### ⚪ 5. ~~PnL / IB Redis 缓存日志可见命中 = 0~~ → 已澄清，**不是 cache 问题**

原假设：cache 命中率异常。**澄清后**：PnL / IB 是 ClickHouse 功能，目前 user 基本不用，"零命中" = 这俩 endpoint 几乎没被调用，不是 cache 实现问题。

留作 follow-up 信号：如果未来 PnL / IB 重新启用，再回头看命中率是否符合预期；当前不投入诊断。

### 已知的灰色地带（先记着，audit 时验证）

**活跃路径**：
- Burst Open `_latest_result` 模块级变量（`burst_open_scheduler.py:32-35`），**无 size / TTL 限制** —— risk-monitor 在用，需评估
- Client Return 3h TTL 业务上是否过长？（财务团队对实时性的要求需要确认）—— 唯一活跃的 Redis 缓存

**ClickHouse 路径（低优先，等 feature 复活再看）**：
- IB Groups 进程内内存缓存 TTL **7 天**（`clickhouse_service.py:114-183`）—— 组别中途变更无 invalidation 路径
- `singleflight.py` 日志零信号 —— 是真没并发，还是日志被吞了，暂不深究

## 假设 / 待验证

- [ ] Redis maxmemory 设多少合适：当前 peak 5.6M（且 PnL/IB 几乎没在写），1 年内 Client Return + 未来新 feature 峰值估算
- [ ] 是否引入 React Query：依赖体积 vs 收益（涉及全前端改造，可能要单独 L 级别 OPT）
- [ ] HTTP ETag 是否值得加：要看典型 endpoint payload 大小（小 payload 时 ETag 协商开销可能 ≥ 收益）
- [ ] Burst Open `_latest_result` 内存边界：极端场景能涨多大？要不要加上限
- [ ] Client Return 3h TTL 业务上是否过长：财务实时性需求确认
- [ ] **ClickHouse 路径整体去留**：PnL/IB 长期低活的话，是否考虑下线（单独开 OPT 评估，不在本 audit 内决定）

## 验收标准

本 OPT 是 audit/scoping 性质，**不直接修代码**。完成定义：

- [ ] 产出 `docs/architecture/cache-layers.md`（或合并进现有 architecture 文档）：
  - 一张表列出 4 层每一层的当前实现、TTL、失效策略、覆盖范围
  - 标注哪些是 OPT-0006/0014 已经做过的（避免回炉）
- [ ] 对上面 5 条真问题逐条决定：
  - 拆成独立 sub-OPT 立刻 file（priority + effort 都定下来），或
  - 写明"现状可接受、不动"的理由（live with）
- [ ] 至少为以下先拆出子 OPT（优先级建议见下）：
  - Redis maxmemory + eviction policy 硬化（建议 P1 / S，1 行 compose + 选 policy）
  - Redis 显式 volume + 持久化策略（P2 / S，需和运维确认丢失容忍度）
  - Burst Open `_latest_result` 内存边界评估（P3 / S，估值后决定是否加上限）
- [ ] 决定 React Query 引入 yes/no（如果 yes，单开 L 级别 OPT；如果 no，写明判断依据）
- [ ] （可选）独立开 OPT 评估 ClickHouse 路径去留 —— 不在本 audit 内决定

## 笔记

### 扫描时的发现汇总（2026-05-19）

**前端持久化使用情况**（`frontend/src/` grep）：
- `auth_token` → localStorage（`login-ip-search-cache.ts:28`）
- `login-ip-manual-search:v1:{token}` → sessionStorage，4.5MB 上限（同文件 L38）
- `grid-state-{gridId}` → localStorage（`useGridColumnPersist.ts:36`，OPT-0015）

**后端 Redis key 命名规范**（实际线上）：
- `app:pnl:cache:{md5_hash}` TTL=1800s
- `app:ib_report:cache:{md5_hash}` TTL=600s
- `app:client_return:cache:{md5_hash}` TTL=10800s

命名一致：`app:<feature>:cache:<md5>`，可作为未来新 cache 的规范。

**Python 进程内 cache 清单**：
- `clickhouse_service.py:114-183` IB groups, 7 天
- `rule_gap_trade_so_service.py:104-129` `@lru_cache(maxsize=64)` 读 IP→accounts JSON
- `burst_open_scheduler.py:32-35` `_latest_result` 模块级单例

**SQLite PRAGMA 配置**：
- `risk_monitor_db.py:106-110`：WAL + 64MB cache + busy_timeout=5000 + temp_store=MEMORY（OPT-0014 已优化）
- `login_ip_db.py:176-177`：仅 WAL + foreign_keys=ON
- `client_roace_db.py:23-30`：无自定义 PRAGMA（OPT-0006 做的预聚合，本身查询很小不需要）

**APScheduler 预热任务（间接给后续查询暖缓存）**：
- ROACE refresh 06:00 HKT（OPT-0006）
- Login IP analysis 07:00 HKT
- Burst Open scan 10-60s tier（OPT-0011）
- IB Financial report 17:00 HKT
- Fund Flow monitoring 5-10min

**Redis prod 运行时 snapshot（2026-05-19 audit 时）**：
```
uptime_in_days: 29           ← 上次重启
DBSIZE: 0                    ← 当前空（也许刚 BGSAVE 后 reload？需复查）
maxmemory: 0B                ← 无上限
maxmemory_policy: noeviction ← 默认
keyspace_hits: 9
keyspace_misses: 99          ← 8.3% hit rate（uptime 29 天累积，明显偏低）
expired_keys: 99
evicted_keys: 0
aof_enabled: 0
rdb_last_save: 24h ago
```

## 结果

_待填_
