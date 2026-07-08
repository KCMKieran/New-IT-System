---
id: OPT-0044
title: 告警邮件中心 hardening —— 发送移出扫描锁 / digest 渲染上限 / digest UNIQUE 认领 / 注册表 dataclass 化
status: ready
priority: P2
area: backend
effort: M
created: 2026-07-08
related: [[OPT-0042]] [[OPT-0043]]
---

## 背景

OPT-0042/0043 close 前的 Stage 1 outsider-review（combined diff）确认了 4 条"规模变大才爆"的隐患，用户拍板全部打包本 hardening OPT（不阻塞当时的 merge）。原始 review 全文见 OPT-0043 item 的结果段。核心文件：`backend/app/services/alert_mail_dispatcher.py`、`backend/app/services/alert_mail/registry.py` + `service.py`、`backend/app/core/risk_monitor_db.py`（mail_* helpers）、`backend/app/core/burst_open_scheduler.py`（dispatch 钩子）。操作手册：`alert-mail-center` skill。

## 交付内容（4 条 + 2 条捎带）

1. **SMTP 发送移出扫描锁**（review F1，最重）：`dispatch_alert_mails()` 目前在 `_run_scan` 持 `_scan_lock` 时同步发信，`_retry_outbox` 每订阅每 tick 最多 50 行 × 30s 超时——订阅多 + Office365 故障 = 检测管线被堵几十分钟。改法：tick 内只**生成**（条件求值 + 写 outbox 行），实际 SMTP 发送移到独立的调度 job/线程（不持扫描锁），outbox 即天然队列。注意保持 at-least-once 与 per-login 冷却语义（回归测试已有）。
2. **Digest 渲染上限**（F5）：`_DIGEST_MAX_ALERTS = 5000` 会把 5000 个 HTML 段渲染进一封 ~5MB 邮件（O365 可能拒收，人也读不动）；`_build_sibling_map` 在大日量下是 O(hits × day_alerts)。改法：top-N（如 50）完整渲染 + "and X more (link to Tab 2)"，cap body 字节数；sibling 查询做成注册表可选项。
3. **Digest 跨进程 UNIQUE 认领**（F6）：`_digest_last_run` 是进程内存 dict，dev/prod 双进程同时到期会重复发（当前只靠 dev 关调度器的纪律防）。改法：DB 层认领——`UNIQUE(subscription_id, digest_date)` 或把 last-composed-date 持久化进 `mail_dispatch_cursor`，INSERT 冲突即让位。
4. **注册表 dataclass/Protocol 化**（F7）：`MAIL_SOURCES` 是 `Dict[str, Any]` 隐式 ~12 键，新模块漏键 = 每 tick 被 per-subscription except 吞掉的静默 KeyError。改法：`@dataclass`（`fetch_for_day`/sibling 等 hedge 特有项给默认值/Optional），import 时即校验；`_hedge_rules` 的 band-clamp 与检测侧 `rule_hedge_open_service` 重复实现——抽一个共享函数防漂移；可选：`make_fetchers(select_sql, band)` 工厂砍每模块 ~4 个复制粘贴 SQL fetcher。
5. **捎带（review live-with 中指名并入本单的）**：删掉 `schemas/alert_mail.py` 里定义但从未使用的 response envelope models（或给非 legacy 端点接上 `response_model`）；加 `GET /outbox/{id}`（`get_mail_outbox_row` helper 已存在），前端预览 Dialog 改用它（当前重拉 50 行整页找一行，翻页后还会 404）。
6. **顺手**：字段从 `filterable_fields` 下架时对存量订阅的 no-match 记 WARNING（当前 DEBUG）；`requeue_stale_mail_outbox` 从每订阅每 tick 提到循环顶部一次。

## 验收标准（AC）

1. 模拟 SMTP 挂起（mock 阻塞 send）时，slow-tick 扫描耗时不受影响（生成完成即返回）；邮件在发送 job 恢复后照常送出，无丢失无重复。
2. digest 订阅命中 >N 条时邮件只渲染 top-N + 汇总行，body 字节数有硬上限；现有 digest 语义测试不回归。
3. 双进程并发触发同一 digest 窗口（测试起两个调用方）只产出一行 outbox / 一封邮件。
4. 注册表改 dataclass 后：漏必填键在 import/启动时报错而非运行时静默；band-clamp 单一实现被检测侧与邮件侧共用（anti-drift 测试）。
5. `GET /outbox/{id}` 可用且前端预览改走它；schemas 无死 envelope。
6. 全量 alert-mail pytest + tsc + vitest 绿，零新增失败。

## 开放问题

- 发送 job 的形态：复用 APScheduler 分钟级 job（与 digest job 合并成一个"邮件泵"？）还是常驻后台线程——执行时按最小侵入定。
- webhook channel 列是否趁 outbox 表还小一起加（review F8，用户当时选 live with——执行本单时可再问一句）。
