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

- [x] **客户 ID 映射**：rule 81 的 `client_userid`（= `mt4_users.userid`）== CRM 的 `user` id。**命门**——用户 2026-06-02 确认成立（100017 实测：CRM read 返回 cid=0 + email `test@kohlecapital.com`）。
- [x] CRM 端点 = `/rest/users/update`（`ca-rest` 是 cabinet UI，已实测排除）+ Bearer 鉴权（已实测 token 有效）。
- [x] **IP 白名单**：项目机出口 `218.253.255.122` 已加白，实测 `read_user(100017)` → HTTP 200。（注：会话中对话用的是 VPS 转发 IP `52.221.111.184`，未加白；**生产后端容器出口 IP 仍待 dry-run 时确认**。）
- [x] POST `{"user": id}`（不带 tags）= 安全只读，返回对象含 `cid` + `tags`（实测返回 100017 的 13 个 tags）。
- [x] cid 取 CRM 响应的 `cid` 字段（权威），不从 MT group 推。
- [ ] **生产后端容器出口 IP** 是否 == 218.253.255.122（Docker 出网路径可能不同）→ P1 dry-run 首跑时验证（连不通会全 403 + 失败告警邮件）。

## 验收标准

- [x] **盘中快扫 tier**：`_run_gap_trade_intraday_scan`（rule 81 only），HKT 开盘窗口内每 N min，`end_mt=min(now, 窗口收盘)` 增长窗口；env `GAP_TRADE_INTRADAY_ENABLED` 控制（默认 OFF，对齐 `BURST_FAST_TIER_ENABLED`）。保留 07:20 终值扫 + 末尾 tagging 兜底。
- [x] **跨扫去重**：审计表 `has_successful_crm_tag(source, dedup_key)`（终态成功/跳过不重打、失败可重试）+「tag 已存在则跳过」双保险；`alert_events` 每客户每日单写（`has_gap_profit_alert`）。
- [x] **新建 `crm_client`**：read/update（read-modify-write）、限流 + 重试（照搬 Swap_Free）。专用凭证 env `CRM_RISK_API_URL`/`CRM_RISK_API_TOKEN`，隔离。**通用化**：上 tag 编排抽到 `crm_tag_service.apply_tags`（其他 tab 可复用），gap 是薄适配器。
- [x] **cid 兜底**：cid ∉ {0,1} → resolver 返回 None → `skipped_cid` + 记 log，不报错。
- [x] **审计表**：通用 `crm_tag_log`（source/dedup_key/user_id/cid/tag/result/http_status/tags_before/after/context/attempted_at），每次尝试一行。
- [x] **邮件通知**：每轮有变化（新 tag 或失败）发汇总邮件到 `GAP_TRADE_TAG_MAIL_TO`（默认 `risk@kcmtrade.com`）；失败必告警；`GAP_TRADE_TAG_MAIL_ON_SUCCESS=false` 可只留失败告警。
- [x] **分阶段上线**：env flag 就位（`GAP_TRADE_INTRADAY_ENABLED` + `GAP_TRADE_CRM_TAG_DRY_RUN`，安全默认 OFF + dry-run）。**P1 dry-run 实测待明天有数据时执行**（见下「上线执行清单」）。
- [x] verify.sh 红绿闸门通过（backend pytest 167 + tsc + vitest；新增 19 单测）。

## 笔记

- **高危写操作**：`禁止出金` 直接 hold 客户出金，误报 = 误封正常客户 → 必须 dry-run 先行 + ID 映射实测。
- **覆盖语义竞态**：read→write 间隙若他人（CS/Swap_Free）改了同一客户 tags 会被冲掉；窗口极小，读完立刻写缓解，记录此风险。
- **字符串精确匹配**：tag 名繁/简、全半角必须与 CRM 逐字节一致（从 CRM 原样复制，不手敲），否则建出 CS 没在筛的新 tag → 风控形同虚设。
- **安全**：dev token 已在对话明文出现，上线后建议轮换。
- 触发口径用户已定：**只看已实现利润**（沿用 rule 81，不引入浮盈）。
- 复用 `email-notification` skill 基建。

