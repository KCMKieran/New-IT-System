---
id: OPT-0024
title: Risk-monitor 4 个 tab 加「佣金试算」列（CN 账户 D03 公式）
status: done
priority: P2
area: frontend
effort: M
created: 2026-05-21
claimed: 2026-05-21
completed: 2026-05-21
branch: opt/risk-monitor-est-commission
---

## 问题

`/risk-monitor` 的 quick-open-close / quick-profit / hedge-open / gap-trade
四个 tab 当前展示客户 profit，但**未扣除 broker 实际支付的代理佣金**。

KCM 是 B-book CFD 券商：「客户 profit」≠「公司净亏损」。代理佣金
（External + Internal + Dark Points）是真实成本。完整佣金表在 CRM 里太大、
查询困难，需要用 `KCM_Daily_Report/Azure_Function_BAU/D03_daily_report.py`
里的公式当场粗略试算。

burst-open tab **不加** —— burst 是开仓告警、无平仓记录，谈不上佣金。

## 背景

- 公式参考：`/opt/myproject/KCM_Daily_Report/Azure_Function_BAU/D03_daily_report.py`
- D04 cent 公式（VN/TH）暂不实现 → 非 CN 账户显示 null
- CN 判定：`group.startsWith('KCMc')`（与 D03 源逻辑一致）

### 公式（D03 严格复刻）

每行 commission = External + Internal + Dark Points

**External Commission**（代理 IB payout）
```
extract_number(group) × lots
extract_number: group.indexOf('c')+1 到 group.indexOf('_') 之间的整数
  e.g. "KCMc60_xxxx" → 60
```

**Internal Commission**（broker 内部 markup）
```
(fixed_fee + symbol_fee) × lots
fixed_fee = 2.5（恒定）
symbol_fee 按表：
  XAU* / XAU-CNH         → 20
  XAGUSD                 → 40
  XTIUSD / USOIL* / DXUSD* / GOLD* → 23
  GBPUSD/EURUSD/USDJPY/AUDUSD/NZDUSD/USDCAD/USDCHF → 10
  match /^[A-Za-z]+\d+$/（股指如 US100, FA40）→ 18
  其他                   → 12
```

**Dark Points**（隐性点差）
```
1. 调整 lots：US100/AUS200/FA40 ×2、XAGUSD/US500 ×5、EU50 ×3、其他 ×1
2. 若 group 不以 'A' 开头但含 'A' → count('A') × 10 × adjLots
3. 若 group 含 'P' → P 后两位数字 × adjLots
4. 都不匹配 → 0
```

### 实施面（7 处）

| Tab | 子视图 | 行结构 | 处理方式 |
|---|---|---|---|
| quick-open-close | 主表 | `AlertEvent` | `estimateCommission(symbol, total_lots, group)` |
| quick-profit | 主表 | `AlertEvent` | 同上 |
| hedge-open | 明细 | `AlertEvent` | 同上 |
| hedge-open | 聚合 | `HedgeOpenAggregatedRow` | 用 `symbols.split(',')[0].trim()` 当主 symbol，乘 `total_lots`，group 用聚合 group |
| gap-trade | SO+AB（rule 71） | `AlertEvent` 双腿 | L 腿 `(l_lots, l_groupsid)` + C 腿 `(c_lots, c_groupsid)` 分别算后 sum |
| gap-trade | 客户对汇总 | `ClientPairAggRow` | **显示 `—`**（没有可用的 total_lots 字段，跨多笔订单） |
| gap-trade | Gap 主表（rule 81） | `AlertEvent` 客户级 | **显示 `—`**（客户级聚合 symbols 多个 + 无客户级 lots 总和） |

## 验收标准

### 公式与库
- [ ] 新增 `frontend/src/lib/commission.ts`，导出：
  - `extractExternalRate(group: string | null): number`
  - `getSymbolFee(symbol: string | null): number`
  - `calcInternalFee(symbol, lots): number`
  - `calcDarkPoints(symbol, lots, group): number`
  - `estimateCommission(symbol, lots, group): number | null`
  - `isCNGroup(group: string | null): boolean`
- [ ] 非 CN group（不以 `KCMc` 开头）→ `estimateCommission` 返回 `null`
- [ ] group / symbol / lots 任一为 null/空 → 返回 `null`

### 列添加（7 处）
- [ ] colId: `est_commission`
- [ ] 列名：「佣金试算」
- [ ] 默认可见
- [ ] 列头加 ℹ tooltip：`基于 KCM_Daily_Report D03 公式粗略试算（External + Internal + Dark Points）。仅 CN 账户（KCMc 组）计算，其他显示 —。多 symbol 行用主 symbol 近似。`
- [ ] cellClass: `ag-right-aligned-cell`
- [ ] 渲染：`null` → `—`；数字 → 2 位小数 + USD 风格（如 `$2,250.00`）
- [ ] 5 处用 `useGridColumnPersist` 配套列设置（QOC / QP / hedge明细 / hedge聚合 / gap-trade 3 张表）

### 行为一致性
- [ ] 不动后端、不动 SSE、不动 SQLite
- [ ] `useGridColumnPersist` 用法符合 CLAUDE.md 规范（不内联 localStorage）

### 验证
- [ ] dev 跑起来，4 个 tab 都肉眼看一遍：CN 账户有数字、非 CN 留 `—`
- [ ] gap-trade 客户对汇总 / 主表全部 `—`（设计如此）
- [ ] 列设置抽屉里能勾掉「佣金试算」并保留状态

## 假设 / 待验证

