---
id: OPT-0047
title: 归集引擎最小版（云 PostgreSQL 案卷 4 表）+ 观察清单页 —— 风控V2 Phase A 核心
status: done
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

## 开放问题 → 拍板结果（2026-07-12，用户离开前确认）

- ~~云 PG 前置~~ → **已完成 2026-07-12**：`risk_cases` 库 + `risk_app` 最小权限账号建好并
  冒烟验证（连接/建表/增删 OK）；`backend/.env` 已有 `RISK_CASES_PG_DBNAME/USER/PASSWORD`，
  host/port 复用 `POSTGRES_*`。root `.env` 尚未同步（上 prod 前补）。
- **处置动作/行内标记功能：V2 不做（范围变更，用户原话「先不需要这个部分的动作，不需要
  开发这个功能，先展示出来，带有 filter 的功能，由人工先观察，后续我们讨论再决定」）**。
  即：观察清单 = 纯展示 + 全列筛选/排序；无标记下拉框、无状态变更 UI、无 case_actions
  写入口。DDL 中 state / action / review_after / case_actions 表**照建作预留**（schema
  已定稿，避免后续迁移），只是 V2 UI 不提供写入口。动作枚举值随后续讨论定。
- 案卷 Sheet per-alert 取证明细弹层：**不做**——时间线行内展示关键 detail 字段即可。
- 观察清单入册线：**未拍板**，实施用保守默认 = OPT-0046 触发即入册（与 rule 同口径），
  更宽口径留讨论。

### 范围变更对交付内容/AC 的影响（2026-07-12）

- 交付 4 中「行内标记处置（动作枚举 + 日期，写 case_actions）」**移出本 OPT**；
  状态列/状态筛选保留（V2 全部行恒为「观察中」，筛选器为 Phase B/V3 预铺）。
- AC3 改为：**DDL 四表建成且 case_actions/state 写路径预留可用**（后端单测覆盖 upsert
  即可），UI 不验标记流。
- 其余 AC 不变。今晚（无人值守）为契约先行分段交付：DDL/Pydantic/fixture 端点 →
  前端页 → case upsert/日基线，做到哪个完整节点算哪个，**不 merge 半成品**。

## 结果（2026-07-13 closed）

**交付**：5 commits（`39cc2e5` PG 层 → `27084f0` 契约+fixture → `90dce7b` case upsert →
`399ba2c` 日基线+NULL修复 → `23a6599` 前端页），隔离 worktree 实施（主树被 Claude AI WIP
占用）。PG 真库四表+11 索引已建，fixture 12 案卷已 seed（`seed_risk_cases_fixture.py
--remove` 可清）。范围变更按 2026-07-12 拍板执行：纯展示+filter，无处置标记 UI。

**AC**：1 ✅(fixture 17 账户归并 e2e；真 127582 待管道) 2 ✅ 3 ✅(DDL 预留+upsert 单测)
4 ✅(断连重试集成测试) 5 ✅(同日重跑幂等；∆1 待连续 2 天快照) 6 ✅(50 新测/480 passed 基线
一致/tsc/vitest 115/vite build)。冒烟：8011 真 PG 契约全过；唯一偏差 = bad sort_by 静默
回落 combined_30d（路由文档化行为，非 500/注入面，保留还是改 422 待定）。

**Stage 1 冷审**（workflow 21 agents，11 confirmed 全 yellow / 4 refuted）：全部打包
[[OPT-0048]] hardening（头号 = case sync at-least-once 幂等化，Phase B 前必做前 3）。

**Follow-up**：∆1/真信号归并两 AC 部署后补验；root .env 补 RISK_CASES_PG_*；
bad-param 422 化拍板；merge 后用户恢复 Claude AI WIP 需解 stash 冲突。