---

## 检测逻辑（rule 81 — Gap Trade 超额获利客户）

服务：`backend/app/services/rule_gap_trade_gap_service.py::detect_gap_trade_gap_profit`

**窗口**：MT（UTC+3 无 DST）当日 `[window_start_hour_mt, window_end_hour_mt)` = 默认 `[00:00, 02:00)`。
- 每日 07:20 跑：`end_mt = 窗口收盘`（02:00），扫已收盘的完整窗口。
- 盘中 tier 跑：`end_mt = min(now_mt, 窗口收盘)`（**增长窗口**），所以 06:10 跑就扫 00:00–06:10... 实际被 cap 在窗口内，即 00:00–min(now,02:00)。

**SQL**（`_query_closed_trades_in_window`）：拉窗口内**已平仓**市价单
- `fxbackoffice.mt4_trades L JOIN mt4_users U ON U.loginsid = L.loginSid`
- `WHERE L.closeDate IN (窗口涉及日期) AND L.CLOSE_TIME ∈ [start,end) AND L.sid IN sid_list AND L.CMD IN (0,1) AND (isDeleted=0 OR NULL)`
- 排除 demo/test：`groupsid / NAME NOT LIKE '%demo%'/'%test%'`
- `sid_list` 默认 `[1,5,6]`（MT4_Live / MT5 / MT4_Live2）

**聚合**（Python，按 `userid` 客户级）：
- 同一客户名下**所有账户**合并；累加 `totalProfit`，**CEN 符号（`.cent`/`.kcmc`）÷100** 转 USD（`_to_usd`）。
- 记录每账户 profit（选 profit 最高的账户作 alert 的 `login`/`server`）、symbols、loginSids 集合、首开/末平时间。

**净入金**（`_query_net_deposit_by_userid`，按 `userid`）：
- `SUM(deposit) + SUM(withdrawal + ib withdrawal)`（CEN ÷100），`fxbackoffice.stats_transactions JOIN mt4_users`，`sid IN (1,2,5,6)` + 非 demo。与 client-return-rate「历史净入金」公式一致。

**触发**（双阈值任一，per client）：
- 先过滤：`total_profit > 0`、净入金非 None、净入金 ≥ `min_net_deposit_hist`（默认 100，挡极小本金假阳）
- `profit_ratio = total_profit / net_deposit ≥ profit_ratio_min`（默认 **1.0**，即 2h 翻倍）
- 或 `total_profit ≥ min_profit_usd`（默认 **$1000**）
- `triggered_by` ∈ {`ratio` / `absolute` / `both`}

**输出**：每个命中客户一条 alert（`rule_id=81`），含 `client_userid` / `total_profit_usd` / `profit_ratio` / `contributing_login_sids` / `window_date` 等。

> rule 71（SO+AB 配对）是**另一个子检测器**，只在每日 07:20 跑（依赖开仓日 IP 文件 + 是事后审计），**盘中 tier 不跑**。本 OPT 的上 tag **只针对 rule 81**。

## 实现架构 / 项目逻辑细节

