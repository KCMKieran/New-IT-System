---
id: OPT-0058
title: prod 日志降噪：6 种模板消息占 95% 字节，行数 −61% / 字节 −65%
status: done
priority: P2
area: backend
effort: S
created: 2026-08-17
related: [[OPT-0012]], [[OPT-0037]], [[OPT-0038]], [[OPT-0040]], [[OPT-0035]]
---

> **流程说明**：本条不是从 backlog 走 file→claim→wip 的常规 OPT。用户直接要求
> 「分析 prod log 有没有重复无用信息」，分析完当场拍板做 1–5 项，同一会话实施完毕。
> 补记进 tracker 是为了让 done.md 保持完整——**不是**在追认一次流程违规，
> README §执行隔离铁律 针对的是「已 file 的 OPT」，本条从未进过 backlog。

## 问题

`backend/logs-prod/backend.log.2026-08-14`（普通工作日，**8171 行 / 1.31 MB**）实测：
**6 种模板化消息占当日日志字节的 95.2%**，而同一天 WARNING+ERROR 合计只有 **17 行（0.2%）**。

| 消息 | 行数 | 字节占比 | 性质 |
|---|---:|---:|---|
| `Martingale: snapshot ladder behind gate` | 1712 | 32.6% | 噪声，且逐字重复 |
| `Request started` | 1445 | 16.7% | 与完成行冗余 |
| `Scan complete [...]` | 1440 | 14.8% | 心跳，52% 是 `0 new` |
| `XAUUSD snapshot: wrote N rows` | 1440 | 14.1% | 纯心跳，91% 同值 |
| `Request completed` | 1445 | 13.7% | 与开始行冗余 |
| `Fast tier: scan_lock held by slow tier` | 287 | 3.3% | 设计内行为 |

三条关键实证：

1. **周末零用户仍写 3331 行**（08-16），其中 3168 行（95%）纯粹是两个 scheduler 的心跳——
   没人用系统它也在满速写日志。
2. **martingale 行真的在逐字重复**：1712 行只有 1253 个唯一 message body。同一句
   `snapshot latest=2026-08-14T02:33:18Z < gated open=...T03:13:59Z` 在
   **11:14:53 / 11:15:53 / 11:16:53 连续三分钟一字不差打了三遍**——fast tier 的
   overlap window 每 60s 重新评估同一 candidate，状态没变也照打。
3. **`VIEW_PROFILES_ADMIN_DEVICES is empty` 占掉全天 WARNING 的 47%**（8/17）。
   它是 lifespan 里的启动期配置检查，但 prod 跑 `uvicorn --workers 4`，
   4 个 worker 各打一遍 ×2 次部署 = 8 行。真信号（OIDC state 未绑定、
   `alert_events` NULL user_id）被埋在里面。

**为什么值得修**：一个没人能扫的日志就是没人看的日志。17 条真信号混在 8171 行里，
出事时第一步是 `grep -v` 一串已知噪声——而那正是漏掉新噪声里藏的真问题的方式。

## 改动（6 处，5 项）

| 文件 | 改动 |
|---|---|
| `app/services/rule_martingale_service.py` | 逐 ladder 行降 DEBUG；每 tick 一条聚合 INFO（`N 个候选 / M 条 ladder`，单条时直接点名） |
| `app/core/trace_middleware.py` | `Request started` 降 DEBUG；完成行改成自带 method/path/status/duration/client 的单行 `Request:` |
| `app/core/xauusd_snapshot_scheduler.py` | 新增 `_should_log_write()`：行数变化才 INFO，否则 DEBUG + 每小时心跳 |
| `app/core/burst_open_scheduler.py` | 新增 `_should_log_scan_complete()`：有新告警才 INFO，否则 DEBUG + 每 tier 每小时心跳 |
| 同上 | `scan_lock` skip 降 DEBUG，新增 `_consecutive_fast_skips`，**连续 3 次**才升 WARNING |
| `app/main.py` | `VIEW_PROFILES_ADMIN_DEVICES` 警告移进 scheduler flock 选举内（4 份 → 1 份） |

### 保留原则（写进了代码注释 + logging-system.md §2.2.1）

**只降「正常态」，不降「有事发生」。** 三条不许碰：

- 任何 WARNING / ERROR 路径
- 产生了新告警的扫描 tick（这是 scanner 存在的意义）
- 连续故障的升级行

每个降级都配了**每小时心跳**，保住「静默 = 真的停了」这个判读——否则
「行数稳定所以不打」和「job 挂了所以不打」在日志里长得一模一样。

### 为什么 `Request` 是合并不是删掉

先核对了 nginx `logs/nginx-prod/access.log`（`log_format kcm_audit`，JSON）：
它有 `method` / `uri` / `status` / `req_time` / `client_ip` / `trace_id`，
backend 那对 trace 行的字段它全有，`trace_id` 还能直接 join 回 `backend.log`。

