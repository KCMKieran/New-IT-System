---
id: OPT-0035
title: 视图档案服务端化（View Profiles）—— 命名档案 + 排他认领 + 观摩他人 + 管理员强制解绑
status: done
priority: P1
area: mixed
effort: XL
created: 2026-06-04
claimed: 2026-06-04
completed: 2026-06-05
branch: opt/view-profiles-p1
related: [[OPT-0015]], [[OPT-0025]], [[OPT-0027]], [[OPT-0003]]
supersedes: [[OPT-0029]]
---

> **⚠ 这是 [[OPT-0029]] 的取代单，不是它的延续。**
> OPT-0029 选「显式命名模板 + lead 设 official + 自报 email」，且 §假设 里**强烈倾向**这条、
> 明确反对「每人设置静默同步」。本 OPT 由用户拍板**走它反对的那条**（per-身份静默自动同步 +
> 排他认领 + 临时观摩），并从「只 risk-monitor」**放大到全页面**。产品形态不同 → 取代而非 reopen。

> **⚠ 这是一个无人值守（headless）试点单。** 拆 P1-P4，每 phase 的 AC 分两栏：
> **机器可验 AC**（先写成 pytest/vitest 失败测试骨架，AI 让它由红转绿，进 `verify.sh` 范围）
> 与**人验 AC**（merge 时人工 checklist，机器测不了的 UI/时序）。见文末「§ 无人值守执行框架」。

## 问题

今天所有「用户看数据的习惯」——每个 tab 显示哪些列、列序/列宽/排序、什么过滤器、聚合开关——
全部存在 **per-browser localStorage**（[[OPT-0015]] 列状态 9 key + [[OPT-0025]] 过滤器 5 key +
聚合/active-tab 等，共 **30+ key**，全无「用户」维度）。换机、换浏览器即丢，**无法记录某个人的
习惯、更无法让别人复用**。

用户要的产品形态（三条已锁定决策）：

1. **命名档案 + 排他认领**：设置页里有 Kieran / Sammy / Teresa … 一组命名档案（名字仅作区分，
   档案可自建）。我选中 Kieran = 把我浏览器的视图状态绑定到 Kieran，之后我的所有操作**默认静默
   保存**到 Kieran。**默认未认领（空）**。认领具**排他性**：我在我电脑上认了 Kieran，别人就认不了，
   **只有我从我电脑解绑才行**。
2. **观摩他人**：可只读查看 Sammy / Teresa 的视图（看他们怎么排列/过滤）。**临时预览，退出自动
   还原**我自己的视图，全程不写对方档案。
3. **逃生口**：认领是永久锁，认领那台机清缓存丢了 device-id 会永久锁死 → 设置页提供**管理员
   强制解绑**（二次确认）。

## 背景

### 现状（实测，决定方案）

- 视图状态散在 30+ localStorage key，三类 hook 管理：
  `useGridColumnPersist`（[[OPT-0015]]，key 在 `frontend/src/hooks/useGridColumnPersist.ts` 的
  `GRID_STORAGE_KEYS` 注册表集中管理）、`useFilterPersist`（[[OPT-0025]]，`<PAGE>_<TAB>_FILTERS_V1`）、
  RiskMonitor 内联的聚合开关 / `RISK_MONITOR_ACTIVE_TAB_V1`，外加 ClientPnL*/IBReport 等老页面**未收口**的内联 key。
- **关键约束**：AG-Grid 实例**只在 `onGridReady` 读一次 localStorage**，挂载后改 localStorage 不会自动重读
  → 「切换档案/进出观摩」必须靠**重挂载**（页面/路由 `key` bump）触发 hook 重新注水。这是本 OPT 最大工程摩擦点。
- **后端没有任何用户/登录体系**，全站一把共享 `X-API-Key`（`core/api_key_middleware.py`）。
  `auth-provider.tsx` / `login-form.tsx` 是占位 demo（`VITE_DISABLE_AUTH`），**不要误当真实认证去接**。
- **没有 device-id / 浏览器指纹**，`apiFetch`（`@/lib/fetch`）今天不带任何身份标识。
- 已有 **SQLite 应用库模式**（`core/*_db.py`，6 个 .db，裸 `sqlite3` + WAL，见 [[OPT-0014]]）→ 新表沿用。
- 管理员授权可复用 IB Financial 的 `admin_whitelist` 模式（`backend/app/api/v1/routes/ib_financial.py`）。
- `frontend/src/pages/Settings.tsx` 是空壳占位，路由 `/settings` 已注册，侧边栏暂无入口。

### 核心架构

- **身份抓手 = device-id**：前端首载生成 `kcm_device_id`(UUID) 存 localStorage，作为「我的电脑」唯一句柄
  （无登录，设备即排他单位）。`apiFetch` 注入 `X-Device-ID`。
