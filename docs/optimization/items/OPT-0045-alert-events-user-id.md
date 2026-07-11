---
id: OPT-0045
title: 风控V2 前置 —— alert_events 补 user_id 列 + 存量回填（归集引擎的硬前置）
status: ready
priority: P1
area: db
effort: S
created: 2026-07-11
related: [[OPT-0046]] [[OPT-0047]]
---

## 背景

风控系统V2（检测器 → 归集引擎 → 观察清单，设计全文见本地 skill
`.cursor/skills/risk-disposition/SKILL.md`）要按 **userId（clientId）归并信号**。
2026-07 独立评审实测：7 个检测规则族中 6 个的告警行不带 userId——同一客户多账户
（loginSid = `{SID}-{LOGIN}`）的告警散成独立行（案例 uid 127582：17 个账户合刷 $52.5k）。
不补此列，归集引擎无米下锅。本 OPT 是 Phase A 三件套（0045/0046/0047）的第一件，
**无外部依赖，可立即做**。

2026-07-11 subagent 已完成改动点分析（勿重查）：

- **统一 choke point**：`append_scan_and_events`（`backend/app/core/risk_monitor_db.py:1771`）
  是唯一插入函数，全 app 仅 2 个调用点——`backend/app/core/burst_open_scheduler.py:474`
  （burst/quick_oc/quick_profit/hedge/leverage/martingale 六族共用）和 `:698`
  （gap SO 71 + gap profit 81）。**不必逐规则改 6 个 service**。
- gap 两族的 userId 已在 detail 表（`l_userid`/`c_userid`、`client_userid`，
  `risk_monitor_db.py:284,294,317`）。
- alert_events DDL 在 `risk_monitor_db.py:238-262`（22 列），现有列迁移模式
  `_migrate_alert_events_columns`（`:829-850`，PRAGMA-check + ADD COLUMN）。
- enrichment 侧 `backend/app/services/account_enrichment.py`：`get_account_info_map`（`:27`）
  查 mt4_users 但未 SELECT userId，且只被 4/6 族调用——所以方案是**新增独立函数**统一回填。
- userId 权威来源 = MySQL `fxbackoffice.mt4_users.userId`（按 loginSid 映射，
  SID_MAP 见 `backend/app/core/sql_helpers.py:20`）。

## 交付内容

1. **schema**：`_migrate_alert_events_columns` 加 `ALTER TABLE alert_events ADD COLUMN
   user_id INTEGER`（nullable）；`_COMMON_INSERT_COLS`（`risk_monitor_db.py:2187`）与
   append 取值（`:1810-1833`）带上新列；新索引
   `idx_alert_events_user_scanned ON alert_events(user_id, scanned_at DESC)`。
2. **enrichment**：`account_enrichment.py` 新增 `get_user_id_map(conn, alerts) ->
   {loginsid: userId}`（批量查 mt4_users）；`burst_open_scheduler.py` 两个 append 调用点
   前统一回填 `alert["user_id"]`。**fail-open**：查不到/MySQL 故障时写 NULL 照常落库
   （先例 `account_enrichment.py:92-97`），绝不因 enrichment 失败阻断告警。
3. **回填脚本**：仿 `backend/scripts/backfill_alert_events_currency.py`（dry-run 默认 /
   `--apply` / 幂等 `WHERE user_id IS NULL`）。30 天存量 ≈ 8 万行、distinct 账户数千：
   SELECT DISTINCT server,login WHERE user_id IS NULL → 查 mt4_users → UPDATE；
   rule 71/81 行直接从 detail 表 l_userid/client_userid 回填，免查 MySQL。
4. NULL 补写：夜间或下轮扫描对 user_id IS NULL 的近期行重试一次（轻量，可并进回填脚本
   的定时调用，形态执行时定）。

## 验收标准（AC）

1. 新告警行 user_id 正确填充（dev 环境跑一轮扫描验证，多账户客户的多行 user_id 相同）。
2. 回填脚本 dry-run 输出计划行数、`--apply` 后 30 天窗口内 user_id NULL 率 < 1%
   （允许 mt4_users 缺行的自然残留）。
3. mock enrichment 失败时告警照常落库（user_id=NULL），扫描不中断、无异常泄漏。
4. 索引存在；`PRAGMA index_list(alert_events)` 可见。
5. 现有 risk-monitor pytest 全绿，零新增失败。

## 开放问题

- 无（方案已定，纯执行，~0.5-1 天）。
