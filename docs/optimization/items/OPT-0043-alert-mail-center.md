---
id: OPT-0043
title: 风控告警邮件中心 v2 —— Risk Control 下新页面：订阅规则 / 发送记录 / 全局设置，源注册表泛化
status: wip
priority: P1
area: mixed
effort: L
created: 2026-07-08
related: [[OPT-0042]] [[OPT-0032]] [[OPT-0035]]
---

## 问题 / 背景

[[OPT-0042]]（v1 灭火版）把"对冲刷佣命中即发邮件"的 dispatcher 内核落地了（`mail_subscriptions` / `mail_outbox` / `mail_dispatch_cursor` 三表 + digest/冷却/重试），但订阅是 seed 进 SQLite 的，改阈值/收件人要 IT 动手。长期形态：**risk team 在页面上自助配置**——哪个检测模块、什么条件、发给谁、实时还是汇总，全程无 IT 参与、无发版。

架构定位：**检测层与通知层解耦**。所有检测模块的产出已汇在同一张 `alert_events`（rule_id 分段），邮件中心只是这张表的消费者；给新模块加邮件能力 = 在**源注册表**登记一条，检测代码零改动。

口径铁律（写进 feature doc）：**检测规则决定"页面上有什么"，订阅条件决定"邮箱里有什么"**，后者永远是前者的子集。

## 依赖

- 基于 [[OPT-0042]] 的 branch（`opt/hedge-mail-alert`）继续开发——本 OPT branch `opt/alert-mail-center` 从它切出。三表 schema 已按本 OPT 需求设计，**不迁移**；dispatcher 从"遍历 seed 订阅"泛化为"遍历所有 enabled 订阅 + 按注册表求值"。

## 交付内容

### 后端

1. **源注册表** `backend/app/services/alert_mail/registry.py`（代码级 dict，扩展点）：
   ```python
   MAIL_SOURCES = {
     "hedge_open": {
       "label": "对冲刷单 Hedge Open",
       "rule_id_range": (91, 100),
       "rules_loader": ...,            # 供 UI 下拉列出该模块检测规则（load_hedge_open_config）
       "filterable_fields": {
         # field -> (type, zh label); 通用字段来自 alert_events，模块字段经 detail_loader
         "matched_lots_std": ("float", "匹配手数(标准手等值,cent已折算)"),
         "orders_per_side":  ("int",   "单边笔数"),
         "total_lots":       ("float", "总手数"),
         "equity":           ("float", "当前净值"),
         "net_deposit_hist": ("float", "历史净入金"),
       },
       "detail_loader": ...,           # JOIN alert_hedge_open_detail
       "template_builder": ...,        # v1 的模板函数
     },
   }
   ```
   v2 只注册 `hedge_open` 一个源，但结构为全部 6 个 risk-monitor tab + fund-flow 预留。
2. **API**（新 route 文件 `backend/app/api/v1/routes/alert_mail.py` + `backend/app/schemas/alert_mail.py`，注册进 v1 routers；响应走项目统一 shape `{data, total, page, page_size, total_pages, statistics}`）：
   - `GET /api/v1/alert-mail/sources` — 注册表：模块列表 + 各模块可过滤字段(类型/label) + 该模块检测规则列表
   - `GET/POST/PUT/DELETE /api/v1/alert-mail/subscriptions` — 订阅 CRUD（Pydantic 校验：mode ∈ realtime/digest、cooldown_min 0-1440、邮箱格式）
   - `POST /api/v1/alert-mail/subscriptions/{id}/test-send` — 取最近一条匹配的真实告警渲染发送（无匹配则样例数据），收件人=请求 body 指定（默认订阅的 mail_to）
   - `GET /api/v1/alert-mail/outbox?page=&page_size=&module=&status=&start=&end=` — 发送记录分页
   - `POST /api/v1/alert-mail/outbox/{id}/resend` — 手动重发
   - 订阅写操作记 `updated_by`（复用 view-profiles 的 device-id 头，OPT-0035 机制），`updated_at` 自动。
3. **dispatcher 泛化**（改 v1 的 `alert_mail_dispatcher.py`）：遍历 enabled 订阅 → 按 module 从注册表取 rule_id_range/字段求值器 → 条件 JSON（`{"logic":"or","conditions":[{"field":"matched_lots_std","op":">=","value":10}]}`，op ∈ >=,<=,>,<,==）→ 其余（digest/冷却/outbox/重试）复用 v1。条件留空 = 该模块全部告警都发。digest mode 订阅由每日 HH:mm 打包（挂现有 scheduler，HKT）。

### 前端