- **新表 `view_profiles`（SQLite）**：`name`(PK) / `state_json` / `owner_device`(nullable，认领锁) /
  `owner_label`(友好设备名，排障用) / `claimed_at` / `updated_at`。锁是**持久列、不用 Redis TTL**
  （要永久锁，TTL 会自动过期，语义相反）。
- **三模式**：本地态（默认=今天行为不变，纯 localStorage）/ 主人态（写 localStorage + 防抖上传我的档案）/
  观摩态（拉对方快照注水、只读、退出还原）。
- **PROFILE_MANIFEST**：把今天散落的视图 key 收敛成单一清单 + `captureSnapshot()/applySnapshot()` 两原语。

### 复用资产（不重做）

- `useGridColumnPersist` / `useFilterPersist` / `<ColumnVisibilityMenu>` 的本地逻辑**保留**——localStorage 仍是
  快速缓存 + 离线兜底；档案是「来源之一」。
- `GRID_STORAGE_KEYS` 注册表 = manifest 的现成种子。
- 后端 `core/*_db.py` SQLite 模式 + `admin_whitelist` 模式。

## 分期与验收标准

> 拓扑：**每 phase 一个 PR → main（串行）**，上一个 phase 被人 merge 进 main 后才切下一个 phase 的 branch
> `opt/view-profiles-p{N}`。每个 PR 落地时 `verify.sh` 必须绿（含本 phase 新测试）。

### P1 — 前端地基（manifest + capture/apply + 重挂载）　branch `opt/view-profiles-p1`

纯前端，可用「导出/导入 JSON」自测，不依赖后端。自治度 🟢 高。

**机器可验 AC（vitest 失败骨架先行）**
- [ ] `PROFILE_MANIFEST` 单一清单覆盖全部视图 key（含 `GRID_STORAGE_KEYS` 全集 + `*_FILTERS_V1` + 聚合开关）；
      新增/遗漏 key 有一条测试断言 manifest 与注册表一致（防漂移）。
- [ ] `captureSnapshot()` 读 manifest 全部 key → 返回完整快照对象；manifest 外的 key **不**进快照。
- [ ] `applySnapshot(s)` 写回后 `captureSnapshot()` 等值（往返幂等）。
- [ ] 快照含未知/已废弃 key 时 `applySnapshot` 忽略它、不抛错（向前兼容自愈）。
- [ ] 「调查上下文」key（`loginInput`/`zipcodeInput`/绝对 `customRange`）**不**进快照（沿用 [[OPT-0025]] 边界）。

**人验 AC**
- [ ] 切换快照后页面重挂载，AG-Grid 列布局/过滤器肉眼正确刷新（无残留旧布局）。

### P2 — 后端档案存储 + 排他认领　branch `opt/view-profiles-p2`

自治度 🟢 最高（几乎纯 pytest 可证）。**schema phase，merge 时人额外重审建表 SQL。**

**机器可验 AC（pytest 失败骨架先行，全用 `tmp_path` 临时 DB，绝不碰 `backend/data/*.db`）**
- [ ] `view_profiles` 建表 + CRUD：list / get / create / save-state（upsert `state_json`）。
- [ ] `claim`：`owner_device IS NULL` → 写入成功；**已被他设备占 → 409**；已是本设备 → 幂等 200。
- [ ] `release`：仅 `owner_device == 调用设备` 才成功；他设备 release → 403/409。
- [ ] **★并发认领排他性**：两个线程同时 claim 同一 name，**断言恰好一个拿到 owner、另一个被拒**
      （条件 UPDATE `WHERE owner_device IS NULL OR owner_device=?` 的正面证明）。← 本 OPT 唯一高危正确性点。
- [ ] **管理员强制解绑**：白名单设备可清任意 `owner_device`；非白名单调用被拒（复用 `admin_whitelist`）。
- [x] 后端对 `state_json` 做**有界校验**（防任意写入）：总大小 ≤ 128 KB、key 数 ≤ 100、单 value ≤ 64 KB，
      且 key 形状走 allowlist 正则 `^[A-Z0-9_]+_(GRID_STATE|FILTERS|AGGREGATED|ACTIVE_TAB)_V\d+$`。
      **刻意不**镜像前端那份精确的 22-key manifest（会随前端漂移而误拒），改用 key-shape 约束 + 容量上限。
      并强制**仅 owner 可写**。
      ⚠ 本条之前被**静默漏掉**（计划写的是「key 在 manifest 白名单内」但实现里没做），此次重写让计划诚实对账。

**人验 AC**
- [ ] `X-Device-ID` 经 `apiFetch` 正确注入并被后端读到（一次端到端手测）。

### P3 — Settings UI（认领/解绑 + 观摩 + 自动防抖保存）　branch `opt/view-profiles-p3`

