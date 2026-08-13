---
id: OPT-0029
title: Risk-monitor 视图模板服务端化（团队共享 view presets + 轻量自报身份）
status: dropped
superseded_by: [[OPT-0035]]
priority: P1
area: mixed
effort: M
created: 2026-05-27
related: [[OPT-0002]], [[OPT-0003]], [[OPT-0015]], [[OPT-0025]]
---

## 问题

老板要让 risk team **用统一的显示设置**看 [risk-monitor](http://10.6.20.138:5173/risk-monitor)
（显示哪些列、什么 filter、怎么排序），并希望能看到 / 复用别人调好的设置。

当前所有「自定义设置」存在 **localStorage**（[[OPT-0015]] 列状态 + [[OPT-0025]] 过滤器，共 17 个 key）。
localStorage 按「浏览器 + 机器」沙箱隔离，**数据永不出本机** → 无法共享、无法跨机、老板零可见性。
这不是「缓存做得好不好」的问题，是**存储层选错了**：「团队共用」的数据天生该在服务端。
`docs/features/grid-column-persist.md` 已白纸黑字写明「per browser, not synced server-side, **by design**，cross-device sync 是未来 OPT」——本 OPT 就是那个未来 OPT。

**核心判断**：满足这个需求**不需要造完整 login/认证系统**。它只需要 ①服务端存设置 ②一个「谁」的标签。
身份用项目**已有的「自报 email」轻量模式**（见背景），不碰密码 / 会话 / RBAC——那些是更大的另一件事（如真出现问责/合规需求再单独立 OPT）。

## 背景

### 要同步的状态（已经被 17 个 localStorage key 完整定义好了）

| 类别 | key 数 | 内容 | 进 payload? |
|---|---|---|---|
| 列状态 `RISK_MONITOR_*_GRID_STATE_V1` | 9 | 列显隐 / 顺序 / 宽度 / pin / **排序**（AG-Grid `ColumnState[]`） | ✅ |
| 过滤器 `RISK_MONITOR_*_FILTERS_V1` | 5 | time preset / rule / server / sharedIpOnly（envelope `{v,value}`） | ✅ |
| UI `RISK_MONITOR_ACTIVE_TAB` / `*_AGGREGATED` | 3 | active tab、聚合视图开关 | 🤔 待定（偏个人，可能不进模板） |

→ 一个模板的 `payload_json` = 上面 14 个偏好 key 的快照（UI 那 3 个待定）。
**故意不进模板**：`loginInput` / `zipcodeInput` / 绝对 `customRange`——这些是「调查上下文」（在追谁），不是「偏好」（怎么看），
[[OPT-0025]] 已经把它们排除在持久化外，服务端化时**继续排除**（也避免把调查上下文同步上云，隐私）。

### 已有的「轻量身份」先例（直接复用，不必造认证）

- IB Financial 审计：`backend/app/api/v1/routes/ib_financial.py` 把前端传来的 `req.email` 当 `operator` 写进 `audit_log`。
- 即「自报 email / 名字」——未经校验，但内网互信场景够用，且**项目已经在这么做**。
- 本 OPT 沿用：进页面时让用户选 / 填一次自己的名字（存本地），之后请求自动带上当 `owner_label`。
- ⚠ ~~现有 `login-form.tsx` / `auth-provider.tsx` 是**占位 demo**~~ —— **已于 auth P3（2026-08-13）换成真登录**（Entra ID OIDC + 服务端会话），后端可从 `request.state.user` 拿到可信身份。本条当时的告诫已作废。

### 落点候选

- 后端分层：routes → schemas → services → core，新表走 `core/*_db.py` 的 SQLite 模式（已有 6 个 .db）**或** PostgreSQL `reporting_db`（rw、持久）。
- 见「假设 / 待验证」——表放哪里需要拍板（牵连 [[OPT-0003]] risk-monitor SQLite 性能）。

## 验收标准

### Backend

- [ ] 新表 `view_presets`：`id, page, tab, name, owner_label, scope(private|shared|official), payload_json, created_at, updated_at`
- [ ] CRUD 端点：`GET /api/v1/view-presets?page=&tab=`（列私有+共享+official）/ `POST` / `PUT /{id}` / `DELETE /{id}`
- [ ] `scope=official` 只能由白名单 owner（lead/老板）设置——复用 IB Financial 的 `admin_whitelist` 模式
- [ ] Pydantic schema 校验 `payload_json` 的 key 在已知 17 个白名单内（拒绝任意写入）

### Frontend (`RiskMonitor.tsx`)

- [ ] 每个 tab 工具栏加「💾 保存为模板 / 📋 加载模板 / 🌐 共享」入口（复用 shadcn + `ColumnVisibilityMenu` 旁边）
- [ ] 一次性身份录入（选/填名字，存本地），后续 `apiFetch` 带上
- [ ] 「加载模板」= 把 payload 覆盖到当前 grid + filter state（复用 `useGridColumnPersist.setColumnState` / `useFilterPersist` 的 set）
- [ ] **不改** `useGridColumnPersist` / `useFilterPersist` 的本地逻辑——localStorage 仍是快速缓存 + 离线兜底；模板只是「来源之一」
- [ ] lead 设的 `official` 模板在加载列表里高亮 / 置顶（这才是「全队用相同设置」的落地）

### Tests

- [ ] 后端：preset CRUD + payload 白名单校验 + official 权限校验
- [ ] 前端：加载模板后 grid columnState / filter state 被正确覆盖；身份缺失时的兜底

### 文档

- [ ] `docs/features/grid-column-persist.md`：把「per browser by design」那段更新为「localStorage = 本地缓存，view_presets = 跨机/共享层」
- [ ] `.cursor/skills/risk-monitor/SKILL.md` File Map 加 view_presets 端点

## 假设 / 待验证

- [ ] **表放哪**：SQLite（跟现有 6 个 .db 一致、零新依赖、但牵连 [[OPT-0003]] 增长性能）vs PostgreSQL（rw/持久/易跨部署）。preset 数据量极小（每人每 tab 几条），SQLite 大概率够——claim 时定
- [ ] **产品形态**：确认走「显式命名模板 + lead 设 official」，而非「每人设置静默同步 + 老板逐个查看」。前者命中「团队共用」意图、避免同步调查上下文、写入更少；后者偏监控、隐私敏感。**强烈倾向前者**——claim 时跟老板确认他要的是「共享标准」还是「逐人审视」
- [ ] **UI 那 3 个 key**（active tab / 聚合开关）进不进模板？倾向不进（偏个人导航习惯，不算「显示标准」）
- [ ] **身份够不够**：自报名字无校验，能否区分重名 / 防冒充？内网互信下应可接受；若老板要「可信归属」则升级为单独的认证 OPT（本 OPT 不做）
- [ ] **迁移**：已有用户 localStorage 里的设置要不要一键「另存为我的第一个模板」？还是只对新动作生效？

## 笔记

- 这条 OPT 的边界是**刻意收窄**的：只做「服务端共享视图模板 + 自报身份」，**明确不含**真实认证（密码/SSO）、RBAC、行为/审计日志。后三者是更大的超集，由「问责 / 合规」驱动，不由「共享 view 设置」驱动——在纯内网互信小团队里为这个需求上整套认证是杀鸡用牛刀。
- 复用资产清单：身份=IB Financial `req.email` 模式；白名单=IB Financial `admin_whitelist`；本地缓存层=[[OPT-0015]]/[[OPT-0025]] 原样保留；新表=core `*_db.py` SQLite 模式。新增的只有一张表 + 4 个端点 + 一层「模板覆盖本地」的前端 glue → effort M。

### 反观点

- **「直接做完整 login 不是一劳永逸？」**：半成品 auth 比没有更糟（现有占位登录就是这个坑）；真要做认证应走公司 SSO/Azure AD（你们 DB 都在 Azure），别自建密码表自背安全锅。那是另一个 XL OPT，不该塞进这个 P1。
- **「localStorage 不要了，全走服务端？」**：不行。localStorage 留着当快速缓存 + 离线/网络失败兜底，体验最稳；服务端只做「共享/跨机」这一层。两层各司其职。
- **「official 模板会不会变成强推、压制个人偏好？」**：official 只是「置顶推荐的默认」，用户仍可加载后自由微调（微调落本地 localStorage，不回写 official）。共享 ≠ 强制。

## 结果

**被 [[OPT-0035]] 取代（2026-06-04，未实施）。** 用户拍板走本 OPT §假设 里**倾向反对**的那条产品形态——
「每人设置 per-身份静默自动同步 + 排他认领 + 临时观摩」，而非本 OPT 的「显式命名模板 + lead official +
自报 email」；且范围从「只 risk-monitor」放大到全页面。产品形态根本不同 → 取代而非 reopen。
本 OPT 沉淀的分析（要同步的 14-17 个 key 清单、localStorage 留作缓存层、不造完整 auth、official≠强制）
已被 OPT-0035 吸收。
