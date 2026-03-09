## 金融交易系统前端开发指南

### 项目架构概览
- **技术栈**: React 18 + TypeScript + Vite + Tailwind CSS + shadcn/ui
- **状态管理**: React Hooks (useState, useCallback, useMemo)
- **UI组件**: shadcn/ui 组件库，遵循现代金融界面设计规范
- **路由**: React Router v6，支持懒加载
- **构建工具**: Vite (开发服务器 + 生产构建)

### 核心文件职责
```
src/
├── components/
│   ├── app-sidebar.tsx     # 侧边栏导航定义
│   ├── site-header.tsx     # 页面标题映射
│   └── ui/                 # shadcn/ui组件库
├── pages/                  # 页面组件
├── App.tsx                 # 路由注册
└── main.tsx               # 应用入口
```

## 标准筛选卡片组件设计

### 设计规范
- **响应式布局**: 桌面端筛选项同行，移动端自动换行
- **控件规格**: 统一高度 `h-9`，按钮内边距 `px-3`，选择器宽度 `w-36`
- **交互模式**: 显式触发分析，避免实时查询造成性能问题
- **状态管理**: 集中状态，支持条件渲染和加载状态

### 核心状态定义
```tsx
// 对象筛选规则类型
type Rule = 
  | { type: "customer_ids"; ids: number[]; include: boolean }
  | { type: "customer_tags"; source: "local" | "crm"; tags: string[]; operator: "ANY" | "ALL"; include: boolean }
  | { type: "account_ids"; ids: string[]; include: boolean }

// 筛选状态
const [rules, setRules] = useState<Rule[]>([])
const [startDate, setStartDate] = useState<Date | undefined>(new Date())
const [endDate, setEndDate] = useState<Date | undefined>(new Date())
const [quickRange, setQuickRange] = useState<"last_1w" | "last_1m" | "last_3m" | "all" | "custom">("last_1m")
const [symbolsMode, setSymbolsMode] = useState<"all" | "custom">("all")
const [selectedSymbols, setSelectedSymbols] = useState<string[]>([])

// 分析状态
const [isAnalyzing, setIsAnalyzing] = useState(false)
const [analysisData, setAnalysisData] = useState<any>(null)
const [analysisError, setAnalysisError] = useState<string | null>(null)
```

### 响应式布局结构
```tsx
<Card>
  <CardHeader>
    <CardTitle>筛选</CardTitle>
  </CardHeader>
  <CardContent className="space-y-4">
    {/* 筛选项区域 - 响应式布局 */}
    <div className="flex flex-wrap items-center justify-between gap-3">
      <div className="flex flex-wrap items-center gap-3">
        {/* 对象选择 - 响应式 Dialog/Drawer */}
        <div className="flex items-center gap-2">
          <span className="text-sm text-muted-foreground">选择对象：</span>
          <div className="hidden sm:block">
            <Dialog open={dialogOpen} onOpenChange={setDialogOpen}>
              <DialogTrigger asChild>
                <Button variant="outline" className="h-9 px-3">选择对象</Button>
              </DialogTrigger>
              {/* Dialog内容 */}
            </Dialog>
          </div>
          <div className="block sm:hidden">
            <Drawer open={drawerOpen} onOpenChange={setDrawerOpen}>
              {/* Drawer内容 */}
            </Drawer>
          </div>
        </div>

        {/* 时间范围 - 双日历 + 快捷选择 */}
        <div className="flex items-center gap-2">
          <span className="text-sm text-muted-foreground">时间范围：</span>
          <Popover>
            <PopoverTrigger asChild>
              <Button variant="outline" className="h-9 px-3 min-w-[140px]">{rangeLabel}</Button>
            </PopoverTrigger>
            <PopoverContent>
              {/* 双日历布局 */}
            </PopoverContent>
          </Popover>
          <Select value={quickRange} onValueChange={applyQuickRange}>
            <SelectTrigger className="h-9 w-36">快捷范围</SelectTrigger>
          </Select>
        </div>

        {/* 交易品种 */}
        <div className="flex items-center gap-2">
          <span className="text-sm text-muted-foreground">交易品种：</span>
          <Select value={symbolsMode} onValueChange={setSymbolsMode}>
            <SelectTrigger className="h-9 w-36">选择方式</SelectTrigger>
          </Select>
        </div>
      </div>

      {/* 分析按钮 - 桌面端右对齐 */}
      <div className="hidden sm:flex">
        <Button onClick={handleAnalyzeData} className="h-9 gap-2" disabled={isAnalyzing}>
          {isAnalyzing ? <Loader2 className="size-4 animate-spin" /> : <Search className="size-4" />}
          {isAnalyzing ? "分析中..." : "开始分析"}
        </Button>
      </div>
    </div>

    {/* 移动端分析按钮 - 独立行居中 */}
    <div className="flex justify-center sm:hidden">
      <Button onClick={handleAnalyzeData} className="h-9 gap-2" disabled={isAnalyzing}>
        {/* 同上 */}
      </Button>
    </div>
  </CardContent>
</Card>

{/* 条件渲染分析结果 */}
{(analysisData || isAnalyzing) && (
  <div className="grid grid-cols-1 gap-4 md:grid-cols-3 lg:grid-cols-5">
    {/* KPI卡片 + 图表区域 */}
  </div>
)}
```

