---
id: OPT-0031
title: risk-monitor tab 栏响应式横滑 + 数据驱动（防加 tab 挤压）
status: wip
priority: P2
area: frontend
effort: S
created: 2026-06-01
related: [[OPT-0025]], [[OPT-0030]]
---

## 问题

[risk-monitor](http://10.6.20.138:5173/risk-monitor) 现有 6 个 sub-tab（批量下单 / 快开快平 / 快速获利 / 对冲刷单 / 滥用杠杆 / Gap Trade）。当浏览器窗口较窄或移动端访问时，**tab 文字相互挤压、溢出重叠**。后续还要继续加 tab（第 7、第 8 个检测规则），现状会持续恶化。

## 背景

`frontend/src/pages/RiskMonitor.tsx:1054` 的 tab 栏：

```tsx
<TabsList className="grid w-full grid-cols-6 sm:auto-cols-fr sm:grid-flow-col">
```

两个 scaling 隐患：

1. **`grid-cols-6` 是写死的列数** —— 加第 7 个 tab 拿不到列，必须手改 className，每次加 tab 都要碰，脆弱。
2. **`whitespace-nowrap` + 等分列** —— 窄屏单列宽 ≈ 视口/N，4 个汉字不允许换行 → 文字横向溢出、压到相邻 tab（即"字相互冲突"）。`max-w-4xl` 限宽 + 右侧并排 `RealtimeIndicator` 进一步提前触发。

6 个 `TabsTrigger` 当前是手写硬编码（`:1055-1090`），没用到已存在的 `RISK_MONITOR_TABS` 数组（`:832`）。

## 假设 / 待验证

- [x] shadcn Tabs（Radix）支持横滑容器包裹 TabsList —— 可行，`overflow-x-auto` + `inline-flex w-max`。
- [x] 桌面端保留等分铺满（`sm:grid auto-cols-fr grid-flow-col`），视觉与现状一致。

## 验收标准

- [ ] 窄屏（< sm）tab 文字不再挤压/重叠，整条 tab 栏可横向滚动，每个 tab 取自然宽度。
- [ ] 桌面（≥ sm）tab 栏视觉与改动前一致（等分铺满 + RealtimeIndicator 同排）。
- [ ] tab 栏数据驱动：以后往 `RISK_MONITOR_TABS` 加一项 + 补一个 label，自动多一个 tab，**无需再改 grid 列数 className**。
- [ ] `?tab=` 深链、localStorage 记忆、`forceMount` 行为均不回归。
- [ ] verify.sh 红绿闸门通过（tsc + vitest）。

## 笔记

- 方案选型与用户确认走 **A（横滑 + 数据驱动）**，不上移动端 Select（B）—— 桌面为主的内部风控台，横滑足够，避免引入 Select 状态同步成本。
- 沉淀出"tab 栏数据驱动 + 响应式横滑"约定，未来加 tab 的标准姿势（类比 [[OPT-0015]] grid-persist 后来成全项目规范）。

## 结果

<done 时填>
