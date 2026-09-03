---
id: OPT-0060
title: Client Return Rate 加 Max Drawdown(MDD)5 个窗口列 —— TWR 口径 + 夜间全量批处理
status: wip
priority: P2
area: mixed
effort: L
created: 2026-08-28
related: [[OPT-0020]], [[OPT-0006]], [[OPT-0061]]
---

> ## 🔄 2026-09-01 更新 —— [[OPT-0061]] 已上线，本 OPT 的形态变了
>
> [OPT-0061](OPT-0061-client-return-floating-inclusive.md)（**done**，`f1441fe`，2026-08-31）
> 由另一个并行 session 完成，**吃掉了本 OPT 原计划里的大半基础设施**，并且是**有意为本 OPT 留的接口**
> （见它的 §决策 7 与 §Follow-up 6）。本文档已按它的产出全面改写，主要变化：
>
> | 项 | 原计划（08-28） | **现状（09-01）** |
> |---|---|---|
> | SQLite 快照表 | 新建 `client_mdd_db.py` | **`roace_snapshot_v2` 已存在**，加列即可 |
> | 夜间 refresh service | 新建 `client_mdd_refresh_service.py` | **扩展现有** `client_roace_refresh_service.py` |
> | scheduler | 新建一步 | **已有**，06:00 HKT 在跑 |
> | gate 机制 | 从零设计 | **`capital_locked` 已落地**，含阈值与「不渲染成空白」原则 |
> | `MAX_EXECUTION_TIME` | 要补 | **已补**（`MAX_EXECUTION_TIME(300000)` hint）|
> | 「扛单率」 | 本 OPT 的配套建议列 | **已上线** `floating_burden_ratio` |
> | effort | `XL` | **`L`** |
>
> 🔴 **仍然成立、且未被 OPT-0061 解决的**：§ROACE 的顺风车搭不上（服务端 `GROUP BY` vs 流式）、
> §三样不能抄里的**两样**（`INNER JOIN stats_trading` 与 `endingEquity > 0` 仍在现行 SQL 里）、
> `autocommit` 仍未设、§已归零账户陷阱、§日内盲区。

> ## 🧊 2026-09-03 冷审（outsider-review）—— 2 个算法级缺陷 + 加固清单，已就地写回各节
>
> 零 context 独立 agent 对照源码冷审本方案（源码断言逐条核实：autocommit 未设 / 两个毒过滤器仍在 /
> 缓存前缀 v8 / `loadCachedState` 无版本守卫，全部属实）。发现按落点就地改写，此处只留索引：
>
> | # | 发现 | 落点 |
> |---|---|---|
> | R1 🔴 | TWR 递推在「大额入金日小亏」时把 u 永久钉死成假 100%（clamp 触发 + re-base 条件永不满足） | §算法（公式已修正） |
> | R2 🔴 | 「过滤器下推成 Python 逐日判定」数学上**不等价**——过滤发生在账户-日粒度、`GROUP BY` 求和之前 | §三样不能抄（改为 SQL 条件聚合双列集） |
> | R3 🔴 | 待拍板第 1/3/5 题是**算法阻塞项**非 scope 项；且 08-27 全部实测是求和口径，拍板 MAX 则实测分布要重跑 | §待用户拍板（已标注） |
> | R4 🟡 | 快照写入无原子性 + web 层不读新鲜度 → 中途断掉端出混代快照且静默 | §冷审加固清单（新增节 H1/H2） |
> | R5 🟡 | 从库侧三坑：201s filesort 物化在共享从库 / SSCursor 消费端停顿撞 `net_write_timeout`(60s) / 06:00 冬令时末日行可能没写全 | §冷审加固清单 H3–H5 |
> | R6 🟡 | `wiped_out` gate 必须**按窗口**存；§边界情况(=100%) 与 G4(=`—`) 原文自相矛盾 | §Gate 清单 G4（已改） |
> | R7 ⚪ | 合并形态本身应重新拍板（爆炸半径：一个 MDD bug 连带打挂已上线三列），给出 A/B/C 三选 | §待用户拍板 第 8 题 |

> ## ✅ 2026-09-03 用户拍板 —— 6 条全部收敛，升级 **ready**
>
> | 题号 | 拍板 | 连锁影响 |
> |---|---|---|
> | **1** | 多账户取 **MAX**：按 **loginSid** 建独立 TWR 序列，客户级 MDD = 名下账户 MAX | 流式按 loginSid 分组；`GROUP BY userId, date` **不再需要**（`stats_balances` 本来就是账户-日粒度，SQL 更简单）；🔴 08-27 实测分布是求和口径 → **上线前按 MAX 重跑**；「多账户缺行 → 幽灵回撤」问题随之消失（各账户序列独立，无需对齐日期，前向填充 / coverage gate 作废） |
> | **3** | G5 = **re-base 每段起始自有权益 ≥ $500** 才产生 TWR 样本 | 上线后看分布再调，先 $500 |
> | **4** | `include_mdd` **进** refusal list（无 `risk` 模块 403） | 路由 + scope 测试 AC 定案 |
> | **5** | `transfer in` / `transfer out` **计入 `F_t`**（MAX 口径下互转对单个账户就是外部资金流，随第 1 题自动得出） | §现金流口径 F4；⚠ 前置验证：`stats_transactions` 须能按 `(loginSid, date)` 聚合 |
> | **6/7** | OPT-0020 **本期不并**（流式段纯函数预留多指标位）；Calmar **不加** | scope 收口；B 形态下将来加 0020 只扩纯函数、不再改写作业 |
> | **8** | 合并形态选 **B**：同一作业**顺序两条查询** —— OPT-0061 的 48s 聚合查询**一字不动**，后接只算 MDD 的流式查询 | 「7 值挪 Python + 逐客户对账」的 AC **作废**（没动就无需对账）；R2 的条件聚合双列集**不再需要**（新查询直接不带毒过滤器，R2 保留作「为什么放弃方案 A」的论证）；§架构图 ③④ 按 B 理解 |

