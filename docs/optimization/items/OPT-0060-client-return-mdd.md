---
id: OPT-0060
title: Client Return Rate 加 Max Drawdown(MDD)5 个窗口列 —— TWR 口径 + 夜间全量批处理
status: idea
priority: P2
area: mixed
effort: XL
created: 2026-08-28
related: [[OPT-0020]], [[OPT-0006]]
---

> **归类说明（用户拍板）**：按 [`README.md` §什么进 tracker](../README.md#什么进-tracker--什么不进) 的规则，
> 「新列」属于 net-new feature，本该走普通 `feat/<slug>` branch 而不进 tracker。
> **本条是用户明确要求开 OPT 的例外**——记录在此以免未来 reader 误以为规则被推翻。
> 归类之外的流程（claim 纪律 / 执行隔离铁律 / 双 hook）照 README 正常走。

> **effort 说明**：用户初估 `L`。按下面 §实现文件清单（新建 3 个后端文件 + 改 6 个文件 + 2 篇文档）
> 与 §待用户拍板的开放问题（7 条，其中 3 条会改变算法本身），实际是**多天**工作量，故记 `XL`。
> 若 §待拍板问题在 scope 阶段全部收敛、且 Calmar / 隔夜比例列砍掉，可以降回 `L`。

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

**未决**：`transfer in` / `transfer out`（账户间互转，各约 3 万笔）目前**未计入** `F_t`，
会在多账户客户上留噪声 —— 见 §待用户拍板 第 5 题。

---

## 算法

### 伪代码（逐字实现）

```
按 (userId, date) 升序流式扫描全历史；每个客户维护一条 unit 序列 + 5 组窗口累加器

own_t   = endingEquity_t − endingCredit_t          # 客户级自有权益（多账户求和）
F_t     = 当日外部资金净流入(deposit + withdrawal + ib transfer to account)
ret_t   = (own_t − F_t) / own_{t-1}                # own_{t-1} > 0 时
u_t     = u_{t-1} × max(ret_t, 0)                  # ret<0 视为归零，clamp 到 0
                                                   # own_{t-1} <= 0 时序列 re-base（爆仓后再入金）

窗口 = 锚定后缀（到今天为止的最近 N 天），W ∈ {30d, 90d, 180d, 365d, all}
对每个 W:
    首次进入窗口时 peak_W = u_t          # 窗口内自己的局部峰值，不用全局峰值
    peak_W = max(peak_W, u_t)
    mdd_W  = max(mdd_W, (peak_W − u_t) / peak_W)
```

### 两条关键性质

1. **单调性由构造保证**：因为每个窗口用**自己的局部峰值**（而不是全局峰值），
   `MDD_30d ≤ MDD_90d ≤ MDD_180d ≤ MDD_365d ≤ MDD_all` 恒成立。
   **实测 3,918 个合格客户 0 违反**（2026-08-27）。
   👉 **这条应该做成回归测试**（见 §验收标准）。
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
| G4 | **「还活着」gate** | 见 §已归零账户陷阱。**具体定义待拍板**（第 2 题）|
| G5 | **起始 / 基准资本下限** | 实测名单里 uid **149035** 显示全历史收益 **+140,271%**、uid **142089** **+12,958%**，都是**起始资本极小**造成的假象。**只设 G1 峰值下限挡不住这个**（峰值是被那笔暴涨撑起来的）。阈值待拍板（第 3 题）|

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
| **多账户客户** | 客户级 MDD 建议取名下各账户 MDD 的 **MAX**（直接把 equity 相加会**稀释**：一个账户爆仓、另一个躺钱 → 被平均成中等回撤）。另出 `account_count`。🔴 **这条需要用户拍板**（第 1 题）——若他要的是「客户总资产的回撤」则用求和法 |
| **多账户缺行** | 某天某账户没有行 → 客户级 equity **凭空掉一截 → 幽灵回撤**。需**前向填充**或记录 **coverage%**，`coverage < 80%` 显示 `—` |
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

`client_roace_refresh_service.py` 的 SQL 是 `GROUP BY mu2.userId` ——
**在库里聚合完，只返回约 3 万行**。

MDD 需要**逐日峰谷**，**必须把 1,300 万行整条序列拉到 Python**。

> 同样是「扫 `stats_balances`」，但**网络传输和 Python 处理完全是两码事**。
> **这是一个新的批处理作业，不是在旧作业上加一行 SQL。**

---

## 🔴 ROACE SQL 里三样东西一条都不能抄（已逐条核对源码）

| ROACE 的写法 | MDD 必须 | 后果 |
|---|---|---|
| `INNER JOIN stats_trading st2 ON st2.loginSid = mu2.loginsid AND st2.date = sb.date` | **删掉** | 只保留交易日 = **删掉浮亏累积的持仓日**，把不相邻的日子拼成相邻，**凭空制造 / 抹掉回撤** |
| `AND sb.endingEquity > 0` | **删掉** | **直接删掉爆仓那一刻**——而那**就是**最大回撤本身 |
| 连接函数：无 `MAX_EXECUTION_TIME`、`read_timeout=600`、**未设 `autocommit`** | **三条都补**：`autocommit=True` + `MAX_EXECUTION_TIME < read_timeout` | 这正是 **2026-08-09 / 08-15 两次从库 MDL 事故的形状**：`autocommit=False` 时**第一条 SELECT 就开事务**、MDL 持有到连接关闭，而 `PROCESSLIST` 里显示成人畜无害的 `Sleep`。全仓规则见 [`db-timeout-guard`](../../../.cursor/skills/db-timeout-guard/SKILL.md) skill |

**另外两条排期 / 环境注意**：

- 🔴 MDD 作业**时间要错开 ROACE 的 06:00 HKT** —— 别让两个全表扫撞在一起。
- ⚠ `.cursor/rules/temp-primary-db.mdc` 说这页指向**主库**，但**实测 `backend/.env` 里 `MYSQL_HOST_PRIMARY` 是注释掉的**，所以实际走**从库**。**那条 rule 已过期**（本 OPT 的所有实测都是在从库上跑的）。

---

## 实现文件清单

### 新建

| 文件 | 内容 |
|---|---|
| `backend/app/core/client_mdd_db.py` | 抄 `client_roace_db.py`。⚠ 注意 `bulk_get` 的 **900 参数分批**（`client_roace_db.py:74`）|
| `backend/app/services/client_mdd_refresh_service.py` | 流式扫描 + 5 窗口累加器 + gate 判定 + 批量 upsert |
| scheduler 一步 | 可挂进现有 `client_roace_scheduler.py` **或**新建 —— 但**必须错开 06:00** |
| `config.py` env 开关 + `main.py` lifespan | 与 `CLIENT_ROACE_SCHEDULER_ENABLED` 同模式 |

### 改

| 文件 | 改动 |
|---|---|
| `backend/app/services/client_return_service.py` | ①函数签名加 `include_mdd`；②**`cache_params` 加 `include_mdd` 并 bump `client_return_v8_floating_inclusive_` → `v9_`**（`:486`，两件事**同一处**）；③`allowed_sort_columns`（`:453`）加新列——**不加则无法排序且静默 fallback**；④ROACE attach 那块后面加 MDD attach |
| `backend/app/schemas/client_return_rate.py` | `ClientReturnRateRow` 加 5 个 `Optional[float] = None`（**默认 None 不是 0**）+ 2 个布尔列 + 导出请求 schema |
| `backend/app/api/v1/routes/client_return_rate.py` | 加 Query 参数 + 透传；**决定是否加进 `refused` 元组**（`:117`，`caller_has_module(request, "risk")`，`COMMON_MAX_PAGE_SIZE = 5000`）—— **MDD 是重列，倾向加**（第 4 题）|
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
- 若 `include_mdd` 进 refusal list，`backend/tests/test_client_return_rate_common_scope.py` 要加 case
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

---

## 验收标准

> 🔴 **开工前必须先过 §待用户拍板的开放问题**（7 条，其中第 1/2/3 题会改变算法本身）。
> 未拍板就实施 = 做完要返工。

### Drop 1 — 夜间批处理作业（后端，无前端改动）

- [ ] `client_mdd_db.py`：SQLite 表 `mdd_snapshot`（user_id PK / `mdd_30d`…`mdd_all` / `wipeout` / `negative_equity` / `account_count` / `coverage_pct` / `refreshed_at`）+ `bulk_get_mdd()` / `upsert_mdd_batch()`（900 参数分批）+ `PRAGMA journal_mode=WAL`
- [ ] `client_mdd_refresh_service.py`：
  - [ ] 三条腿 SQL（资金流 / 活跃度 / 权益曲线），权益曲线用 `SSCursor` **流式**读，**峰值内存可控**
  - [ ] 连接函数三道防线：`autocommit=True` + `MAX_EXECUTION_TIME` + `read_timeout`（且 `MAX_EXECUTION_TIME < read_timeout`）
  - [ ] **不抄** ROACE 的 `INNER JOIN stats_trading` 与 `endingEquity > 0`
  - [ ] TWR 递推 + 5 组窗口累加器 + `max(ret,0)` clamp + `own_{t-1} <= 0` re-base
  - [ ] MDD clamp 到 100%
  - [ ] 5 条 gate（G1–G5）全部实现，不满足写 **NULL**
- [ ] scheduler 挂一步，**时间错开 06:00**；env 开关默认 dev 关 / prod 开
- [ ] 手动触发端点（同 `POST /client-return-rate/roace/refresh` 的形状），返回 `rows_written` / `duration_ms`
- [ ] 实跑一次全量，耗时 **≤ 8 分钟**（实测基线 5.5 分钟 + 余量），从库无 MDL 告警

### Drop 2 — 页面接入（后端 web 层 + 前端）

- [ ] `client_return_service.py`：`include_mdd` 参数 + **cache prefix bump `v8_` → `v9_`** + `allowed_sort_columns` 加 5 列 + attach
- [ ] `schemas/client_return_rate.py`：5 个 `Optional[float] = None` + `wipeout` / `negative_equity` 布尔
- [ ] `routes/client_return_rate.py`：Query 参数 + 透传 + （按第 4 题结论）加进 `refused`
- [ ] `client_return_export_service.py`：`_CSV_FIELDS` 加新列（**手动核对，无护栏**）
- [ ] `ClientReturnRate.tsx`：5 列 + 2 个布尔标记；`—` 渲染（**绝不 0%**）；列头 `InfoHeader` tooltip 至少写明：**「2021-07 起」「日频 EOD 是下界，日内不可见」「窗口固定，不随上方时间选择器变」**
- [ ] `loadCachedState()` 加旧-schema 探测（缺新字段则丢弃缓存），**否则老用户空白 3 小时**
- [ ] **默认排序 / 默认筛选走 365d 或 all**，30d 不做默认

### 冒烟 / 回归

- [ ] **单调性回归测试**：随机抽 N 个客户断言 `MDD_30d ≤ 90d ≤ 180d ≤ 365d ≤ all`
- [ ] `TestCacheVersionPinnedToTheFormula` 同 commit 更新为 `v9_`
- [ ] 三个对账客户（uid 144501 / 一个已归零 / 一个负权益）手算比对，误差 < 0.1pp
- [ ] 已归零账户在页面上**不出现在低 MDD 榜首**（G4 生效的直接验证）
- [ ] 未设 gate 的客户返回 `—`，CSV 导出为空值而非 0
- [ ] `./verify.sh` 绿（tsc + vitest + pytest）

---

## 笔记

### 架构图

```
        ┌────────────────────────────┐   ┌────────────────────────────┐
        │ APScheduler 06:00 HKT      │   │ APScheduler ??:?? HKT      │
        │ client_roace_scheduler     │   │ client_mdd_scheduler（新） │
        │ （现有，GROUP BY → 3万行） │   │ 🔴 必须错开 06:00          │
        └─────────────┬──────────────┘   └─────────────┬──────────────┘
                      │                                │
                      ▼                                ▼
        ┌──────────────────────────┐   ┌─────────────────────────────────────┐
        │ client_roace_refresh_svc │   │ client_mdd_refresh_service（新）    │
        │  库内聚合，只回 3 万行   │   │  ① 资金流腿   6s                    │
        └─────────────┬────────────┘   │  ② 活跃度腿   7s                    │
                      │                │  ③ 权益曲线 201s(filesort)+114s(流) │
                      │                │  → 1,300 万行拉进 Python 逐日峰谷   │
                      │                └─────────────┬───────────────────────┘
                      ▼                              ▼
        ┌──────────────────────────┐   ┌──────────────────────────┐
        │ SQLite client_roace.db   │   │ SQLite client_mdd.db（新）│
        │  - roace_snapshot         │   │  - mdd_snapshot           │
        └─────────────┬────────────┘   └─────────────┬────────────┘
                      │ bulk_get                     │ bulk_get
                      └──────────────┬───────────────┘
                                     ▼
                    ┌────────────────────────────────────┐
                    │ Web API Phase 2（cache v8_ → v9_） │
                    │  Python 端拼接 ROACE + MDD 列      │
                    └────────────────────────────────────┘
```

> **若与 OPT-0020 合并做**（第 6 题选 yes），右侧那条流水线同时产出
> Sharpe ×3 / Consistency ×3 / MDD ×5，**共用同一次 1,300 万行扫描**，
> SQLite 也合成一张表 —— 这是本 OPT 与 OPT-0020 关系的落地形态。

### 🔴 待用户拍板的开放问题（claim 前必须全部收敛）

| # | 问题 | 推荐答案 |
|---|---|---|
| **1** | **多账户客户**：各账户 MDD 取 **MAX**（风控视角）还是 equity **求和**（总资产视角）？ | **MAX** —— 求和会稀释（一个爆仓 + 一个躺钱 = 中等回撤，掩盖了真实风险）。但若老板要的是「客户总资产的回撤」则用求和法，**两者不是同一个问题** |
| **2** | **「还活着」gate 的具体定义**？ | 推荐：**当前权益 ≥ 峰值的某个比例**，或 **近 N 天有交易 + 当前权益 ≥ $500**。这一条直接决定 §已归零账户陷阱 的表格从 88.6% 收到 9.2% 的那个动作 |
| **3** | **起始 / 基准资本下限**设多少？ | 待定。要挡住 uid 149035（+140,271%）/ 142089（+12,958%）这类**起始资本极小**的假象 |
| **4** | `include_mdd` 要不要进**模块闸的 refusal list**（无 `risk` 模块即 403）？ | **要** —— MDD 是重列（夜间 5.5 分钟算出来的），且属风控判断信号 |
| **5** | `transfer in` / `transfer out`（账户间互转，各约 3 万笔）要不要计入 `F_t`？ | 待定。**不计入会在多账户客户上留噪声**；计入则要小心同一客户内部对冲的双边重复 |
| **6** | **是否与 [[OPT-0020]] 合并做**？ | **推荐合并** —— 共用同一条序列扫描，**分开做等于扫两遍 1,300 万行** |
| **7** | **Calmar Ratio 要不要一并加**？ | 可加，但**必须提前告知老板**：gate 之后预计**只有 20–25% 的客户有值**，「这一列大部分是 `—`」 |

### Future（本 OPT 不做）

- **日内 MDD**：需新建每小时权益快照任务（`kcm.user_account_state` 是 30s 时点值、事后不可重算）。做了才能消除 §日内盲区
- **给 `stats_balances` 加 `(userId, date)` 索引**：能干掉那 201 秒 filesort，但那是**从库上一张 19.8M 行 / 5.3GB 的表**，加索引要走 DBA —— 先看 A 方案的 5.5 分钟够不够用
- **方案 B / C（增量 / 混合）**：A 方案跑一段时间、真实耗时确认后再评估是否值得换
- **MDD 进告警邮件 / watchlist**：稳健名单（A 类 14 人）本身有商业价值，可考虑做成周报

---

## 结果

（done/dropped 时填）
