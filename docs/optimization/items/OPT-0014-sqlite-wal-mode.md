---
id: OPT-0014
title: Risk-monitor SQLite 启用 WAL 模式（消除读写互阻塞）
status: wip
priority: P1
area: db
effort: S
created: 2026-05-16
related: [[OPT-0003]] [[OPT-0011]] [[OPT-0012]]
---

## 问题

`backend/data/risk_monitor.db` 当前 journal_mode 是默认的 `DELETE`（rollback journal），写操作会锁住整个 DB，前端读 `/alerts` 在 scheduler 写入瞬间会被阻塞 200-500ms，表现为「切 Tab 偶发卡顿」（SKILL.md 早有记录）。

随着 OPT-0011 / OPT-0012 上线后扫描节拍提到 60s，写入频率 ×10，这个卡顿会从「偶发」变「经常」。所以 WAL 是 fast tier 的**前置条件**。

## 背景

- 提取自 [[OPT-0003]] 笔记中的「剩余子问题 5 · 问题 4 · 开 WAL」（独立 file 以便明确 claim 和验收）
- 现有代码：`backend/app/core/risk_monitor_db.py` 共 4 处 `sqlite3.connect(...)`：
  - L318 `init_risk_monitor_db()` — 启动 schema + migrations
  - L373 VACUUM 独立连接（`isolation_level=None`）
  - L663 `get_risk_monitor_db()` context manager —— 主路径
  - L1277 `get_alerts(...)` 直接打开（读路径）
- WAL 模式是**数据库级**持久属性（一次设置永久生效），但每次 open 时重新 set PRAGMA 是幂等且便宜的 → **采用「每次 connect 都应用」策略**，更稳健

## 假设 / 待验证

- [x] WAL + `synchronous=NORMAL` 的耐久性：崩溃可能丢失**最后一次 commit**，不会损坏 DB → 对告警数据可接受（下一轮扫描会重新写入）
- [x] WAL 副生文件 `*.db-wal` / `*.db-shm` 在 dev/prod 共享 mount 上是否正常 → 是的，SQLite 自动在 .db 同目录创建
- [x] `VACUUM` 是否能在 WAL 模式下跑 → 可以，autocommit 连接里执行
- [x] 备份脚本是否需要改 → `.db` 直接拷贝在 WAL 下是不完整的，但项目目前用的是 `.db.bak-*` 命名（手工预升级备份），不是热备份；常规不会踩到

## 验收标准

- [x] 新增 `_apply_pragmas()` 函数集中应用 5 个 PRAGMA
- [x] 在 4 处 `sqlite3.connect(...)` 后立刻调用（VACUUM 那处也调，幂等无害）
- [x] `journal_mode=WAL` / `synchronous=NORMAL` / `busy_timeout=5000` / `cache_size=-64000`（64MB）/ `temp_store=MEMORY`
- [x] 单元测试 `test_sqlite_pragmas.py` 验证：
  - 连接打开后 `PRAGMA journal_mode` 返回 `wal`
  - `PRAGMA synchronous` 返回 `1`（NORMAL）
  - `PRAGMA busy_timeout` 返回 `5000`
  - 写入 → 关闭 → 重开 → 读到 ✓（数据持久）
- [x] 并发读写 smoke test：单连接写入时另一连接同时读，不阻塞、不报错
- [x] `.gitignore` 加 `*.db-wal` + `*.db-shm`（避免 WAL 副生文件被意外提交）
- [x] 现有测试套件全过（`backend && .venv/bin/pytest tests/`）

## 笔记

**为什么 5 行**：原始 OPT-0003 笔记说「5 行改动」指的是 5 个 PRAGMA。实际实现因为要包成 helper + 在 4 处调用，会多出 ~10 行胶水代码。本质改动仍是 5 个 PRAGMA。

**为什么 cache_size=-64000**：负数表示 KB 而不是页数（页 = 4KB），更直观。64MB 对 220MB 的 DB 来说能 cache 大约 1/3 内容，热查询全部命中内存。当前默认 2MB 远远不够。

**为什么 busy_timeout=5000**：未来 fast tier 60s 节拍下，写并发会增加，SQLITE_BUSY 错误概率上升。5 秒等待窗口足够让任何一次扫描的写事务完成（典型 < 100ms），同时不会让前端请求挂死。

**WAL checkpoint**：SQLite 会自动在 WAL 文件达到 1000 页（~4MB）时 checkpoint 回主 DB。无需手动管理。如果发现 `-wal` 文件持续增长，可以加 `PRAGMA wal_autocheckpoint = 500` 调激进些。

**反对意见 / 验证过的非问题**：
- 「WAL 模式下 SELECT 看到的可能是旧数据」—— 是的，但只在「读事务开启的瞬间和写事务的瞬间」之间有几 ms 差异，对告警场景完全无所谓
- 「VACUUM 不能在 WAL 跑」—— 错误说法，VACUUM 可以在 WAL 模式下跑，只是不能在事务里跑。代码里 VACUUM 连接是 autocommit，符合要求

## 结果

_待填_