自治度 🟡 中（UI/时序大量靠人验）。

**机器可验 AC（vitest）**
- [ ] 主人态保存的**防抖逻辑**单测：连续多次变更只触发一次上传（用 fake timer）；
      `visibilitychange`/`beforeunload` 立即 flush。
- [ ] 观摩进入前 `captureSnapshot` 的「备份当前态」纯逻辑可测；退出 `applySnapshot(备份)` 还原等值。

**人验 AC**
- [ ] 设置页双区：「我的身份」（认领/解绑 + 排他状态展示 + 冲突文案「Kieran 已被另一台设备认领」）、
      「观摩他人」（选档案只读）。
- [ ] 主人态：拖列/改过滤后约 3-5s 静默上传成功；切页/关页 flush。
- [ ] 观摩态：进入显示只读横幅「正在观摩 Sammy 的视图 · 退出还原」，退出后我的视图完整还原，
      **对方档案未被写**。
- [ ] 管理员强制解绑：二次确认后目标档案变回未认领、可被重新认领。

### P4 — 收尾（老页面迁 manifest + 文档）　branch `opt/view-profiles-p4`

自治度 🟢 高。

**机器可验 AC（vitest）**
- [ ] ClientPnL*/IBReport 等老内联 localStorage key 迁入 `PROFILE_MANIFEST`；测试断言这些 key 在 manifest 全集内。

**人验 AC**
- [ ] `docs/features/grid-column-persist.md` 更新「per browser by design」段为「localStorage=本地缓存，
      view_profiles=命名档案/跨机层」；`docs/features/` 加一篇 view-profiles.md。

## 假设 / 待验证（claim P1 前定）

- [ ] **表放哪**：SQLite（跟现有 6 个 .db 一致、零新依赖、preset 数据量极小够用）vs PostgreSQL `reporting_db`。
      倾向 **SQLite**——但牵连 [[OPT-0003]]，claim 时拍板。
- [ ] **档案与「人」的关系**：一个 name = 一个档案（单档），还是一人可多档？本 OPT 取**单档**（最贴近「记录某人的习惯」）。
- [ ] **迁移**：认领时是否把我现有 localStorage「另存为该档案首版」？倾向**是**（认领即上传当前快照）。
- [ ] **观摩重挂载粒度**：整页 `key` bump vs 仅相关 grid 容器。倾向先**整页**（最稳），P3 性能不行再细化。
- [ ] **device-id 友好名**：认领时让用户填一次 `owner_label`（如「Kieran 工位机」）便于强制解绑时辨认。

## 已知限制（审计记录，收口前正视）

- **(a) 认领是先到先得、无任何校验**：任何人都能认领任意一个未被认领的名字（可被恶意抢占 / 锁住别人想用的名字），
  仅靠管理员强制解绑兜底。
- **(b) admin 强制解绑的 bootstrap 需要改 `backend/.env` + 重建容器**：第一次锁死救火是**一次部署、不是一次点击**
  （白名单为空时强制解绑根本不可用）。
- **(c) P1–P3 的人验 AC 至今未在浏览器里跑过**：收口时**要么**真跑一遍 browser pass，**要么**明确标注为 deferred，
  不能默认它们已通过。
- **(d) verify.sh 里 lint 仅 advisory**：无人值守循环可能**悄悄引入新 lint error 而不被闸门拦下**。建议的硬化是
  **delta-lint 闸门**（只在「相对 base 新增」的 lint error 上 fail），作为 follow-up。

## 笔记

- 边界**刻意收窄**：只做「per-身份命名档案 + 排他认领 + 观摩」，**不含**真实认证（密码/SSO）、RBAC、行为审计日志——
  内网互信小团队，device-id + 自报名字足够；真要可信归属走公司 SSO，是另一个 XL OPT。
- localStorage **不删**：留作快速缓存 + 离线兜底，档案只做「记录/跨机/共享」这一层，两层各司其职。
- 排他锁用 SQLite 持久列而非 Redis TTL——这是与「永久锁、只有本人能解」语义对齐的关键决策。

### 反观点

- **「直接做完整 login 不一劳永逸？」**：半成品 auth 比没有更糟（现有占位登录就是坑）。device-id 是「设备级排他」
  的最小够用解，不冒充真实身份。
- **「观摩为什么不直接抄成我的？」**：用户明确要「临时观摩、退出还原」，不污染我的档案。「一键采用」可作 P3 之后的
  follow-up，不进本 OPT 主线。