### 响应式布局优化实践 (移动端优先)
- **筛选条件堆叠**: 在窄屏幕上 (如 `< sm`)，筛选组应从水平排列 (`flex-row`) 变为垂直堆叠 (`flex-col`)，每个筛选组各占一行，以保证清晰的布局结构。
- **控件对齐与伸缩**:
  - **左侧对齐**: 为保证垂直对齐，所有筛选条件的文字标签 (label) 应设置**统一的固定宽度**并禁止换行。例如: `w-20 whitespace-nowrap`。
  - **右侧伸缩**: 标签右侧的输入控件（如 `Button`, `Select`）应设为弹性填充 (`flex-1`)，以占满剩余空间，确保行内布局的整洁。
  ```tsx
  <div class="flex items-center gap-2">
    {/* 统一宽度的标签，确保垂直对齐 */}
    <span class="w-20 flex-shrink-0 whitespace-nowrap">时间范围：</span>
    {/* 弹性填充的按钮 */}
    <Button class="flex-1 min-w-0">...</Button>
    {/* 弹性填充的选择框 */}
    <SelectTrigger class="flex-1 min-w-0">...</SelectTrigger>
  </div>
  ```
- **组件内部响应式**: 对于弹层等复杂组件，其内部也需要响应式设计。例如，并排的双月日历选择器在移动端应改为**单月视图**，避免内容溢出。
- **页面边距调整**: 移动端屏幕空间宝贵，应适当减小页面最外层容器的水平内边距 (`padding`)，如从 `px-4` 减至 `px-1`，并在 `sm` 断点恢复，以实现“全面屏”效果。
- **全宽操作按钮**: 在移动端，主要的页面操作按钮（如“开始分析”、“查询”）应设为**100%宽度** (`w-full`)，使其更加突出且易于点击。

### 关键交互逻辑
```tsx
// 时间快捷范围处理
const applyQuickRange = useCallback((qr: typeof quickRange) => {
  const today = new Date()
  const d0 = new Date(today.getFullYear(), today.getMonth(), today.getDate())
  
  if (qr === "all") {
    setStartDate(undefined)
    setEndDate(undefined)
  } else if (qr === "last_1m") {
    const s = new Date(d0)
    s.setMonth(s.getMonth() - 1)
    s.setDate(s.getDate() + 1)
    setStartDate(s)
    setEndDate(d0)
  }
  setQuickRange(qr)
}, [])

// 分析数据处理
const handleAnalyzeData = useCallback(async () => {
  if (effectiveAccounts.length === 0) return
  
  setIsAnalyzing(true)
  setAnalysisError(null)
  
  try {
    const analysisParams = {
      accounts: effectiveAccounts,
      startDate: quickRange === "all" ? undefined : startDate,
      endDate: quickRange === "all" ? undefined : endDate,
      symbols: symbolsMode === "all" ? null : selectedSymbols
    }
    
    const result = await postAnalysisRequest(analysisParams)
    setAnalysisData(result)
  } catch (error: any) {
    setAnalysisError(error?.message ?? "分析失败")
  } finally {
    setIsAnalyzing(false)
  }
}, [effectiveAccounts, quickRange, startDate, endDate, symbolsMode, selectedSymbols])
```

