---
id: OPT-0020
title: Client Return Rate 加 4 个风控判断列(过夜比例 / USDT tag / Sharpe / Consistency)
status: idea
priority: P2
area: mixed
effort: L
created: 2026-05-19
related: [[OPT-0006]]
---

> **Update 2026-05-20**: USDT tag 列已拆到 [OPT-0022](OPT-0022-client-return-usdt-tag.md) 独立完成。
> 本 OPT 剩余 7 列：过夜比例 + Sharpe ×3 + Consistency ×3。
> 下方涉及 USDT 的 AC 条目已 ~~strikethrough~~。

## 问题

Risk team 用 `/client-return-rate` 决定客户 A-book / B-book 归类,现有列不够支撑这个判断:

- 看不出客户是 **短炒** 还是 **过夜持仓**(swing/carry) —— 决定 B-book 候选的核心信号
- 看不出客户是不是 **USDT 入金** —— 合规端需要额外标记的客户类型
- 现有 ROACE / 收益率指标 **不含风险维度** —— 高 ROACE 可能只是运气好,分辨不出「真本事」vs「单日大赢扛起整个月利润」

Risk team 需要在同一张表里看到这 4 个补充信号,不要再开第二个报表 / 跑 SQL。

## 背景

### 当前数据 pipeline

- 页面: `frontend/src/pages/ClientReturnRate.tsx`
- 后端: `backend/app/services/client_return_service.py`(两阶段查询)
- 现有 ROACE 列走的是 **夜间预计算 → SQLite 快照** 模式 (OPT-0006 落地):
  - `client_roace_scheduler.py` 06:00 HKT 触发
  - `client_roace_refresh_service.py` 跑 MySQL 全量 → 写 `data/client_roace.db`
  - `client_roace_db.py` 提供 `bulk_get_roace / upsert_roace_batch`
  - Web 请求在 `client_return_service.py:516` 之后从 SQLite 批量读出,Python 端拼回行
- **本 OPT 直接复用这个模式扩展**,不新建 scheduler / 不新建 DB 文件

### 表与索引(已验证)

- `fxbackoffice.mt4_trades`:**6300 万行**,关键列:
  - `openDate`、`closeDate` 都是 **STORED generated DATE 列**(`cast(OPEN_TIME / CLOSE_TIME as date) STORED`)→ 物理落地,可直接比较
  - `totalProfit` 是 **STORED generated**(`CMD IN (0,1,6) → PROFIT+SWAPS+COMMISSION`)→ Sharpe/Cons 直接 SUM 这一列
  - 索引: `INDEX_CLOSEDATE`, `IDX_OPEN_DATE`, `loginSid`, `IDX_IB_COMMISSION2(loginSid, closeDate)`
  - 没有 PARTITION BY(之前代码注释里说是 partition key,是误读)
- `fxbackoffice.stats_trading`:按 `(loginSid, date)` 预聚合,有 `totalPlClosed`、`tradeCnt`、`currency`
- `fxbackoffice.stats_balances`:按 `(loginSid, date)` 预聚合,有 `endingEquity`、`currency`,19M 行
- `fxbackoffice.user_tags`:tagid 直接过滤,已有 AKCM tag 先例

### 关键设计决策(已与用户对齐)