- **「永久锁会不会锁死？」**：会——所以 P2 强制带管理员强制解绑（逃生口），这是 AC 而非可选项。
- **「全队统一一个标准视图」哪去了？**：[[OPT-0029]] 原始的老板诉求是**全队统一/官方默认视图**（由 lead 钉一份），
  本 OPT 的 **per-身份模型并不服务这条**，且现在**没有任何单子在追它** → **明确 out of scope**。
  老板若仍要「全队统一一个标准视图」，那是**另开一个未来 OPT**，不在 OPT-0035 范围内。

## § 无人值守执行框架

- **闸门 = `./verify.sh` exit code**（pytest+tsc+vitest 硬闸，lint advisory）。无人值守循环只信 `VERIFY: PASS`。
- **每 phase 先写失败测试骨架（人/AI 写 AC-as-test）→ AI 让红转绿 → verify 绿 → 开 PR → 人 merge**（Stage 1 自治，
  绝不自动 merge——本 OPT 命中「永不自动 merge 清单」的 schema/migration + auth-adjacent 两条）。
- **测试 DB 隔离**：P2 pytest 用 `tmp_path` 临时 SQLite，绝不碰真库。
- **回滚 = 删未 merge 的 branch**，main 永远干净。
- 人工 checkpoint：①写/审 AC-as-test 骨架（尤其 P2 并发认领）②merge 每个 phase PR ③P2 建表 SQL 重审 ④P3 观摩 UX 人验。

> **⚠ 实际执行偏离了计划拓扑（诚实记录）。** 计划是「每 phase 一个 PR → main，串行」，但实践中
> P1 + P2（service + HTTP）+ P3 + admin 白名单**全部落在单一 branch `opt/view-profiles-p1`**，phase 之间
> **没有 merge**。后果：framework 本想在各自 PR 边界单独评审的 **schema/migration 切片**与
> **auth-adjacent 切片**（命中文末「永不自动 merge 清单」两条），现在被**和 UI diff 捆在一起**——
> schema/auth 审查被埋在 Settings UI 的大 diff 之下。
> **收口建议**：把 CLOSE 拆成**至少两个 PR**（先 backend/schema，后 frontend/UI），让 schema/auth 评审不被 UI 淹没。

## 结果

**P1–P3 + admin 白名单全部交付，2026-06-05 直接落 main（用户授权跳过 PR 评审 + outsider-review）。**

### 实际交付 vs AC
- **P1**：`PROFILE_MANIFEST`(22 key) + capture/apply（全替换）+ anti-drift 守卫。机器 AC 全绿。
- **P2**：`view_profiles` 表（`owner_device` 持久锁列）+ 条件 UPDATE 排他认领（**并发 20/20 + 压测 10/10**）
  + release/force-release/save + 7 端点 + device-id（X-Device-ID 注入）。机器 AC 全绿。
  - 「key ∈ manifest 白名单」AC **原实现是空的（假绿）**，审计后**重写**为有界校验（128KB/100key/64KB +
    key-shape 正则 + 仅 owner 可写），不镜像前端精确清单（避免漂移）。
- **P3**：Settings 双区 UI + 观摩（备份-还原，整页 reload 重挂载）+ 主人态防抖自动保存。
  机器 AC（observe/sync）全绿；**人验 AC 由用户 2026-06-05 在 dev(5173) 浏览器验证通过**。

### 审计（两轮独立 reviewer + 修复）
两个无 context reviewer 并行审（代码 / 方案），**独立撞车**两条：state_json 无界+假绿 AC、丢 device-id 永久锁。
3 个并行 agent 按 disjoint 文件修复：state 有界校验 + admin 空白名单 fail-loud warning + 失配停存(409→清认领) +
flush catch + 卸载 keepalive + pollMs<debounce。全程 verify 绿。
- **live with**（minor，reviewer 判 acceptable）：F5 force-release 403-before-404、F6 claimed_at 双 strftime、
  F8 confirm 中文硬编码。

### 流程偏离（记录）
计划的「每 phase 一 PR、phase 间 merge」**未执行**——P1–P3+admin+fixes 全在一条 `opt/view-profiles-p1`。
收口时曾拆成 backend/ui 两条 PR 分支（已 push），但用户最终**授权直接 push main**，故 2 条 PR 分支废弃。

### Follow-up
- **P4**（未做）：老页面 ClientPnL*/IBReport 内联 localStorage key 迁进 manifest + 更新 grid-column-persist.md。
- **delta-lint 闸门**：verify.sh lint 仅 advisory = 无人值守假绿向量，建议硬化（只拦相对 base 新增的 lint error）。
- **「全队统一官方视图」**（OPT-0029 原始老板诉求）本 OPT 不覆盖，如仍需要另开 OPT。
- 观摩无刷新重挂载（现整页 reload）、observe-of-force-released、双 tab 同档案并发保存 —— 均 live with，已记录。
- 课件：`docs/lessons/lesson-opt-0035-*.md`（本地资产，learn-from-opt 生成）。