### 设计要点
- **响应式断点**: `sm:` (640px) 作为桌面/移动分界
- **加载状态**: 使用 `Loader2` 图标 + `animate-spin` 类
- **禁用逻辑**: 未选择账户时禁用分析按钮
- **条件渲染**: 只在有数据或加载中时显示结果区域
- **骨架屏**: 加载中显示带动画的占位内容

## 页面管理操作

### 1. 新增页面（完整流程）
```tsx
// 1. 创建页面组件
// src/pages/YourPage.tsx
export default function YourPage() {
  return <div>Your Page Content</div>
}

// 2. 注册路由 - App.tsx
const YourPage = lazy(() => import("@/pages/YourPage"))
// 在 Routes 中添加
<Route path="/your-path" element={<YourPage />} />

// 3. 添加标题映射 - site-header.tsx
const titleMap: Record<string, string> = {
  "/your-path": "你的页面标题",
}

// 4. 添加到侧边栏 - app-sidebar.tsx
const data = {
  navMain: [
    { title: "你的页面标题", url: "/your-path", icon: IconReport },
  ]
}
```

### 2. 隐藏页面（从侧边栏移除，保留路由和代码）

**文件**: `frontend/src/components/app-sidebar.tsx`

**隐藏步骤**:
1. 找到目标页面所在的 `navSections` 条目
2. 将该行注释掉，并在上方加 `// [HIDDEN]` 标记和原因说明

```tsx
// Before
{ title: t("nav.profitAnalysis"), url: "/profit" },

// After
// [HIDDEN] Profit Analysis - temporarily hidden
// { title: t("nav.profitAnalysis"), url: "/profit" },
```

**恢复步骤**:
1. 在 `app-sidebar.tsx` 中搜索 `[HIDDEN]`
2. 取消目标条目的注释，同时删除 `[HIDDEN]` 注释行

```tsx
// Before (hidden)
// [HIDDEN] Profit Analysis - temporarily hidden
// { title: t("nav.profitAnalysis"), url: "/profit" },

// After (restored)
{ title: t("nav.profitAnalysis"), url: "/profit" },
```

> **注意**: 隐藏后用户仍可通过直接输入 URL 访问页面（路由未移除）。如需完全禁止访问，还需在 `App.tsx` 中注释对应的 `<Route>`。

#### 当前隐藏页面清单

| Page | URL | Section | Reason |
|------|-----|---------|--------|
| Client PnL Monitor | `/client-pnl-monitor` | CS Department | Page hidden |
| Client PnL Analysis | `/client-pnl-analysis` | Risk Control | Temporarily hidden |
| Basis Analysis | `/basis` | Risk Control | 10.6.20.138:8050 service disabled |
| Profit Analysis | `/profit` | Risk Control | Temporarily hidden |
| Agent Global | `/warehouse/agent-global` | Other | Static JSON page, not using backend API |

> 此清单最后更新于 2026-03-09，以 `app-sidebar.tsx` 中 `[HIDDEN]` 注释为准。

### 3. 删除页面（完全移除）
```tsx
// 从 app-sidebar.tsx 移除条目
// 从 App.tsx 移除路由
// 从 site-header.tsx 移除标题映射
// 删除 src/pages/ 下的页面组件文件
```

## API 调用规范

### 标准请求封装
```tsx
// POST 请求
async function postJson<T>(url: string, body: unknown, signal?: AbortSignal): Promise<T> {
  const res = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json", accept: "application/json" },
    body: JSON.stringify(body),
    signal,
  })
  if (!res.ok) throw new Error(`HTTP ${res.status}`)
  return (await res.json()) as T
}

// 使用示例
const handleAnalyze = useCallback(async () => {
  setIsLoading(true)
  try {
    const data = await postJson<AnalysisResponse>(
      "/api/v1/trading/analysis", 
      { accounts, startDate, endDate, symbols }
    )
    setResult(data)
  } catch (error: any) {
    if (error?.name !== "AbortError") setError(error.message)
  } finally {
    setIsLoading(false)
  }
}, [accounts, startDate, endDate, symbols])
```

