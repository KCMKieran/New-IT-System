---
id: OPT-0042
title: 对冲刷佣邮件告警 v1（灭火版）—— 批量对冲命中即发 digest 邮件，outbox 可靠投递
status: done
priority: P0
area: backend
effort: M
created: 2026-07-08
related: [[OPT-0021]] [[OPT-0032]] [[OPT-0043]]
---

## 问题 / 背景

2026-07-03 客户 userId 154795（泰国）用两个账户（1-60011332 / 1-60011333，合计入金仅 $139.89）脚本化对冲刷佣：同一秒内成对开出 62 buy + 62 sell（NZDJPY，每单恰好 8.6 手，票号连续交替），单边 533.2 手；开仓瞬间点差+佣金把 equity 打穿至 **-$44,697**（两户合计 -$48,249 负余额由公司承担），IB 返佣按全量成交结算 —— "负余额 + 返佣套利"。

现有 hedge-open 检测（OPT-0021，rule_id 91-100）**已经抓到了**该事件（alert_events id=273504，08:02 入库），但淹没在噪音里（当前唯一规则是最宽松观察档 3s/1单/0.01手，7 天 1663 条告警，98.7% 是 <1 手碎单锁仓）。老板要求：此类行为发生时**立刻邮件推送**。

本 OPT 是 v1 灭火版：**不做 UI、不改检测引擎**，在已产出的 alert_events 之上加一层"订阅匹配 → digest → outbox → SMTP"的通知层。表结构按 v2（[[OPT-0043]] 风控告警邮件中心）的最终 schema 设计，v2 直接复用不迁移。

## 检测条件（已用近 7 天数据回测）

对每条新增的 hedge-open 告警（rule_id 91-100），**A 或 B 命中即进发信队列**：

- **条件 A · 大额对冲**：`min(buy_lots, sell_lots)` 的**标准手等值 ≥ 10**。
  - `.cent` 后缀品种（如 `XAUUSD.cent`，suffix 已在 alert_events 数据中确认）手数先 **÷100** 折算，否则 cent 户 15 手（实际 0.15 标准手）会伪命中。
  - 回测：7 天命中 4 条 / 4 账户（533.2 / 100 / 42.2 / 15 手），cent 折算后剩 3 条，零噪音。
- **条件 B · 程序化成对开单**：`min(buy_count, sell_count) ≥ 5` **且**匹配手数标准手等值 ≥ 1。
  - 防拆单绕线（533 手拆 0.5×1000 单绕过 A，但脚本的"成对连开"特征绕不掉）。
  - 回测：7 天命中 2 条 = 本案主案 + 彩排（彩排比主案早 17 分钟，邮件能赶在主案前送达）。
- 预期邮件量 ≈ 3-5 封/周。

**不做触发、只做邮件展示字段**：资金利用率 `matched_lots_std / max(net_deposit_hist, 1)`（net_deposit_hist 有 NULL/负数脏数据面，v1 不当硬触发；负数=已出金超入金，邮件里高亮）。

## 涉及文件（file:line 锚点）

- `backend/app/core/burst_open_scheduler.py:316-325` — hedge 扫描在 **slow tier**（`include_hedge = tier in ("all","slow")` :215，5min 一轮）。发送钩子挂 slow tick 的 `append_scan_and_events` 之后（本轮新增且已 dedup 的告警此刻是现成列表）。
- `backend/app/core/risk_monitor_db.py:133` `_DB_PATH`（SQLite risk_monitor.db）；`:1193 load_hedge_open_config()`；`:1646 append_scan_and_events()`。hedge 相关表 DDL 在 :333-357。
- `alert_events` 关键列：id, rule_id, server, login, symbol, order_count, total_lots, first_open(格式 `2026-07-03T08:00:56Z`), equity, balance, account_group（**不是** group）, currency, zipcode, net_deposit_hist, orders_json。
- `alert_hedge_open_detail`（id = alert_events.id 1:1）：buy_count, sell_count, buy_lots, sell_lots, window_start, window_end。
- `backend/app/services/email_service.py:30 send_email(subject, body, to, cc, attachments)` — HTML 邮件，SMTP 凭据来自 Settings（dev 容器已确认配好，smtp.office365.com:587）。
- `backend/app/api/v1/routes/risk_monitor.py` — hedge config API 所在，test-send 端点加这里。
- 参考告警行：`alert_events id=273504`（本案主案）、`id=272988`（XAUUSD 试探单）——试发邮件用真实数据渲染。

## 交付内容

1. **新表**（`risk_monitor_db.py` DDL，`CREATE TABLE IF NOT EXISTS`，按 v2 最终 schema）：
   - `mail_subscriptions`: id PK, name, module, rule_ids(json), conditions_json, mail_to, mail_cc, mode('realtime'/'digest'), cooldown_min, digest_time, enabled, updated_at, updated_by
   - `mail_outbox`: id PK, subscription_id, alert_ids_json, subject, body_html, recipients, status('pending'/'sent'/'failed'), error, created_at, notified_at
   - `mail_dispatch_cursor`: subscription_id PK, last_alert_id
   - **seed 一条订阅**：name=`批量对冲刷佣`, module=`hedge_open`, conditions = 上述 A OR B（JSON 表达，条件求值逻辑放 dispatcher 代码），mode=realtime, cooldown_min=30, enabled=1, **mail_to=`kieran.xiang@kohleservices.com`**（用户拍板：测试期只发 Kieran，正式收件人后续再改）。
