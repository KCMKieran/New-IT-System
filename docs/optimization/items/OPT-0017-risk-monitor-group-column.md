---
id: OPT-0017
title: Risk Monitor 各 Tab 添加账户组（Group）列
status: done
priority: P2
area: mixed
effort: S
created: 2026-05-18
---

## 问题

风控分析时分析师要快速分辨告警账户在 **MT AKCM 组**（组名含 `AKCM` 子串，如 `4sd_L1_AKCM`）还是普通 B-book 组（如 `STD_USD`）。当前只有 Tab 1（批量下单）展示了「账户组」列；Tab 2（快开快平）/ Tab 3（快速获利）/ Tab 4（Gap Trade）三段主表都没有 —— Gap Trade 仅在右侧 Detail Sheet 里能看到。

要求：把 Tab 1 现有的「账户组」列原样补到 Tab 2 / Tab 3 / Tab 4 三段 grid，**只显示原始 group 字符串，不加 chip / 派生 type / CRM tag 关联**。分析师肉眼识别后缀。

## 背景

**当前 `group` 字段后端覆盖情况**：

| Tab | 来源 | 现状 |
|---|---|---|
| 1 批量下单 | `risk_monitor_service._enrich_account_info` (l.598 / 655) 从 `mt4_users.GROUP` / `mt5_users.Group` 填 | ✅ 已有 |
| 2 快开快平 | `rule_quick_open_close_service.py:306` 写死 `None`，后续 enrichment 只填 currency/zipcode/net_deposit_hist | ❌ alert 里永远 null |
| 3 快速获利 | `rule_quick_profit_service.py:387` 同上 | ❌ alert 里永远 null |
| 4 Gap Trade Rule 81 | `rule_gap_trade_gap_service.py:340` 从 `fxbackoffice.mt4_users.groupsid` 填 | ✅ 已有 (`group` + `client_groupsid`) |
| 4 Gap Trade Rule 71 | `rule_gap_trade_so_service.py:472` 填 `l_groupsid` / `c_groupsid` | ✅ 已有（字段名不同） |

**前端列展示**：仅 Tab 1（`RiskMonitor.tsx:1288`）有 `{ headerName: "账户组", field: "group", colId: "group", width: 150 }`。Tab 2/3/4 主表都没补。

**为什么选 MT 组名而不是 CRM tag**：
- `mt4_users.GROUP` / `fxbackoffice.mt4_users.groupsid` 是账户级，与 risk monitor 当前粒度一致；零额外 JOIN
- `user_tags.tagid=30154` 是客户级 AKCM tag（client-return-rate `is_akcm` 用），要新加 JOIN + alert_events 新列；本次不引入
- 用户已确认走前者，分析师肉眼识别 `%AKCM%` 后缀

## 假设 / 待验证

- [x] `fxbackoffice.mt4_users` 既有 `GROUP` 列也有 `groupsid` 列 —— 同一表存在两个语义近似的列：`account_enrichment.py:99` 用 `mu.\`GROUP\``，`rule_gap_trade_gap_service.py:95` 用 `U.groupsid`。本次按 `\`GROUP\`` 取（和 Tab 1 的 broker `mt4_users.GROUP` 同语义），避免值不一致
- [ ] Tab 1 `_enrich_account_info` 走 broker mt4_users（直查 MT 库），Tab 2/3 改后走 fxbackoffice.mt4_users —— 两个表的 `GROUP` 值一致吗？dev 抽样验证一两个 loginsid

## 验收标准

- [ ] **后端 extend** `account_enrichment.get_account_info_map`：SELECT 多取 `` `GROUP` AS `group` ``，返回 dict 多 `group` key
- [ ] **后端 wire** Tab 2 `rule_quick_open_close_service.py` enrichment 循环加 `alert["group"] = info.get("group")`
- [ ] **后端 wire** Tab 3 `rule_quick_profit_service.py` enrichment 循环加 `alert["group"] = info.get("group")`
- [ ] **前端列** Tab 2 `QuickOpenCloseTab` columnDefs 末尾加 `{ headerName: "账户组", field: "group", colId: "group", width: 150 }`
- [ ] **前端列** Tab 3 `QuickProfitTab` columnDefs 末尾加同样一行
- [ ] **前端列** Tab 4 `clientPairColumns` + `soAbColumns` 各加「爆仓方组」(`l_groupsid`) + 「对手方组」(`c_groupsid`) 两列
- [ ] **前端列** Tab 4 `gapColumns` 加「账户组」(`client_groupsid`) 一列
- [ ] **dev 验证** 用浏览器打开 `http://10.6.20.138:5173/risk-monitor`，4 个 tab 都能看到新列；Tab 2/3 新 alert 行有值（历史行空白，符合预期）
- [ ] **prod build** 通过 `npx tsc -b --noEmit` 无新错