4. **新页面** `frontend/src/pages/RiskAlertMailCenter.tsx`，路由 `/risk-alert-mail`，侧边栏 Risk Control 组下（走 add-sidebar-page skill：`lazyWithRetry` + `LazyErrorBoundary`，i18n en-US/zh-CN 双语 key）。三个 tab：
   - **Tab 1 订阅规则**（落地 tab）：shadcn Table（量少不用 AG-Grid）——名称/告警源/触发条件摘要/收件人/策略/enabled Switch(行内直接切)/最近触发/7天发送数；行尾 编辑/试发/删除。**创建/编辑用 Sheet 抽屉**四段式：①告警源(模块下拉→该模块检测规则多选或"全部") ②触发条件(动态渲染 字段+运算符+值 行，字段列表来自 sources API，支持 AND/OR，可留空) ③收件人(to/cc) ④发送策略(realtime+冷却分钟 / digest+每日时间)。抽屉底部 **试发**（发给当前输入的 to，成功 toast）+ 保存。
   - **Tab 2 发送记录**：AG-Grid（⚠ 走 `useGridColumnPersist` + `<ColumnVisibilityMenu>`，计算列显式 `colId`；grid key 命名匹配 `^[A-Z0-9_]+_GRID_STATE_V\d+$` 并注册进 `GRID_STORAGE_KEYS`；工具栏过滤器走 `useFilterPersist`，key 手列进 `FILTER_STATE_KEYS`）。列：时间(HK)/订阅/模块/包含告警数/收件人/状态(sent 绿·failed 红·pending 灰，不只靠颜色，带文字)/操作(正文预览 Dialog + 重发)。顶部过滤：时间 preset/模块/状态。
   - **Tab 3 全局设置**（弱层级，一张卡片）：SMTP 连通状态(只读+测试按钮)/说明文案。v2 不做全局限流与收件人组（记 follow-up）。
   - 所有 fetch 用 `apiFetch()`；useEffect 数据拉取**必须 AbortController**；空状态引导文案（"从一个告警源开始，命中即发邮件"）。
   - AG-Grid zebra 不用 `hsl(var(--primary))`（ui-pitfalls）。

### 文档

5. `docs/features/alert-mail-center.md`：架构图（检测层/通知层）、口径铁律、源注册表扩展指南（"给新模块加邮件的 3 步"）、条件 JSON 语法。documentation-sync 规则要求的 docs 更新一并做。

## 涉及文件锚点

- v1 交付的三表 + dispatcher：见 [[OPT-0042]] §交付内容（同一 branch 链上，代码已在工作区）。
- 侧边栏/路由/i18n：`frontend/src/components/app-sidebar.tsx`、`frontend/src/App.tsx`、`frontend/src/i18n/locales/en-US.ts` + `zh-CN.ts`（add-sidebar-page skill 有完整步骤）。
- grid/filter 持久化规范：`docs/features/grid-column-persist.md`（§13 过滤器）；view-profiles manifest：`frontend/src/lib/view-profiles/manifest.ts`（新 key 若纳入档案需手列，不纳入也要符合命名 regex 否则后端 422——本页面 key **暂不进** PROFILE_MANIFEST，记 follow-up）。
- 路由注册：`backend/app/api/v1/routers.py`。

## 验收标准（AC）

1. 页面在 dev（`http://10.6.20.138:5173/risk-alert-mail`）可用：能看到 v1 seed 的"批量对冲刷佣"订阅；能新建/编辑/开关/删除订阅；条件表单字段来自 sources API 动态渲染。
2. Tab 1 试发按钮 → **真实邮件到达 kieran.xiang@kohleservices.com**（测试期收件人一律 Kieran）。
3. Tab 2 能看到 v1 已发的 outbox 记录，预览正文、手动重发可用；列显隐/过滤器持久化生效（刷新不丢）。
4. dispatcher 泛化后 v1 的 seed 订阅行为不回归（同样的 A∪B 命中集合）。
5. `./verify.sh` 三闸门（tsc + vitest + pytest）全绿（对照 main 现状，不引入新失败）；前端新增至少 1 个条件求值/表单序列化的 vitest。
6. `docs/features/alert-mail-center.md` 落地。

## Follow-up（不在本 OPT 范围）

- 其余 5 个 risk-monitor tab + fund-flow 注册进 MAIL_SOURCES（每模块一条注册即可）。
- 全局限流保险丝、收件人组、按角色权限（平台级问题）。
- 本页面 grid/filter key 纳入 view-profiles PROFILE_MANIFEST。
- Webhook 渠道（Slack/Telegram）——outbox 已留 status/error 通用结构。