| 项 | 决策 | 理由 |
|---|---|---|
| 过夜定义 | `openDate != closeDate` (broker time UTC+3) | STORED 列,索引可走 |
| 未平仓单 | 已持过夜的(`closeDate='1970-01-01' AND openDate < CURDATE()`)进分子,所有未平进分母 | 反映"当前真实状态" |
| 过夜窗口 | 仅全历史一列 | 过夜习惯随时间变化慢 |
| 过夜显示 | "13.2%" + tooltip "13 / 98 trades" | 主信息简洁 + 详情可查 |
| 过夜样本下限 | 不设门槛 | 用户明确选择 |
| Sharpe 公式 | `mean(daily_return) / std × sqrt(252)`,Rf=0 | 教科书 |
| Sharpe 分母 | `daily_pnl[d] / endingEquity[d-1]` | 教科书做法 |
| Sharpe 首日 | 跳过(无 d-1) | 不引入合成数据 |
| Sharpe 窗口 | 全历史 / 30d / 90d 三列 | 用户明确选择 |
| Sharpe gate | active_days ≥ 30 **且** std > 0,否则 null | 防爆 + 防发散 |
| Consistency 公式 | `Max Daily Profit / Total Profit × 100%` | prop firm 业内标准 |
| Consistency gate | `Total > 0` 且 `Max > 0`,否则 null | 用户明确确认 |
| Consistency 窗口 | 全历史 / 30d / 90d 三列 | 跟 Sharpe 对齐 |
| 窗口语义 | 自然日(`date >= CURDATE() - INTERVAL N DAY`),不是"最近 N 个活跃日" | 跟 deposits_90d 已有口径一致 |
| Daily PnL 数据源 | `stats_trading.totalPlClosed` (已含 swap+commission,CEN÷100) | 跟 month_trade_profit / profit_hist 同口径 |
| ~~USDT tag~~ | ~~`tagid IN (6148, 214, 172)` 任一命中~~ | ~~用户明确指定~~ → **OPT-0022** |
| 列布局 | 8 列默认全可见(方案 B) | 用户明确选择 |

### 性能预估

- 过夜 SQL 全表扫:6300 万行,带 sid/CMD/isDeleted 过滤后 GROUP BY userId,估算 **5-15 分钟**
- Sharpe/Cons 用 stats_trading × stats_balances 流式扫:估算 **2-5 分钟**(`stats_trading` 表小很多)
- 现有 ROACE refresh ~1-3 分钟。**扩展后整夜 job 总耗时 ~10-25 分钟,可接受**(后台任务,不阻塞用户)

## 假设 / 待验证

- [ ] mt4_trades 全量过夜聚合 SQL 在 prod MySQL slave 上 < 20 分钟。**Drop 1 上线前用 EXPLAIN 验证**,超时则把 `_get_mysql_connection()` 的 `read_timeout` 从 600s 提到 1800s
- [ ] PG ETL (`pnl_user_summary*`) **真的**没有 cid 过滤(Explore agent 验证过没找到,但用户记得可能是 cid=1 only)。**本 OPT 不依赖 PG 数据**,所以即使有 cid 过滤也不影响,留作 future 优化的前置研究
- [ ] `closeDate = '1970-01-01'` 这个 sentinel 涵盖**所有**未平仓单(不会有 `closeDate IS NULL`)。已通过 schema 验证 STORED 列的定义,应当成立
- [ ] 现有 cache key prefix `client_return_v4_` 在 redis 内的覆盖范围(确认没有外部脚本依赖具体前缀,可以安全 bump 到 `v5_`)
- [ ] 单客户 130130 (已有 ROACE 数据) 适合做端到端 sanity check,人工跑 SQL 对照 API 输出

## 验收标准

### Drop 1 — 过夜比例 + USDT tag (1-2 天)

**SQLite / 后端 ETL**

- [ ] `client_roace_db.py` 扩展:
  - 加 `_OVERNIGHT_SCHEMA_SQL` 到 `init_client_roace_db()`
  - 加 `bulk_get_overnight() / upsert_overnight_batch()`
  - 加 `PRAGMA journal_mode=WAL`(同时影响 ROACE 表,无副作用)
- [ ] `client_roace_refresh_service.py` 加 `refresh_overnight()`:
  - 单条 SQL GROUP BY userId,带 sid/demo/employee/CMD/isDeleted 过滤
  - 批量 upsert SQLite
- [ ] `client_roace_scheduler.py` 的 `_refresh_job` 内,在 `refresh_all_clients()` 之后串行 `refresh_overnight()`,共享同一把锁

**后端 web 层**

