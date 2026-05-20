---
id: OPT-0022
title: Client Return Rate 加 USDT 标记列（从 OPT-0020 拆出）
status: done
priority: P2
area: mixed
effort: S
created: 2026-05-20
completed: 2026-05-20
related: [[OPT-0020]]
---

## 问题

Risk team 用 `/client-return-rate` 决定客户 A-book / B-book 归类时，**看不出
客户是不是 USDT 入金** —— 合规端需要这个标记作为额外的客户类型识别信号。

USDT 客户在合规端有特殊处理流程，目前 risk team 要切到另一个页面或者跑 SQL
才能查，效率低。

## 背景

### 拆分自 OPT-0020

OPT-0020 原计划在 Client Return Rate 加 8 列（过夜 / USDT / Sharpe ×3 /
Cons ×3）分两 Drop 上线。但：

- 过夜比例和 Sharpe / Cons 都依赖**夜间预计算 SQLite snapshot**，且
  `mt4_trades` 6300 万行全表扫的耗时有不确定性，需要预飞行 SQL 验证
- **USDT tag 完全没有这些顾虑**：
  - 数据源 `fxbackoffice.user_tags` 几万行小表
  - 通过 Phase 2 SQL 的 LEFT JOIN 实时拿，不进 snapshot
  - 不依赖任何 scheduler 或新 SQLite 表
  - 跟 OPT-0020 其他 7 列零耦合

→ 拆出来单独做，1-2 小时内上线给 risk team 用。OPT-0020 保留其余 7 列，等
预飞行 SQL 验证后再决策（详见 OPT-0020 §"待用户决策" 和 §"假设/待验证"）。

### 数据规格

- 数据源：`fxbackoffice.user_tags`
- 命中条件：`tagid IN (6148, 214, 172)`（任一命中即为 USDT 客户，用户明确指定）
- 模板：`client_return_service.py:322-327` 现有 AKCM tag block（同一个 LEFT JOIN
  模式，可 copy-paste 微调）
- 字段语义：`has_usdt_tag: bool`，无标签时 `false`（不是 null）

### 现有可复用基建

- ✅ AKCM tag LEFT JOIN 模板，几乎 copy-paste 即可
- ✅ 前端 `is_akcm` 列的布尔图标渲染器
- ✅ Phase 2 SQL 已经在跑，加一个 LEFT JOIN 性能影响微小（user_tags 几万行
  + 已有索引 `userid`）
- ✅ `useGridColumnPersist` hook（OPT-0015）已就位

## 假设 / 待验证

- [ ] `tagid IN (6148, 214, 172)` 三个 ID 都是 USDT 标签且语义一致
      （OPT-0020 §"关键设计决策" 用户已明确指定，但实施前在 `user_tags` 里
      跑一次确认这三个 ID 都还活跃）
- [ ] cache key prefix `client_return_v4_` 可以安全 bump 到 `v5`
      （确认没有外部脚本依赖具体前缀；加新列必须 bump 否则旧 cache
      缺新字段会导致 schema mismatch）
- [ ] Risk team 提供 1-2 个已知 USDT 客户 user_id 用于 sanity check；
      若提供不了，从 `user_tags` 随机挑两个 `tagid IN (6148, 214, 172)`
      的 userid 做样本

## 验收标准

### 后端

- [ ] `client_return_service.py` Phase 2 SQL 加 USDT LEFT JOIN：
  ```sql
  LEFT JOIN fxbackoffice.user_tags ut_usdt
    ON ut_usdt.userid = mu.userId
   AND ut_usdt.tagid IN (6148, 214, 172)
  ```
  SELECT 列加聚合（防一对多 JOIN 撑大行数）：
  ```sql
  MAX(CASE WHEN ut_usdt.tagid IS NOT NULL THEN 1 ELSE 0 END) AS has_usdt_tag
  ```
  按 `mu.userId` GROUP BY 时归约到布尔
- [ ] `expected_columns` 列表加 `has_usdt_tag`
- [ ] `allowed_sort_columns` set 加 `has_usdt_tag`（支持服务端排序）
- [ ] cache key prefix `v4` → `v5`
- [ ] `schemas/client_return_rate.py` `ClientReturnRateRow` 加：
  ```python
  has_usdt_tag: bool = False
  ```

