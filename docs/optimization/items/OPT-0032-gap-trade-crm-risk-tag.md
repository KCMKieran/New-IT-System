---
id: OPT-0032
title: Gap Trade 超额获利客户 → 自动给 CRM 上风控 tag（+ 盘中实时快扫）
status: wip
priority: P1
area: mixed
effort: L
created: 2026-06-02
related: [[OPT-0024]], [[OPT-0030]]
---

## 问题

[risk-monitor Gap Trade tab](http://10.6.20.138:5173/risk-monitor?tab=gap-trade) 的「Gap Trade 超额获利客户」（rule 81）目前**每天只在 HKT 07:20 cron 跑一次**，且只是把命中客户展示在页面上。业务诉求：客户在休市开盘缺口里拿到超额收益后，常常**获利即出金**，而出金是**自动审批**的 —— 等第二天分析师看到为时已晚。

要做两件事：
1. **盘中实时检测**：开盘 1 小时内（缺口活跃期）以 ~5min 频率快扫，尽早发现超额获利客户。
2. **检测到即给 CRM 上风控 tag**：上 tag 后该客户的出金转为 **CS 人工审核**（不再自动审批），把出金 hold 住。

## 背景

**当前检测（rule 81）** —— `backend/app/services/rule_gap_trade_gap_service.py`：
- 窗口 MT 00:00–02:00，按 `mt4_users.userid` 客户级聚合**已平仓** P&L（CEN ÷100）。
- 双阈值任一命中：`profit_ratio ≥ profit_ratio_min`(默认 1.0) 或 `total_profit ≥ min_profit_usd`(默认 $1000)。
- 调度：`burst_open_scheduler.py:551-562` 的 `CronTrigger(mon-sat, 07:20, Asia/Hong_Kong)`，独立于 burst 的 slow(5-10min)/fast(60s) tier。

**CRM 写接口**（来自独立项目 `/opt/myproject/Swap_Free` 的成熟实现 + 用户实测 + 2026-06-02 实测确认）：
- 端点：`POST https://mt4.kohleglobal.com/rest/users/update`（`?version=1.0.0` 可选）。**实测确认**：`ca-rest` 是 cabinet 后台 UI（404），不是 API；API 与 Swap_Free 同走 `/rest/`。
- 鉴权：`Authorization: Bearer <token>`（专用账户，权限仅 `/users/update`，token 实测有效）。
- ⚠ **IP 白名单**：CRM API 有 IP allowlist。新账户需把**调用方出口 IP** 加白，否则 403 `{"error":"invalid_grant","error_description":"Client IP is not allowed."}`。测试机(AWS)出口 = `52.221.111.184`；**生产后端出口 IP 待确认并单独加白**。
- **tags 是「覆盖」语义**（整列 replace，不是 append）→ 必须 **read-modify-write**：先 POST `{"user": id}` 读回 `{cid, tags}`，内存追加，再整列写回。
- **tags 用字符串（名字）存储**，不是 tagid → tag 名硬编码成唯一常量。

**cid → tag 映射**（用户确认，短期只有 0/1）：
- `cid=0`（CN）→ tag 字符串 `禁止出金(風控)`（tagid 488374，仅参考）
- `cid=1`（Global）→ tag 字符串 `Withdrawal Notice`（tagid 263196，仅参考）
- **cid ∉ {0,1} → 不打 tag，记 log（留一手）**。

## 假设 / 待验证

- [ ] **客户 ID 映射**：rule 81 的 `client_userid`（= `mt4_users.userid`）== CRM 的 `user` id。**命门**，落地前用真实样本（如 100017）实测确认。
- [x] CRM 端点 = `/rest/users/update`（`ca-rest` 是 cabinet UI，已实测排除）+ Bearer 鉴权（已实测 token 有效）。
- [ ] **IP 白名单**：调用方出口 IP 需加白（测试机 52.221.111.184 + 生产后端出口 IP）。加白前无法实测写入 / ID 映射。
- [ ] POST `{"user": id}`（不带 tags）= 安全只读，返回对象含 `cid` + `tags`（用户已实测：返回了 tags）。
- [ ] cid 取 CRM 响应的 `cid` 字段（权威），不从 MT group 推。

## 验收标准

- [ ] **盘中快扫 tier**：新增一个 gap-trade 盘中检测 job（~5min），只在配置的 HKT 开盘窗口（默认约 05:55–07:05）内执行；复用 `detect_gap_trade_gap_profit`，`end_mt=now_mt`（增长窗口）。env flag 控制开关（对齐 `BURST_FAST_TIER_ENABLED` 模式）。保留 07:20 终值扫。
- [ ] **跨扫去重**：盘中多次扫到同一客户不重复 POST CRM（本地去重 + 「tag 已存在则跳过」双保险）。
- [ ] **新建 `crm_client`**（New-IT-System 后端）：read-modify-write 上 tag，按 cid 选 tag，保留原有 tag，限流 + 重试（照搬 Swap_Free 模式）。专用凭证进 env（`CRM_RISK_API_URL`/`CRM_RISK_API_TOKEN`），与现有配置隔离。
- [ ] **cid 兜底**：cid ∉ {0,1} → 跳过 + 记 log，不报错。
- [ ] **审计表**：每次上 tag（成功/跳过/失败）落本地审计记录（谁/何时/哪个客户/cid/tag/结果），可追溯。
- [ ] **邮件通知**：每轮扫描一封汇总邮件（本轮新打了谁 + 失败哪些）发 `risk@kcmtrade.com`；上 tag 失败必须告警（即使成功 0 条）。env flag 控制成功汇总开关（稳态可只留失败告警）。
- [ ] **分阶段上线**：P1 dry-run（检测 + 邮件，不真写 CRM）→ P2 live（真打 + 每轮汇总 + 失败告警）→ P3 稳态（只留失败告警）。dry-run 用 env flag 控制。
- [ ] verify.sh 红绿闸门通过（tsc + vitest + pytest）。

## 笔记

- **高危写操作**：`禁止出金` 直接 hold 客户出金，误报 = 误封正常客户 → 必须 dry-run 先行 + ID 映射实测。
- **覆盖语义竞态**：read→write 间隙若他人（CS/Swap_Free）改了同一客户 tags 会被冲掉；窗口极小，读完立刻写缓解，记录此风险。
- **字符串精确匹配**：tag 名繁/简、全半角必须与 CRM 逐字节一致（从 CRM 原样复制，不手敲），否则建出 CS 没在筛的新 tag → 风控形同虚设。
- **安全**：dev token 已在对话明文出现，上线后建议轮换。
- 触发口径用户已定：**只看已实现利润**（沿用 rule 81，不引入浮盈）。
- 复用 `email-notification` skill 基建。

## 结果

（完成后填）