- [ ] `client_return_service.py`:
  - ~~Phase 2 SQL 加 USDT LEFT JOIN(模板抄 `client_return_service.py:322-327` 的 AKCM block)~~ → **OPT-0022**
  - `bulk_get_overnight()` 调用放在 `bulk_get_roace()` 旁边
  - Python 端算 `overnight_ratio = overnight_count / total_count`(带 `total_count > 0` 守卫,否则 None)
  - cache key prefix `v4` → `v5`（注：OPT-0022 已经 bump 过；本 OPT 继续 bump 或保持 `v5` 待定）
  - `expected_columns` 列表加 `overnight_count / overnight_total / overnight_ratio` ~~`/ has_usdt_tag`~~
  - `allowed_sort_columns` set 加新列(支持服务端排序)
- [ ] `schemas/client_return_rate.py` `ClientReturnRateRow` 加字段:
  - `overnight_count: Optional[int]`
  - `overnight_total: Optional[int]`
  - `overnight_ratio: Optional[float]`
  - ~~`has_usdt_tag: bool = False`~~ → **OPT-0022**

**前端**

- [ ] `ClientReturnRate.tsx` 加 ~~2~~ **1** 列,**显式 `colId`**(CLAUDE.md grid-column-persist 规则要求):
  - 过夜比例列: `valueFormatter` 渲染百分比、`tooltipValueGetter` 返回 "X / Y trades"、null 时显示 "—"
  - ~~USDT 列: 布尔图标渲染器(copy `is_akcm` 那列)~~ → **OPT-0022**
- [ ] 用 `useGridColumnPersist` hook 注册新 colId,默认可见(方案 B)

**冒烟验收**

- [ ] 手动调一次 `POST /api/v1/client-return-rate/roace/refresh`(若需要,扩成 `?include=overnight`)或重启 backend 触发 06:00 cron 提前跑
- [ ] 单客户 130130 验证:手跑下面 SQL 与 API 对比

```sql
SELECT
    SUM(CASE WHEN t.closeDate != '1970-01-01' AND t.openDate != t.closeDate THEN 1
             WHEN t.closeDate  = '1970-01-01' AND t.openDate <  CURDATE() THEN 1
             ELSE 0 END)                                    AS overnight,
    COUNT(*)                                                AS total
FROM fxbackoffice.mt4_trades t
INNER JOIN fxbackoffice.mt4_users mu ON mu.loginSid = t.loginSid
WHERE mu.userId = 130130 AND mu.sid IN (1,5,6) AND mu.`GROUP` NOT LIKE '%demo%'
  AND t.CMD IN (0,1) AND (t.isDeleted = 0 OR t.isDeleted IS NULL);

-- ↓ USDT 验证 SQL 已迁移到 OPT-0022（本 OPT 不做）
SELECT tagid FROM fxbackoffice.user_tags
WHERE userid = 130130 AND tagid IN (6148, 214, 172);
```

### Drop 2 — Sharpe ×3 + Consistency ×3 (2-3 天)

**SQLite**

- [ ] `client_roace_db.py` 加 `pnl_metrics_snapshot` 表:
  ```sql
  CREATE TABLE IF NOT EXISTS pnl_metrics_snapshot (
      user_id      INTEGER PRIMARY KEY,
      sharpe_all   REAL, sharpe_30d REAL, sharpe_90d REAL,
      cons_all     REAL, cons_30d   REAL, cons_90d   REAL,
      active_days  INTEGER NOT NULL,
      refreshed_at TEXT NOT NULL
  );
  ```
- [ ] 加 `bulk_get_pnl_metrics() / upsert_pnl_metrics_batch()`

**后端 ETL**

- [ ] `refresh_pnl_metrics()`:
  - 一条 SQL 流式拉每客户 daily PnL × equity 序列(`ORDER BY userId, date`),pymysql `SSCursor` 或 `cursor.fetchone()` 循环,避免一次性载入内存
  - Python 端按 userId 分组,每客户一组计算 3 Sharpe + 3 Consistency,emit 后丢弃
  - 峰值内存 ~200 floats per user,完全可控
- [ ] scheduler `_refresh_job` 内串行第三步:`refresh_pnl_metrics()`

**SQL 草图**:

```sql
SELECT mu.userId, st.date,
       SUM(IF(st.currency='CEN', st.totalPlClosed/100.0, st.totalPlClosed)) AS pnl,
       MAX(IF(sb.currency='CEN', sb.endingEquity/100.0,  sb.endingEquity)) AS equity
FROM fxbackoffice.stats_trading st
INNER JOIN fxbackoffice.mt4_users mu ON mu.loginSid = st.loginSid
LEFT  JOIN fxbackoffice.stats_balances sb ON sb.loginSid = st.loginSid AND sb.date = st.date
INNER JOIN fxbackoffice.users u ON u.id = mu.userId AND COALESCE(u.isEmployee, 0) = 0
WHERE mu.sid IN (1,5,6) AND mu.`GROUP` NOT LIKE '%demo%'
  AND mu.userId > 0 AND st.tradeCnt > 0
GROUP BY mu.userId, st.date
ORDER BY mu.userId, st.date;
```

**Python 聚合伪代码**:

```python
def compute_metrics(daily_rows):  # 一个客户的全部 (date, pnl, equity)
    series = [(d, pnl, eq) for d, pnl, eq in daily_rows if eq is not None]
    today = date.today()
    for window_name, days in [("all", None), ("30d", 30), ("90d", 90)]:
        rows = series if days is None else [r for r in series if (today - r[0]).days <= days]
        if len(rows) < 30: yield (window_name, None, None); continue
        # Sharpe
        returns = [pnl / prev_eq for (_, pnl, _), (_, _, prev_eq) in zip(rows[1:], rows[:-1]) if prev_eq > 0]
        std = stdev(returns) if len(returns) >= 2 else 0
        sharpe = (mean(returns) / std * sqrt(252)) if std > 0 else None
        # Consistency
        max_daily = max(pnl for _, pnl, _ in rows)
        total = sum(pnl for _, pnl, _ in rows)
        cons = (max_daily / total * 100) if (total > 0 and max_daily > 0) else None
        yield (window_name, sharpe, cons)
```

**后端 web 层**

- [ ] 加 `bulk_get_pnl_metrics()` 调用,跟 ROACE/overnight 并列
- [ ] schema `ClientReturnRateRow` 加 6 个 `Optional[float]` 字段

**前端**

- [ ] 加 6 列,每列显式 `colId`,默认可见
- [ ] Sharpe: `valueFormatter` 2 位小数,null 渲染 "—"
- [ ] Cons: percent 格式,null 渲染 "—"
- [ ] 加列宽合理,避免页面横向滚动过长

**冒烟验收**

- [ ] 客户 130130 拉 daily series CSV,Python REPL 手算 3 个 Sharpe + 3 个 Cons,跟 API 比对(误差 < 0.01)
- [ ] 拉一个 `Total Profit < 0` 的客户(选 backlog 里一个亏损客户),确认 API 返回 Consistency = null
- [ ] 拉一个新客户(active_days < 30) 确认 Sharpe = null
- [ ] 性能:Phase 2 默认 7 天查询 cache miss 时长跟 Drop 1 之后比无明显回退(< +200ms)

## 笔记

### 架构图

```
                ┌──────────────────────────────┐
                │ APScheduler 06:00 HKT (现有) │
                │ client_roace_scheduler       │
                └──────────────┬───────────────┘
                               │ trigger
                               ▼
        ┌──────────────────────────────────────────┐
        │ _refresh_job(同一把锁,串行执行):         │
        │   1. refresh_all_clients()  ← 现有 ROACE  │
        │   2. refresh_overnight()    ← 新增 Drop 1 │
        │   3. refresh_pnl_metrics()  ← 新增 Drop 2 │
        └──────────────┬───────────────────────────┘
                       │ upsert
                       ▼
        ┌──────────────────────────┐
        │ SQLite client_roace.db   │
        │ - roace_snapshot          │ (现有)
        │ - overnight_snapshot      │ (新)
        │ - pnl_metrics_snapshot    │ (新)
        │ - roace_meta              │
        │   PRAGMA journal_mode=WAL │
        └──────────────┬───────────┘
                       │ bulk_get
                       ▼
        ┌──────────────────────────┐
        │ Web API Phase 2          │
        │ - mt4 SQL (现有,加 USDT) │
        │ - Python 拼接 4 个 metric │
        └──────────────────────────┘
```