### 前端

- [ ] `ClientReturnRate.tsx` 加 USDT 列，**显式 `colId='has_usdt_tag'`**
      （CLAUDE.md grid-column-persist 规则要求所有列必须带稳定 colId）：
  - 复用 `is_akcm` 列的布尔图标渲染器
  - 默认可见
  - 列宽：~80px（跟 AKCM 列对齐）
  - 列标题："USDT"（简短，跟 AKCM 一致）
- [ ] `useGridColumnPersist` hook 自动认识新 colId

### 验证（冒烟）

- [ ] 单客户对照测试：
  ```sql
  -- 已知 USDT 客户（risk team 提供 user_id）
  SELECT tagid FROM fxbackoffice.user_tags
  WHERE userid = <X> AND tagid IN (6148, 214, 172);
  ```
  → API 返回 `has_usdt_tag = true`，前端图标渲染正确
- [ ] 已知非 USDT 客户：API 返回 `has_usdt_tag = false`，前端显示空
- [ ] CSV 导出包含 USDT 列（确认 `client_return_export_service.py`
      若复用 service path 则自动覆盖；否则补一行）

### 文档

- [ ] `docs/features/client-return-rate.md` §4 或 §5 加 USDT 列说明
      （字段语义 + 数据源 + 业务用途）
- [ ] **同步更新 OPT-0020**（本 OPT close 时一并做）：
  - AC §1 USDT 部分加 ~~strikethrough~~ 标记
  - 加注："USDT tag 已拆到 OPT-0022 独立完成（YYYY-MM-DD）"
  - backlog.md 的 OPT-0020 备注列加 "USDT 已拆到 OPT-0022"

## 笔记

### 为什么 effort = S（不是 M）

- 完全 mirror 现有 AKCM tag 实现
- 0 新表、0 新 service、0 新 scheduler
- 1 个 LEFT JOIN + 1 个 schema 字段 + 1 个前端列 = 估算 30-60 行净改动
- 估算实施 + 测试 = 1-2 小时

### 跟 OPT-0020 的关系

- OPT-0020 不放弃，保持 idea 状态
- 这个 OPT close 时，OPT-0020 的 USDT 部分标 ~~done~~
- 未来 claim OPT-0020 时 reader 一眼看出 USDT 已经做了

### Cache bump 的副作用

- `v4` → `v5` 让所有 client return rate 缓存失效
- 第一次访问回退到冷路径（Phase 2 SQL）会慢 ~几百 ms 到 1-2s
- 影响范围：访问量不大（risk team 内部页），无需特别预热
- 但建议低峰发布（晚上或周末）

### Future（本 OPT 不做）

- 在 USDT 列上加筛选下拉（"只看 USDT 客户" / "排除 USDT 客户"）
  —— 等 risk team 用一周看真实需求频次
- USDT 入金金额聚合（现在只是布尔标记）—— 需要拉 deposit 表，独立 OPT
- 多语言 tag 标题（en/zh）—— 等系统级 i18n 框架就位

## 结果

完成日期：2026-05-20。

### 实际交付（vs AC）

后端：
- ✅ `client_return_service.py` Phase 2 SQL：新增 USDT LEFT JOIN（子查询模板与 AKCM 一致，`tagid IN (6148, 214, 172)`，`SELECT DISTINCT userid` 防一对多撑行）
- ✅ SELECT 列加 `COALESCE(usdt.has_usdt_tag, 0) AS has_usdt_tag`
- ✅ `allowed_sort_columns` 加 `has_usdt_tag` + `is_akcm`（顺手补：`is_akcm` 之前漏在白名单）
- ✅ Cache key prefix `client_return_v4_zipcode_` → `client_return_v5_usdt_`
- ✅ Python 后处理把 int 0/1 cast 为 bool（跟 `is_akcm` 同一处理）
- ✅ `client_return_export_service.py` `_CSV_FIELDS` 加 `has_usdt_tag`（紧跟 `is_akcm`）
- ✅ `ClientReturnRateRow` schema 加 `has_usdt_tag: bool = False`