### 关键约定
- **相对路径**: 统一使用 `/api/...`，避免硬编码主机地址
- **错误处理**: 检查 `res.ok`，捕获 `AbortError`
- **加载状态**: 统一管理 `loading/error/data` 状态
- **可取消请求**: 使用 `AbortController` 避免竞态条件

### 响应式对话框模式
```tsx
// 桌面端用 Dialog，移动端用 Drawer
<div className="hidden sm:block">
  <Dialog open={open} onOpenChange={setOpen}>
    <DialogContent className="w-[90vw] sm:max-w-[1100px]">
      {/* 内容 */}
    </DialogContent>
  </Dialog>
</div>
<div className="block sm:hidden">
  <Drawer open={open} onOpenChange={setOpen}>
    <DrawerContent>
      {/* 相同内容 */}
    </DrawerContent>
  </Drawer>
</div>
```

### 表格滚动处理
```tsx
// 单层滚动策略，避免双重滚动条
<div className="border rounded-md overflow-hidden">
  <div className="overflow-auto max-h-64">
    <Table className="min-w-[800px]">
      {/* 表格内容 */}
    </Table>
  </div>
</div>
```

## 高级表格 (Data Table) 规范

### 核心技术栈
项目中的高级表格功能（如排序、筛选）基于 `shadcn/ui` 的 `Table` 组件与 `TanStack Table v8` 逻辑库的结合。

- **`shadcn/ui Table`**: 负责表格的 UI 样式渲染。
- **`TanStack Table`**: 作为无头 (headless) 库，负责处理表格所有的状态和逻辑，包括排序、筛选、分页、行选择等。

这种分离的架构提供了极高的灵活性和可定制性。开发者需要自行组合 `shadcn/ui` 的 UI 组件（如 `Input`, `DropdownMenu`, `DatePicker`）来构建交互控件，并将其与 `TanStack Table` 的状态管理API连接。

### 功能实现指南

#### 1. 排序 (Sorting)
- **支持情况**: 完全支持。
- **实现方法**: 在列定义 (`ColumnDef`) 的 `header` 属性中，渲染一个 `Button` 组件。在该按钮的 `onClick` 事件中调用 `column.toggleSorting()` 方法即可切换升序、降序和无序状态。

#### 2. 筛选 (Filtering)
- **支持情况**: 完全支持，且高度可定制。
- **基础筛选 (文本包含)**:
  - 创建一个 `Input` 组件作为筛选UI。
  - 监听其 `onChange` 事件，并调用 `table.getColumn("columnId")?.setFilterValue(value)` 来更新筛选状态。`TanStack Table` 会自动过滤表格数据。
- **高级筛选 (范围、等于、大于/小于、为空等)**:
  - **自定义UI**: 根据需求创建对应的UI组件。例如，使用两个 `Input` 实现数值范围筛选，或使用 `Select` 组件提供 `等于` / `不等于` 等操作符选项。
  - **自定义筛选函数 (`filterFn`)**: `TanStack Table` 允许为每一列或全局定义 `filterFn`。通过编写自定义函数，可以实现任意复杂的筛选逻辑。
    - **日期范围**: 使用 `DateRangePicker` 组件获取日期范围对象，在 `filterFn` 中判断行的日期是否在该范围内。
    - **标签 (Tags)**: 使用多选 `Select` 或 `Combobox` 组件获取标签数组，在 `filterFn` 中判断行的标签是否匹配。
    - **为空/不为空**: 在 `filterFn` 中直接判断单元格的值是否为 `null`、`undefined` 或空字符串。

### 参考实现
完整的表格交互实现可参考官方的 `data-table-demo` 示例，该示例包含了排序、文本筛选、列可见性切换、行选择和分页等功能的标准实现方法。

## 参考实现
完整的筛选卡片实现可参考 `src/pages/ClientTradingAnalytics.tsx`，包含：
- 响应式对象选择弹层
- 双日历时间选择器
- 品种自定义添加
- 分析状态管理
- 条件渲染逻辑