---
id: OPT-0004
title: Risk-monitor 架构/框架重构
status: idea
priority: P3
area: mixed
effort: XL
created: 2026-05-13
related: [[OPT-0003]]
---

## 问题

用户给出的原话："优化当前 risk monitor 的架构上的框架"。这是一个**信号**，不是任务 —— 说明现状用起来感觉不顺，但具体痛点没拆出来。直接开干会变成漫无目的的重构，吞掉时间且容易引入新 bug。

## 背景

Risk-monitor 当前结构（见 `.cursor/skills/risk-monitor/SKILL.md`）：
- Backend: 一个 APScheduler 后台 scheduler → 3 个 server 的 SQL 查询 → Python 滑窗检测 → 富化 → 缓存 → 写 SQLite
- 多条 detection rule（burst-open / martingale / quick-profit / gap-trade …），都挂在同一个 scheduler 上
- Frontend: 多个 tab 展示不同视图（关联到 OPT-0001）

## 假设 / 待验证（待用户/Claude 拆解）

**这是个 idea 而不是 ready 的 item，必须先拆成具体子任务后才能 claim。** 候选拆解方向：

- [ ] **规则注册机制**：当前加新规则要改多少处？能不能做成"加一个 Detector 类就自动注册"？
- [ ] **服务分层**：scheduler / detector / enricher / persister 是否混在一起？分层是否合理？
- [ ] **配置集中**：scan_interval / 阈值参数 / 服务器列表当前散在多个文件？是否集中到一个 config？
- [ ] **可观测性**：scheduler 的失败/延迟当前怎么发现？有 metrics 吗？
- [ ] **测试覆盖**：detection 逻辑有单测吗？规模多大？
- [ ] **前端架构**：tab 之间的数据流是各自 fetch 还是共享 store？

每个方向值得开一个独立 OPT-NNNN 后续放到 Ready。

## 验收标准

- [ ] **本 item 不直接实现** —— 它的"完成" = 拆成 ≥3 个具体子 item 进入 Ideas/Ready 后，此 item 状态改为 `dropped` 并在结果段引用子 item 列表
- [ ] 拆解时**先量化痛点**：哪一处现在最痛？（"加新规则要改 5 个文件" vs "scheduler 偶尔挂掉" vs "配置散落"）
- [ ] 不接受"全部重写"作为子任务

## 笔记

⚠ 警惕"为重构而重构"。每个拆出来的子 item 必须能回答："这个改完，我接下来要做什么会变更容易/更快/更安全？"

## 结果

_待填_
