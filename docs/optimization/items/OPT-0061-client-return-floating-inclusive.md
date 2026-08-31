---
id: OPT-0061
title: Client Return Rate 加「含浮动收益率 + 扛单率」两列 —— 修正 ROACE 只含已平仓导致的收益率扭曲
status: ready
priority: P1
area: mixed
effort: M
created: 2026-08-31
related: [[OPT-0006]], [[OPT-0020]], [[OPT-0060]]
---

> **归类说明（用户拍板 2026-08-31）**：按 [`README.md` §什么进 tracker](../README.md#什么进-tracker--什么不进)，
> 「新列」属 net-new feature，本该走普通 `feat/<slug>` branch。**本条是用户明确要求开 OPT 的例外**，
> 同 [[OPT-0060]] 的处理。
>
> 不过本条比 OPT-0060 更靠近「优化」一侧：**§开放问题 1（账户范围不一致）是一个现存的口径 bug**，
> 修它会改变**现有** ROACE 列的数值，跟「加新列」是两件事。如果最终只做那一半，它就是纯粹的 bug fix。

---

## 问题

`/client-return-rate` 页面的 **ROACE 列**（`return_on_avg_equity`）对「只平盈利单、长期扛亏损单」的
客户**系统性高估**，而这类客户恰恰是 A-book / B-book 归类时最需要识别出来的。

用户 2026-08-28 从客户 128535 观察到症状并提出推论，已用从库全量数据求证成立。

### 扭曲机制：不是漏了一项，是双重放大

现行公式 `Return = profit_hist / avg_daily_equity`：

| 位置 | 字段 | 对浮动盈亏的处理 | 偏差方向 |
|---|---|---|---|
| 分子 | `profit_hist`（`stats_trading_running_totals.plClosedHavingActivityRunningTotal`） | **纯已平仓，完全不含浮动** | 浮亏被删掉 → 偏高 |
| 分母 | `avg_daily_equity`（`stats_balances.endingEquity`） | **equity 含浮亏** | 被浮亏压低 → 偏小 |

同一笔浮亏，在分子里被删掉、在分母里被减掉，**两个偏差同向叠加**。扛得越重，收益率越高。

### 实测：客户 128535

口径与页面 ROACE 完全一致（`sid IN (1,5,6)`、非 demo、`endingEquity > 0`、`INNER JOIN stats_trading`）：

| | 值 |
|---|---|
| 活跃天 | 548（2024-10-29 起） |
| Closed PnL | +29,288 |
| Floating | 0 → −29,351 |
| **真实净盈亏** | **−63 USD（−0.4%）** |
| 页面显示 | **+177.7%** |
| 扛单率（日均浮动 / 日均 balance） | −58.1% |

两年下来实质打平，页面读作赚了 178%。

### 另两个佐证客户

- **110367**：真实 +136.3%，若照用户初版公式（用日均浮动）会给出 −48.5% —— 见 §笔记「为什么不能用日均浮动」
- **125420**：日均 equity 只剩 1,379 / 日均 balance 76,308（**扛单率 −98.3%**，99.5% 的钱套在浮亏里，
  实质是等待爆仓），页面显示 **+531.3%**，真实净盈亏 **−35,442**

---

## 背景

### 当前实现（三个落点）

| 文件 | 行 | 现状 |
|---|---|---|
| `backend/app/services/client_return_service.py` | ~338-345 | `profit_hist` 子查询（`rt`）。**注意：无 sid / demo 过滤** |
| 同上 | ~281-289 | `eq` 子查询：`SUM(IF(UPPER(CURRENCY)='CEN', EQUITY/100.0, EQUITY))` from `mt4_users`，取**实时** equity |
| 同上 | ~584-606 | Python 侧 attach：从 SQLite 快照读 `avg_daily_equity`，算 `return_on_avg_equity` |
| `backend/app/services/client_roace_refresh_service.py` | `_REFRESH_SQL` | 夜间全量扫 `stats_balances`，`GROUP BY userId` 出 `avg_daily_equity` + `active_days` |
| `backend/app/core/client_roace_db.py` | `_SCHEMA_SQL` | SQLite 表只有 4 列：`user_id` / `avg_daily_equity` / `active_days` / `refreshed_at` |
| `backend/app/core/client_roace_scheduler.py` | — | 每日 06:00 HKT，`CLIENT_ROACE_SCHEDULER_ENABLED=true`（prod 已开） |

### 关键发现：数据全部现成，无需新数据源

`Total PnL = Closed PnL + (Floating_end − Floating_start)`，其中 `Floating = Equity − Balance − Credit`。
四个量都在**已经被扫描的表**里：

| 需要的量 | 来源 | 现状 |
|---|---|---|
| Closed PnL | `stats_trading_running_totals` | 页面已有 |
| 每日 Floating | `stats_balances.endingBalance` / `endingCredit` | **同一张表同一行**，夜间作业已在扫，只是没 SELECT 这两列 |
| 当前 Floating | `mt4_users.BALANCE` / `CREDIT` | **同一个 `eq` 子查询**，`EQUITY` 已在读 |
| `avg_daily_equity` | `client_roace.db` | 已有 |

**零新增表 / 零新增连接 / 零新增采集。**

### 实测性能（2026-08-31，从库）

| | 耗时 | 输出行数 |
|---|---|---|
| 现行 `_REFRESH_SQL`（prod 昨夜实测，读 `roace_meta`） | **17.5 秒** | 25,538 |
| 扩展版（两级聚合 + 窗口函数取首末日浮动） | **58.3 秒** | 25,553 |

⚠ **`client_roace_refresh_service.py` 的 docstring 写「typically 1-3 min」是错的**，实际 17.5 秒。

慢 3.3 倍但绝对值 < 1 分钟，跑在 06:00 无人时段，`read_timeout=600`。余量充足。

实测用的扩展 SQL 已验证可跑，见 §笔记。

### 实测影响面

按 `avg_eq ≥ 20% × avg_bal` 且 `≥30 活跃天` 且 `avg_eq ≥ $1,000` 过 gate 后共 **5,416 个客户**：

| | 数量 | 占比 |
|---|---|---|
| 新旧两指标**符号相反** | 59 | 1.1% |
| 差 **≥20 个百分点** | 465 | **8.6%** |
| ROACE ≥ 50% 且新指标为负 | 19 | — |
| 被 low-equity gate 拦下（本身即最强信号） | 46 | — |

扛单率分布（过 gate 内）：中位 −4.3% / p25 −16.9% / p5 −50.9% / p1 −70.3%。

---

## 假设 / 待验证

- [ ] 扩展 SQL 在 **prod 时段**（06:00）跑仍在 60 秒量级 —— 58.3 秒是白天从库实测，夜间应更快但未验
- [ ] `first_float` 取「首个活跃日」的语义是否符合业务预期（见开放问题 2）
- [ ] 统一账户范围后，现有 ROACE 列数值变化是否可接受（见开放问题 1）

---

## ✅ 已拍板决策（用户 2026-08-31）

| # | 问题 | 决策 | 后果 |
|---|---|---|---|
| 1 | 账户范围不一致 | **(a) 统一到 `sid IN (1,5,6)` 非 demo** | 与分母同口径；⚠ **现有 ROACE 列数值会变**（2,044 客户不同，540 个差 >$1,000） |
| 2 | 首日浮动 `Floating_start` | **(a) 取首个活跃日的真实浮动** | 不做「假设为 0」的简化（那会让 10% 的客户算错） |
| 4 | 扛单率列 | **加** | 零额外成本；可作 [[OPT-0060]] MDD 的 sanity gate |
| 5 | 新指标 vs ROACE | **并列，不替换** | 两列差值本身即扛单强度读数（8.6% 客户差 ≥20pp） |
| 6 | 全历史 vs 窗口化 | **v1 只做全历史** | 与现有 ROACE 对齐，零额外成本；窗口化留给 [[OPT-0060]] |
| 7 | 排期 | **(a) 单独做，走服务端 GROUP BY** | 1.5-2 天，收益立即。将来与 [[OPT-0060]] 合并流式骨架时这段 SQL 被替换，但列定义 / schema / 前端 / 测试全部留用 |

### 决策 1 的连带要求（🔴 实施者注意）

选了 (a) 之后，**`profit_hist` 子查询要加 `INNER JOIN mt4_users` + sid/demo 过滤**，这会**改变页面上
现有 ROACE 列的数值**。这不是新列的副作用，是一个独立的口径修正。

- [ ] merge 前跑一次「改前 vs 改后」全量对账，产出受影响客户清单（预期 2,044 个不同 / 540 个差 >$1,000）
- [ ] 上线前知会实际在用这个页面的人（同 2026-08-28 CEN 修复那次，那次是 101 个 ROACE 符号翻转）

---

## 🔴 仍未拍板：low-equity gate 的阈值（原开放问题 3）

用户 2026-08-31 回复时未涉及此项。**实施者按下面的默认执行，用户可随时改。**

提议默认（分析时实测过的一版）：

| 条件 | 默认值 |
|---|---|
| `avg_daily_equity ≥ ratio × avg_daily_balance` | ratio = **0.20** |
| `active_days ≥` | **30** |
| `avg_daily_equity ≥` | **$1,000** |

实测按此默认：过 gate **5,416 个**客户，拦下 **46 个**。

⚠ **被拦下的不要渲染成空白 `—`** —— 那批恰恰是最该看的（如客户 125420，扛单率 −98.3%、
99.5% 的钱套在浮亏里）。建议渲染成明确标记（如「资本已套牢 / Capital locked」），
并让「扛单率」列照常显示（它不受 gate 影响）。

> 待用户确认：三个阈值各定多少；被拦下时前端显示什么文案。

---

## 验收标准

> 开放问题 1 / 2 / 4 / 5 / 6 / 7 已于 2026-08-31 拍板（见上），AC 已按决策更新。
> **仅 low-equity gate 的三个阈值仍未拍板** —— 按 §仍未拍板 那节的默认执行。

### 后端

- [ ] `client_roace_db.py` 表加 4 列：`avg_daily_balance` / `avg_daily_credit` / `first_float` / `last_float`
  - ⚠ **SQLite `CREATE TABLE IF NOT EXISTS` 对活库是 no-op**，加列必须走迁移。
    但本库是**纯派生数据、可整体重建** —— 推荐**换表名（`roace_snapshot_v2`）或 drop 重建**，
    跑一次全量刷新（约 60 秒）即恢复，比写 `_migrate_add_column` 更干净。
  - ⚠ `backend/data` 是 **dev/prod 共享 bind mount**（`docker-compose.prod.yml:127`），
    dev 一重启就会改到 prod 的库 —— 同 `users.db` 的坑。
- [ ] `client_roace_refresh_service.py` 的 `_REFRESH_SQL` 换成两级聚合（见 §笔记里已验证的 SQL）
- [ ] **`_REFRESH_SQL` 补上 `MAX_EXECUTION_TIME` hint** —— 现在**没有**（只有 `read_timeout=600`），
      而它跑在**从库**上（`MYSQL_HOST_PRIMARY` 在 `.env` 里是注释掉的，回落 `MYSQL_HOST` = slave）
      扫 2,617 MB / 2,100 万行。查询要从 17s 变 58s，这个缺口更值得补。见 skill `db-timeout-guard`。
- [ ] 顺手修 docstring：「typically 1-3 min」→ 实测 17.5 秒
- [ ] **`profit_hist` 子查询加 `INNER JOIN mt4_users` + `sid IN (1,5,6)` + 非 demo 过滤**（决策 1a）
      —— 这会改变现有 ROACE 数值，见 §决策 1 的连带要求
- [ ] `client_return_service.py` attach 段算出新列：
      `含浮动收益率 = (profit_hist + (last_float - first_float)) / avg_daily_equity × 100`
      与 `扛单率 = (avg_eq - avg_bal - avg_credit) / avg_bal × 100`（决策 4）
- [ ] `allowed_sort_columns`（~462 行）加新列名
- [ ] **缓存前缀 `client_return_v7_cen_profit_hist_` → `v8_`**
      （⚠ `v7_` 已被 2026-08-28 的 CEN 修复占用；[[OPT-0020]] / [[OPT-0060]] 的方案文档里
      还写着要升到 `v7_`，那两处也该一并改成后续版本）
- [ ] `schemas/client_return_rate.py` 加字段
- [ ] `services/client_return_export_service.py` 的 `_CSV_COLUMNS` 加列
- [ ] `client_return_rate.py:120` 那段 403 文案里的「average-equity columns」改成涵盖新列

### 前端

- [ ] `ClientReturnRate.tsx`：interface + `columnDefs` + `InfoHeader` tooltip（中英双语，照现有列的写法）
- [ ] **ROACE 列保留**，新列与它并列（决策 5）；ROACE 的 tooltip 补一句「本列不含浮动盈亏，
      与右侧含浮动收益率的差值即扛单强度」
- [ ] tooltip **必须写明这是 mark-to-market 数字，会随行情每天变**（见 §风险 1）
- [ ] 不需要动 `GRID_STORAGE_KEYS` / view-profiles manifest ——
      `CLIENT_RETURN_RATE_GRID_STATE_V1` 已注册（`useGridColumnPersist.ts:39`），新列自动纳入

### 授权

- [ ] **不需要动 `MODULE_MAP`** —— 新列跟着现有 `include_avg_equity` 开关走，
      而 `client_return_rate.py:120` 的 handler 级收窄已把该 flag 绑到 `risk` 模块，新列自动继承

### 验证

- [ ] 客户 128535 / 110367 / 125420 三个案例手算对账
- [ ] 恒等式自检：`Closed + ΔFloating ≡ ΔEquity − 净流入`（同一批账户）
- [ ] `./verify.sh` 绿（基线约 pytest 1647 / vitest 256 / tsc 0；⚠ `test_xauusd_snapshot_service.py`
      有 2 个**既有**红，与本 OPT 无关）

---

## 笔记

### 为什么不能用「日均浮动」（用户初版提案的修正过程）

用户初版提案是 `(Closed PnL + Avg Daily Floating PnL) / Avg Daily Balance`。方向正确，
但分子把 **flow（累计量）和 stock（平均水位）相加**，没有单位。三个实测后果：

**① 测路径不测结果。** 构造：观察期 100 天、Closed +20,000、期初浮动 0、期末浮动 −20,000
（真实净盈亏 = 0）：

| 路径 | Avg Daily Floating | 提案公式 | 正确口径 |
|---|---|---|---|
| 甲：第 91 天才扛，扛 10 天 | (90×0 + 10×−20,000)/100 = −2,000 | **+18.0%** | 0.0% |
| 乙：第 1 天就扛，扛满 100 天 | −20,000 | **0.0%** | 0.0% |

同样的盈亏，只因开仓时间不同差 18 个百分点。

**② 已解套的浮亏被凭空记一笔。** 客户 **110367**（560 活跃天，账户已收尾）：
Closed +2,052，期初/期末浮动都是 0（长期扛单但最终全部了结），日均浮动 −5,398。
提案 `(2,052 − 5,398)/6,904 = −48.5%`，正确 `(2,052 + 0)/1,506 = **+136.3%**`。
差 185pp 且符号相反 —— 一个真实赚钱的客户被判成亏。

**③ 信号被历史长度稀释。** 两个客户此刻扛着**完全一样**的 −50,000、都已持续 60 天：

| | Avg Daily Floating | 浮亏被体现的比例 |
|---|---|---|
| 客户 A（250 活跃天） | −50,000 × 60/250 = −12,000 | **24%** |
| 客户 B（1,250 活跃天） | −50,000 × 60/1,250 = −2,400 | **4.8%** |

稀释系数 = 持仓天数 / 窗口天数，与盈亏无关。**这个公式的目的正是抓扛单，却被自己削弱。**

**④ 换窗口就翻天**（客户 128535 实测）：

| 窗口 | 提案公式 | 正确口径 |
|---|---|---|
| 全历史 548 天 | +20.5% | −0.4% |
| 近 365 活跃天 | −10.9% | +20.9% |
| 近 90 活跃天 | −82.6% | −3.4% |
| 近 30 活跃天 | −63.1% | +0.8% |

提案跨度 103pp，正确口径跨度 24pp（且变的是真实业绩）。

### 为什么分母保留 `avg_daily_equity` 而不换 balance

**分子分母必须同口径。** 分子一旦含浮动，就是承认那笔钱已经亏掉了；分母若用 balance，
等于同时声称那笔钱还在账上干活。同一个数不能在分子里是亏损、在分母里是资本。

另外 equity 口径与 [[OPT-0060]] 的 MDD、[[OPT-0020]] 的 Sharpe **同源**，换 balance 会打架。

**但 equity 分母有一个真实失效模式**（这是 balance 派唯一站得住的论点）：equity → 0 时分母爆炸。
客户 125420 真实净盈亏 −35,442，除以 avg_eq（1,379）= **−2,570.6%** 不可读，除以 avg_bal = −46.4% 可读。
→ 所以需要开放问题 3 的 gate，而不是换分母。

### 恒等式（可对账性，正确写法的核心优势）

```
Equity = Balance + Credit + Floating
Equity_end − Equity_start = 净流入 + Closed PnL + (Floating_end − Floating_start)
⇒ Total PnL = Closed PnL + ΔFloating = Equity_end − Equity_start − 净流入
```

两条路都能算且**必须相等** → 自带对账闸门。日均浮动写法不满足任何恒等式，算错了不会有人知道。

### 已验证可跑的扩展 SQL（2026-08-31 从库实测 58.3 秒 / 25,553 行）

```sql
SELECT uid,
  COUNT(*) AS active_days,
  SUM(eq)/COUNT(*)  AS avg_eq,
  SUM(bal)/COUNT(*) AS avg_bal,
  SUM(cr)/COUNT(*)  AS avg_cr,
  MAX(IF(d = mn, eq - bal - cr, NULL)) AS first_float,
  MAX(IF(d = mx, eq - bal - cr, NULL)) AS last_float
FROM (
  SELECT uid, d, eq, bal, cr,
         MIN(d) OVER (PARTITION BY uid) AS mn,
         MAX(d) OVER (PARTITION BY uid) AS mx
  FROM (
    SELECT mu2.userId AS uid, sb.date AS d,
      SUM(IF(sb.currency='CEN', sb.endingEquity/100.0,  sb.endingEquity))  AS eq,
      SUM(IF(sb.currency='CEN', sb.endingBalance/100.0, sb.endingBalance)) AS bal,
      SUM(IF(sb.currency='CEN', sb.endingCredit/100.0,  sb.endingCredit))  AS cr
    FROM mt4_users mu2
    INNER JOIN stats_balances sb  ON sb.loginsid  = mu2.loginsid
    INNER JOIN stats_trading  st2 ON st2.loginSid = mu2.loginsid AND st2.date = sb.date
    WHERE mu2.sid IN (1,5,6) AND mu2.`GROUP` NOT LIKE '%demo%'
      AND sb.endingEquity > 0 AND mu2.userId > 0
    GROUP BY mu2.userId, sb.date
  ) AS per_day
) AS w
GROUP BY uid
```

⚠ 上线时**必须加 `/*+ MAX_EXECUTION_TIME(...) */` hint**（见 AC）。

⚠ 注意最内层是按 `(userId, date)` 聚合的两级结构 —— **不能沿用现行 `_REFRESH_SQL` 那个
「SUM 所有行 / COUNT(DISTINCT date)」的捷径**。那个捷径算平均值没问题，但取不到首末日的值。

### 风险

**1. 数字每天会动，这是新的用户预期。** 新指标是 mark-to-market 的，客户不做任何交易它也会随行情变。
客户 128535 实测：2026-08-28 算是 **−0.4%**，2026-08-31 同口径是 **+9.0%**（浮亏从 −29,351 收窄到 −27,739）。
金融上完全正确，但和旁边单调累加的「历史利润」列并排放，**看的人会以为出 bug**。tooltip 必须写清。

**2. 日内盲区。** 所有基于 `stats_balances` 的浮动都是日终快照，当天开当天平的仓位看不见。
同 [[OPT-0060]] 的已知局限。

**3. `stats_balances` 索引全是 date-leading**（`PRIMARY(date, loginSid)` / `IDX_USERID(date, userId)` /
`IDX_DATE(date)` / `IDX_ACCOUNT(loginSid)`），无 userId-leading 索引 → 只能全扫。
21,027,201 行 / 2,617 MB。这也是现行作业已经在做的事，不是新增风险。

### 上游来源

- 用户 2026-08-28 提出，同日已用从库全量求证并发出两封分析邮件给 kieran.xiang@kohleservices.com
  （第二封含三个影响的逐步算例）
- 求证过程中**顺带发现并已修复上线**一个独立 bug：`profit_hist` 漏做 CEN ÷100
  （commit `96d5752` → merge `0065161`，回滚点 `pre-cenfix-20260828`）。
  该修复影响 3,781 个客户（13.8%）、116 个 `profit_hist` 符号反、**101 个 ROACE 符号翻转**。
  ⚠ 本 OPT 的所有实测数字都是在**修复之后**取的。

## 结果

<待填>