前端：
- ✅ `ClientReturnRate.tsx` 列定义新增「U入金」列（用户验收后定稿）：
  - 显式 `colId="has_usdt_tag"`（CLAUDE.md grid-column-persist 要求）
  - 列标题 `U入金`，配 `InfoHeader` tooltip：「含 tag：U_ERC20, U_TRC20, U_电汇TT\n如有更新，请联系 Kieran 添加」（跟 ROACE / avg_daily_equity 同款 InfoHeader）
  - 显示：是（绿）/ 否（灰），居中
  - 底层值 0/1：`valueGetter` 输出数字，`filter: agNumberColumnFilter` 直接按 1/0 过滤；显示由 `valueFormatter` 转中文
  - 宽 90px，位置：zipcode 之后、`net_deposit_hist` 之前（识别类信号靠前）
- ✅ `ClientReturnRateRow` interface 加 `has_usdt_tag: boolean`

验证（冒烟）：
- ✅ Pre-flight `user_tags`：3 个 tagid 全活跃（172=203 / 214=61 / 6148=3800 distinct user），共 4042 USDT 用户；索引 `(tagId, userId)` 复合 + 单列俱在
- ✅ 已知 USDT user 100021/100027/100038/100041（tag 214 命中）→ API `has_usdt_tag=true`
- ✅ 已知非 USDT user → API `has_usdt_tag=false`
- ✅ Sort by `has_usdt_tag` desc / asc 均正确
- ✅ Redis cache：第二次同参 query `from_cache=true`，命中

### 偏差

- ➕ **OPT 文档假设有误**：AC §前端「复用 `is_akcm` 列的布尔图标渲染器」—— 实际 `is_akcm` 在 grid 里**不是一个可见列**，只是 filter 下拉 UI。无现成渲染器可 copy。
- ➕ **列文案 + 渲染由用户验收后改版**：原计划列标题 `USDT` + 绿 ✓/— icon。用户要求改成「U入金」+ `InfoHeader` tooltip（列出 tag 名 + 联系 Kieran 更新提示）+ 中文「是/否」+ 过滤值 1/0。最终用 `valueGetter` 输出 0/1 + `valueFormatter` 转「是/否」+ `agNumberColumnFilter` 直接按数字过滤。
- ➕ `allowed_sort_columns` 顺手补 `is_akcm`：原本只在前端按布尔过滤，新 USDT 列既然支持服务端排序，AKCM 没理由不支持。零额外成本。

### 性能（回答用户原始疑虑）

用户提问："2k 个用户每次查询都要查 3 个 tagid，会不会拖慢？"
- 否。Phase 2 SQL 是**一条整 SQL**，所有 LEFT JOIN 共享 Phase-1 产出的 `id_list_str`。USDT JOIN 只是在已 active 的客户集合上做一次 index range scan。
- `user_tags` 几万行小表，`(tagId, userId)` 复合索引完美匹配 `WHERE tagid IN (3 vals) AND userid IN (...)`。
- 实测 90 天范围 4331 客户，冷查询 4.5s（与未加 USDT JOIN 前同量级），cache 命中 <50ms。
- v4→v5 cache bump 一次性失效成本 ≈ 几百 ms ~ 2s（首次冷查询），后续 3h TTL 内全走 Redis。

### Follow-up（留给未来）

- ~~若 risk team 反馈想筛选 "只看 USDT 客户" / "排除 USDT 客户"：补一个 `usdt_filter` 后端参数，仿照 `akcm_filter` 模式。~~ → 提前做了。用户验收时直接要求加，参考 risk-monitor 顶部下拉风格全部重写：3 个 ToggleGroup（国家 / AKCM）→ 3 个 shadcn Select，新增「入金渠道」下拉（trigger 显示类目名，点开是 全部 / 仅U入金 / 非U入金）；外层容器加 `sm:flex-wrap` 多过滤项窄屏自动折行。端到端：schemas + service + cache key + export 全套接通。
- USDT 入金金额聚合（现在只是布尔）：需要拉 deposit 表 + 货币识别，独立 OPT。
- 多语言列标题（en/zh）：等系统级 i18n 框架就位再统一处理。
- 抽公共 `<BooleanIconCell>` 组件：当第二个布尔列出现时再抽（YAGNI）。