> **归类说明（用户拍板）**：按 [`README.md` §什么进 tracker](../README.md#什么进-tracker--什么不进) 的规则，
> 「新列」属于 net-new feature，本该走普通 `feat/<slug>` branch 而不进 tracker。
> **本条是用户明确要求开 OPT 的例外**——记录在此以免未来 reader 误以为规则被推翻。
> 归类之外的流程（claim 纪律 / 执行隔离铁律 / 双 hook）照 README 正常走。

> **effort 说明（2026-09-01 修订）**：初记 `XL`，理由是「新建 3 个后端文件 + 7 条待拍板」。
> [[OPT-0061]] 上线后，新建的三件（SQLite 库 / refresh service / scheduler）**全部变成「扩展现有」**，
> gate 机制也有了落地先例，待拍板从 7 条收敛到 **5 条**（第 2 题有现成语义可沿用）。
> 故**降回 `L`**。若第 1 / 3 / 5 题在 scope 阶段又长出新的算法分支，再升回 `XL`。

---

## 问题

老板要在 `/client-return-rate` 上直接看到每个客户的 **Max Drawdown（最大回撤，MDD）**，用来回答一句话：

> **「在历史上任何一个时间点买入并持有该账户，最糟糕会亏多少百分比？」**

### ⚠ 用途在讨论中反转过一次，这条必须写在最前面

| | 最初设想 | **收敛后的真实用途** |
|---|---|---|
| 方向 | MDD **>** 15% → 熔断 / 强平 | MDD **<** 15% → **挑稳健客户** |
| 命中率（实测，见 §实测分布） | 命中 **96%** 的客户 → **不可用**（等于全量强平） | 只留下 **1.9%**（all 窗口）→ 高选择性筛子 |
| 结论 | ❌ 放弃 | ✅ 本 OPT 按这个方向做 |

**这个反转决定了后面一连串设计**：因为方向是「越小越好」，所有会**低估** MDD 的口径缺陷都从
「安全的保守偏差」变成了**会把危险客户误标为稳健的假阳性**（尤其 §已归零账户陷阱 与 §日内盲区）。
如果将来有人把用途改回「熔断」，本文档 60% 的 gate 设计可以放宽——**改用途前先回来读这一节**。

### 配套用途

- **Calmar Ratio** = 年化收益 / |MDD| —— 风险调整后收益，比裸收益率更能分辨「真本事」vs「运气」
- **A-book / B-book 归类** —— 与 OPT-0020 的 Sharpe / Consistency 同一个决策场景

---

## 背景

### 与 OPT-0020 的关系（🔴 必读：不要扫两遍）

[OPT-0020](OPT-0020-client-return-rate-risk-signals.md) 的 **Drop 2（Sharpe ×3 + Consistency ×3）**
与本 OPT **用的是同一条数据序列**——按 `(userId, date)` 升序的每日 equity / PnL 时间序列。

| | OPT-0020 Drop 2 | OPT-0060（本 OPT） |
|---|---|---|
| 序列 | 每客户每日 `(date, pnl, equity)` | 每客户每日 `(date, own_equity, flow)` |
| 主源表 | `stats_trading` × `stats_balances` | `stats_balances` × `stats_transactions` |
| Python 侧 | 按 userId 分组、组内算指标、emit 后丢弃 | **完全相同的流式分组骨架** |
| 输出 | 6 列（Sharpe/Cons × 3 窗口） | 5 列（MDD × 5 窗口）+ 2 布尔列 |

**分开做 = 把 1,300 万行的客户-日序列扫两遍**（实测每遍权益曲线腿 201s 服务端排序 + 114s 流式读取）。
两者都是压在**从库**上的全表扫，撞在一起或前后脚跑都是纯浪费。

**建议**：合并为一次扫描、一个 refresh service、一张 SQLite 快照表，产出 11 列。
这是 §待用户拍板 的第 6 题。**若用户选分开做**，至少要保证两个 job 在时间上错开、且各自记录扫描耗时，
以便将来合并时有对比数据。

### 与 OPT-0061 的关系（🔴 必读：一半的地基已经建好了）

[OPT-0061](OPT-0061-client-return-floating-inclusive.md)（**done** 2026-08-31）加了两列
`return_with_floating`（含浮动收益率）+ `floating_burden_ratio`（扛单率）+ `capital_locked` 标记，
并顺手修了 `profit_hist` 的 sid/demo 口径。它与本 OPT **共用同一张表、同一个夜间作业、同一张快照库**。

**两者测的不是同一件事，是互补不是替代：**

| | OPT-0061（已上线） | OPT-0060（本 OPT） |
|---|---|---|
| 测什么 | **终点**：`(profit_hist + ΔFloating) / avg_daily_equity` | **最惨的路径点**：峰谷落差 |
| 时间维度 | **只有全历史**（其决策 6 明确把窗口化留给本 OPT）| 5 个锚定窗口 30d/90d/180d/365d/all |
| 资金流 | 分母用日均净值，**不剥离出入金** | **TWR 份额净值**，完整剥离出入金（见 §口径决策）|
| 能否看见「中途掉到 −80% 又涨回打平」 | ❌ 看不见 | ✅ **这就是 MDD 不可替代的部分** |

**本 OPT 可以直接吃到的现成件**（原计划要新建，现在只要扩展）：

| 现成件 | 位置 | 本 OPT 怎么用 |
|---|---|---|
| SQLite 快照表 `roace_snapshot_v2` | `client_roace_db.py` | **加 MDD 的列**，不新建库（v1 `roace_snapshot` 保留作回滚镜像）|
| 夜间 refresh service（已扩到 48.3s / 25,555 行） | `client_roace_refresh_service.py` | **换成流式骨架**，见 §ROACE 的顺风车搭不上 |
| scheduler 06:00 HKT | `client_roace_scheduler.py` | 直接挂 |
| **每日每客户的三列聚合**（`eq` / `bal` / `cr`，含 CEN 折算） | `_REFRESH_SQL` 的 `per_day` 内层子查询 | 🟢 **正是 MDD 需要的粒度**——差别只在外层把它 collapse 掉了 |
| gate 机制 + 「被拦下不渲染成空白」原则 | `capital_locked`（`avg_eq < 20% × avg_bal`）| G4「还活着」gate 的现成语义，见 §Gate 清单 |
| `MAX_EXECUTION_TIME(300000)` hint | `_REFRESH_SQL` 首行 | 已补，本 OPT 不用再操心这条 |

⚠ **OPT-0061 的 Follow-up 6 已经点名本 OPT**：落地时它的刷新 SQL 被流式骨架替换（列定义 / schema /
前端 / 测试留用），并**引入 per-metric 状态枚举替代 `capital_locked` 布尔**——因为届时会有两个带 gate
的指标，一个布尔不够表达「哪个指标为什么被 gate 掉了」。**这是本 OPT 的前置动作，别漏。**

⚠ **缓存前缀已被推到 `client_return_v8_floating_inclusive_`**（OPT-0061 占用），本 OPT 要 bump 到 `v9_`。

> **实测数字的有效性**：本 OPT 所有 2026-08-27 的实测**不受 OPT-0061 影响** ——
> MDD 是直接从 `stats_balances` 自算（自己做 CEN 折算 + `sid IN (1,5,6)` + 非 demo 过滤），
> 没有经过 `profit_hist`，所以 08-28 的 CEN 修复与 OPT-0061 的 `profit_hist` 口径收窄都碰不到它。
> 且本 OPT 用的账户范围**恰好与 OPT-0061 修正后的范围一致**。

### 当前 pipeline（本 OPT 复用的模式）

现有 ROACE 列走 **夜间预计算 → SQLite 快照** 模式（[OPT-0006](OPT-0006-m2-roace-precompute.md) 落地）：

- `backend/app/core/client_roace_scheduler.py` — 06:00 HKT 触发
- `backend/app/services/client_roace_refresh_service.py` — 跑 MySQL 全量 → 写 `backend/data/client_roace.db`
- `backend/app/core/client_roace_db.py` — `bulk_get_roace` / `upsert_roace_batch`（⚠ `bulk_get` 内部按 **900 个参数**分批，见 `client_roace_db.py:74`）
- Web 请求在 `client_return_service.py` Phase 2 之后从 SQLite 批量读出，Python 端拼回行

**本 OPT 沿用这个模式**（夜间算、白天读快照），但 **不复用同一个 job**——原因见下面「ROACE 的顺风车搭不上」。

### 页面现状

- 前端：`frontend/src/pages/ClientReturnRate.tsx`
- 后端：`backend/app/services/client_return_service.py`（两阶段查询）
- 现有列定义 / 净入金口径 SSOT：[`docs/features/client-return-rate.md`](../../features/client-return-rate.md) §3 / §3.1
- ROACE 口径 SSOT：[`docs/features/roace-return-rate.md`](../../features/roace-return-rate.md)
- 当前缓存版本前缀：`client_return_v8_floating_inclusive_`（`client_return_service.py:486`）
- 🆕 **2026-08-31 起页面已有** [[OPT-0061]] 的三个字段：`return_with_floating` / `floating_burden_ratio`（扛单率）/ `capital_locked`。MDD 列上线后应与它们**并排展示**并在 tooltip 里点明分工（一个测终点、一个测当下浮亏、MDD 测最惨路径点）

---

## 口径决策：必须用 TWR，**不能**用裸 equity

### 公式

**TWR（Time-Weighted Return，时间加权收益 / 份额净值）**：

```
u_t = u_{t-1} × (E_t − F_t) / E_{t-1}
```

- `E_t` = 当日**客户级自有权益** = `endingEquity − endingCredit`（剥离赠金）
- `F_t` = 当日**外部资金净流入**（见 §现金流口径）
- `u_t` = 第 t 日的份额净值（unit value），`u_0 = 1`

MDD 在 `u` 序列上算，不在 `E` 序列上算。

### 为什么裸 equity 曲线是错的（三个理由，带数值）

**理由 1 —— 裸 equity 会凭空制造回撤（分不清「亏掉的」和「客户提走的」）**

| 步骤 | 账户权益 |
|---|---|
| 入金 $10,000 | $10,000 |
| 赚到 $12,000 | $12,000（峰值）|
| 出金 $6,000 | $6,000 |

裸口径：`(12,000 − 6,000) / 12,000` = **MDD 50%**。
实际：**该客户一分钱没亏**，TWR MDD = **0%**。

**理由 2 —— 裸 equity 也会低估（入金重置了峰值基准）**

| 步骤 | 权益 | 分段收益 |
|---|---|---|
| 起始 $10,000 | 10,000 | — |
| 亏到 $8,000 | 8,000 | ×0.800 |
| 入金 $90,000 | 98,000 | （不是收益）|
| 亏到 $88,000 | 88,000 | ×0.898 |

裸口径：两段跌幅分别 20% 与 10.2%，取 `max` = **20%**。
TWR：`0.800 × 0.898 = 0.718` → **28.2%**。
**两段亏损连乘才是「从头持有到尾」的真实体验**，裸口径把它被入金截成了两段。

**理由 3 —— 所以裸 equity 不是「保守版 TWR」，是方向不定的噪声**
理由 1 里它高估、理由 2 里它低估，**偏差方向取决于客户的出入金习惯**，不是一个可以用「留安全边际」搪塞过去的系统性偏差。

### 理由 4 —— 老板原文的定义本身就要求 TWR

> 「在历史上任何一个时间点**买入并持有该账户**，最糟糕会亏多少」

「买入并持有」= 持有**一份份额**、**不控制**账户的出入金 = 份额净值 = TWR。
换句话说：TWR 不是我们额外加的复杂度，它就是这句话的数学翻译。

### 理由 5 —— 实测代价（2026-08-27，从库，365d 单窗口合格池 2,545 人）

| 口径 | MDD < 15% 的人数 |
|---|---|
| **TWR** | **49 人** |
| 裸 equity | 17 人 |

**32 个真正稳健的客户会因为「提过款」被裸口径误杀**——而被误杀的恰恰是
**真赚到钱并把钱拿走**的那一批，即最应该进白名单的人。

### 唯一该用裸 equity 的场合

问「**公司在这个账户上的敞口最大掉过多少**」（AUM / 保证金视角）时，裸 equity 才是对的。
那是另一个问题、另一个指标，**不要**用它来回答本 OPT 的问题。

---

## 数据源（2026-08-27 从库实测确认）

### 主表 `fxbackoffice.stats_balances`

| 项 | 实测值 |
|---|---|
| PK | `(date, loginSid)` |
| 规模 | **~19.8M 行 / 5.3 GB** |
| 索引 | `IDX_ACCOUNT(loginSid)` · `IDX_DATE(date)` · `IDX_USERID(date, userId)` · `UQ KEY_UK(date, loginSid)` |
| **覆盖起点** | **2021-07-13**（`SELECT MIN(date)` 实测）→ 至今 |
| 采样 | **每个历日都有行，含周末** |
| 365d 窗口内 | 878 万行 / 35,599 账户 / 22,650 客户 |

⚠ **「全历史」的真实含义是「2021-07 起」**——比这更老的账户被截断。
**这一句必须写进 `MDD(all)` 列头的 `InfoHeader` tooltip**，否则老账户的 MDD 会被当成真的全生命周期值。

字段口径：

| 字段 | 含义 |
|---|---|
| `endingEquity` | **含浮动盈亏** |
| `endingBalance` | **不含**浮动盈亏 |
| `endingCredit` | 赠金 |

⚠ `currency = 'CEN'` 时**三列都要 ÷100**（不是只除 equity）。

### 资金流腿 `fxbackoffice.stats_transactions`

全历史按 `(userId, date)` 聚合后 **283,845 行，6 秒出**。

### 活跃度腿 `fxbackoffice.stats_trading`

提供 `active_days` / `trades` / `overnight_days`（`hasOpenTrades = 1` 的天数），全历史 **7 秒出**。

### 🔴 三条排除项（别再去试）

| 候选 | 为什么不能用 |
|---|---|
| `stats_trading_running_totals` | **不是时间序列**——每账户仅一行、**无 `date` 列**。ROACE 的 `profit_hist` 用它是对的，MDD 用不了 |
| ClickHouse | `stats_balances` **不在 CDC 表集内**；旧的 `Fxbo_Trades` 库已退役 |
| `kcm.daily_user_equity`（T8） | **已于 2026-07-21 按决策 K12 删除**（它是 `stats_balances` 的冗余副本）。**不要复活它** |

---

## 现金流口径：必须与页面现有 `net_deposit_*` **有意分叉**（三处）

> 🔴 **这是本方案最容易被后人「顺手统一」改错的地方。**
> 页面上已有一套 `net_deposit_*` 口径（[client-return-rate.md §3.1](../../features/client-return-rate.md#31-净入金口径为什么不含-ib-withdrawal2026-07-15-修正)），
> MDD 的 `F_t` **不能直接复用它**。下面三条是**有意分叉**，不是遗漏。

| # | 分叉点 | 页面 `net_deposit_*` 现状 | **MDD 的 `F_t` 必须** | 不改的后果 |
|---|---|---|---|---|
| **F1** | sid 域 | `sid IN (1,2,5,6)`（**含** IB 钱包 sid=2） | **收窄到 `(1,5,6)`**，与 equity 曲线同域 | IB 钱包上的普通 `deposit` 被当成「外部注资」从收益里扣掉 → **凭空制造回撤** |
| **F2** | `'ib transfer to account'` | **不进**任何净入金公式（文档标为「已知残留偏差，对收益率是保守方向」） | **必须计入 `F_t`** | 「爆仓后从佣金钱包补钱续命」的 IB 兼交易者会被系统性**洗白成低回撤客户**——正好污染稳健名单。实测 365d 窗口内 **37,336 笔**（`ib transfer to account out` 35,031 笔），量级不小 |
| **F3** | CREDIT 赠金 | `equity` 含 credit | 曲线走 `equity − credit`，**credit 变动同时进 `F_t`** | 赠金入账被算成交易盈利，**把回撤填平** |

**沿用不变的**：`'ib withdrawal'` 依然**剔除**（客户赚到的佣金不是投入的本金）。
SSOT 见 [client-return-rate.md §3.1](../../features/client-return-rate.md) + [`rebate-arbitrage` SKILL.md §2.2](../../../.cursor/skills/rebate-arbitrage/SKILL.md)。

✅ **F4（2026-09-03 拍板）**：`transfer in` / `transfer out`（账户间互转，各约 3 万笔）**计入 `F_t`** ——
MAX 口径下序列按账户建，互转对单个账户就是外部资金流；不计入会把「从 A 账户搬钱救 B 账户」
算成 B 的交易盈利，**填平真实回撤**。⚠ 前置：`F_t` 必须按 **`(loginSid, date)`** 聚合（不是 userId），见 §假设/待验证。

---

## 算法

### 伪代码（逐字实现）

```
按 (userId, date) 升序流式扫描全历史；每个客户维护一条 unit 序列 + 5 组窗口累加器

own_t   = endingEquity_t − endingCredit_t          # 客户级自有权益（多账户求和；第 1 题若拍板 MAX 则按 loginSid）
F_t     = 当日外部资金净流入(deposit + withdrawal + ib transfer to account)

# 🔴 2026-09-03 冷审修正（R1）：净入金日分母必须含当日入金（视为日初到账）
ret_t   = own_t / (own_{t-1} + F_t)                # F_t > 0（净入金日）
ret_t   = (own_t − F_t) / own_{t-1}                # F_t ≤ 0（净出金日 / 无资金流）
u_t     = u_{t-1} × max(ret_t, 0)                  # clamp 只该在真爆仓时触发

# re-base（重开新段，u 置回 1）三个触发条件，命中任一即触发：
#   ① own_{t-1} ≤ 0                                # 爆仓后再入金（原有）
#   ② u == 0 且 F_t > 0                            # clamp 归零后客户回来了（🔴 原方案缺这条 → 假 100% 永不恢复）
#   ③ |F_t| > 10 × own_{t-1}                       # 资金流远大于存量、比率失真（系数 10 实施时校准）

窗口 = 锚定后缀（到今天为止的最近 N 天），W ∈ {30d, 90d, 180d, 365d, all}
对每个 W:
    首次进入窗口时 peak_W = u_t          # 窗口内自己的局部峰值，不用全局峰值
    peak_W = max(peak_W, u_t)
    mdd_W  = max(mdd_W, (peak_W − u_t) / peak_W)
```

### 🔴 为什么原版递推是错的（R1 反例，2026-09-03 冷审）

客户原有 $1,000，当天入金 $100,000、日终 $96,000（真实亏损约 5%）：

- **原版**：`ret = (96,000 − 100,000) / 1,000 = −4` → clamp 到 0 → **u 永久钉死，MDD = 100%**；
  且 re-base 永不触发（`own_{t-1} = 1,000 > 0`），之后客户翻 10 倍也看不见
- **修正版**：`ret = 96,000 / (1,000 + 100,000) = 0.950` → 回撤 ~5%，符合事实

在「挑稳健客户」方向上这是**假阴性**（稳健客户被误标成爆仓）——比假阳性安全，
但「入过大额金的客户系统性进不了白名单」恰好剔掉最有商业价值的那批人，不能不修。

### 两条关键性质

1. **单调性由构造保证**：因为每个窗口用**自己的局部峰值**（而不是全局峰值），
   `MDD_30d ≤ MDD_90d ≤ MDD_180d ≤ MDD_365d ≤ MDD_all` 恒成立。
   **实测 3,918 个合格客户 0 违反**（2026-08-27）。
   👉 **这条应该做成回归测试**（见 §验收标准）。
   ⚠ 冷审补充：测试必须断言 **gate 之前**的原始值——G2 会独立把某个窗口置 NULL，gate 后不保证单调。
2. **5 个窗口是同一条序列的 5 个后缀** → **一趟扫描并行维护 5 组累加器，不是 5 倍成本**。

---

## 🔴 已归零账户陷阱（本次调研最重要的发现）

账户爆完之后份额净值 `u` 钉死在 0，而**一条平的零线没有任何回撤**。
所以在短窗口里这些账户 **MDD = 0%** —— **死得最透的账户会排在「最稳健」的最前面**。

**实测（2026-08-27 从库）**：

- **30d 口径下 MDD < 15% 的 3,046 人里，2,728 人（90%）是全历史已归零的死账户**
- 其中 **2,909 人 30d MDD 恰好等于 0**

**必须加一道「还活着」的 gate。** 剔除后数字才合理：

| 窗口 | 达标人数 | MDD 中位数 | MDD<15% 原始 | **剔除已归零后** |
|---|---|---|---|---|
| 30d | 3,439 | 0.0% | 88.6% | **9.2%** |
| 90d | 3,395 | 0.0% | 80.1% | **6.7%** |
| 180d | 3,049 | 0.0% | 75.9% | **5.7%** |
| 365d | 2,475 | 8.1% | 50.9% | **2.9%** |
| all | 3,423 | 100.0% | 1.1% | 1.1% |

> 注意 `all` 窗口天然免疫（爆仓那一跌落在窗口内，MDD = 100%），
> **窗口越短这个陷阱越致命** —— 与 §日内盲区 一起构成「短窗口不可单独使用」的两大理由。

---

## Gate 清单（不满足显示 `—`，🔴 **绝不显示 0%**）

| # | Gate | 依据 |
|---|---|---|
| G1 | **峰值权益 ≥ $500** | 实测：不设此条，全量客户 MDD 中位数会是 **4.0%**，看起来「大家都很稳健」，其实全是**没在交易**的账户 |
| G2 | **窗口内快照天数达标**：30d≥20 / 90d≥60 / 180d≥120 / 365d≥240 / all≥90 | 样本不足的窗口给 `—` |
| G3 | **活跃交易日 ≥ 30** | 与 OPT-0020 的 Sharpe gate 对齐 |
| G4 | **「还活着」gate** | 见 §已归零账户陷阱。🟢 **2026-09-01：有现成语义可沿用** —— [[OPT-0061]] 的 `capital_locked`（`avg_daily_equity < 20% × avg_daily_balance`）已上线，配套阈值 30 活跃日 / $1,000。MDD 的「还活着」与它**方向一致但不等价**（capital_locked 说「钱被套住」，MDD 要的是「账户还没归零」），建议**在同一套状态枚举里加一个 `wiped_out` 取值**而不是另起一套。🔴 **2026-09-03 冷审（R6）：gate 状态必须按窗口存（状态枚举 × 5 窗口），不能是客户级单值**——半年前爆过、重新入金在交易的客户，30d 窗口的 MDD 是合法值，客户级单 flag 要么把它永远藏掉、要么什么都拦不住。同时裁掉原文自相矛盾（§边界情况说爆仓 MDD=100%、本表说 gate 掉显示 `—`）：**统一为 `all` 窗口显示 100%、爆仓之后的短窗口显示 `—`** |
| G5 | **起始 / 基准资本下限** | 实测名单里 uid **149035** 显示全历史收益 **+140,271%**、uid **142089** **+12,958%**，都是**起始资本极小**造成的假象。**只设 G1 峰值下限挡不住这个**（峰值是被那笔暴涨撑起来的）。✅ **2026-09-03 拍板：re-base 每段起始自有权益 ≥ $500 才产生 TWR 样本**（与 G1 的 $500 呼应），上线后看分布再调 |

> **为什么 `—` 不能写成 0%**：0% 在「挑稳健」方向上是**最好**的分数，
> 把「没数据」渲染成「最稳健」正好是本 OPT 最不能犯的错。
> schema 里对应 `Optional[float] = None`（**默认 None，不是 0**）。

---

## 实测分布与剖面分类（2026-08-27 从库）

### 合格池定义

近 365 天有交易 + 峰值权益 ≥ $500 + 活跃日 ≥ 30 → **3,918 人**。

### 剖面分类（同时有 90d 与 365d 达标样本的 **2,455 人**，阈值 15%）

| 类别 | 人数 | 占比 | 特征 |
|---|---|---|---|
| **A · 三窗口全稳健** | **19** | 0.8% | 全历史 MDD 中位 **8.3%**、爆仓次数中位 **1**、最大浮亏/余额 **0.3%** |
| **B · 365d 稳健但全历史爆过** | 1,232 | 50.2% | 峰值权益中位 $6,179 |
| **C · 最近 90d 安分、1 年内爆过** | **1,075** | 43.8% | 365d MDD 中位 **100%**、爆仓中位 **1 次**、最大浮亏/余额 **48.6%** ← **最该盯的一类** |
| **D · 最近 90d 正在出事** | 129 | 5.3% | 90d 收益中位 **−61.4%** |

> **C 类 1,075 人 = 「只看短窗口会被骗」的具体规模。**
> 这 1,075 人在 90d 上看起来很乖，365d 上是刚爆过仓的人。

### 30d 的判别力问题

**实测：30d 说稳健的 3,046 人里，365d 也稳健的只有 1,251 人（41%）—— 59% 是假阳性。**

👉 **结论**：5 列都出，但**默认排序 / 筛选走 365d 或 all**，
**30d 绝不能单独使用**；它只作为剖面的一格（D 类正是靠它抓出来的）。

### 最终名单

**A 类且全历史赚钱 = 14 人。** 商业上最有价值的两个：

| uid | 峰值权益 | 全历史 TWR | MDD_all | 交易笔数 | 隔夜日 |
|---|---|---|---|---|---|
| **144501** | $229,457 | **+14.7%** | **0.1%** | 20 | 47 |
| **146897** | $139,310 | **+56.2%** | 10.9% | — | — |

### 早期实测：365d 单窗口「筛子有效性」证据（合格池 2,545 人）

MDD<15% 的 **49 人** vs 其余 **2,496 人**：

| 指标 | MDD<15%（49 人） | 其余（2,496 人） |
|---|---|---|
| TWR 总收益中位数 | **+9.0%** | **−100.0%** |
| 盈利者占比 | **67%** | **6%** |
| 最大浮亏/余额 中位 | **0.3%** | **47.3%** |
| 交易笔数中位 | 232 | 1,212 |

同一合格池的两个背景数字：
- **87% 的客户至少爆仓过一次**
- **20.6% 出现过负权益穿仓**

---

## ⚠ 日内盲区（必须写进 tooltip 与文档的诚实性声明）

**MDD 对采样频率单调不减** —— 采样越密，能看到的谷越深。
所以日频 EOD 快照算出来的是**真实 MDD 的下界**。

| 用途方向 | 下界的含义 |
|---|---|
| 熔断 / 抓风险（已放弃的方向） | ✅ 安全（宁可低估风险） |
| **挑稳健（本 OPT 的方向）** | 🔴 **错的方向** —— 会把**日内波动大**的人标成稳健 |

**实测**：MDD<15% 的 **49 人里 26 人（53%）从不隔夜持仓** ——
他们的日终 equity ≈ 余额，**日内波动完全不可见**。

**缓和证据**：合格池整体上「从不隔夜」与「有隔夜」两组的 MDD 分布**几乎一样**（中位数都是 100%），
所以**日内交易本身不会自动产生低 MDD**——盲区是「看不见」，不是「系统性偏低」。

**建议**：
1. MDD 列旁配一列 **「隔夜持仓日占比」**（`stats_trading.hasOpenTrades=1` 的天数占比，与 OPT-0020 的过夜比例列同源）
2. 低 MDD + 从不隔夜的客户标注 **「日内盲区，需人工复核」**，**不直接进白名单**

**本期不做日内落库**：`kcm.user_account_state` 是 **30s 时点值、事后不可重算**，
要拿到日内 MDD 得**新建每小时快照任务**，成本不小 —— 留作 future。

---

## 边界情况

| 场景 | 处理 |
|---|---|
| **负权益穿仓** | `(peak − v) / peak` 会 **> 100%** → **必须 clamp 到 100%**，另出 `negative_equity` 布尔列。实测合格池 **523 / 2,545（20.6%）** 出现过。不 clamp 会出现 **MDD = 9,894.8%** 这种荒谬值 |
| **peak ≤ 0** | 该段跳过（不产生 MDD 样本） |
| **账户归零** | MDD = 100%，**单出 `wipeout` 布尔 + 归零日期**，与「−99% 但还活着」区分开。爆仓次数分布（合格池 2,545）：**0次 340 / 1次 925 / 2次 448 / 3次 245 / 4次 157 / 5+次 430** |
| **cent 账户** | `currency='CEN'` 时 `endingEquity` / `endingBalance` / `endingCredit` **三列一起 ÷100**。比率类指标免疫，但**绝对额列会差 100 倍** |
| **多账户客户** | ✅ **2026-09-03 已拍板：MAX** —— 按 loginSid 建独立序列，客户级 MDD = 名下账户 MAX（求和会稀释：一个账户爆仓、另一个躺钱 → 被平均成中等回撤）。另出 `account_count` |
| **多账户缺行** | ✅ **MAX 口径下此问题消失**（各账户序列独立、无需对齐日期）——前向填充 / `coverage%` gate **作废**。⚠ 若将来改回求和口径，此条复活 |
| **日界** | 是 **MT server 日**（`Europe/Athens`，夏 GMT+3 / 冬 GMT+2），**不是 HK 日**。与 `stats_trading.date` / `closeDate` 同界（见 memory「MT 日界是 DST 制不是固定 UTC+3」）|
| **时间窗口与页面选择器的关系** | **固定 5 列，不跟随页面顶部时间选择器**。理由：①页面本来只有 3 列随区间变（见 client-return-rate.md §2.1 的告诫框）；②MDD 扫全序列，跟随会让**缓存 key 爆炸**；③MDD 单调不减，用户拖动区间时会看到数字只增不减，**误以为数据不稳定** |

---

## 成本实测（2026-08-27，从库，全历史一趟算 5 个窗口）

| 阶段 | 耗时 |
|---|---|
| 资金流腿（全历史 `stats_transactions`） | 6 s |
| 活跃度腿（全历史 `stats_trading`） | 7 s |
| **权益曲线：服务端 GROUP BY + ORDER BY（首行前等待）** | **201 s** |
| 权益曲线：流式读取 + Python 计算 | 114 s |
| **合计** | **328 s（5.5 分钟）** |

处理 **13,167,235 客户-日 → 25,347 客户**。

> ⚠ **201 秒那段是 MySQL 的临时表排序**：`stats_balances` **没有 `(userId, date)` 顺序的索引**
> （`IDX_USERID` 是 `(date, userId)`，**date 在前**），所以必须 **filesort**。
> **这是压在从库上最重的一下**，是超时保护（见 §N）真正要防住的东西。

---

## 落地方案（三选一，**推荐 A**）

| 方案 | 做法 | 夜间成本 | 复杂度 |
|---|---|---|---|
| **A · 全量重算（推荐）** | 每晚流式扫全历史，5 窗口一趟算完 | **实测 5.5 分钟** | **低**：无状态、幂等、随时可重跑 |
| B · 增量 | 持久化每客户 unit 缓冲（最近 365 天）+ `all` 的 running peak/mdd，每晚只读昨天 | 秒级 | **高**：要一次性回填 + 状态管理；**滚动窗口的 max 无法纯增量维护** |
| C · 混合 | 4 个滚动窗口扫最近 365 天（实测 **192 s**），`all` 每周算一次 | ~3 分钟/天 | 中 |

**推荐 A 的理由**：这是**每天自动跑、没人盯着**的作业。
无状态批处理出错**重跑即可**；增量方案**状态写坏要全量回填才能修**——
在一个没人看的夜间 job 上，5.5 分钟买来的「随时可重跑」远比省下的 5 分钟值钱。

---

## 🔴 ROACE 的顺风车搭不上（重要澄清）

> **2026-09-01 修订**：OPT-0061 已经把这段 SQL 改成**两级聚合**，内层 `per_day` 子查询
> 已经是 `GROUP BY mu2.userId, sb.date` —— **正是 MDD 需要的粒度**。所以措辞从
> 「这是一个新作业」改为「**要把外层的 collapse 换成流式**」。但下面这条核心限制没变：

`client_roace_refresh_service.py` 的**外层**仍是 `GROUP BY uid` ——
**在库里聚合完，只返回约 2.5 万行**（实测 25,555 行 / 48.3 秒）。

MDD 需要**逐日峰谷**，**必须把 1,300 万行的 `per_day` 中间结果整条拉到 Python**。

> 同样是「扫 `stats_balances`」，但**网络传输和 Python 处理完全是两码事**：
> 25,555 行 vs 13,167,235 行，实测 48.3 秒 vs 328 秒。

**所以合并后的作业形态是**：保留 OPT-0061 的 `per_day` 内层（**去掉那两个毒过滤器**，见下节），
用 `SSCursor` 流式读出并按 `(uid, date)` 排序，在 Python 里**一趟同时算出**
OPT-0061 的 7 个聚合值 + 本 OPT 的 5 个窗口 MDD。
⚠ 这意味着 OPT-0061 的 7 个值要从 SQL 挪到 Python 计算 —— **它们的数值必须逐客户对账不变**，
这是合并时最大的回归风险，AC 里已列。

---

## 🔴 ROACE SQL 里三样东西不能抄（2026-09-01 复核：**一样已修，两样仍在**）

| ROACE 的写法 | 现状（09-01 复核源码） | MDD 必须 | 后果 |
|---|---|---|---|
| `INNER JOIN stats_trading st2 ON st2.loginSid = mu2.loginsid AND st2.date = sb.date` | 🔴 **仍在** `per_day` 内层 | **删掉** | 只保留交易日 = **删掉浮亏累积的持仓日**，把不相邻的日子拼成相邻，**凭空制造 / 抹掉回撤** |
| `AND sb.endingEquity > 0` | 🔴 **仍在** `per_day` 内层 | **删掉** | **直接删掉爆仓那一刻**——而那**就是**最大回撤本身 |
| `MAX_EXECUTION_TIME` | ✅ **OPT-0061 已补** `MAX_EXECUTION_TIME(300000)` hint | 沿用，但流式版耗时 328s、要重估 hint 值 | — |
| `autocommit` | 🔴 **仍未设**（`_get_mysql_connection` 只有 `connect_timeout=15` + `read_timeout`）| **补 `autocommit=True`** | 这正是 **2026-08-09 / 08-15 两次从库 MDL 事故的形状**：`autocommit=False` 时**第一条 SELECT 就开事务**、MDL 持有到连接关闭，而 `PROCESSLIST` 里显示成人畜无害的 `Sleep`。**流式版把这个窗口从 48 秒拉长到 328 秒，风险跟着放大 6.8 倍**。全仓规则见 [`db-timeout-guard`](../../../.cursor/skills/db-timeout-guard/SKILL.md) skill |

> 🔴 **前两条是本 OPT 与 OPT-0061 唯一的真冲突**：那两个过滤器对「算平均值」是**对的**
> （OPT-0061 靠它们排掉尘埃日），对「算回撤」是**致命的**。合并作业时**不能简单删掉**——
> 删了会改变 OPT-0061 三列的数值。
>
> ~~正确做法：内层不过滤，把这两个条件下推成 Python 侧逐日判定~~
> 🔴 **2026-09-03 冷审推翻（R2）：下推数学上不等价。** 两个过滤器作用在**账户-日**粒度、
> 发生在 `GROUP BY userId, date` 求和**之前**；流式拿到的是客户-日汇总，Python 无法还原
> 「剔掉一个账户的负值行、保留兄弟账户的正值行」。反例：双账户客户某天 +5,000 / −200，
> 旧 SQL 聚合用 5,000，下推方案只能用 4,800 或整天丢弃——「七列对账不变」按原方案**做不到**。
>
> **正确做法：过滤器留在 SQL 里做条件聚合。** `INNER JOIN stats_trading` 改 `LEFT JOIN`，
> 每客户-日同时输出**两套列**：
> - **过滤后**（`eq_f / bal_f / cr_f` + `day_qualifies` 标志——只聚合 `endingEquity > 0` 且当日有
>   `stats_trading` 行的账户）→ 喂 OPT-0061 的 7 个聚合值
> - **未过滤**（`eq / bal / cr`，全部账户-日）→ 喂 MDD 的 TWR 序列
>
> OPT-0061 的「日子取舍」逻辑因此与现行 SQL 完全一致，对账风险从「语义等价性」
> 降级为「SQL Decimal vs Python float 末位容差」。这是合并方案的核心设计点。

**另外两条排期 / 环境注意**：

- 🔴 MDD 作业**时间要错开 ROACE 的 06:00 HKT** —— 别让两个全表扫撞在一起。
- ⚠ `.cursor/rules/temp-primary-db.mdc` 说这页指向**主库**，但**实测 `backend/.env` 里 `MYSQL_HOST_PRIMARY` 是注释掉的**，所以实际走**从库**。**那条 rule 已过期**（本 OPT 的所有实测都是在从库上跑的）。

---

## 🧊 冷审加固清单（2026-09-03，R4/R5 落点）

| # | 问题 | 修法 |
|---|---|---|
| H1 | **快照写入无原子性**：现行代码边流式边每 2,000 行 upsert，作业在第 900 万行断掉 → **混代快照被静默端出去**。328s 作业断掉概率比 48s 放大 ~7 倍 | 写 **staging 表**，全量成功后原子换名（与 `_v2`→`_v3` 换表路线天然同构）；失败保留旧代快照 |
| H2 | **web 层不读新鲜度**：`refreshed_at` 没人查、`last_refresh_error` 写进 meta 没人读，夜间失败只留 log = 静默 | `statistics` 块带出 `roace_refreshed_at`；刷新失败接**告警邮件**（alert-mail-center 现成通道） |
| H3 | **201s filesort 物化在共享从库**：每晚在从库排 1,300 万行临时表、随表增长逐年变重——正是 08-09/08-15 MDL 事故那类长读；且 `MAX_EXECUTION_TIME` 放大到 328s+ 本身就在削弱那道防线 | **无序流出（实测 ~114s）+ 本地落盘排序**（磁盘 spool / 本地 SQLite 临时库），服务端语句变短，hint 不用放大 |
| H4 | **`net_write_timeout`（MySQL 默认 60s）**：SSCursor 消费端停下来写 SQLite 超过 60s，服务端掐连接——「流式读 + 逐批 upsert」正好是这个形状 | 读循环保持热：读线程只管拉行进队列 / spool，SQLite 写走独立线程；或整段 spool 到本地再写 |
| H5 | **末日行可能不完整**：06:00 HKT 冬令时 = MT 午夜刚过（Athens 00:00），`stats_balances` 最后一个 date 的行上游可能没写全 → 全员凭空一天「权益骤降」= 幽灵回撤 | 跑前校验最后一个 date 的行数 vs 近 7 日均值，明显偏低则丢弃末日（记 log） |
| H6 | SQLite dev/prod 共享 bind mount 双写（dev 手动刷新 vs prod 夜间作业） | 连接加 `busy_timeout`；H1 的 staging 换名覆盖最坏情况 |

> ⚪ 冷审 nice-to-have（实施时顺手，不单独立项）：
> ① 换表时把表名从 `roace_snapshot_v3` 改成 **`client_metrics_snapshot`**——这张表即将装 ROACE + 0061 + MDD（+ 可能的 Sharpe），继续叫 roace 名不副实，换表期是唯一免费改名的窗口；
> ② 流式作业的数学抽成**纯函数** `compute_client_metrics(rows) -> Metrics`（TWR 递推 / clamp / re-base / 窗口累加器全在里面），可脱库单测；
> ③ 前端 sessionStorage 缓存加显式 `schema_version` 字段，别用「探测新字段是否存在」（探测会烂）。

---

## 实现文件清单

### 新建

> **2026-09-01 修订**：原计划的三个新文件（`client_mdd_db.py` / `client_mdd_refresh_service.py` /
> 新 scheduler）**全部取消** —— [[OPT-0061]] 已把这三件建好，本 OPT 改为**扩展**。
> 新建的只剩下面这一项（可选）。

| 文件 | 内容 | 必需？ |
|---|---|---|
| `docs/features/client-return-mdd.md` | 口径 SSOT（同 `roace-return-rate.md` 的规格）| 建议 |

### 扩展现有（原「新建」的替代方案）

| 文件 | 改动 |
|---|---|
| `backend/app/core/client_roace_db.py` | `roace_snapshot_v2` **加 MDD 的列**（5 个窗口 × MDD + 窗口样本天数 + gate 状态）。⚠ **SQLite `CREATE TABLE IF NOT EXISTS` 对活库是 no-op**，加列要走迁移 —— **沿用 OPT-0061 的换表路线**（`_v2` → `_v3`，旧表留作回滚镜像），跑一次全量刷新即恢复，比写 `_migrate_add_column` 干净。⚠ `backend/data` 是 **dev/prod 共享 bind mount**，dev 一重启就会改到 prod 的库。⚠ `bulk_get` 的 **900 参数分批**（`client_roace_db.py:74`）不要动坏 |
| `backend/app/services/client_roace_refresh_service.py` | **核心改动**：外层 `GROUP BY uid` 换成 `SSCursor` 流式读 `per_day` + Python 侧分组；内层去掉两个毒过滤器并把它们下推成 Python 判定（见 §三样不能抄）；补 `autocommit=True`；重估 `MAX_EXECUTION_TIME` hint（流式版 328s vs 现在 48s）；🔴 **OPT-0061 的 7 个聚合值挪到 Python 计算后必须逐客户对账不变** |
| `backend/app/core/client_roace_scheduler.py` | 不用改（06:00 HKT 已在跑）。⚠ 但耗时从 48s 变 328s，**确认 06:00 那个窗口仍然容得下** |
| `config.py` / `main.py` | 通常不用动（沿用 `CLIENT_ROACE_SCHEDULER_ENABLED`）|

> 🟢 **「MDD 作业要错开 ROACE 的 06:00」这条作废** —— 合并成同一个作业后不存在撞车问题。
> 这也是选择合并（而非并行两个 job）的第二个理由。

### 改

| 文件 | 改动 |
|---|---|
| `backend/app/services/client_return_service.py` | ①函数签名加 `include_mdd`；②**`cache_params` 加 `include_mdd` 并 bump `client_return_v8_floating_inclusive_` → `v9_`**（`:486`，两件事**同一处**）；③`allowed_sort_columns`（`:453`）加新列——**不加则无法排序且静默 fallback**；④ROACE attach 那块后面加 MDD attach |
| `backend/app/schemas/client_return_rate.py` | `ClientReturnRateRow` 加 5 个 `Optional[float] = None`（**默认 None 不是 0**）+ 2 个布尔列 + 导出请求 schema |
| `backend/app/api/v1/routes/client_return_rate.py` | 加 Query 参数 + 透传；**加进 `refused` 元组**（`:117`，`caller_has_module(request, "risk")`，`COMMON_MAX_PAGE_SIZE = 5000`）—— ✅ **2026-09-03 已拍板：加** |
| `backend/app/services/client_return_export_service.py` | `_CSV_FIELDS`（`:43`）。⚠ **无 anti-drift 测试，漏改是静默的** |
| `frontend/src/pages/ClientReturnRate.tsx` | row interface + columnDefs（`useMemo` 依赖数组**保持 `[]`**；后端返回字段则只写 `field`、不需要 `colId`；列头用 `InfoHeader` **不用** `headerTooltip`）。⚠ **`loadCachedState()`（`:145`）要加旧-schema 探测**——这页把**整份 rows 存进 sessionStorage 且无版本号**，老用户会看到新列**空白 3 小时**。⚠ `searchHint`（`:338`）里的**硬编码耗时提示**（>180d「8-20秒」等）若耗时变化要重测 |

### 前端**不用改**（明确列出以免有人多此一举）

| 文件 | 为什么不用改 |
|---|---|
| `frontend/src/hooks/useGridColumnPersist.ts` | key 已注册（`CLIENT_RETURN_RATE_GRID_STATE_V1`，`:39`）。🔴 **不要 bump V1→V2** —— 会清掉所有人的列自定义 |
| `frontend/src/components/.../ColumnVisibilityMenu.tsx` | 通用组件，自动读 columnDefs |
| `frontend/src/lib/view-profiles/manifest.ts` | **grid key 自动派生**（见 CLAUDE.md 视图档案约定）|
| i18n | 这页列名**硬编码中文** |

### 测试

- `backend/tests/test_client_return_trading_net_deposit.py` 的 **`TestCacheVersionPinnedToTheFormula`（`:119`）会因 bump 到 `v9_` 变红** —— **这是设计意图**，同 commit 更新
- `include_mdd` 进 refusal list（✅ 已拍板），`backend/tests/test_client_return_rate_common_scope.py` 要加 case
- 🆕 **建议新增单调性回归测试**：`MDD_30d ≤ MDD_90d ≤ MDD_180d ≤ MDD_365d ≤ MDD_all`（实测 3,918 人 0 违反，是构造保证的性质，正好适合当护栏）

### 文档

- [`docs/features/client-return-rate.md`](../../features/client-return-rate.md)：§3 列表加行 + §4.4 缓存版本 + §5 API 参数 + §7 超时表
- 口径复杂，**建议单开 `docs/features/client-return-mdd.md`**（同 `roace-return-rate.md` 的规格）

---

## 假设 / 待验证

- [ ] 5.5 分钟的实测（2026-08-27）在**部署时的从库负载**下仍成立 —— 上线前重跑一次，并确认与 06:00 ROACE 无重叠
- [ ] `stats_balances` 的 2021-07-13 起点在**将来**不会被上游清理再往后推（会静默缩短 `all` 窗口的含义）
- [ ] `ib transfer to account` 在 `stats_transactions` 里的 **type 字面量**逐字确认（F2 依赖它，写错就是静默漏计）
- [ ] 单客户端到端对账：挑 **uid 144501**（A 类、MDD_all 0.1%）+ 一个已归零账户 + 一个负权益穿仓账户，三个各手算一遍与 job 输出比对
- [ ] 确认 `client_return_v8_` 前缀无外部脚本依赖，可安全 bump 到 `v9_`（同 OPT-0020 的同名假设）
- [x] ~~🆕 把 OPT-0061 的 7 个聚合值从 SQL 挪到 Python 后数值不变~~ —— ✅ **随第 8 题拍板方案 B 作废**（老查询一字不动，不存在挪动）
- [ ] 🆕 **两条查询顺序跑的总时长塞进 06:00 窗口，不与其它夜间任务撞车** —— MAX 口径 + H3 本地排序后重新实测（08-27 的 328s 是求和口径 + 服务端 filesort）
- [ ] 🆕 🔴 **`stats_transactions` 能按 `(loginSid, date)` 聚合** —— MAX 口径要求 `F_t` 逐账户；若该表只有 userId 粒度，第 1 题的 MAX 实现受阻，要回头找账户级资金流源（08-27 实测是按 `(userId, date)` 聚合的，没验证过账户粒度）
- [ ] 🆕 🔴 **按 MAX 口径重跑 08-27 实测分布** —— 文档内 49 人 / A 类 14 人 / 剖面四分类等全部数字是「客户级求和」口径，MAX 口径下会变（预期变严：任一账户爆仓即客户级 MDD=100%）
- [ ] 🆕 `roace_snapshot_v2` → `_v3` 换表期间 **dev/prod 共享 bind mount** 不会互相踩（同 OPT-0061 的预填做法：merge 前先填好）

---

## 验收标准

> ✅ **2026-09-03 开放问题全部拍板完毕**（见顶部拍板表：1=MAX · 3=$500 · 4=进 refused · 5=计入 · 6/7=都不 · 8=方案 B），
> 本 OPT 升级 **ready**，可 claim。

### Drop 1 — 夜间批处理作业（后端，无前端改动）

> **2026-09-01 改写**：不再新建作业，改为**扩展 [[OPT-0061]] 已上线的那一个**。

- [ ] `client_roace_db.py`：`roace_snapshot_v2` → **`_v3`**（沿用 OPT-0061 的换表路线，旧表留作回滚镜像），加 `mdd_30d`…`mdd_all` / 各窗口样本天数 / gate 状态枚举 / `negative_equity` / `account_count` / `coverage_pct`
- [ ] `client_roace_refresh_service.py` 按**方案 B** 扩展（✅ 2026-09-03 拍板第 8 题）：
  - [ ] **现有 48s 聚合查询与 OPT-0061 的 7 个聚合值一字不动**（原「7 值挪 Python + 逐客户对账」AC 作废——没动就无需对账）
  - [ ] 其后**追加**第二条 MDD 流式查询：直接读 `stats_balances` 账户-日原始行（MAX 口径下**无需 GROUP BY**），**不带** `INNER JOIN stats_trading` / `endingEquity > 0` 两个过滤器（R2 的双列集方案随 B 作废），`SSCursor` 流式 + **本地排序**（H3），按 **loginSid** 分组进 Python，emit 后丢弃、**峰值内存与账户数无关**
  - [ ] 补 `autocommit=True`（🔴 **OPT-0061 仍未设**，两条查询共用连接工厂顺手一起补）；H3 本地排序后服务端语句变短、`MAX_EXECUTION_TIME` hint 不用放大；保持 `MAX_EXECUTION_TIME < read_timeout`
  - [ ] TWR 递推（🔴 按 R1 修正版：净入金日分母含 `F_t`）+ 5 组窗口累加器 + clamp + **三条件 re-base**；`F_t` 按 **`(loginSid, date)`** 聚合且**含 transfer in/out**（✅ 第 5 题）
  - [ ] MDD clamp 到 100%；**客户级 MDD = 名下账户 MAX**（✅ 第 1 题），另出 `account_count`
  - [ ] 5 条 gate（G1–G5）全部实现（G5 = re-base 段起始自有权益 ≥ $500，✅ 第 3 题；gate 状态**按窗口存**，R6），不满足写 **NULL**
  - [ ] 🆕 冷审加固 H1–H6：staging 表原子换名 / `statistics` 带 `refreshed_at` + 失败接告警邮件 / 本地排序替代服务端 filesort / 读写分离防 `net_write_timeout` / 末日完整性校验 / SQLite `busy_timeout`
- [ ] gate 表达从 `capital_locked` 布尔换成**状态枚举**（OPT-0061 Follow-up 6 的前置动作），加 `wiped_out` 取值
- [ ] scheduler **不用改**（06:00 HKT 已在跑）；确认该时段容得下两条查询顺序总时长（48s + 流式段；MAX 口径 + H3 本地排序后耗时要**重新实测**，08-27 的 328s 是求和口径 + 服务端 filesort 的数字）
- [ ] 手动触发端点复用现有 `POST /client-return-rate/roace/refresh`
- [ ] 实跑一次全量，耗时 **≤ 8 分钟**（实测基线 5.5 分钟 + 余量），从库无 MDL 告警

### Drop 2 — 页面接入（后端 web 层 + 前端）

- [ ] `client_return_service.py`：`include_mdd` 参数 + **cache prefix bump `v8_` → `v9_`** + `allowed_sort_columns` 加 5 列 + attach
- [ ] `schemas/client_return_rate.py`：5 个 `Optional[float] = None` + `wipeout` / `negative_equity` 布尔
- [ ] `routes/client_return_rate.py`：Query 参数 + 透传 + 加进 `refused`（✅ 已拍板）
- [ ] `client_return_export_service.py`：`_CSV_FIELDS` 加新列（**手动核对，无护栏**）
- [ ] `ClientReturnRate.tsx`：5 列 + 2 个布尔标记；`—` 渲染（**绝不 0%**）；列头 `InfoHeader` tooltip 至少写明：**「2021-07 起」「日频 EOD 是下界，日内不可见」「窗口固定，不随上方时间选择器变」**
- [ ] `loadCachedState()` 加显式 `schema_version` 字段守卫（不匹配则丢弃缓存；冷审 ⚪③：字段探测会烂），**否则老用户空白 3 小时**
- [ ] **默认排序 / 默认筛选走 365d 或 all**，30d 不做默认

### 冒烟 / 回归

- [ ] **单调性回归测试**：随机抽 N 个客户断言 `MDD_30d ≤ 90d ≤ 180d ≤ 365d ≤ all`（断言 **gate 之前**的原始值——G2 会独立 NULL 掉窗口）
- [ ] `TestCacheVersionPinnedToTheFormula` 同 commit 更新为 `v9_`
- [ ] 三个对账客户（uid 144501 / 一个已归零 / 一个负权益）手算比对，误差 < 0.1pp
- [ ] 已归零账户在页面上**不出现在低 MDD 榜首**（G4 生效的直接验证）
- [ ] 未设 gate 的客户返回 `—`，CSV 导出为空值而非 0
- [ ] 🆕 **OPT-0061 三列对账**（✅ 方案 B 后降级为冒烟抽查）：老查询未动，理论上零差异——抽 100 客户比对 `return_with_floating` / `floating_burden_ratio` / `capital_locked` 确认即可，不再需要全量逐客户对账
- [ ] 🆕 OPT-0061 的护栏测试 `test_client_return_floating_inclusive.py`（21 用例）**全绿且断言零改动**（✅ 方案 B 后老 SQL 没动，任何一条要改断言 = 说明动了不该动的东西，停下来查）
- [ ] `./verify.sh` 绿（tsc + vitest + pytest）

---

## 笔记

### 架构图

**2026-09-01 重画** —— 不再是两条并行流水线，而是**把现有那一条从「库内聚合」改成「流式」**：

> ⚠ **2026-09-03**：本图按第 8 题的**方案 A**（完全合并）画。若拍板 **B**（推荐），③④ 改为
> 「老聚合查询原样保留 → 流式查询只算 MDD（+ 将来的 Sharpe）」，其余不变。

```
                    ┌──────────────────────────────────────┐
                    │ APScheduler 06:00 HKT（现有，不改）  │
                    │ client_roace_scheduler               │
                    └──────────────────┬───────────────────┘
                                       ▼
    ┌───────────────────────────────────────────────────────────────────┐
    │ client_roace_refresh_service  ← 本 OPT 的核心改动都在这一个文件里 │
    │                                                                   │
    │  ① 资金流腿  stats_transactions        6s   ← 本 OPT 新增         │
    │  ② 活跃度腿  stats_trading             7s   ← 本 OPT 新增         │
    │  ③ per_day 内层（OPT-0061 已建）                                  │
    │     GROUP BY userId, date → eq / bal / cr（含 CEN 折算）          │
    │     🔴 去掉 INNER JOIN stats_trading + endingEquity > 0           │
    │        （下推成 Python 侧逐日判定，见 §三样不能抄）               │
    │                                                                   │
    │     ✂ 外层 GROUP BY uid 撤掉 ──► SSCursor 流式 ORDER BY uid, d    │
    │        201s(filesort) + 114s(流式) = 13,167,235 行进 Python       │
    │                                                                   │
    │  ④ Python 侧按 uid 分组，组内一趟同时算：                          │
    │     • OPT-0061 的 7 个聚合值（🔴 必须对账不变）                   │
    │     • 本 OPT 的 TWR unit 序列 → MDD × 5 窗口                       │
    │     • （若第 6 题选 yes）OPT-0020 的 Sharpe ×3 / Consistency ×3   │
    │     emit 后丢弃 → 峰值内存与客户数无关                             │
    └──────────────────────────────┬────────────────────────────────────┘
                                   ▼
                 ┌──────────────────────────────────────┐
                 │ SQLite client_roace.db               │
                 │  roace_snapshot_v2 ──► _v3（加列）   │
                 │  （_v2 保留作回滚镜像，同 0061 做法）│
                 └──────────────────┬───────────────────┘
                                    │ bulk_get（900 参数分批）
                                    ▼
                 ┌──────────────────────────────────────┐
                 │ Web API Phase 2（cache v8_ → v9_）   │
                 │  Python 端拼接 ROACE + 0061 + MDD 列 │
                 └──────────────────────────────────────┘
```

> **一个作业、一次扫描、一张快照表。** 这同时解决了三件事：
> ①「MDD 作业要错开 06:00」的排期问题消失；
> ② OPT-0061 Follow-up 6 预告的「刷新 SQL 被流式骨架替换」在此兑现；
> ③ 若第 6 题选 yes，OPT-0020 的 Sharpe/Cons 直接插进 ④ 那一步，**不需要再改一次作业**。
>
> 代价是**这个作业变成三个 OPT 的共同关键路径** —— 改它要同时对账三批列。

### ✅ 待用户拍板的开放问题（2026-09-03 全部拍板完毕）

> ✅ **拍板结果**：1=**MAX** · 3=**$500** · 4=**进 refused** · 5=**计入** · 6=**不并** · 7=**不加** · 8=**方案 B**。
> 汇总表见文档顶部「2026-09-03 用户拍板」；下表保留原始论证不再逐行改。

> **2026-09-01 收敛**：原 7 条 → **5 条**。
> 第 **2** 题（「还活着」gate）降级为「沿用 [[OPT-0061]] 的状态枚举 + 加 `wiped_out` 取值」，不再是开放设计题。
> 第 **6** 题（是否与 OPT-0020 合并）范围扩大 —— 现在是**三方合并**（0020 Sharpe/Cons + 0060 MDD + 0061 已上线的 7 个聚合值），
> 而且 OPT-0061 的 Follow-up 6 已经**预先同意**了合并，所以这题实际只剩「OPT-0020 要不要同时并进来」。
>
> **2026-09-03 冷审再修订**：新增第 **8** 题（合并形态 A/B/C，R7）；第 **1 / 3 / 5** 题升级为
> 🔴 **算法阻塞级**（R3）——它们改变流式分组键 / TWR 样本资格 / `F_t` 定义本身，不是 scope 微调。
> ⚠ 且 **08-27 的全部实测分布是「客户级求和」口径**：第 1 题若拍板 MAX，49 人 / A 类 14 人等数字要重跑。
> 共 **6 条**待拍板。

| # | 问题 | 推荐答案 |
|---|---|---|
| **1** 🔴阻塞 | **多账户客户**：各账户 MDD 取 **MAX**（风控视角）还是 equity **求和**（总资产视角）？ | **MAX** —— 求和会稀释（一个爆仓 + 一个躺钱 = 中等回撤，掩盖了真实风险）。但若老板要的是「客户总资产的回撤」则用求和法，**两者不是同一个问题**。⚠ 冷审（R3）：MAX 要求按 **loginSid** 分组流式（更大的流、不同 GROUP BY），且 08-27 实测分布全是求和口径、拍 MAX 要重跑；本题与第 5 题耦合 |
| ~~2~~ | ~~**「还活着」gate 的具体定义**？~~ | 🟢 **2026-09-01 已收敛**：沿用 [[OPT-0061]] 的 gate 状态枚举，加一个 `wiped_out` 取值（`当前权益 < 峰值 × 5%`，与本 OPT 实测用的爆仓判定同阈值）。仍需实施者在 scope 时确认那 5% 与 OPT-0061 的 20% 阈值并存是否会让前端文案含混 |
| **3** 🔴阻塞 | **起始 / 基准资本下限**设多少？ | 建议：**re-base 每段起始自有权益 ≥ $500 才产生 TWR 样本**（与 G1 峰值 $500 呼应），上线跑一遍分布再调。要挡住 uid 149035（+140,271%）/ 142089（+12,958%）这类**起始资本极小**的假象 |
| **4** | `include_mdd` 要不要进**模块闸的 refusal list**（无 `risk` 模块即 403）？ | **要** —— MDD 是重列（夜间 5.5 分钟算出来的），且属风控判断信号 |
| **5** 🔴阻塞 | `transfer in` / `transfer out`（账户间互转，各约 3 万笔）要不要计入 `F_t`？ | **跟随第 1 题**（R3）：拍 **MAX**（账户级序列）→ **必须计入**（对单个账户就是外部资金流）；拍**求和**（客户级）→ 同 sid 域内互转在客户-日层面自然抵消，**可不计**（只剩跨 sid 域转账残噪） |
| **6** | **合并范围**：本 OPT 与 [[OPT-0061]] 的作业合并**已是既定路线**（其 Follow-up 6 预先同意）。剩下的问题是 **[[OPT-0020]]（Sharpe ×3 + Consistency ×3）要不要同时并进来**？ | **推荐一起并** —— 三者共用同一条 1,300 万行序列。分批做意味着这个作业要被改写两次，每次都要重跑一次全量对账。若 OPT-0020 的口径还没想清楚，则**至少把流式骨架设计成能容纳它**（按 userId 分组、组内多指标、emit 后丢弃）。⚠ 冷审后与第 8 题耦合：选 B/C 后老查询不再被改写，将来加 0020 只是扩流式段的纯函数——「作业被改写两次」的顾虑大幅减轻，**本期不并、骨架预留**变得更可取 |
| **7** | **Calmar Ratio 要不要一并加**？ | 可加，但**必须提前告知老板**：gate 之后预计**只有 20–25% 的客户有值**，「这一列大部分是 `—`」 |
| **8** 🆕 | **合并形态**（R7，冷审新增）：**A** 完全合并成一条流式查询（原方案，架构图按它画）/ **B** 同一个作业里**顺序跑两条查询**——老的 48s 聚合查询一字不动，后面接新的流式 MDD 查询 / **C** 完全独立两个作业错峰 | **B** —— OPT-0061 七列对账风险**直接归零**（老查询没动）、排期撞车消失（同作业顺序执行）、爆炸半径隔离（MDD 段挂掉不影响已上线三列的刷新，前提是分段写）；代价只是从库顺序扫两遍（48s + ~114s 流式，错峰无叠加）。A 的「一次扫描」收益不值它带来的对账 AC；C 引入两个作业的调度协调，比 B 没有额外好处。⚠ 选 B/C 则 §架构图 的 ③④ 形态相应改变，且 OPT-0061「7 值挪 Python 对账」那批 AC 作废 |

### Future（本 OPT 不做）

- **日内 MDD**：需新建每小时权益快照任务（`kcm.user_account_state` 是 30s 时点值、事后不可重算）。做了才能消除 §日内盲区
- **给 `stats_balances` 加 `(userId, date)` 索引**：能干掉那 201 秒 filesort，但那是**从库上一张 19.8M 行 / 5.3GB 的表**，加索引要走 DBA —— 先看 A 方案的 5.5 分钟够不够用
- **方案 B / C（增量 / 混合）**：A 方案跑一段时间、真实耗时确认后再评估是否值得换
- **MDD 进告警邮件 / watchlist**：稳健名单（A 类 14 人）本身有商业价值，可考虑做成周报

---

## 结果

（done/dropped 时填）