**数据流（一次命中 → 上 tag）：**
```
盘中 tier (HKT 5-7 每 5min) 或 每日 07:20
  → detect_gap_trade_gap_profit(end_mt=min(now,close)) → rule-81 alerts
  → [alert_events] 每客户每交易日只写一次 (has_gap_profit_alert 判重)
  → gap_trade_tag_service.tag_gap_profit_clients(alerts, window_date, dry_run)
       └─ 构造 TagItem(user_id=client_userid,
                       dedup_key="<window_date>:<userid>",
                       context={profit_usd, ...})
       └─ crm_tag_service.apply_tags(items, source="gap_trade",
                                     tag_resolver=按cid选tag, dry_run)
            for each item:
              1. has_successful_crm_tag(source, dedup_key)? → skipped_dedup（不碰 CRM）
              2. crm_client.read_user(uid) → {cid, tags}   （读失败 → failed，可重试）
              3. tag_resolver: TAG_BY_CID[cid]；None → skipped_cid + log
              4. tag 已在 tags 里 → skipped_existing（幂等）
              5. dry_run → 记 dry_run（不写 CRM）
              6. else crm_client.update_user_tags(uid, tags+[tag])  ← read-modify-write
                 200 → tagged / 非200 → failed
              每步 append_crm_tag_log(...)（审计）
  → 有 ≥1 tagged 或 ≥1 failed → 发汇总邮件 (risk@kcmtrade.com)
```

**模块职责（三层，下层通用、上层专用）：**

| 层 | 文件 | 职责 | 复用性 |
|---|---|---|---|
| HTTP | `services/crm_client.py` | `read_user` / `update_user_tags`，Bearer + 限流 + 退避重试 + 超时；REPLACE 语义 | **通用** |
| 引擎 | `services/crm_tag_service.py` | `apply_tags(items, *, source, label, dry_run, tag_resolver)`：read-modify-write + 幂等 + `(source,dedup_key)` 去重 + 审计 + `build_tag_email` | **通用**（任何 tab 接 CRM 上 tag 走这里）|
| 适配器 | `services/gap_trade_tag_service.py` | rule-81 alert → `TagItem`（dedup_key/context）+ `TAG_BY_CID` cid resolver + `source="gap_trade"` | gap 专用 |
| 调度 | `core/burst_open_scheduler.py` | 盘中 tier `_run_gap_trade_intraday_scan` + `_locked_*` + tagging 钩子 `_run_gap_trade_tagging` + 邮件 `_maybe_send_tag_email` + 每日末尾兜底 + 注册 | — |
| DB | `core/risk_monitor_db.py` | `crm_tag_log` 表 + `has_successful_crm_tag` / `append_crm_tag_log` / `has_gap_profit_alert` | 审计表通用 |
| 配置 | `core/config.py` | `CRM_RISK_API_URL/TOKEN` + 重试/限流/超时 6 项 | — |

**关键设计点：**
- **检测函数零改动**：盘中只是用增长窗口再调一次现成的 `detect_gap_trade_gap_profit`。
- **去重不靠改检测签名，靠审计表** `(source, dedup_key)`。gap 的 dedup_key=`"<window_date>:<userid>"` → 同一客户当天多 tick 只打一次；**失败（`failed`）不算终态 → 下一 tick 重试**。
- **`alert_events` 每客户每日单写**（`has_gap_profit_alert`）：避免 5min×12 tick 灌 12 行；每日 07:20 仍权威写（含 rule 71）。
- **read-modify-write**：CRM `tags` 是整列覆盖，必须先读现有 tags 再追加，否则抹掉客户原有 tag。
- **cid 取 CRM 响应字段**（权威），不从 MT group 推。
- **tag 字符串硬编码常量** `TAG_BY_CID`（从 CRM 原样复制，繁简/全半角逐字节对）。

**测试**：`test_crm_client.py`（payload/Bearer/重试/403/缺 token）、`test_crm_tag_service.py`（任意 resolver/source 隔离去重/批内去重）、`test_gap_trade_tag_service.py`（cid 选 tag/保留原 tag/dry-run/跨扫去重/失败可重试/邮件）。共 19，verify 全绿。

## 上线执行清单（P1 → P2 → P3）

> 所有 flag 走 **生产 compose 的 backend env**（`backend/docker-compose.prod.yml` 或对应 `.env`）。改 env 后需**重启 backend** 生效（cron 在启动时注册）。`CRM_RISK_API_TOKEN` 已在 `backend/.env`；`CRM_RISK_API_URL` 留空用默认。