2. **dispatcher 服务** `backend/app/services/alert_mail_dispatcher.py`：
   - 游标增量拉 alert_events（`id > last_alert_id AND rule_id BETWEEN 91 AND 100`），JOIN detail 表取 buy/sell 字段。
   - 条件求值（含 `.cent` ÷100 折算，用 `symbol.lower().endswith('.cent')` 判断）。
   - **per-login 冷却 30min**：冷却期内命中仍写 outbox，合并进下一封。
   - **一轮一封 digest**：本轮所有命中合并成一封（多账户各一段）。
   - **outbox at-least-once**：先写 outbox 行（status=pending）再发；发成功填 notified_at + status=sent；失败记 error + status=failed，下一 tick 自动重试 pending/failed 行。
   - **Sibling accounts 查询**：发信时查同 login 前缀关联不可行——用 CRM 不现实，v1 降级为"同 IP/同日其他 hedge 告警账户"？→ **v1 只查：当日 alert_events 里是否有其他 login 命中同样条件**，列在邮件里（本案 332/333 互现）。不查 MySQL、不查 CRM（保持 dispatcher 零外部依赖）。
3. **调度钩子**：`burst_open_scheduler.py` slow tick 末尾调用 `dispatch_alert_mails()`，全程 try/except 包裹（发信失败绝不能影响扫描主流程）。
4. **邮件模板**（英文正文，遵循 alert-email-style skill：无 emoji、纯文本感、表格对齐字段、MT/HK 双时间、选择性加粗）。每账户一段：Account/Matched rule/Window(MT+HK)/Orders(buy+sell counts, symbol, lots each)/Matched lots/Equity(负值高亮)/Net deposit/Lots per $1/Other accounts alerted today。页脚附 risk-monitor 页面链接 + 订阅配置最后修改时间。
5. **test-send 端点**：`POST /api/v1/risk-monitor/hedge-mail/test-send` — 用最近一条真实命中（无则用 id=273504）渲染模板发给指定收件人，响应走项目统一 shape。
6. **pytest**：条件求值（含 cent 折算边界）、冷却合并、outbox 重试、游标推进。测试种子时间**必须相对 now**（⚠ [[OPT-0041]] date-rot 教训，30 天保留窗会清掉硬编码旧日期的种子）。

## 环境注意

- ⚠ **dev 后端共享 prod SQLite**（见 commit 355052b）。DDL 是加法安全的；seed 订阅 mail_to 只有 Kieran，不会误发业务方。
- dev 后端 uvicorn --reload 热加载，改完代码 docker exec 即可驱动真实试发。
- MT 时间 UTC+3，HK UTC+8；alert_events 时间已是 UTC ISO8601（`...Z`），模板里换算两地时间展示。
- 代码注释一律英文。

## 验收标准（AC）

1. 新表建好 + seed 订阅存在；`load` 后 dispatcher 能跑通全链路。
2. 用 2026-07-03 真实数据回放（或 test-send 端点）**实际发出一封邮件到 kieran.xiang@kohleservices.com**，内容含 60011332 主案字段，格式符合 alert-email-style。
3. 条件回测复现：近 7 天数据上 A∪B 命中集合 = {60011332, 60011333, +cent 折算后存活的账户}，无碎单噪音。
4. SMTP 故障模拟（mock）下 outbox 行保留并在下一 tick 重试。
5. `cd backend && python -m pytest -q` 新增用例全绿、无新失败；scan 主流程不受 dispatcher 异常影响（异常被吞并 log）。

## 结果（2026-07-08 close）

**实际交付 = AC 全项达成**：三表 + seed 订阅（mail_to=kieran）、dispatcher 全链路、slow-tick 钩子、test-send 端点、30 个 pytest（全绿，41 个 date-rot 失败为 main pre-existing）。7 天回测 1834 条 hedge 告警 → 精确 2 命中（60011332 主案 / 60011333 彩排），零碎单噪音；真实邮件经 SMTP 送达 kieran 验证（alert 273504 渲染）。

**与 spec 的偏差**（见实施记录）：冷却期"仍写 outbox"实现为游标 holdback + 下一封 digest 合并（可观测语义等价）；页脚链接硬编码 prod URL（后端无 frontend-base-url 配置）。

**Workflow 内 review（实施期，非 Stage 1）**：4 视角 + 对抗核实，7 条确认全部当场修——SMTP 30s socket 超时（扫描锁内发信防挂死）、游标冷启动初始化到 MAX(id)（防 30 天历史回放）、重试上限 + dead 状态、rule_ids 生效、dev/prod 双进程 dispatch 护栏、test-send async 阻塞、扫描深度 500→2000。

**Stage 1 outsider-review**：与 [[OPT-0043]] 合并跑（combined diff），处理记录见 OPT-0043 结果段。
