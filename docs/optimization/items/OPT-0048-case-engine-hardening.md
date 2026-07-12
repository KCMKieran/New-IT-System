---
id: OPT-0048
title: 风控V2 hardening 包 —— case sync 幂等化 + 检测去重 UNIQUE + 快照 catch-up + 前端护栏
status: ready
priority: P1
area: mixed
effort: M
created: 2026-07-13
related: [[OPT-0046]] [[OPT-0047]] [[OPT-0044]]
---

## 背景

OPT-0046/0047 两轮 workflow 冷审（各 4 视角 + 逐条对抗验证）确认的 live-with findings
打包。全部 yellow（无 red）：正常路径都正确，触发条件是故障恢复 / 规模增长 / 人为操作。
**前 3 项是 Phase B（接入其余 7 检测源）的前置**——多源灌入后这些窗口从"偶发"变"常态"。

## 交付内容（按优先级）

### P0 —— Phase B 前必做

1. **case sync 幂等化**（0047 冷审头号项，初判 red 降级 yellow）：
   `case_engine_service.py` 的 upsert 纯加性（signal_count 累加、timeline JSONB 拼接），
   而投递语义是 at-least-once——(a) PG commit 与 SQLite cursor 推进非原子，中间 crash 重放
   整批；(b) 两个调度器（rebate tick + 07:10 日基线 catch-up）无互斥，PG 故障积压恢复时
   并发重放会双计。**修法（验证 agent 给的两选一）**：cursor 移进 PG 用 CAS 同事务推进；
   或 PG 侧 `case_synced_events(source, event_id PK)` 标记表同事务 ON CONFLICT DO NOTHING
   过滤 + `pg_advisory_xact_lock` 串行化。timeline 条目已带 event_id，历史重复可事后清洗。
2. **0046-F3：检测去重 UNIQUE 化**：`alert_rebate_arb_detail` 的 per-day 去重是
   check-then-insert（仅进程内锁）。加 UNIQUE index（**新名** idx_rebate_arb_dedup，旧
   idx_rebate_arb_window 非 unique 已在存量库，init 里 drop）+ append 121-130 分支
   INSERT OR IGNORE + total_changes 未推进时同事务删孤儿 alert_events 行。
3. **日基线 catch-up**：07:10 错过（重启/DB 抖动）= case_metrics_daily 永久空洞，∆ 列
   该锚点永远 "—"。启动时检查最近快照日期，缺口内的交易日补跑（幂等已具备）。

### P1 —— 上量前做

4. **PG init 重试 + UndefinedTable→503**：init 只在 lifespan 跑一次，fresh DB + 启动时
   PG 不可达 = 永久半接线（后续全 500 直到重启）。`_schema_ready` 状态 + 首个成功连接补跑
   DDL；路由把 UndefinedTable / 查询期 OperationalError 一并映射 503（现在查询期故障 500）。
5. **观察清单 page_size=2000 静默截断**：>2000 案卷时显示 total 全量但只画前 2000。
   服务端分页或分页循环拉全 + 截断警告条。当前 149 真实案卷，不急但要在 Phase B 前。
6. **案卷 Sheet stale-response guard**：快速连点两行，慢请求后到会盖错客户。AbortController
   / request-id 比对（repo 既有约定就是 AbortController，页内列表 fetch 有、Sheet fetch 漏了）。

### P2 —— 顺手

7. **ColumnVisibilityMenu 展平叶子列重名**：4 个「30d」3 个「7d」分不清——leaf defs 给
   menu 场景带分组前缀 headerName。
8. **fixture seed 脚本 prod 护栏**：`seed_risk_cases_fixture.py` 对 env 指向的任何库直写。
   加护栏：库里存在非 fixture tag 案卷时要求显式 `--force`。
9. **case_metrics_daily 无保留策略**：1k 客户 ≈ 365k 行/年（共享实例）。定保留窗（如 400 天）
   或归档规则——先跟用户对齐合规要求再动手。

## 验收标准（AC）

1. 幂等：同一批 events 重放两次（模拟 crash 窗口 + 双调度器并发），signal_count/timeline
   与单次一致；集成测试覆盖。
2. UNIQUE：并发双进程写同 (window_date, client_userid) 只落一行，无孤儿 alert_events。
3. catch-up：删掉昨日快照行重启，缺口自动补齐；∆1 恢复出数。
4. 前端：>2000 场景（fixture 扩容模拟）有截断警告；连点两行 Sheet 永远显示最后点击的客户。
5. 全量 pytest / tsc / vitest 零新增红。

## 开放问题

- #9 保留窗口年限（合规要求？）——用户拍板。
- #1 两个修法选哪个（PG CAS cursor 更干净 vs 标记表侵入小）——实施时定，倾向 CAS。
