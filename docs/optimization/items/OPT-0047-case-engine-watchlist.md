---
id: OPT-0047
title: 归集引擎最小版（云 PostgreSQL 案卷 4 表）+ 观察清单页 —— 风控V2 Phase A 核心
status: ready
priority: P1
area: mixed
effort: L
created: 2026-07-11
related: [[OPT-0045]] [[OPT-0046]]
---

## 背景

风控系统V2 的核心新建层（完整设计 + 全部拍板见本地 skill
`.cursor/skills/risk-disposition/SKILL.md`；可视化 `references/v2-overview.html`）。
三层架构：检测器 → **归集引擎（Case Engine）** → **观察清单（单页）**。
本 OPT 交付后两层。**依赖 [[OPT-0045]]（alert_events.user_id）与 [[OPT-0046]]
（返佣套利检测源）先合**；Phase A 单源（返佣套利）跑通，其余 7 源接入是 Phase B。

2026-07-11 已拍板的关键决策（勿重新讨论）：
- 案卷 4 表**建云 PostgreSQL**（PITR 合规备份 + 零迁移成本窗口），检测层留 SQLite 不迁。
- 无综合评分 score；观察清单**默认按 PL+Rebate (30d) 降序**，全列可排序。
- 单页 + 状态筛选，**不做**三 tab；**不做**系统内动作执行（V3）——处置线下执行，
  系统内一次点击标记（状态+动作+日期）。
- 状态机：观察中 →（人工标记）→ 已处置 →（到期）→ 解除/归档；观察中 →（误报，须备注）→
  豁免 whitelisted。已处置行带 `review_after` 到期回看（角标/置顶，不独立 tab）。
- 实体三层级联：userId（案卷）→ (server,login)（账户画像）→ (server,login,family)
  （处置挂载）。
- alert_events 30 天 purge ⇒ 案卷层必须**冷凝存储**信号摘要（计数/首见/最近/指标快照），
  不能只存外键。
- rule 81 / AI comment / IP国家取数不在本 OPT（ai_comment、ip_country 只留字段）。

## 交付内容

1. **云 PG 案卷 4 表**（DDL 草案见 risk-disposition skill §10，按 PG 方言落地
   SERIAL/timestamptz/JSONB）：`risk_cases`（user_id 主键 / state / tags / action /
   review_after / ai_comment / ip_country 预留）、`case_entities`（三层级联）、
   `case_metrics_daily`（每日指标快照，∆ 列的数据源）、`case_actions`（append-only 处置史）。
2. **case upsert**：`append_scan_and_events` 后加一步——返佣套利信号（rule 121-130）按
   user_id upsert 进 risk_cases（tag、signal_count、first/last_signal_at）。PG 不可达时
   fail-open：告警照常落 SQLite，case 补写进重试队列/下轮补。
3. **日基线任务**：对在册客户算长窗指标（90d/all 订单数手数、加权持仓、净入金拆两列、
   Total Profit、总反佣、top2 symbol），落 case_metrics_daily 一行/客户/日。
   30d 全量聚合实测 29.8s——只对在册客户（千级）算，不进 10min tick。
4. **观察清单页**（新 sidebar 页，走 add-sidebar-page 流程 + i18n）：
   - 列定义**逐列已拍板**，见 risk-disposition skill §4 表（Userid/Country/账户列表/
     订单数/手数/加权持仓 Now·∆1·∆30/短线占比 <5m·<10m/净入金拆两列/Total Profit/
     总反佣/主要产品 top2/IP国家占位 + 状态 + 处置动作/日期）。
   - 展开子列 = AG-Grid column group（`columnGroupShow:'open'`，默认折叠显 30d 主列），
     叶子列显式稳定 colId；useGridColumnPersist + ColumnVisibilityMenu + useFilterPersist +
     InfoHeader + view-profiles key 注册，全套约定照走（grid-column-persist.md）。
   - 状态筛选（观察中/已处置/豁免/已归档）+ 行内标记处置（动作枚举 + 日期，写 case_actions）。
   - 行点开 = 案卷 Sheet：信号摘要时间线、分账户明细、处置历史。
5. **∆1/∆30 列**：从 case_metrics_daily 取 T-1 / T-30 快照差值；快照不足 30 天时显示 "—"。
6. **user_id NULL 修复接线**（OPT-0045 冷审 F2 并入，2026-07-11 用户拍板）：归集引擎是
   user_id 的唯一消费方，本 OPT 负责把 `backend/scripts/backfill_alert_events_user_id.py`
   的修复逻辑接进 APScheduler（每日一次即可）+ 每 tick 记 user_id NULL 计数 log——
   否则 MySQL 故障期产生的 NULL 行终老 30 天，GROUP BY user_id 时被静默丢弃
   （故障期的告警恰恰最不该丢）。

## 验收标准（AC）

1. OPT-0046 的信号进来后，多账户客户在观察清单**一人一行**（案例 127582 十七账户归并）。
2. 列与 skill §4 定义一致；展开/折叠、列持久化、过滤器持久化全部工作。
3. 标记「已处置」后：状态变更 + case_actions 落行 + 行的指标窗口锚点切到处置日期语义
   （最低限度：∆ 列继续可用，处置日期可见）；豁免后客户不再回观察中（除非新信号 + 人工解除豁免）。
4. PG 断连时：检测扫描不受影响（告警照常落 SQLite），恢复后 case 补写成功。
5. 日基线任务幂等（重跑同日不重复行），快照连续 2 天后 ∆1 出数。
6. tsc + vitest + 相关 pytest 全绿；verify.sh 过闸。

## 开放问题（claim 前需解决）

- **云 PG 实例开通是硬前置**（选型/网络/凭据管理走 root .env 模式）——claim 前用户先备好
  连接串；Python 侧驱动建议 psycopg（新增依赖需进 requirements.txt）。
- 处置动作枚举值定稿（已ZIP/已调返佣/已转dealing/警告/其他？）。
- 观察清单入册线 = OPT-0046 的触发阈值，还是更宽（触发进清单，更宽口径仅供检索）？
- 案卷 Sheet 里是否需要跳回取证的明细视图（0046 未做独立 tab，可能需要一个轻量
  per-alert 明细弹层）。
