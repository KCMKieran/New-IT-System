---
id: OPT-0052
title: IBData / IB Report 净入金拆出「交易净入金」（不含 ib withdrawal）
status: idea
priority: P2
area: mixed
effort: S
created: 2026-07-15
related: [[OPT-0049]]
---

## 背景

2026-07-15 做了一轮「净赚 / 净入金 / 公司盈亏」口径全库审计
（报告：`docs/analysis/net-gain-terminology-audit-2026-07-15.md`，含 Opus 4.8 独立复核，
判定 25 CONFIRMED / 3 IMPRECISE / 0 WRONG / 1 NEW）。

审计确立的标准口径（SSOT 在 `.cursor/skills/rebate-arbitrage/SKILL.md` §2.2）：

```
客户净赚 = 净值(Equity) − 交易净入金 + 总反佣 = 已平仓PL + 浮动PL + 全链返佣
```
其中**交易净入金不含 `'ib withdrawal'`**（IB 佣金提现必须单独拆开 —— 案例 110386 教训）。

审计实查结论：**全库 9 套「净入金」实现，7 套把 `'ib withdrawal'` 混在里面**。
唯一的标准拆分实现是 `rule_rebate_arb_service.py:307-357` 的 `_query_net_deposit_split`
（2026-07-15 已被提升到 `account_enrichment.py` 作为 canonical）。

2026-07-15 已修的（不在本 OPT 范围）：
- RiskMonitor「淨賺」分母 → `trading_net_deposit` ✅
- Client Return Rate 分子分母对称化 ✅（实测 130/1687 收益率变化，126 向下修正；
  headline: 客户 123261 页面显示 +60.27%，实际 −32.13%）
- IBData Net Deposit 去掉「− IB 钱包当前余额」的量纲错误 ✅（窗口流量减生涯存量）

## 本 OPT 范围（剩余项）

IBData 与 IB Report 的「Net Deposit」现口径仍是 `deposit + withdrawal + 'ib withdrawal'`
（与全站 7/9 实现一致，且 `docs/features/ib-net-deposit-reform.md:4-7` 有**业务确认**背书）。
2026-07-15 仅在 tooltip 注明了「含 IB 佣金提现」，**未改口径**。

按标准定义，应额外提供**不含 ibw 的交易净入金**，让「客户自己投了多少钱」与
「IB 佣金提走了多少」可分开看。

涉及位置：
- `backend/app/services/ib_data_service.py:74-83`（IBData 的 `net_deposit_usd`）
- `backend/app/services/clickhouse_service.py:550-551`（IB Report 的 `net_deposit_range/month`）
- `frontend/src/pages/IBData.tsx:590-609`（列 + tooltip）
- `frontend/src/pages/IBReport.tsx:489-493`
- `docs/features/ib-data.md`、`docs/features/ib-report.md:100`

## 交付内容（建议）

1. 复用 canonical 的 `_query_net_deposit_split` 口径（**不要**再 fork 第 4 套实现），
   为 IBData / IB Report 拆出 `trading_net_deposit` 与 `ib_withdrawal` 两列。
2. 保留现有「Net Deposit」列（业务确认口径，向后兼容），新增拆分列 —— 参考
   Client Return Rate 2026-07-15 的做法（拆而不换：老数字可精确重建
   `legacy = net_deposit + ib_withdrawal`，用户能看见数字为什么动）。
3. 文档同步（两个 features 文档 + tooltip）。

## AC

- [ ] IBData / IB Report 能同时看到「含 ibw」与「不含 ibw」两个净入金口径
- [ ] 不 fork 新的净入金实现 —— 复用 `account_enrichment` 的 canonical 拆分
- [ ] 现有「Net Deposit」列数字不变（向后兼容）
- [ ] 列头 tooltip 明确标注各自口径（CLAUDE.md 新约定：net deposit 类指标定义处必须注明
      是否含 `'ib withdrawal'`）

## 开放问题（需用户决策）

1. **IBData 的「欠 IB 佣金」负债视角要不要做** —— 原实现减钱包余额的意图无从证实
   （`git log -S "ib_wallet_balance"` 只命中初始 commit `5835431`，message 全文
   「更新完成，后续优化UI」，**无 rationale**）。唯一线索是 `docs/features/ib-data.md`
   把 IB Wallet Balance 标为 `Liability`，推测原意为「净沉淀资金 = 存入 − 已提 − 尚欠 IB 佣金」。
   若老板确实要这个视角，**正确做法不是减存量**，而是需要钱包**流水**表 ——
   当前 `mt4_users` 只有余额快照，**做不了**。需先确认是否存在钱包流水数据源。
2. **优先级** —— 全站 7/9 实现都含 ibw，且有业务确认文档背书。这更像"提供更精细的视角"
   而非"修 bug"。是否值得做由用户判断（故标 `status: idea` 而非 `ready`）。

## 注意

`docs/**` 与 `.cursor/**` 是 gitignored 本地资产（`.gitignore:57`，仅 `docs/optimization/**`
和 `docs/operations/docs-portal.md` 例外）。上述审计报告、features 文档、SKILL.md 的更新
**都不随 commit 走** —— 这是项目既定设计，别 `git add -f`。
