---
id: OPT-0046
title: 返佣套利检测 rule（band 121-130）+ detail 表 + 邮件告警源 —— 风控V2 Phase A 检测腿
status: ready
priority: P1
area: backend
effort: M
created: 2026-07-11
related: [[OPT-0045]] [[OPT-0047]] [[OPT-0032]] [[OPT-0043]]
---

## 背景

老板需求：B-book 收益要扣 campaign 返佣成本，存在「客户交易不赚甚至小亏，但产给上级的
rebate 超过我们从他身上赚的」→ 公司净亏。要求自动识别 **返佣+交易盈亏 > 0** 的客户。
本 OPT 是风控系统V2 Phase A 三件套（0045/0046/0047）的检测腿；口径已全部实测验证，
**全文见本地 skill `.cursor/skills/rebate-arbitrage/SKILL.md`（含可跑 SQL，勿重新调研）**。
V2 设计见 `.cursor/skills/risk-disposition/SKILL.md`。

已验证口径（skill §2，要点复写以自洽）：
- 返佣腿：`stats_ib_commissions_by_login_sid`，`fromLoginSid`=产佣客户账户（**不碰**
  1.36 亿行的 ib_processed_tickets）；交易腿：`mt4_trades` 已实现口径
  `CLOSE_TIME > '1971-01-01'`，`totalProfit` 生成列 = PROFIT+SWAPS+COMMISSION，
  窗口查询走 `closeDate`（STORED+索引）。
- 触发（30 天滚动、userId 聚合多账户）：`combined = SUM(totalProfit)+SUM(rebate)`；
  **触发 = rebate ≥ 阈值 AND combined > 0，两条件缺一不可**（只按 combined 混入纯赢钱
  客户；只按 rebate 抓到亏损大户）。触发用已实现盈亏，浮动仅展示。
- 短线占比 `ratio_Xm = SUM(lots WHERE hold<X)/SUM(lots)` 双档 <5min/<10min，
  **分层标记不做硬触发**（r10<50% 的照进名单、走人工处置——ZIP 滑点对他无效）。
- CEN ÷100（currency 权威 = mt4_users）；排除 demo（GROUP NOT LIKE '%demo%'）+ 员工
  （COALESCE(isEmployee,0)=0）。
- 实时性：CRM 佣金引擎 10min 一轮，rebate 端到端新鲜度 15~25min。查询实测：trades 30d
  全量 29.8s（只能日基线）/ 当天 0.2s；rebate 30d 全量 1.0s。**架构 = 日基线 + 10min tick
  叠加当天增量；rebate 必须每轮全量重读**（引擎 18h 滑动窗会回改昨天的行）。

## 交付内容

1. **检测 rule**：band **121-130**（常量 `REBATE_ARB_RULE_ID_BASE/MAX`），调度用现有
   `_scheduler.add_job()` 加 10min interval job（**禁止**新建 BackgroundScheduler），
   模式 = Gap Trade 日终扫 + 盘中快扫（OPT-0032）的翻版。
2. **去重**：每客户每交易日一条（30 天滚动指标过线后会连续过线，同
   `has_gap_profit_alert` 模式）。
3. **detail 表**：rule-specific 字段进 `alert_rebate_arb_detail`（主表 23 列不动）：
   rebate_30d / total_pl_30d / combined / 订单数 / 手数 / ratio_5m / ratio_10m /
   加权持仓 / 交易净入金 / ib_withdrawal / equity / 上级钱包 / 账户列表。
4. **邮件告警源**：注册进 alert-mail-center `MAIL_SOURCES`（OPT-0043 的源注册表，
   操作手册见 `.cursor/skills/alert-mail-center/SKILL.md`）。
5. 告警行带 user_id（依赖 [[OPT-0045]] 先合，本 rule 是客户级聚合，天然有 userId）。
6. **不做**：ZIP 滑点自动写 MT（V3）；独立前端 tab（观察清单 [[OPT-0047]] 是唯一 UI，
   是否需要取证明细视图在 0047 里定）。

## 验收标准（AC）

1. dev 环境跑通日基线 + 10min tick，两案例复现：1-8614411（返佣 $15,001/净亏 $381）
   进名单；纯亏损大户（rebate 高但 combined<0）不进。
2. 每客户每交易日最多一条告警（连续过线不重复报）。
3. detail 表行与 skill §3 的 CSV 实测数字同数量级对得上（temp_folder/返佣正收益客户_近30天_20260710.csv，980 客户）。
4. 邮件源注册后 test-send 可达；digest 正常渲染。
5. 10min tick 单轮耗时 < 5s（架构目标 ~1.5s）；30s 级日基线只跑每日一次。
6. 全量 risk-monitor pytest 绿。

## 开放问题（claim 前需用户拍板或执行时问）

- `min_rebate_usd` 默认阈值：参考 rebate≥$500 ∧ r10≥50% → 67 人 / rebate≥$1k → 38 人 /
  combined≥$1k → 226 账户（skill §7）。建议先取 rebate≥$500 + combined>0（分层不硬触发）。
- Phase 1 是否先做日更版（更快上线），10min tier 放后。
- IB 兼交易者（如 110386，自返佣 97%）是否单独标记（用户已拍板不做自返佣维度，
  但案卷 tag 是否区分待定）。
