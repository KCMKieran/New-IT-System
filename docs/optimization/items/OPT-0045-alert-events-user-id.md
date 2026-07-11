---
id: OPT-0045
title: 风控V2 前置 —— alert_events 补 user_id 列 + 存量回填（归集引擎的硬前置）
status: done
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

## 结果（2026-07-11 closed）

**交付 vs AC**（commits `ab55846` 实施 + `8f0b4f7` 冷审修复）：
- AC2/3/4/5 全过：真库副本 + 真实 MySQL 实测——116,480 行 NULL 回填后剩 12（0.01%，
  4 账户 mt4_users 天然缺行）、幂等重跑输出 0；案例 uid 127582 十七账户 4,065 行归并
  同一 user_id；fail-open pytest 过；索引 PRAGMA 可见；新测试 18/18 绿。
- AC1 部分：写路径/归并形态有 pytest 锁定；「dev 真实扫一轮」留到部署重启后（避免动
  运行中共享 SQLite）。
- 全量 backend 413 passed / 41 failed——失败清单与 main 干净基线 diff 逐条一致，
  为既有日期 fixture 炸弹（fixture 硬编码 2026-05-28 超 30 天 retention 被启动清扫删），
  **= WIP 中 OPT-0041 的修复范围**，不另立单。

**Stage 1 冷审（9 findings）处理记录**：
- F1 当场修：回填脚本事务重排——SELECT 规划前置 → Phase A 即时 commit → 才连 MySQL →
  Phase B 500 行/块短事务；脚本连接补 busy_timeout=5000（原版写锁横跨 MySQL 往返最长
  ~40s，活库上会丢 tick 告警）
- F7 当场修：MySQL IN-list 500/块分块
- F4 当场修：脚本删手抄 SID_MAP，import `app.core.sql_helpers.SID_MAP` + 同源断言测试
- F5 当场修：`_alter_ignore_duplicate_column` helper——多 worker 启动 check-then-ALTER
  竞态不再打死输家 worker（5 个 ALTER 全走 helper）
- F2 并入 [[OPT-0047]]（交付内容第 6 条）：归集引擎负责把修复脚本接进 APScheduler +
  每 tick NULL 计数 log
- F3 live with：rule 71 AB 对告警 user_id 只归亏损腿（l_userid），获利腿 c_userid 不建案。
  **OPT-0047 建案前必须拍归属规则**（从 detail 表读 c_userid 双边归档 or 双行方案），
  事后重归属很痛
- F6 live with：每 tick 对 mt4_users 多一次批查（get_account_info_map 已查同批账户），
  1-10min 频率可忍，0046/0047 时可顺手把 userId 并进那条 SELECT
- F8 部分覆盖（main() 级 dry-run/apply 测试已补），其余盲区 live with
- F9 live with（get_user_id_map 的 loginsid 构造在 try 外，KeyError 会整批 NULL——
  fail-open 语义不破，粒度粗）

**Follow-up / 上线顺序**：部署重启（迁移自动跑）→ `backfill_alert_events_user_id.py
--apply` → NULL 补写接线归 OPT-0047。另：F5 同款竞态在
`_migrate_leverage_streak_miss_count` / `_migrate_mail_outbox_columns` 仍存在（范围外，
下次动那两个函数时顺手包 helper）。