**前置（一次性）：**
1. 确认生产后端容器**出口公网 IP** 已加入 CRM 该 API 账户白名单（P1 首跑若全 403 即此项未过）。
2. 确认 `CRM_RISK_API_TOKEN` 在生产 backend env。

**P1 — dry-run（只检测+邮件，不写 CRM）：**
```
GAP_TRADE_INTRADAY_ENABLED=true
GAP_TRADE_CRM_TAG_DRY_RUN=true        # 默认就是 true
# 可选微调：GAP_TRADE_INTRADAY_HOURS=5-7  GAP_TRADE_INTRADAY_INTERVAL_MIN=5
```
重启 backend。验证（挑有 gap 数据的交易日）：
- 收到 `[DRY-RUN] Gap Trade 风控上 tag — <date>` 邮件，名单/cid 合理；**HTTP 列应为 200**（证明后端容器连通 CRM + IP 白名单 OK）。若 HTTP=403/−1 → 出口 IP 未加白或 token 问题。
- `crm_tag_log` 有 `result='dry_run'` 行；**CRM 上 tag 数为 0**（去 CRM 后台抽查未变）。
- 手动复跑（不等开盘）：`python -c "from app.core.burst_open_scheduler import trigger_gap_trade_scan_now as t; t()"`（扫当日已收盘窗口，dry-run 兜底 tagging 会跑）。

**P2 — live（真打 tag）：**
```
GAP_TRADE_CRM_TAG_DRY_RUN=false
```
重启 backend。验证：
- 用测试账户 100017 类比一次，确认 tag 真上去且**原有 tag 全保留**（read-modify-write）。
- 重复 tick 不重打（`crm_tag_log` 同 `(gap_trade, date:uid)` 只一条终态）。
- 失败路径告警：临时构造失败（如错 token）应收到失败告警邮件。

**P3 — 稳态（降噪，只失败告警）：**
```
GAP_TRADE_TAG_MAIL_ON_SUCCESS=false
```

**回滚：**
- 立即停止上 tag：`GAP_TRADE_CRM_TAG_DRY_RUN=true`（退回 dry-run）或 `GAP_TRADE_INTRADAY_ENABLED=false`（关盘中 tier；每日 07:20 兜底 tagging 也受 DRY_RUN 控制）。
- 误打的 tag：CS 在 CRM 后台手动移除（tag 可逆）。
- 代码级：本 OPT 全部 env 默认安全（OFF + dry-run），不重启不会自己生效。

## env flag 速查

| flag | 默认 | 作用 |
|---|---|---|
| `GAP_TRADE_INTRADAY_ENABLED` | `false` | 盘中 5min tier 开关 |
| `GAP_TRADE_INTRADAY_HOURS` | `5-7` | 盘中 tier CronTrigger `hour=`（HKT）|
| `GAP_TRADE_INTRADAY_INTERVAL_MIN` | `5` | 盘中 tier 间隔（`minute=*/N`）|
| `GAP_TRADE_CRM_TAG_DRY_RUN` | `true` | true=只检测+邮件不写 CRM；false=真打 |
| `GAP_TRADE_TAG_MAIL_TO` | `risk@kcmtrade.com` | 汇总/告警邮件收件 |
| `GAP_TRADE_TAG_MAIL_ON_SUCCESS` | `true` | false=只在有失败时发邮件 |
| `CRM_RISK_API_URL` | `…/rest/users/update` | CRM 端点 |
| `CRM_RISK_API_TOKEN` | （unset）| Bearer token（缺失→tagging 静默跳过）|
| `CRM_RISK_MAX_RETRIES`/`_RETRY_BACKOFF_SEC`/`_MAX_REQ_PER_SEC`/`_CONNECT_TIMEOUT`/`_READ_TIMEOUT` | 2/2.0/10/10/30 | 重试/限流/超时旋钮 |

## 结果

（完成后填——merge 时补 Stage 1 review 处理 + 实际交付 vs AC + P1/P2 上线日期）