### 反复确认的边界情况

| 场景 | 行为 |
|---|---|
| 客户没在 overnight_snapshot 里(新客户/首次刷新前) | API 返回 overnight_* = null,前端显示 "—" |
| 客户没在 pnl_metrics_snapshot 里 | 同上,6 个字段都 null |
| 客户 total_count = 0 | overnight_ratio = null(`total > 0` 守卫) |
| 客户某窗口 active_days < 30 | 该窗口 Sharpe/Cons 都 null |
| 客户某天 stats_balances 缺数据(LEFT JOIN 没拿到 equity) | 那一天的 daily_return 跳过(不算进 mean/std) |
| 客户 std(daily_return) = 0(全平 PnL) | Sharpe = null |
| 客户 Total Profit ≤ 0 | Consistency = null |
| Max Daily Profit ≤ 0 | Consistency = null(理论上 Total ≤ 0 时已经 null,这是防御性) |

### 待用户决策的开放问题(claim 前过一遍)

1. **两 Drop 还是一 Drop**:推荐两 Drop(Drop 1 简单先上 → 验证 SQLite 第二张表 + scheduler 串行 → Drop 2 含复杂 Python 聚合)。可改为一 Drop 但 PR 会胖
2. **30d/90d 窗口语义**:推荐 **自然日**(`date >= CURDATE() - INTERVAL N DAY`),跟 deposits_90d 同口径。若改成"最近 N 个活跃日"语义更紧但跟现有口径不一致
3. **OPT 拆 1 个还是 2 个**:推荐 **1 个 OPT,2 个 PR**(2 个 PR 都关联 OPT-0020)
4. **预飞行 SQL**:开干前先在 prod replica 跑下面 3 条只读,确认全量扫耗时:
   ```sql
   SELECT COUNT(*), MIN(closeDate), MAX(closeDate) FROM fxbackoffice.mt4_trades;
   SELECT sid, COUNT(*) FROM fxbackoffice.mt4_trades 
       WHERE closeDate >= DATE_SUB(CURDATE(), INTERVAL 7 DAY) GROUP BY sid;
   EXPLAIN <过夜 SQL>;
   ```

### 涉及文件清单

| 文件 | 改动概要 |
|---|---|
| `backend/app/core/client_roace_db.py` | 加 2 张表的 schema + bulk_get/upsert,加 WAL pragma |
| `backend/app/services/client_roace_refresh_service.py` | 加 `refresh_overnight()` + `refresh_pnl_metrics()` |
| `backend/app/core/client_roace_scheduler.py` | `_refresh_job` 内串行新增两步 |
| `backend/app/services/client_return_service.py` | Phase 2 USDT JOIN + bulk_get 调用 + cache key bump + sort columns 注册 |
| `backend/app/schemas/client_return_rate.py` | 加 10 个字段(4 + 6) |
| `frontend/src/pages/ClientReturnRate.tsx` | 加 8 列 def(都带 colId)+ tooltip + 布尔渲染器 |
| `backend/app/services/client_return_export_service.py` | 确认 CSV 导出自动带新列(若复用 service path 则免改) |
| `docs/features/client-return-rate.md` | 第 4/5 节加 4 个新指标说明 + 边界 |

### Future(本 OPT 不做)

- 过夜统计**增量刷新**:track `last_processed_closeDate` 每客户,首次全量后只扫 `closeDate >= yesterday`,累加到现有 SQLite 计数。Drop 1 上线后跑一周看真实耗时再决定是否立新 OPT
- 复用 PG `pnl_user_summary*` 表跳过 sid=6 部分的 MySQL 扫(需先验证 PG 是否真的 cid=1 only)
- TWR / MWR 替代 ROACE(独立大改动,不在本 OPT 范围)
- 30d/90d 也加给过夜比例(本 OPT 只算全历史一列)

## 结果

(done 时填)
