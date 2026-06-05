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

> **2026-06-05 用户拍板（覆盖下列原标准中的两条）**：
> 1. **P1 dry-run 阶段豁免**，日扫 + 盘中两个 tier 同时上线。补偿控制（不可省）：写上限 `max_tags_per_scan`、双 kill-switch（env `GAP_TRADE_CRM_WRITE_ENABLED` × DB `crm_tag.write_enabled`，后者运行时可改）、上线后第一个开盘窗口必须有人全程盯邮件、下方 D0 人工闸门全部完成后才许翻 live。
> 2. **邮件改为「每次改 tag 必须通知」**：每轮 conditional digest（result ∈ {tagged, failed, skipped_cid, dry_run} 才发，每个变更恰好出现在一封邮件里），07:20 终扫无条件发心跳。收件人 = `it.th@kcmtrade.com` + `tobe.wong@kohleservices.com` + `cs@kcmtrade.com`（env `CRM_RISK_MAIL_TO`），替代原 `risk@kcmtrade.com` 每轮汇总。SMTP 失败不阻塞/不回滚写，`notified_at` outbox 下轮重发。

- [x] **盘中快扫 tier**：`gap_trade_intraday_scan` job（5min，CronTrigger 限定 HKT 5-7 点 + 函数内 05:55–07:05 双层窗口闸），复用 `detect_gap_trade_gap_profit`，`end_mt = min(now_mt, window_end)`（增长窗口、不越过 MT 02:00）。env `GAP_TRADE_INTRADAY_ENABLED`（默认 off，dev 容器安全）。保留 07:20 终值扫（改为阻塞抢锁 300s，不再静默跳过；带当日 reconciliation diff：盘中已 tag 但终扫未确认的客户在邮件里高亮）。
- [x] **跨扫去重**：审计表 `(window_date, client_userid)` 终态记录为唯一事实源（人工摘 tag ≠ 同意重打）+「tag 已存在则跳过」兜底；盘中与 07:20 共用同一去重。
- [x] **新建 crm_client**（`services/crm_risk_tag_client.py`）：read-modify-write、写后 read-back 校验（旧 tags ⊆ 新 tags）、429/5xx 退避重试、非 200 响应体全留痕、解析不出可回写形态→跳过该客户（绝不写回半解析的 tags 列表）、`remove_tag()` 回滚路径 day one 就有。凭证 `CRM_RISK_API_URL`/`CRM_RISK_API_TOKEN` 进 env + Settings（带 `.strip()`）。
- [x] **cid 兜底**：cid ∉ {0,1} → 跳过 + 审计 + **进 digest 邮件**（新区域出现要人眼看到，不是埋在 log 里）。
- [x] **审计表**：`gap_trade_crm_tag_log`（schema 迁移进 `risk_monitor_db.py`），含 `tags_before/after` JSON 快照（full-replace 语义下唯一恢复依据）、`notified_at` outbox 列；无保留期清理（金融审计轨迹永久保留）。
- [x] **邮件通知**：按上方拍板执行；连续 3 次失败中止本轮并告警；超 cap 整轮不写 + `[GAP-TAG FAILED]` 告警。
- [~] **分阶段上线**：dry-run 豁免（见拍板）；log-only 模式保留为 `write_enabled=false` 的行为（检测 + 审计 dry_run 行 + 邮件，不碰 CRM），可随时回退。
- [ ] verify.sh 红绿闸门通过（tsc + vitest + pytest）。

## D0 人工闸门（翻 live 前全部完成，工具：`backend/scripts/crm_tag_probe.py`）

- [ ] **生产出口 IP 加白**：prod 容器内 `python scripts/crm_tag_probe.py --egress-ip` → CRM 管理员加白。
- [ ] **Token 轮换**：旧 token 已在对话明文泄露，翻 live 前作废换新（顺带验证新 token + 白名单：从 prod 跑一次 probe）。
- [ ] **ID 映射实测**：`--probe-historical --verify-db`（23 个历史命中 + 100017），任一 mismatch = 硬停。
- [ ] **阈值复盘会**：与 risk + CS 走查 23 个历史命中客户「冻他对不对」；如有明显 FP，先调 `gap_profit` 阈值或要求 `triggered_by='both'`。
- [ ] **Canary**：cid 0/1 各选一个带 ≥2 已有 tag 的内部账户，`--canary-add` → CRM UI 验证 tag 逐字节一致 + 原 tag 保留 + **CS 确认出金转人工** → `--canary-remove` 演练回滚。
- [ ] **测试邮件**：向三个收件人发一封测试 digest，三方确认收到。
- [ ] 以上全绿 → `.env` 置 `GAP_TRADE_INTRADAY_ENABLED=true` + `GAP_TRADE_CRM_WRITE_ENABLED=true`，DB config `crm_tag.write_enabled=true`，`./deploy.sh`；第一个开盘窗口（HKT 05:55–07:20）有人全程盯邮件，kill-switch 随手可按（DB flag 秒级生效，无需重启）。

## 笔记

- **高危写操作**：`禁止出金` 直接 hold 客户出金，误报 = 误封正常客户 → 必须 dry-run 先行 + ID 映射实测。
- **覆盖语义竞态**：read→write 间隙若他人（CS/Swap_Free）改了同一客户 tags 会被冲掉；窗口极小，读完立刻写缓解，记录此风险。
- **字符串精确匹配**：tag 名繁/简、全半角必须与 CRM 逐字节一致（从 CRM 原样复制，不手敲），否则建出 CS 没在筛的新 tag → 风控形同虚设。
- **安全**：dev token 已在对话明文出现，上线后建议轮换。
- 触发口径用户已定：**只看已实现利润**（沿用 rule 81，不引入浮盈）。
- 复用 `email-notification` skill 基建。

## 结果

（完成后填）