**但两个缺口让「整个删掉 backend 侧」不成立**：

1. **没有认证用户身份**——`cf_user` 字段恒为空（CF Access 正在撤）。
   `grep <邮箱> backend.log` 这个用例只有 backend 侧能满足。
2. **绕过 nginx 的流量它看不见**——08-17 当天 376 个后端请求里 16 个（4%）直连 `:8001`，
   包括 `/risk-monitor/alerts`、`/admin/users`、`/client-return-rate/query` 这些真实
   API 调用（还有 SSE `/alerts/stream`）。

所以合并成一行、保留 user 列和直连覆盖。

## 结果

**投影**（把新规则回放到 08-14 的日志上算出来的，**不是**已部署的实测）：

| 类别 | 原 | 新 | 依据 |
|---|---:|---:|---|
| Martingale gate | 1712 | 647 | 当天有 skip 的 tick 数 = 647 |
| `Request started` | 1445 | 0 | 全部降 DEBUG |
| `Scan complete` | 1440 | ~700 | 当天 691 个 tick 真有新告警 + 每小时心跳 |
| XAUUSD snapshot | 1440 | ~35 | **全天只有 11 次行数变化** + 24 次心跳 |
| `scan_lock` skip | 287 | 0 | 当天**零次**连续 3 连跳 → 不会引入新 WARNING |
| `Request completed` → `Request:` | 1445 | 1445 | 行数不变，单行略变长 |

**8171 行 → ~3220 行（−61%）；1.31 MB → ~0.46 MB（−65%）。**
WARNING 从 17 条降到 11 条真信号。

剩下的大头是 `Request:` 的 1445 行，其中 **1041 行是同一个浏览器 tab 每 60 秒轮询
`/api/v1/xauusd-positions/history`**（单一客户端 IP）——见下方 Follow-up。

### 验证

- `pytest` **1349 passed**（改动前基线 1334 + 新增 15）
- 新增 `backend/tests/test_log_volume_guardrails.py`（12 个）+ martingale 文件补 3 个。
  这些是**防回退闸门**：把「有新告警必须 INFO」「连续 skip 必须 WARNING」
  「单次 skip 不许进 WARNING」「tier 之间心跳不共享」钉死——
  这类降噪逻辑最容易被后人一句「就一行日志而已」改回去，而那句话一天成立 1440 次。
- dev 端到端实测：3 次请求 = 3 行，
  `Request: GET /api/v1/health status=200 duration=2.49ms client=172.21.0.1`

### 文档同步

- `docs/architecture/logging-system.md` — 新增 **§2.2.1 周期性日志的降噪规则**
  （实测表 + 新增周期性日志时的三条自问 + 不许降级清单）；§3 inode 验证 grep 串更新
- `docs/architecture/backend-logging.md` — §3.2/§3.3 查询命令改用 `Request:`，补旧格式说明
- `docs/operations/runbooks/log-rotation.md` — inode 跟随验证的 grep 串更新
- `docs/architecture/audit-log-design.md` §D8.5 ① — 「前 2 行没有用户」改成现状
- `docs/architecture/audit-log-process.md` — 接缝问题 3 补后续注
- `docs/optimization/items/OPT-0012-*.md` — `Scan complete` 不再每 tick INFO

## Follow-up（未做，非本条 scope）

1. **每 60 秒轮询的前端 tab**（P2）——`/api/v1/xauusd-positions/history` 全天 1041 次，
   占降噪后日志的三分之一。改 SSE 或降频，或给该端点单独静音。
2. **logrotate 没开压缩**（P3）——`deploy/logrotate/new-it-backend` 是 `nocompress`，
   30 天纯文本，prod 67M + dev 44M。加 `compress` + `delaycompress` 能压掉约 90%。
   ⚠ **绝不能改成 `copytruncate`**（inode 不变 → `WatchedFileHandler` 检测不到 → 日志停写）。
3. **nginx access.log 明文记录 OIDC 授权码**（P2，安全）——
   `frontend/nginx.conf` 的 `map $args $args_redacted` 只脱敏了 `api_key`，
   `/auth/callback` 的 `code=` 和 `state=` 整串落盘、保留 90 天。
   实际风险不高（code 一次性、后端立刻消费），但登录失败时有几分钟未消费窗口。
   修法：在同一个 map 里加 `code=` 规则。⚠ 改 `nginx.conf` 必须 rebuild web 镜像。
4. **磁盘残留**（P3）——`backend/logs/` 里 2026-07-13 起一批旧
   `TimedRotatingFileHandler` 时代的归档（logrotate 的 `dateext` 不认这批命名，
   永不清理，44M）；`logs-prod/backend.log.inode-test` 是 08-08 的验证残留（1.4M）。