- [x] CN 判定用 `group.startsWith('KCMc')` —— 用户已确认
- [x] External Commission 用 D03 严格法（`find('c')+1` 到 `find('_')`）—— 用户已确认
- [x] Dark Points lots 倍数表写死 —— 用户已确认
- [x] 多 symbol / 多腿行的策略（选 X：显示 `—`）—— 用户已确认
- [x] 默认显示而不是默认隐藏 —— 用户已确认
- [ ] hedge 聚合视图的 `total_lots` 是「双边 sum」（buy + sell），用它乘以单边 symbol_fee 会偏大 2x —— 实施时考虑是否要除以 2 或保持原样作为「粗略上限」
- [ ] gap SO+AB 双腿 group 可能不同（`l_groupsid` vs `c_groupsid`），分别算 → 看是否数字合理

## 笔记

- 纯前端
- 单一新 lib + 单文件 `RiskMonitor.tsx` 修改
- 关键风险：D03 `extract_number` 假设 group 含 `c` 和 `_`，非 KCMc 已过滤，但要测 `KCMc0_xxx`（rate=0）的 edge case
- 后续：若决定扩展到 D04 cent 公式 → 独立 OPT-0025（带 country-aware 表 + VN VIP 白名单同步机制）

## 不在范围内

- ❌ Client Return Rate 页面（用户先不要）
- ❌ burst-open tab（无平仓数据）
- ❌ D04 cent 公式（VN/TH 账户暂用 null）
- ❌ 后端字段补充（country / 客户级 lots 聚合）
- ❌ 与 CRM 实际入账对账（D03 程序里的 Difference）

## 结果

实际交付（与 AC 一致 + 实施时多带 2 件）：

**核心**
- `frontend/src/lib/commission.ts` —— D03 公式 JS 复刻（173 行）。导出
  `isCNGroup` / `extractExternalRate` / `getSymbolFee` / `calcInternalFee` /
  `calcDarkPoints` / `estimateCommission` / `estimateCommissionTwoLegs` /
  `formatCommission`。
- `frontend/src/pages/RiskMonitor.tsx` —— 新增 `estCommissionColDef<TRow>`
  列工厂；7 处注入「佣金试算」列：
  - QOC 主表 / QP 主表 / hedge 明细：`AlertEvent.symbol + total_lots + group`
  - hedge 聚合：`symbols.split(',')[0]` 取主 symbol；`total_lots` 双边 sum
    正好匹配「每条腿都付佣金」语义
  - gap SO+AB：`estimateCommissionTwoLegs`，L 腿 + C 腿分别算后 sum（SQL
    约束两腿同 `l_groupsid`）
  - gap 客户对汇总 / gap 主表：显示 `—`（OPT 范围内不做客户级聚合）
- ℹ 图标：复用 `InfoHeader` 组件 + shadcn Tooltip，列头能看到可视化提示

**实施时多带的 2 件**

1. **修 AG-Grid headerTooltip 即时显示问题**（commit `30b9dad`）：
   8 个 `<AgGridReact>` 实例统一 `gridOptions={{ theme: "legacy",
   enableBrowserTooltips: true }}`。AG-Grid v34 自家 tooltip 默认 showDelay
   2000ms，用户短暂悬停看不到 → 改走浏览器原生 `title=""`，所有现存
   `headerTooltip` / `tooltipField` / `tooltipValueGetter`（hedge 聚合 2 处
   + gap-trade 同 IP/客户名/账户ID 等）一并受益。

2. **引入 vitest 测试框架**（commit `11d92cd`）：
   - `npm install -D vitest` + `package.json` 加 `test` / `test:watch` 脚本
   - `frontend/src/lib/commission.test.ts` —— 30 项测试覆盖：
     - `isCNGroup`：KCMc 前缀 + null 安全
     - `extractExternalRate`：D03 严格法 + 畸形 group → 0
     - `getSymbolFee`：XAU / XAG / 油 / 主流外汇 / 股指 / 默认 6 类
     - `calcDarkPoints`：A 计数 + P 解析（含 KCMc20_PRO_P02 这种 P 落在
       'PRO' 里 → NaN → 0 的 D03 quirk）+ symbol lots 倍数表
     - `estimateCommission`：完整算例 (KCMc60_PRO + XAUUSD + 1 lot = 82.5
       等) + KCMc0_xxx + 非 CN 返 null + 字段缺失返 null
     - `estimateCommissionTwoLegs`：双腿全 CN / 半 CN / 全非 CN
     - `formatCommission`：null/NaN/千分位
   - 30/30 通过，216ms。`npm test` 一行验证。

**待澄清后续（非阻塞）**
- 数据准确性：用户验证 4 个 tab 时如果发现某行 CN 账户的试算明显偏差
  CRM 实际佣金，需要看具体 group 字符串是否落进了 `extract_number` 的
  edge case（如 `KCMc60A_xxx` 同时含 A 计数）。当前测试覆盖了这一情况
  （42.5 算例），但生产数据可能有更刁钻的命名。
- D04 cent 公式没实现，VN/TH cent 账户都是 `null` → 显示 `—`。如果业务
  要扩展到这部分，独立开 OPT-0025（country-aware 表 + VN VIP 白名单同步）。
- `total_lots` 在 hedge 聚合是双边 sum，按当前公式跟「每腿付一次佣金」
  语义吻合。若分析师反馈数字对不上，可能要做 buy_lots_sum × buy_symbol_fee
  vs sell_lots_sum × sell_symbol_fee 拆开（但当前 symbol 只有一个，意义
  不大）。

