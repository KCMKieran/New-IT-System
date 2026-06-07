---
id: OPT-0036
title: database-context skill 分层 schema 库 —— fxbackoffice 390 表三级加载 + 生成器脚本
status: wip
priority: P2
area: mixed
effort: M
created: 2026-06-07
claimed: 2026-06-07
branch: opt/database-context-tiered-schema
related: []
---

## 问题

`database-context` skill 的 `mysql-schemas.md`（707 行，~6k token）是**手工维护 + 一次性全量加载**：

- 只覆盖 17 张表，而 `fxbackoffice` 实际有 **390 张**（实测 information_schema，2026-06-07）；
  极度长尾——Top 20 占 ~95% 存储（`ib_processed_tickets` 1.3 亿行 / 106GB，`mt4_trades` 4750 万行 / 25GB）。
- 写一条只涉及 `mt4_trades` 的 SQL 也要加载全部 707 行 → token 浪费 ~80%。
- 手工维护 schema 会漂移（列加了/删了没人同步），且 AI 不知道未覆盖的 373 张表的存在
  和体量（容易对亿级表 SELECT *）。

## 方案（已与用户对齐）

三级加载结构，挂在现有 `database-context` skill 下（CLAUDE.md 元规则三问 → 合并不新建）：

```
.cursor/skills/database-context/
├── SKILL.md                   ← 改写为路由逻辑           [Stage 0, ~50 token]
├── fxbackoffice/
│   ├── _index.md              ← 390 表目录，三档分层      [Stage 1, ~800 token]
│   └── tables/<table>.md      ← Tier A 单表 DDL+业务注释  [Stage 2, 每表 150-300 token]
└── （clickhouse/postgres 文件保留不动）
backend/scripts/dump_fxbackoffice_schema.py   ← 生成器（唯一进 git 的交付物）
```

- **Tier A**（~20-25 张，代码引用白名单）：有单表详情文件。列定义/索引由脚本从
  information_schema 生成；业务注释（CEN cents、UTC+3、CMD=6 等）放 `<!-- manual -->`
  区，重跑只更新机器区。
- **Tier B**（大表但代码未引用，>10MB ~30 张）：index 里一行（名+行数+一句话），
  核心价值是让 AI 知道"这表很大，别全表扫"。
- **Tier C**（长尾 ~335 张）：按前缀分组一行带过；`tmp_*`/`*_old`/空表归为忽略。

> ⚠ `.cursor/` 被 gitignore（本地资产）——schema 内容不进 git 历史（也避免生产库结构
> 入库）；进 git 的只有生成器脚本 + 本 OPT 文档。

## AC

机器可验：
- [ ] `backend/scripts/dump_fxbackoffice_schema.py` 用 readonly 账号跑通（backend 容器内），
      只读 information_schema（TABLES/COLUMNS/STATISTICS），不逐表 SHOW CREATE TABLE。
- [ ] 重跑幂等：单表文件 `<!-- manual -->` 区内容在重跑后逐字保留。
- [ ] `_index.md` 头部带生成日期 + 源（slave），三档分层齐全，390 表无遗漏（A 详情 / B 一行 / C 分组）。

人验：
- [ ] Tier A 单表文件 ≤ ~20 行/表；现有 mysql-schemas.md 的人工业务注释全部迁入对应 manual 区。
- [ ] mysql-schemas.md 的 fxbackoffice 段替换为指向新结构（mt5_live 段保留）；SKILL.md 路由
      改为三级决策流。
- [ ] token 抽查：典型单表任务（如改 mt4_trades 查询）所需 context 从 ~6k 降到 ~1.5k。

## 结果

（完成后填写）