## 笔记

**历史回填策略**：用户明确选**不回填**。
- Tab 2/3 之前的 `alert_events` 行 `group=NULL`，UI 显示空白
- 默认 4h 时间窗 + `scan_interval=10min`，新数据不到 1 天就把历史挤出窗口，零运维风险

**`_enrich_account_info` 不动**：Tab 1 走自己的 `_fill_mt4_account_info` / MT5 直查路径，不消费 `get_account_info_map` 返回里的 `group`。本次给返回 dict 多塞一个 key 对 Tab 1 是零影响（用 `info.get("group")` 兜底，不会因为 key 缺失抛错）。

**SORTABLE_COL_IDS**：`"group"` 已在表里（`RiskMonitor.tsx:198`），Tab 2/3 立刻可排序。Tab 4 三段 grid 用的是 `l_groupsid` / `c_groupsid` / `client_groupsid`，不在 SORTABLE 表里，保持 `sortable: false`（和当前 Gap Trade 其他派生列一致）。

**useGridColumnPersist 兼容性**：新列默认显示，不需要 bump storage key（[[OPT-0015]] / [[OPT-0016]] 的 hook 会把未持久化的新列默认 visible）。

## 结果

**合并到 main**：`e5e102c` (2026-05-18)，单一 feature commit `e2b21ae`，4 文件 +40 / -4。

**实际交付 vs 原 AC**：

| AC | 状态 | 备注 |
|---|---|---|
| 后端 extend `get_account_info_map` 返回 group | ✅ | SELECT 加 `` `GROUP` AS `group` ``；返回 dict 第三个 key |
| Tab 2 quick-open-close 写 `alert["group"]` | ✅ | enrichment 循环加一行 |
| Tab 3 quick-profit 写 `alert["group"]` | ✅ | enrichment 循环加一行 |
| Tab 2 columnDefs 加「账户组」 | ✅ | 表末尾追加，跟 Tab 1 一致 |
| Tab 3 columnDefs 加「账户组」 | ✅ | 同上 |
| Tab 4 clientPairColumns + soAbColumns 加「爆仓方组」+「对手方组」两列 | ⚠ 改为**一列**「账户组」 | SO+AB SQL 强制 `Cu.groupsid = Ls.L_groupsid`（`rule_gap_trade_so_service.py:223`），两腿必然同组，alert dict 也只存 `l_groupsid`。两列展示会显示完全相同的值，浪费宽度 |
| Tab 4 gapColumns 加「账户组」 (`client_groupsid`) | ✅ | 表末尾追加 |
| dev 验证 | ✅ | 用户在浏览器实测 4 个 tab 都能看到新列 |
| `npx tsc -b --noEmit` 无新错 | ✅ | exit 0 |

**与原 AC 偏差**：

- SO+AB 两腿同组的 SQL 约束在 file 阶段没意识到，原 AC 写了两列。实施时发现 alert 只有 `l_groupsid`，于是：
  - `soAbColumns`：直接用 `l_groupsid` 一列
  - `clientPairColumns`：`ClientPairAggRow` 类型新加 `groupsid` 字段，聚合时从第一条 alert 的 `l_groupsid` 取
  - `gapColumns`：照旧 `client_groupsid`

**额外做了（超出原 AC）**：

- `get_account_info_map` 的 docstring 同步更新：`Returns` 段加 group 示例、点出 AKCM substring 语义
- `ClientPairAggRow` 接口加注释说明为什么只一个 groupsid 字段（SQL 约束 self-doc）

**未做（明确推迟）**：

- 历史 `alert_events` 行回填（用户已选「不回填」）：默认 4h 窗口，新数据不到 1 天就把历史挤出窗口
- AKCM 派生 chip / 类型列 / CRM tag JOIN（用户已选「就原始字符串」）

**Follow-up**：无新 OPT。若日后需要跨页面 AKCM/B-Group 筛选（zipcode 那种 backend like），再 file。
