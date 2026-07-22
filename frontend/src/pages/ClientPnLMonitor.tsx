import { useState, useCallback, useMemo, useRef, useEffect } from "react"
import { useTheme } from "@/components/theme-provider"
import { useI18n } from "@/components/i18n-provider"
import { apiFetch } from "@/lib/fetch"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Card, CardContent } from "@/components/ui/card"
import { DropdownMenu, DropdownMenuCheckboxItem, DropdownMenuContent, DropdownMenuLabel, DropdownMenuSeparator, DropdownMenuTrigger } from "@/components/ui/dropdown-menu"
import { Settings2, X, Search, RefreshCw, Filter } from "lucide-react"
import { Badge } from "@/components/ui/badge"
import { FilterBuilder } from "@/components/FilterBuilder"
import { FilterGroup, FilterOperator, operatorNeedsValue, operatorNeedsTwoValues, OPERATOR_LABELS, ColumnMeta } from "@/types/filter"
import { AgGridReact } from 'ag-grid-react'
import { ColDef, GridReadyEvent, SortChangedEvent, GridApi, PostSortRowsParams, IRowNode } from 'ag-grid-community'

// ClientID 汇总数据接口
interface ClientPnLSummaryRow {
  // 主键
  client_id: number | string
  
  // 客户基本信息
  client_name?: string | null
  primary_server?: string | null
  zipcode?: string | null
  is_enabled?: number | boolean | null
  countries?: string[] | null
  currencies?: string[] | null
  
  // 账户统计
  account_count: number
  account_list?: number[] | null
  
  // 聚合金额（统一美元）
  total_balance_usd: number | string
  total_credit_usd: number | string
  total_floating_pnl_usd: number | string
  total_equity_usd: number | string
  
  // 平仓盈亏
  total_closed_profit_usd: number | string
  total_commission_usd: number | string
  total_ib_commission_income_usd: number | string
  total_net_profit_with_ib_usd: number | string
  
  // 资金流动
  total_deposit_usd: number | string
  total_withdrawal_usd: number | string
  net_deposit_usd: number | string
  
  // 聚合手数
  total_volume_lots: number | string
  total_overnight_volume_lots: number | string
  auto_swap_free_status?: number | string
  
  // 聚合订单数
  total_closed_count: number
  total_overnight_count: number
  
  // 更新时间
  last_updated?: string | null
  
  // 账户明细（一次性加载）
  accounts?: ClientAccountDetail[]
}

// 账户明细接口
interface ClientAccountDetail {
  client_id: number
  login: number
  server: string
  currency?: string | null
  user_name?: string | null
  user_group?: string | null
  country?: string | null
  balance_usd: number | string
  credit_usd: number | string
  floating_pnl_usd: number | string
  equity_usd: number | string
  closed_profit_usd: number | string
  commission_usd: number | string
  ib_commission_income_usd: number | string
  net_profit_with_ib_usd?: number | string
  deposit_usd: number | string
  withdrawal_usd: number | string
  volume_lots: number | string
  auto_swap_free_status?: number | string
  last_updated?: string | null
}

function formatCurrency(value: number) {
  const sign = value >= 0 ? "" : "-"
  const abs = Math.abs(value)
  // Round to integer, no decimals (updated per request)
  return `${sign}$${Math.round(abs).toLocaleString()}`
}

function toNumber(v: unknown, fallback = 0): number {
  if (typeof v === "number" && Number.isFinite(v)) return v
  if (typeof v === "string" && v.trim() !== "" && !Number.isNaN(Number(v))) return Number(v)
  return fallback
}

// simple fetch with timeout helper (abort after given ms)
function fetchWithTimeout(url: string, options: any = {}, timeout = 60000) {
  const controller = new AbortController()
  const id = setTimeout(() => controller.abort(), timeout)
  const opts = { ...options, signal: controller.signal }
  return apiFetch(url, opts).finally(() => clearTimeout(id))
}

// backend response for V2 refresh endpoint
interface EtlRefreshResponse {
  status: string
  message?: string | null
  server: string
  processed_rows?: number | null
  duration_seconds?: number | null
  new_max_deal_id?: number | null
  new_trades_count?: number | null
  floating_only_count?: number | null
}

// fresh grad note: keep CRM link helpers isolated for reuse
function getClientCrmLink(clientId: number | string | null | undefined) {
  if (clientId === null || clientId === undefined || clientId === "") return null
  return `https://mt4.kohleglobal.com/crm/users/${clientId}`
}

function getAccountCrmLink(server: string | null | undefined, login: number | string | null | undefined) {
  if (login === null || login === undefined || login === "") return null
  const loginStr = String(login)
  const serverKey = (server || "").toUpperCase()
  if (serverKey === "MT5") return `https://mt4.kohleglobal.com/crm/accounts/5-${loginStr}`
  if (serverKey === "MT4LIVE2") return `https://mt4.kohleglobal.com/crm/accounts/6-${loginStr}`
  return null
}

export default function ClientPnLMonitor() {
  const { theme } = useTheme()
  const { t } = useI18n()
  // fresh grad note: tx() returns fallback when i18n key is missing
  const tx = useCallback((key: string, fallback: string) => {
    try {
      const v = (t as any)(key)
      return (typeof v === 'string' && v && v !== key) ? v : fallback
    } catch {
      return fallback
    }
  }, [t])
  // language-aware fallback: choose zh/en fallback based on i18n separator
  const isZh = useMemo(() => {
    try {
      const sep = (t as any)('common.comma')
      return typeof sep === 'string' && sep !== ', '
    } catch {
      return false
    }
  }, [t])
  const tz = useCallback((key: string, zhFallback: string, enFallback: string) => {
    try {
      const v = (t as any)(key)
      if (typeof v === 'string' && v && v !== key) return v
    } catch {}
    return isZh ? zhFallback : enFallback
  }, [t, isZh])
  
  // 数据状态
  const [rows, setRows] = useState<ClientPnLSummaryRow[]>([])
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null)
  const [loading, setLoading] = useState(false)
  
  // 分页状态
  const [pageIndex, setPageIndex] = useState(0)
  const [pageSize, setPageSize] = useState(50)
  const [totalCount, setTotalCount] = useState(0)
  const [totalPages, setTotalPages] = useState(0)
  
  // 搜索状态
  const [searchValue, setSearchValue] = useState("")
  const [searchInput, setSearchInput] = useState("")
  
  // 展开/收起状态：记录哪些 client_id 被展开
  const [expandedClients, setExpandedClients] = useState<Set<number | string>>(new Set())
  // Track per-client detail loading to avoid duplicate fetches
  const [accountLoadingMap, setAccountLoadingMap] = useState<Record<string, boolean>>({})
  
  // AG Grid 状态
  const [sortModel, setSortModel] = useState<any[]>([
    { colId: 'total_closed_profit_usd', sort: 'desc' }
  ])
  const [columnVisibility, setColumnVisibility] = useState<Record<string, boolean>>({
    client_id: true,
    client_name: true,
    primary_server: true,
    account_count: true,
    total_balance_usd: true,
    total_floating_pnl_usd: true,
    total_equity_usd: false,
    total_closed_profit_usd: true,
    total_ib_commission_income_usd: true,
    total_net_profit_with_ib_usd: true,
    total_deposit_usd: false,
    total_withdrawal_usd: false,
    net_deposit_usd: true,
    total_volume_lots: true,
    auto_swap_free_status: true,
    is_enabled: true,
    last_updated: true,
  })
  const [gridApi, setGridApi] = useState<GridApi | null>(null)
  const gridContainerRef = useRef<HTMLDivElement | null>(null)

  // 判定暗色模式（用于样式注入）
  const isDarkMode = useMemo(() => {
    if (theme === 'dark') return true
    if (theme === 'light') return false
    try {
      return !!(window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches)
    } catch {
      return false
    }
  }, [theme])

  // 刷新状态与 Banner 文本
  const [isRefreshing, setIsRefreshing] = useState(false)
  const [refreshBanner, setRefreshBanner] = useState<string | null>(null)
  // 单独的 V2 刷新 Banner：与 client Banner 独立计时（各 10s）
  const [v2Banner, setV2Banner] = useState<string | null>(null)

  // filter state
  // fresh grad note: keep filter UI local, persist by a simple localStorage key
  const [filterBuilderOpen, setFilterBuilderOpen] = useState(false)
  const [appliedFilters, setAppliedFilters] = useState<FilterGroup | null>(null)
  const FILTERS_STORAGE_KEY = 'client_pnl_filters'
  // fresh grad note: limit zipcode details to keep banner readable
  const ZIPCODE_DETAIL_LIMIT = 10

  // Quick Time Range Filter state
  // fresh grad note: new state for quick time range filtering of account updates
  const [timeRange, setTimeRange] = useState<string>("all")

  // ClientPnL 可筛选字段定义（供筛选器使用）
  const CLIENT_FILTER_COLUMNS: ColumnMeta[] = useMemo(() => ([
    { id: 'client_id', label: tz('clientPnl.columns.clientId', '客户ID', 'Client ID'), type: 'text', filterable: true },
    { id: 'client_name', label: tz('clientPnl.columns.clientName', '客户名称', 'Client Name'), type: 'text', filterable: true },
    { id: 'zipcode', label: tz('clientPnl.columns.zipcode', 'Zipcode', 'Zipcode'), type: 'text', filterable: true },
    { id: 'account_count', label: tz('clientPnl.columns.accountCount', '账户数', 'Accounts'), type: 'number', filterable: true },
    { id: 'total_balance_usd', label: tz('clientPnl.columns.totalBalanceUsd', '总余额 (USD)', 'Total Balance (USD)'), type: 'number', filterable: true },
    { id: 'total_floating_pnl_usd', label: tz('clientPnl.columns.totalFloatingUsd', '总浮动盈亏 (USD)', 'Total Floating PnL (USD)'), type: 'number', filterable: true },
    { id: 'total_equity_usd', label: tz('clientPnl.columns.totalEquityUsd', '总净值 (USD)', 'Total Equity (USD)'), type: 'number', filterable: true },
    { id: 'total_closed_profit_usd', label: tz('clientPnl.columns.totalClosedProfitUsd', '总平仓盈亏 (USD)', 'Total Closed Profit (USD)'), type: 'number', filterable: true },
    { id: 'total_ib_commission_income_usd', label: tz('clientPnl.columns.totalCommissionUsd', 'IB佣金 (USD)', 'IB Commission (USD)'), type: 'number', filterable: true },
    { id: 'total_net_profit_with_ib_usd', label: tz('clientPnl.columns.totalNetProfitWithIbUsd', '净盈亏(含佣金) (USD)', 'Net PnL (w/ Comm) (USD)'), type: 'number', filterable: true },
    { id: 'total_deposit_usd', label: tz('clientPnl.columns.totalDepositUsd', '总入金 (USD)', 'Total Deposit (USD)'), type: 'number', filterable: true },
    { id: 'total_withdrawal_usd', label: tz('clientPnl.columns.totalWithdrawalUsd', '总出金 (USD)', 'Total Withdrawal (USD)'), type: 'number', filterable: true },
    { id: 'net_deposit_usd', label: tz('clientPnl.columns.netDepositUsd', '净入金 (USD)', 'Net Deposit (USD)'), type: 'number', filterable: true },
    { id: 'total_volume_lots', label: tz('clientPnl.columns.totalVolumeLots', '总交易手数', 'Total Volume (lots)'), type: 'number', filterable: true },
    { id: 'auto_swap_free_status', label: tz('clientPnl.columns.autoSwapFreeStatus', 'auto_swap_free_status', 'Auto Swap Free Status'), type: 'percent', filterable: true },
    { id: 'is_enabled', label: tz('clientPnl.columns.isEnabled', '是否启用', 'Enabled'), type: 'number', filterable: true },
    { id: 'last_updated', label: tz('pnlMonitor.columns.lastUpdated', '最后更新', 'Last Updated'), type: 'date', filterable: true },
  ]), [tz])

  // 记录主行，便于在排序时获取父级值
  const parentRowMap = useMemo(() => {
    const map = new Map<number | string, ClientPnLSummaryRow>()
    rows.forEach(row => {
      map.set(row.client_id, row)
    })
    return map
  }, [rows])

  const detailSortValueGetter = useCallback((params: any) => {
    if (params?.data?._rowType === 'detail' && params?.data?._parentClientId != null && params?.colDef?.field) {
      const parent = parentRowMap.get(params.data._parentClientId)
      if (parent) {
        if (params.colDef.field === 'primary_server') {
          return (parent as any)['zipcode']
        }
        return (parent as any)[params.colDef.field]
      }
    }
    return params.value
  }, [parentRowMap])

  // operator label resolver: language-aware, fallback to OPERATOR_LABELS or op code
  const getOperatorLabel = useCallback((op: string) => {
    const mapping: Record<string, [string, string]> = {
      eq: ['等于', 'equals'],
      ne: ['不等于', 'not equals'],
      gt: ['大于', 'greater than'],
      gte: ['大于等于', 'greater or equal'],
      lt: ['小于', 'less than'],
      lte: ['小于等于', 'less or equal'],
      contains: ['包含', 'contains'],
      not_contains: ['不包含', 'not contains'],
      starts_with: ['开头为', 'starts with'],
      ends_with: ['结尾为', 'ends with'],
      between: ['介于', 'between'],
      in: ['属于', 'in'],
      not_in: ['不属于', 'not in'],
      is_empty: ['为空', 'is empty'],
      is_not_empty: ['不为空', 'is not empty'],
      before: ['早于', 'before'],
      after: ['晚于', 'after'],
    }
    const pair = mapping[op]
    if (pair) {
      return tz(`filter.op.${op}`, pair[0], pair[1])
    }
    const alt = (OPERATOR_LABELS as any)?.[op]
    if (typeof alt === 'string' && alt) return alt
    return op
  }, [tz])

  const postSortRows = useCallback((params: PostSortRowsParams<ClientPnLSummaryRow>) => {
    const mainOrder: IRowNode<ClientPnLSummaryRow>[] = []
    const detailMap = new Map<number | string, IRowNode<ClientPnLSummaryRow>[]>()

    params.nodes.forEach(node => {
      const data = node.data as (ClientPnLSummaryRow & { _rowType?: string; _parentClientId?: number | string }) | undefined
      if (!data) return
      if (data._rowType === 'detail' && data._parentClientId != null) {
        const list = detailMap.get(data._parentClientId) || []
        list.push(node)
        detailMap.set(data._parentClientId, list)
      } else {
        mainOrder.push(node)
      }
    })

    const reordered: IRowNode<ClientPnLSummaryRow>[] = []

    mainOrder.forEach(node => {
      reordered.push(node)
      const data = node.data as (ClientPnLSummaryRow & { _rowType?: string }) | undefined
      if (!data) return
      const detailNodes = detailMap.get(data.client_id)
      if (detailNodes && detailNodes.length > 0) {
        reordered.push(...detailNodes)
        detailMap.delete(data.client_id)
      }
    })

    detailMap.forEach(nodes => {
      reordered.push(...nodes)
    })

    params.nodes.length = 0
    reordered.forEach(node => params.nodes.push(node))
  }, [])
  
  // 数据获取函数
  const fetchData = useCallback(async (page: number = pageIndex + 1) => {
    setLoading(true)
    try {
      // 构建排序参数
      const sortBy = sortModel.length > 0 ? sortModel[0].colId : undefined
      const sortOrder = sortModel.length > 0 ? sortModel[0].sort : 'asc'
      
      // 构建查询参数
      const params = new URLSearchParams({
        page: String(page),
        page_size: String(pageSize),
      })
      
      if (sortBy) {
        params.append('sort_by', sortBy)
        params.append('sort_order', sortOrder)
      }
      
      if (searchValue) {
        params.append('search', searchValue)
      }

      // add filters_json when there are active rules
      if (appliedFilters && Array.isArray(appliedFilters.rules) && appliedFilters.rules.length > 0) {
        params.append('filters_json', JSON.stringify(appliedFilters))
      }
      
      const response = await apiFetch(`/api/v1/client-pnl/summary/paginated?${params}`)
      const result = await response.json()
      
      if (result.ok) {
        setRows(result.data || [])
        setTotalCount(result.total || 0)
        setTotalPages(result.total_pages || 0)
        if (result.last_updated) {
          setLastUpdated(new Date(result.last_updated))
        }
      } else {
        console.error('获取数据失败:', result.error)
      }
    } catch (error) {
      console.error('获取数据异常:', error)
    } finally {
      setLoading(false)
    }
  }, [pageIndex, pageSize, sortModel, searchValue, appliedFilters])
  
  // 初始化加载数据
  useEffect(() => {
    fetchData()
  }, [fetchData])
  
  // 触发后端增量刷新：先刷新 V2 (MT5/MT4Live2) 并展示 Banner，再刷新 client 并展示 zipcode 详情 Banner
  const handleClientRefresh = useCallback(async () => {
    if (isRefreshing) return
    setIsRefreshing(true)
    setRefreshBanner(null)
    try {
      // 1) 并发刷新 V2 的两个 server
      const servers = ["MT5", "MT4Live2"] as const
      const v2Results = await Promise.allSettled(
        servers.map(async (srv) => {
          const res = await fetchWithTimeout('/api/v1/etl/pnl-user-summary/refresh', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', accept: 'application/json' },
            body: JSON.stringify({ server: srv })
          }, 60000)
          if (!res.ok) {
            // try json detail then fallback
            try {
              const err = await res.json()
              const msg = (err && (err.detail || err.message)) ? `HTTP ${res.status}: ${err.detail || err.message}` : `HTTP ${res.status}`
              throw new Error(msg)
            } catch {
              throw new Error(`HTTP ${res.status}`)
            }
          }
          const data = (await res.json()) as EtlRefreshResponse
          if (data.status === 'error') {
            throw new Error(data.message || '刷新失败')
          }
          const parts: string[] = []
          if (typeof data.new_trades_count === 'number') parts.push(`成交 ${data.new_trades_count}`)
          // 与 V2 页面一致：MT4Live2 不展示浮动更新数
          if (srv !== 'MT4Live2' && typeof data.floating_only_count === 'number') parts.push(`浮盈更新 ${data.floating_only_count}`)
          if (typeof data.duration_seconds === 'number') parts.push(`用时 ${Number(data.duration_seconds).toFixed(1)}s`)
          const summary = parts.length > 0 ? parts.join('，') : '刷新完成'
          return `${srv}: ${summary}`
        })
      )
      const v2MsgParts: string[] = []
      v2Results.forEach((r, idx) => {
        const srv = servers[idx]
        if (r.status === 'fulfilled') {
          v2MsgParts.push(r.value)
        } else {
          v2MsgParts.push(`${srv}: 失败(${(r.reason as Error)?.message || 'Unknown'})`)
        }
      })
      if (v2MsgParts.length > 0) {
        setV2Banner(`Account Based Server updated：${v2MsgParts.join('，')}`)
      }

      // 2) 刷新 client 汇总，并展示 zipcode 变化详情
      const res = await apiFetch('/api/v1/etl/client-pnl/refresh', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', accept: 'application/json' },
      })
      if (!res.ok) {
        let msg = `HTTP ${res.status}`
        try { const err = await res.json(); if (err?.detail) msg = `HTTP ${res.status}: ${err.detail}` } catch {}
        throw new Error(msg)
      }
      const data = await res.json()
      // 汇总关键信息
      const steps: any[] = Array.isArray(data?.steps) ? data.steps : []
      const find = (name: string) => steps.find(s => s?.name === name) || {}
      const cand = find('candidates')
      // steps 'accounts_upsert' and 'delete_orphans' are not used in banner
      const map = find('mapping')
      const sum = find('summary_upsert')
      // 仅展示：更新的 clientid 数量 + Zipcode 变化详情
      const parts: string[] = []

      // 更新数量：优先 summary_upsert.affected_rows；否则 candidates.total；否则 0
      const updatedCount = (typeof sum.affected_rows === 'number')
        ? Number(sum.affected_rows)
        : (typeof cand.total === 'number' ? Number(cand.total) : 0)
      parts.push(tx('clientPnl.refreshMessages.updated', `更新客户汇总 ${updatedCount}`).replace('{count}', String(updatedCount)))

      // Zipcode 变化数量
      const zipcodeChanges = typeof map.zipcode_changes === 'number' ? Number(map.zipcode_changes) : 0

      // Zipcode 详情：优先 steps.zipcode_details；否则从 raw_log 解析
      let zipcodeDetails: Array<{ clientid: string | number, before: string | null, after: string | null }> = []
      if (Array.isArray((map as any)?.zipcode_details)) {
        zipcodeDetails = (map as any).zipcode_details as any
      } else if (typeof (data as any)?.raw_log === 'string') {
        try {
          const raw: string = (data as any).raw_log
          const regex = /client_id=(\d+)\s+old_zipcode=([^\s]+)?\s+new_zipcode=([^\s]+)?/g
          let m: RegExpExecArray | null
          while ((m = regex.exec(raw)) !== null) {
            zipcodeDetails.push({ clientid: m[1], before: m[2] || null, after: m[3] || null })
          }
        } catch {}
      }

      if (zipcodeChanges > 0) {
        const limitedZipcodeDetails = zipcodeDetails.slice(0, ZIPCODE_DETAIL_LIMIT)
        const detailTexts = limitedZipcodeDetails.map(d => (tx('clientPnl.refreshMessages.zipcodeChangeDetail', `clientid ${d.clientid} (before: ${d.before ?? ''}, after: ${d.after ?? ''})`)
          .replace('{clientid}', String(d.clientid))
          .replace('{before}', String(d.before ?? ''))
          .replace('{after}', String(d.after ?? ''))))
        const detailsJoined = detailTexts.join(tx('common.comma', '，'))
        parts.push(
          tx('clientPnl.refreshMessages.zipcodeChanges', `Zipcode变更 ${zipcodeChanges}${detailsJoined ? `：${detailsJoined}` : ''}`)
            .replace('{count}', String(zipcodeChanges))
            .replace('{details}', detailsJoined)
        )
        if (zipcodeDetails.length > limitedZipcodeDetails.length) {
          const remainingCount = zipcodeDetails.length - limitedZipcodeDetails.length
          const moreText = tz('clientPnl.refreshMessages.zipcodeChangesMore', '还有 {count} 条 Zipcode 变更未展示', '{count} more Zipcode changes not listed')
            .replace('{count}', String(remainingCount))
          parts.push(moreText)
        }
      } else {
        parts.push(tx('clientPnl.refreshMessages.zipcodeNoChange', 'Zipcode变更 0'))
      }

      const message = parts.join('，')
      setRefreshBanner(message)
      // 刷新表格数据
      try { await fetchData() } catch {}
    } catch (e: any) {
      setRefreshBanner(e?.message || (t('pnlMonitor.refreshFailed') || '刷新失败'))
    } finally {
      setIsRefreshing(false)
    }
  }, [isRefreshing, fetchData])

  // 刷新 Banner 10 秒后自动消失
  useEffect(() => {
    if (!refreshBanner) return
    const timer = setTimeout(() => setRefreshBanner(null), 10000)
    return () => clearTimeout(timer)
  }, [refreshBanner])

  // V2 Banner 10 秒后自动消失
  useEffect(() => {
    if (!v2Banner) return
    const timer = setTimeout(() => setV2Banner(null), 10000)
    return () => clearTimeout(timer)
  }, [v2Banner])
  
  // filter: restore from localStorage on mount
  useEffect(() => {
    try {
      const saved = localStorage.getItem(FILTERS_STORAGE_KEY)
      if (saved) {
        const parsed = JSON.parse(saved) as FilterGroup
        setAppliedFilters(parsed)
      }
    } catch {}
  }, [])

  // filter: apply/save/clear helpers
  const handleApplyFilters = useCallback((filters: FilterGroup) => {
    setAppliedFilters(filters)
    try { localStorage.setItem(FILTERS_STORAGE_KEY, JSON.stringify(filters)) } catch {}
    // fresh grad note: reset to first page when filters applied
    setPageIndex(0)
    // optional: debug print
    console.log('filters applied:', JSON.stringify(filters))
  }, [])

  const handleRemoveFilter = useCallback((ruleIndex: number) => {
    setAppliedFilters(prev => {
      if (!prev) return null
      const nextRules = prev.rules.filter((_, i) => i !== ruleIndex)
      const next = nextRules.length > 0 ? { ...prev, rules: nextRules } : null
      try {
        if (next) localStorage.setItem(FILTERS_STORAGE_KEY, JSON.stringify(next))
        else localStorage.removeItem(FILTERS_STORAGE_KEY)
      } catch {}
      setPageIndex(0)
      return next
    })
  }, [])

  const handleClearFilters = useCallback(() => {
    setAppliedFilters(null)
    try { localStorage.removeItem(FILTERS_STORAGE_KEY) } catch {}
    setPageIndex(0)
  }, [])
  
  // 搜索处理
  const handleSearch = useCallback(() => {
    setSearchValue(searchInput)
    setPageIndex(0)
  }, [searchInput])
  
  const handleSearchKeyDown = useCallback((e: React.KeyboardEvent) => {
    if (e.key === 'Enter') {
      handleSearch()
    }
  }, [handleSearch])
  
  const handleClearSearch = useCallback(() => {
    setSearchInput("")
    setSearchValue("")
    setPageIndex(0)
  }, [])

  // Quick Time Range Handler
  const handleTimeRangeChange = useCallback((value: string) => {
    setTimeRange(value)
    
    // Calculate target date based on range
    let targetDate: Date | null = null
    const now = new Date()
    
    if (value === '1w') {
      targetDate = new Date(now.getTime() - 7 * 24 * 60 * 60 * 1000)
    } else if (value === '2w') {
      targetDate = new Date(now.getTime() - 14 * 24 * 60 * 60 * 1000)
    } else if (value === '1m') {
      targetDate = new Date(now.getTime() - 30 * 24 * 60 * 60 * 1000)
    } else if (value === '3m') {
      targetDate = new Date(now.getTime() - 90 * 24 * 60 * 60 * 1000)
    }
    
    // Update filters
    setAppliedFilters(prev => {
      // Remove existing account_last_updated rule if any
      const existingRules = prev?.rules.filter(r => r.field !== 'account_last_updated') || []
      
      if (!targetDate) {
        // "all" selected -> just return remaining rules (or null if empty)
        if (existingRules.length === 0) return null
        return { join: prev?.join || 'AND', rules: existingRules }
      }
      
      // Add new rule
      const newRule = {
        field: 'account_last_updated',
        op: 'after' as FilterOperator,
        value: targetDate.toISOString()
      }
      
      return {
        join: prev?.join || 'AND',
        rules: [...existingRules, newRule]
      }
    })
    
    setPageIndex(0)
  }, [])
  
  // Fetch account rows for a client only when needed
  const fetchAccountsForClient = useCallback(async (clientId: number | string) => {
    const key = String(clientId)
    if (accountLoadingMap[key]) {
      return
    }

    setAccountLoadingMap(prev => ({ ...prev, [key]: true }))

    try {
      // Ensure clientId is converted to number for API call
      const clientIdNum = typeof clientId === 'string' ? Number(clientId) : clientId
      if (!Number.isFinite(clientIdNum) || clientIdNum <= 0) {
        throw new Error(`Invalid client_id: ${clientId}`)
      }

      const response = await apiFetch(`/api/v1/client-pnl/${clientIdNum}/accounts`)
      
      // Check HTTP status before parsing JSON
      if (!response.ok) {
        const errorText = await response.text()
        throw new Error(`HTTP ${response.status}: ${errorText || response.statusText}`)
      }

      const result = await response.json()

      if (result.ok && Array.isArray(result.accounts)) {
        // Convert client_id to string for comparison to handle type mismatch
        const clientIdStr = String(clientId)
        setRows(prevRows =>
          prevRows.map(row => {
            // Compare as strings to handle type mismatch (number vs string)
            const rowClientIdStr = String(row.client_id)
            if (rowClientIdStr === clientIdStr) {
              return {
                ...row,
                accounts: result.accounts,
              }
            }
            return row
          }),
        )
      } else {
        const errorMsg = result.error || 'Unknown error'
        console.error(`加载账户失败 (ClientID: ${clientId}):`, errorMsg)
        // Show error to user (optional: you can add a toast notification here)
      }
    } catch (error: any) {
      console.error(`加载账户异常 (ClientID: ${clientId}):`, error)
      // Show error to user (optional: you can add a toast notification here)
    } finally {
      setAccountLoadingMap(prev => {
        const { [key]: _ignored, ...rest } = prev
        return rest
      })
    }
  }, [accountLoadingMap])

  // 切换展开/收起
  const toggleExpand = useCallback(async (row: ClientPnLSummaryRow) => {
    const clientId = row.client_id
    // Normalize clientId to string for consistent comparison
    const clientIdStr = String(clientId)

    // Check if already expanded (compare as strings)
    const isExpanded = Array.from(expandedClients).some(id => String(id) === clientIdStr)
    
    if (isExpanded) {
      setExpandedClients(prev => {
        const next = new Set(prev)
        // Remove by matching string representation
        Array.from(next).forEach(id => {
          if (String(id) === clientIdStr) {
            next.delete(id)
          }
        })
        return next
      })
      return
    }

    // Fetch accounts if not already loaded
    if (typeof row.accounts === "undefined") {
      await fetchAccountsForClient(clientId)
    }

    // Add to expanded set (use original value to maintain type consistency)
    setExpandedClients(prev => {
      const next = new Set(prev)
      next.add(clientId)
      return next
    })
  }, [expandedClients, fetchAccountsForClient])
  
  // 保存列状态
  const saveGridState = useCallback(() => {
    // isDestroyed + Array.isArray: a destroyed grid api returns undefined
    // (warning, no throw) — stringifying that would persist the literal
    // string "undefined" and corrupt the saved layout.
    if (!gridApi || gridApi.isDestroyed()) return
    try {
      const state = gridApi.getColumnState()
      if (!Array.isArray(state)) return
      localStorage.setItem('client_pnl_grid_state', JSON.stringify(state))
    } catch {}
  }, [gridApi])
  
  // 节流保存
  const throttledSaveGridState = useMemo(() => {
    let last = 0
    let timer: any
    return () => {
      const now = Date.now()
      if (now - last >= 300) {
        last = now
        saveGridState()
      } else {
        clearTimeout(timer)
        timer = setTimeout(() => {
          last = Date.now()
          saveGridState()
        }, 300 - (now - last))
      }
    }
  }, [saveGridState])
  
  // AG Grid 列定义
  const columnDefs = useMemo<ColDef[]>(() => [
    {
      field: "client_id",
      headerName: tz("clientPnl.columns.clientIdLogin", "Client ID / Login", "Client ID / Login"),
      width: 90,
      minWidth: 50,
      maxWidth: 160,
      sortable: true,
      filter: true,
      sortValueGetter: detailSortValueGetter,
      cellRenderer: (params: any) => {
        const rowType = params.data?._rowType
        
        // 明细行显示 Login
        if (rowType === 'detail') {
          const loginLink = getAccountCrmLink(params.data?.server, params.data?.login)
          return (
            <span className="font-mono text-sm font-semibold text-muted-foreground pl-1">
              {loginLink ? (
                <a
                  href={loginLink}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="underline hover:no-underline font-semibold"
                  onClick={(e) => e.stopPropagation()}
                >
                  {params.data.login}
                </a>
              ) : params.data.login}
            </span>
          )
        }
        
        // 主行显示 Client ID
        const clientLink = getClientCrmLink(params.value)
        if (clientLink) {
          return (
            <a
              href={clientLink}
              target="_blank"
              rel="noopener noreferrer"
              className="font-medium text-blue-600 hover:text-blue-800 dark:text-blue-400 dark:hover:text-blue-300 underline hover:no-underline"
              onClick={(e) => e.stopPropagation()}
            >
              {params.value}
            </a>
          )
        }
        return <span className="font-medium">{params.value}</span>
      },
      hide: !columnVisibility.client_id,
    },
    {
      field: "client_name",
      headerName: tz("clientPnl.columns.clientNameGroup", "客户名称 / 组别", "Client Name / Group"),
      width: 180,
      minWidth: 150,
      maxWidth: 300,
      sortable: true,
      filter: true,
      sortValueGetter: detailSortValueGetter,
      cellRenderer: (params: any) => {
        const rowType = params.data?._rowType
        
        // 明细行显示组别
        if (rowType === 'detail') {
          return (
            <span className="text-sm font-semibold text-muted-foreground pl-1">
              {params.data.user_group || '-'}
            </span>
          )
        }
        
        // 主行显示客户名称
        return (
          <span className="max-w-[180px] truncate">
            {params.value || `客户-${params.data.client_id}`}
          </span>
        )
      },
      hide: !columnVisibility.client_name,
    },
    {
      field: "primary_server",
      headerName: tz("clientPnl.columns.zipcodeServer", "Zipcode / 服务器", "Zipcode / Server"),
      width: 80,
      minWidth: 50,
      maxWidth: 160,
      sortable: true,
      filter: true,
      sortValueGetter: detailSortValueGetter,
      cellRenderer: (params: any) => {
        const rowType = params.data?._rowType
        
        // 明细行显示账户服务器
        if (rowType === 'detail') {
          return (
            <span className="text-sm font-semibold pl-1">{params.data.server || ''}</span>
          )
        }
        
        // 主行显示 zipcode
        return <span className="text-muted-foreground">{params.data?.zipcode || ''}</span>
      },
      hide: !columnVisibility.primary_server,
    },
    {
      field: "account_count",
      headerName: tz("clientPnl.columns.accountCountCurrency", "账户数 / 币种", "Accounts / Currency"),
      width: 100,
      minWidth: 80,
      maxWidth: 130,
      sortable: true,
      filter: true,
      sortValueGetter: detailSortValueGetter,
      cellRenderer: (params: any) => {
        const rowType = params.data?._rowType
        
        // 明细行显示币种
        if (rowType === 'detail') {
          const cur = String(params.data.currency || '').toUpperCase()
          let badge = 'bg-slate-200 text-slate-800 dark:bg-slate-700 dark:text-slate-200'
          if (cur === 'CEN') badge = 'bg-red-100 text-red-800 dark:bg-red-900/30 dark:text-red-300'
          else if (cur === 'USD') badge = 'bg-cyan-100 text-cyan-800 dark:bg-cyan-900/30 dark:text-cyan-300'
          else if (cur === 'USDT') badge = 'bg-purple-100 text-purple-800 dark:bg-purple-900/30 dark:text-purple-300'
          return (
            <span className={`text-sm font-semibold font-mono pl-1`}>
              <span className={`inline-block rounded px-1.5 py-0.5 ${badge}`}>{cur || '-'}</span>
            </span>
          )
        }
        
        // 主行显示账户数（可点击展开）
        const clientRow = params.data as ClientPnLSummaryRow & { _rowType?: string }
        const clientId = clientRow.client_id
        // Check expansion status by comparing string representations
        const clientIdStr = String(clientId)
        const isExpanded = Array.from(expandedClients).some(id => String(id) === clientIdStr)
        const isLoadingAccounts = !!accountLoadingMap[clientIdStr]
        const count = params.value || 0
        
        return (
          <button
            type="button"
            onClick={(e) => {
              e.preventDefault()
              e.stopPropagation()
              void toggleExpand(clientRow)
            }}
            className="flex items-center gap-1 text-blue-600 hover:text-blue-800 dark:text-blue-400 dark:hover:text-blue-300 cursor-pointer font-medium transition-colors"
            disabled={isLoadingAccounts}
          >
            <span className={`transform transition-transform ${isExpanded ? 'rotate-90' : ''} ${isLoadingAccounts ? 'opacity-0' : ''}`}>
              ▶
            </span>
            {isLoadingAccounts && (
              <RefreshCw className="h-3 w-3 animate-spin" />
            )}
            <span className="tabular-nums">{count}</span>
          </button>
        )
      },
      hide: !columnVisibility.account_count,
    },
    {
      field: "total_balance_usd",
      headerName: tz("clientPnl.columns.balanceUsd", "余额 (USD)", "Balance (USD)"),
      width: 140,
      minWidth: 120,
      maxWidth: 200,
      sortable: true,
      filter: true,
      sortValueGetter: detailSortValueGetter,
      cellRenderer: (params: any) => {
        const rowType = params.data?._rowType
        
        // 明细行显示账户余额
        if (rowType === 'detail') {
          const value = toNumber(params.data.balance_usd)
          return (
            <span 
              className={`text-right text-sm font-semibold ${value < 0 ? "text-red-600 dark:text-red-400" : "text-emerald-600 dark:text-emerald-400"}`}
            >
              {formatCurrency(value)}
            </span>
          )
        }
        
        // 主行显示总余额
        const value = toNumber(params.value)
        return (
          <span 
            className={`text-right ${value < 0 ? "text-red-600 dark:text-red-400" : "text-emerald-600 dark:text-emerald-400"}`}
          >
            {formatCurrency(value)}
          </span>
        )
      },
      hide: !columnVisibility.total_balance_usd,
    },
    {
      field: "total_floating_pnl_usd",
      headerName: tz("clientPnl.columns.floatingUsd", "浮动盈亏 (USD)", "Floating PnL (USD)"),
      width: 150,
      minWidth: 120,
      maxWidth: 200,
      sortable: true,
      filter: true,
      sortValueGetter: detailSortValueGetter,
      cellRenderer: (params: any) => {
        const rowType = params.data?._rowType
        const value = rowType === 'detail' ? toNumber(params.data.floating_pnl_usd) : toNumber(params.value)
        
        return (
          <span 
            className={`text-right ${rowType === 'detail' ? 'text-sm font-semibold' : ''} ${value < 0 ? "text-red-600 dark:text-red-400" : "text-emerald-600 dark:text-emerald-400"}`}
          >
            {formatCurrency(value)}
          </span>
        )
      },
      hide: !columnVisibility.total_floating_pnl_usd,
    },
    {
      field: "total_equity_usd",
      headerName: tz("clientPnl.columns.equityUsd", "净值 (USD)", "Equity (USD)"),
      width: 150,
      minWidth: 120,
      maxWidth: 200,
      sortable: true,
      filter: true,
      sortValueGetter: detailSortValueGetter,
      cellRenderer: (params: any) => {
        const rowType = params.data?._rowType
        const value = rowType === 'detail' ? toNumber(params.data.equity_usd) : toNumber(params.value)
        
        return (
          <span 
            className={`text-right ${rowType === 'detail' ? 'text-sm font-semibold' : ''} ${value < 0 ? "text-red-600 dark:text-red-400" : "text-emerald-600 dark:text-emerald-400"}`}
          >
            {formatCurrency(value)}
          </span>
        )
      },
      hide: !columnVisibility.total_equity_usd,
    },
    {
      field: "total_closed_profit_usd",
      headerName: tz("clientPnl.columns.closedProfitUsd", "平仓盈亏 (USD)", "Closed Profit (USD)"),
      width: 150,
      minWidth: 120,
      maxWidth: 200,
      sortable: true,
      filter: true,
      sortValueGetter: detailSortValueGetter,
      cellStyle: (params: any) => {
        const rowType = params.data?._rowType
        return rowType === 'detail' ? undefined : { backgroundColor: 'rgba(0,0,0,0.035)' }
      },
      cellRenderer: (params: any) => {
        const rowType = params.data?._rowType
        const value = rowType === 'detail' ? toNumber(params.data.closed_profit_usd) : toNumber(params.value)
        
        return (
          <span className={`text-right ${rowType === 'detail' ? 'text-sm font-semibold' : ''} ${value < 0 ? "text-red-600 dark:text-red-400" : "text-emerald-600 dark:text-emerald-400"}`}>
            {formatCurrency(value)}
          </span>
        )
      },
      hide: !columnVisibility.total_closed_profit_usd,
    },
    {
      field: "total_ib_commission_income_usd",
      headerName: tz("clientPnl.columns.totalCommissionUsd", "IB佣金 (USD)", "IB Commission (USD)"),
      width: 140,
      minWidth: 120,
      maxWidth: 200,
      sortable: true,
      filter: true,
      sortValueGetter: detailSortValueGetter,
      cellRenderer: (params: any) => {
        const rowType = params.data?._rowType
        const value = rowType === 'detail' 
          ? toNumber(params.data.ib_commission_income_usd) 
          : toNumber(params.value)
        return (
          <span className="text-right text-sm font-semibold text-blue-600 dark:text-blue-400">{formatCurrency(value)}</span>
        )
      },
      hide: !columnVisibility.total_ib_commission_income_usd,
    },
    {
      field: "total_net_profit_with_ib_usd",
      headerName: tz("clientPnl.columns.totalNetProfitWithIbUsd", "净盈亏(含佣金) (USD)", "Net PnL (w/ Comm) (USD)"),
      width: 150,
      minWidth: 120,
      maxWidth: 200,
      sortable: true,
      filter: true,
      sortValueGetter: detailSortValueGetter,
      cellStyle: (params: any) => {
        const rowType = params.data?._rowType
        return rowType === 'detail' ? undefined : { backgroundColor: 'rgba(255,165,0,0.08)' } // 橙色半透明底色区分
      },
      cellRenderer: (params: any) => {
        const rowType = params.data?._rowType
        let value = 0
        if (rowType === 'detail') {
          // 明细行需前端动态计算
          const closed = toNumber(params.data.closed_profit_usd)
          const ib = toNumber(params.data.ib_commission_income_usd)
          value = closed + ib
        } else {
          // 主行直接取后端返回
          value = toNumber(params.value)
        }
        
        return (
          <span className={`text-right ${rowType === 'detail' ? 'text-sm font-semibold' : ''} ${value < 0 ? "text-red-600 dark:text-red-400" : "text-emerald-600 dark:text-emerald-400"}`}>
            {formatCurrency(value)}
          </span>
        )
      },
      hide: !columnVisibility.total_net_profit_with_ib_usd,
    },
    {
      field: "total_deposit_usd",
      headerName: tz("clientPnl.columns.totalDepositUsd", "总入金 (USD)", "Total Deposit (USD)"),
      width: 140,
      minWidth: 120,
      maxWidth: 200,
      sortable: true,
      filter: true,
      sortValueGetter: detailSortValueGetter,
      cellRenderer: (params: any) => {
        const rowType = params.data?._rowType
        // fresh grad note: detail row uses per-account deposit_usd
        const value = rowType === 'detail' ? toNumber(params.data.deposit_usd) : toNumber(params.value)
        return (
          <span className="text-right text-sm font-semibold">{formatCurrency(value)}</span>
        )
      },
      hide: !columnVisibility.total_deposit_usd,
    },
    {
      field: "total_withdrawal_usd",
      headerName: tz("clientPnl.columns.totalWithdrawalUsd", "总出金 (USD)", "Total Withdrawal (USD)"),
      width: 140,
      minWidth: 120,
      maxWidth: 200,
      sortable: true,
      filter: true,
      sortValueGetter: detailSortValueGetter,
      cellRenderer: (params: any) => {
        const rowType = params.data?._rowType
        // fresh grad note: detail row uses per-account withdrawal_usd
        const value = rowType === 'detail' ? toNumber(params.data.withdrawal_usd) : toNumber(params.value)
        return (
          <span className="text-right">{formatCurrency(value)}</span>
        )
      },
      hide: !columnVisibility.total_withdrawal_usd,
    },
    {
      field: "net_deposit_usd",
      headerName: tz("clientPnl.columns.netDepositUsd", "净入金 (USD)", "Net Deposit (USD)"),
      width: 140,
      minWidth: 120,
      maxWidth: 200,
      sortable: true,
      filter: true,
      sortValueGetter: detailSortValueGetter,
      cellRenderer: (params: any) => {
        const rowType = params.data?._rowType
        // fresh grad note: detail row computes deposit_usd - withdrawal_usd
        const value = rowType === 'detail'
          ? toNumber(params.data.deposit_usd) - toNumber(params.data.withdrawal_usd)
          : toNumber(params.value)
        return (
          <span className={`text-right ${rowType === 'detail' ? 'text-sm font-semibold' : ''} ${value < 0 ? "text-red-600 dark:text-red-400" : "text-emerald-600 dark:text-emerald-400"}`}>
            {formatCurrency(value)}
          </span>
        )
      },
      hide: !columnVisibility.net_deposit_usd,
    },
    {
      field: "total_volume_lots",
      headerName: tz("clientPnl.columns.totalVolumeLots", "交易手数", "Volume (lots)"),
      width: 140,
      minWidth: 100,
      maxWidth: 200,
      sortable: true,
      filter: true,
      sortValueGetter: detailSortValueGetter,
      cellRenderer: (params: any) => {
        const rowType = params.data?._rowType
        const value = rowType === 'detail' ? toNumber(params.data.volume_lots) : toNumber(params.value)
        
        return (
          <span className={`text-right tabular-nums ${rowType === 'detail' ? 'text-sm font-semibold' : ''}`}>
            {value.toLocaleString(undefined, { minimumFractionDigits: 4, maximumFractionDigits: 4 })}
          </span>
        )
      },
      hide: !columnVisibility.total_volume_lots,
    },
    {
      field: "auto_swap_free_status",
      headerName: tz("clientPnl.columns.autoSwapFreeStatus", "auto_swap_free_status", "Auto Swap Free Status"),
      width: 180,
      minWidth: 140,
      maxWidth: 240,
      sortable: true,
      filter: true,
      sortValueGetter: detailSortValueGetter,
      cellRenderer: (params: any) => {
        const raw = toNumber(params.value ?? (params.data?._rowType === 'detail' ? params.data?.auto_swap_free_status : params.data?.auto_swap_free_status), -1)
        const isDetail = params.data?._rowType === 'detail'
        if (!Number.isFinite(raw) || raw < 0) {
          return <span className={`text-muted-foreground ${isDetail ? 'text-sm font-semibold' : ''}`}>-</span>
        }
        const ratio = Math.max(0, Math.min(1, raw))
        const pct = (ratio * 100).toFixed(1) + '%'
        return <span className={`tabular-nums ${isDetail ? 'text-sm font-semibold' : ''}`}>{pct}</span>
      },
      cellStyle: (params: any) => {
        const raw = toNumber(params.value ?? (params.data?._rowType === 'detail' ? params.data?.auto_swap_free_status : params.data?.auto_swap_free_status), -1)
        if (!Number.isFinite(raw) || raw < 0) return null
        const ratio = Math.max(0, Math.min(1, raw))
        if (ratio < 0.2) return { backgroundColor: 'rgba(16,185,129,0.15)', color: '#111' } as any
        if (ratio < 0.5) return { backgroundColor: 'rgba(245,158,11,0.18)', color: '#111' } as any
        return { backgroundColor: 'rgba(239,68,68,0.18)', color: '#111' } as any
      },
      hide: !columnVisibility.auto_swap_free_status,
    },
    {
      field: "is_enabled",
      headerName: tz("clientPnl.columns.isEnabled", "是否启用", "Enabled"),
      width: 120,
      minWidth: 100,
      maxWidth: 180,
      sortable: true,
      filter: true,
      sortValueGetter: detailSortValueGetter,
      cellRenderer: (params: any) => {
        const rowType = params.data?._rowType
        if (rowType === 'detail') {
          return <span className="text-sm font-semibold text-muted-foreground">-</span>
        }
        const v = params.value ?? params.data?.is_enabled
        const isOn = (typeof v === 'number') ? v === 1 : !!v
        return <span className={isOn ? 'text-emerald-600 dark:text-emerald-400' : 'text-red-600 dark:text-red-400'}>{isOn ? tz('clientPnl.enabled', '启用', 'Enabled') : tz('clientPnl.disabled', '禁用', 'Disabled')}</span>
      },
      hide: !columnVisibility.is_enabled,
    },
    {
      field: "last_updated",
      headerName: tz("pnlMonitor.columns.lastUpdated", "最后更新", "Last Updated"),
      width: 180,
      minWidth: 160,
      maxWidth: 220,
      sortable: true,
      filter: false,
      sortValueGetter: detailSortValueGetter,
      cellRenderer: (params: any) => {
        const isDetail = params.data?._rowType === 'detail'
        return (
          <span className={`whitespace-nowrap text-muted-foreground ${isDetail ? 'text-sm font-semibold pl-1' : ''}`}>
            {params.value ? new Date(params.value).toLocaleString() : ""}
          </span>
        )
      },
      hide: !columnVisibility.last_updated,
    },
  ], [columnVisibility, expandedClients, toggleExpand, accountLoadingMap, detailSortValueGetter, tx, t])
  
  // AG Grid 事件处理
  const onGridReady = useCallback((params: GridReadyEvent) => {
    setGridApi(params.api as any)
    
    try {
      const savedStateRaw = localStorage.getItem('client_pnl_grid_state')
      if (savedStateRaw) {
        const savedState = JSON.parse(savedStateRaw)
        if (Array.isArray(savedState) && savedState.length > 0) {
          ;(params.api as any).applyColumnState({ state: savedState, applyOrder: true })
          
          const visibilityFromState: Record<string, boolean> = {}
          const sortModelFromState: any[] = []
          
          savedState.forEach((s: any) => {
            if (s && typeof s.colId === 'string') {
              visibilityFromState[s.colId] = !s.hide
            }
            if (s.sort) {
              sortModelFromState.push({ colId: s.colId, sort: s.sort })
            }
          })
          
          if (Object.keys(visibilityFromState).length > 0) {
            setColumnVisibility(prev => ({ ...prev, ...visibilityFromState }))
          }
          if (sortModelFromState.length > 0) {
            setSortModel(sortModelFromState)
          }
          return
        }
      }
      
      ;(params.api as any).applyColumnState({ state: sortModel, defaultState: { sort: null } })
    } catch (e) {
      console.error("Failed to restore grid state", e)
    }
  }, [sortModel])
  
  const onSortChanged = useCallback((event: SortChangedEvent) => {
    const newSortModel = (event.api as any).getColumnState()
      .filter((col: any) => col.sort)
      .map((col: any) => ({ colId: col.colId, sort: col.sort }))
    setSortModel(newSortModel)
    saveGridState()
  }, [saveGridState])
  
  // 生成扁平化的行数据（主行 + 展开的账户明细行）
  const flatRows = useMemo(() => {
    const result: any[] = []
    
    rows.forEach(row => {
      // 添加主行
      result.push({
        ...row,
        _rowType: 'main',
      })
      
      // 如果该客户被展开，则添加账户明细行
      // Check expansion status by comparing string representations
      const rowClientIdStr = String(row.client_id)
      const isExpanded = Array.from(expandedClients).some(id => String(id) === rowClientIdStr)
      
      if (isExpanded && row.accounts && row.accounts.length > 0) {
        row.accounts.forEach(acc => {
          result.push({
            ...acc,
            _rowType: 'detail',
            _parentClientId: row.client_id,
          })
        })
      }
    })
    
    return result
  }, [rows, expandedClients])
  
  // 筛选功能暂时移除，等待新后端方案
  
  return (
    <div className="flex h-full w-full flex-col gap-2 p-1 sm:p-4">
      {/* 状态栏 + 筛选 + 列显示切换 */}
      <Card>
        <CardContent className="py-3">
          <div className="flex flex-col gap-3">
            {/* 第一行：状态信息与按钮 */}
            {/* 移动端：第一行仅显示搜索（含清除/触发），保持一行 */}
            <div className="sm:hidden flex items-center gap-2">
              <div className="flex items-center gap-1 w-full">
                <Input
                  type="text"
                  placeholder={tz('clientPnl.searchPlaceholder', '搜索 ClientID / AccountID', 'Search ClientID / AccountID')}
                  value={searchInput}
                  onChange={(e) => setSearchInput(e.target.value)}
                  onKeyDown={handleSearchKeyDown}
                  className="h-9 w-full"
                />
                {searchValue && (
                  <Button
                    variant="ghost"
                    size="sm"
                    onClick={handleClearSearch}
                    className="h-9 px-2"
                  >
                    <X className="h-4 w-4" />
                  </Button>
                )}
                <Button
                  onClick={handleSearch}
                  className="h-9 px-3"
                  disabled={loading}
                >
                  <Search className="h-4 w-4" />
                </Button>
              </div>
            </div>

            {/* Time Range Selector (Mobile: separate row) */}
            <div className="sm:hidden w-full">
              <Select value={timeRange} onValueChange={handleTimeRangeChange}>
                <SelectTrigger className="h-9 w-full">
                  <SelectValue placeholder={tz('pnlMonitor.timeRange', '账户活跃时间', 'Account Active Time')} />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="all">{tz('pnlMonitor.timeRangeAll', '全部时间', 'All Time')}</SelectItem>
                  <SelectItem value="1w">{tz('pnlMonitor.timeRange1w', '过去 1 周', 'Past 1 Week')}</SelectItem>
                  <SelectItem value="2w">{tz('pnlMonitor.timeRange2w', '过去 2 周', 'Past 2 Weeks')}</SelectItem>
                  <SelectItem value="1m">{tz('pnlMonitor.timeRange1m', '过去 1 个月', 'Past 1 Month')}</SelectItem>
                  <SelectItem value="3m">{tz('pnlMonitor.timeRange3m', '过去 3 个月', 'Past 3 Months')}</SelectItem>
                </SelectContent>
              </Select>
            </div>

            {/* 移动端：刷新单独一行，按钮占满整行 */}
            <div className="sm:hidden">
              <Button onClick={handleClientRefresh} className="h-9 w-full" disabled={true}>
                {isRefreshing ? (t('pnlMonitor.refreshing') || '刷新中…') : (t('pnlMonitor.refresh') || '刷新')}
              </Button>
            </div>

            {/* 移动端：第二行两个操作按钮等宽一排（筛选/列切换） */}
            <div className="grid grid-cols-2 gap-2 sm:hidden">
              <Button 
                onClick={() => setFilterBuilderOpen(true)} 
                className="h-9 w-full gap-2 whitespace-nowrap bg-black hover:bg-black/90 dark:bg-white dark:text-black dark:hover:bg-white/90"
              >
                <Filter className="h-4 w-4" />
                {t('pnlMonitor.filter')}
                {appliedFilters && appliedFilters.rules.length > 0 && (
                  <Badge variant="secondary" className="ml-1 h-5 min-w-5 px-1.5 text-xs">
                    {appliedFilters.rules.length}
                  </Badge>
                )}
              </Button>
              <DropdownMenu>
                <DropdownMenuTrigger asChild>
                  <Button variant="outline" className="h-9 w-full gap-2 whitespace-nowrap">
                    <Settings2 className="h-4 w-4" />
                    {t('pnlMonitor.columnToggle')}
                  </Button>
                </DropdownMenuTrigger>
                <DropdownMenuContent align="end" className="w-56">
                  <DropdownMenuLabel>{t('pnlMonitor.showColumns')}</DropdownMenuLabel>
                  <DropdownMenuSeparator />
                  {Object.entries(columnVisibility).map(([columnId, isVisible]) => {
                    const columnLabels: Record<string, string> = {
                      client_id: tz('clientPnl.columns.clientId', '客户ID', 'Client ID'),
                      client_name: tz('clientPnl.columns.clientName', '客户名称', 'Client Name'),
                      primary_server: tz('clientPnl.columns.zipcodeServer', 'Zipcode/服务器', 'Zipcode/Server'),
                      account_count: tz('clientPnl.columns.accountCount', '账户数', 'Accounts'),
                      total_balance_usd: tz('clientPnl.columns.totalBalanceUsd', '总余额 (USD)', 'Total Balance (USD)'),
                      total_floating_pnl_usd: tz('clientPnl.columns.totalFloatingUsd', '总浮动盈亏 (USD)', 'Total Floating PnL (USD)'),
                      total_equity_usd: tz('clientPnl.columns.totalEquityUsd', '总净值 (USD)', 'Total Equity (USD)'),
                      total_closed_profit_usd: tz('clientPnl.columns.totalClosedProfitUsd', '总平仓盈亏 (USD)', 'Total Closed Profit (USD)'),
                      total_ib_commission_income_usd: tz('clientPnl.columns.totalCommissionUsd', 'IB佣金 (USD)', 'IB Commission (USD)'),
                      total_net_profit_with_ib_usd: tz('clientPnl.columns.totalNetProfitWithIbUsd', '净盈亏(含佣金) (USD)', 'Net PnL (w/ Comm) (USD)'),
                      total_deposit_usd: tz('clientPnl.columns.totalDepositUsd', '总入金 (USD)', 'Total Deposit (USD)'),
                      total_withdrawal_usd: tz('clientPnl.columns.totalWithdrawalUsd', '总出金 (USD)', 'Total Withdrawal (USD)'),
                      net_deposit_usd: tz('clientPnl.columns.netDepositUsd', '净入金 (USD)', 'Net Deposit (USD)'),
                      total_volume_lots: tz('clientPnl.columns.totalVolumeLots', '总交易手数', 'Total Volume (lots)'),
                      auto_swap_free_status: tz('clientPnl.columns.autoSwapFreeStatus', 'auto_swap_free_status', 'Auto Swap Free Status'),
                      is_enabled: tz('clientPnl.columns.isEnabled', '是否启用', 'Enabled'),
                      last_updated: tz('pnlMonitor.columns.lastUpdated', '最后更新', 'Last Updated'),
                    }
                    return (
                      <DropdownMenuCheckboxItem
                        key={columnId}
                        checked={isVisible}
                        onSelect={(e) => { e.preventDefault() }}
                        onCheckedChange={(value: boolean) => {
                          try { gridApi?.setColumnsVisible([columnId], !!value) } catch {}
                          setColumnVisibility(prev => ({ ...prev, [columnId]: !!value }))
                          saveGridState()
                        }}
                      >
                        {columnLabels[columnId] || columnId}
                      </DropdownMenuCheckboxItem>
                    )
                  })}
                </DropdownMenuContent>
              </DropdownMenu>
            </div>

            {/* 桌面端：原有一行布局（信息左、搜索/刷新/筛选/列切换右） */}
            <div className="hidden sm:flex items-center justify-between gap-3">
              {/* 左侧状态信息 */}
              <div className="flex flex-wrap items-center gap-3 text-xs sm:text-sm text-muted-foreground">
                <span>{t("pnlMonitor.totalRecords", { count: totalCount })}</span>
                <span>{t("pnlMonitor.currentPage", { current: pageIndex + 1, total: totalPages })}</span>
                {sortModel.length > 0 && (
                  <span className="px-2 py-1 bg-purple-100 dark:bg-purple-900/20 rounded text-purple-700 dark:text-purple-300">
                    {t("pnlMonitor.sortBy", { sort: sortModel.map(s => `${s.colId} ${s.sort === 'desc' ? '↓' : '↑'}`).join(', ') })}
                  </span>
                )}
                {lastUpdated && (
                  <span className="px-2 py-1 bg-slate-100 dark:bg-slate-800/40 rounded">
                    {t("pnlMonitor.dataUpdateTime", { time: new Intl.DateTimeFormat('zh-CN', {
                      timeZone: 'Asia/Shanghai',
                      year: 'numeric', month: '2-digit', day: '2-digit',
                      hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false
                    }).format(lastUpdated) })}
                  </span>
                )}
              </div>
              
              {/* 右侧：搜索 + 筛选 + 列显示切换按钮 */}
              <div className="flex items-center gap-2">
                {/* Time Range Selector (Desktop) */}
                <div className="w-48">
                  <Select value={timeRange} onValueChange={handleTimeRangeChange}>
                    <SelectTrigger className="h-9 w-full">
                      <SelectValue placeholder={tz('pnlMonitor.timeRange', '账户活跃时间', 'Account Active Time')} />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value="all">{tz('pnlMonitor.timeRangeAll', '全部时间', 'All Time')}</SelectItem>
                      <SelectItem value="1w">{tz('pnlMonitor.timeRange1w', '过去 1 周', 'Past 1 Week')}</SelectItem>
                      <SelectItem value="2w">{tz('pnlMonitor.timeRange2w', '过去 2 周', 'Past 2 Weeks')}</SelectItem>
                      <SelectItem value="1m">{tz('pnlMonitor.timeRange1m', '过去 1 个月', 'Past 1 Month')}</SelectItem>
                      <SelectItem value="3m">{tz('pnlMonitor.timeRange3m', '过去 3 个月', 'Past 3 Months')}</SelectItem>
                    </SelectContent>
                  </Select>
                </div>

                {/* 搜索框 */}
                <div className="flex items-center gap-1">
                  <Input
                    type="text"
                    placeholder={tz('clientPnl.searchPlaceholder', '搜索 ClientID / AccountID', 'Search ClientID / AccountID')}
                    value={searchInput}
                    onChange={(e) => setSearchInput(e.target.value)}
                    onKeyDown={handleSearchKeyDown}
                    className="h-9 w-48"
                  />
                  {searchValue && (
                    <Button
                      variant="ghost"
                      size="sm"
                      onClick={handleClearSearch}
                      className="h-9 px-2"
                    >
                      <X className="h-4 w-4" />
                    </Button>
                  )}
                  <Button
                    onClick={handleSearch}
                    className="h-9 px-3"
                    disabled={loading}
                  >
                    <Search className="h-4 w-4" />
                  </Button>
                </div>

                {/* 增量刷新 */}
                <Button onClick={handleClientRefresh} className="h-9 gap-2 whitespace-nowrap" disabled={true}>
                  {isRefreshing ? (t('pnlMonitor.refreshing') || '刷新中…') : (t('pnlMonitor.refresh') || '刷新')}
                </Button>
                {/* 筛选按钮：放在列显示切换左边 */}
                <Button 
                  onClick={() => setFilterBuilderOpen(true)} 
                  className="h-9 gap-2 whitespace-nowrap bg-black hover:bg-black/90 dark:bg-white dark:text-black dark:hover:bg-white/90"
                >
                  <Filter className="h-4 w-4" />
                  {t('pnlMonitor.filter')}
                  {appliedFilters && appliedFilters.rules.length > 0 && (
                    <Badge variant="secondary" className="ml-1 h-5 min-w-5 px-1.5 text-xs">
                      {appliedFilters.rules.length}
                    </Badge>
                  )}
                </Button>
                {/* 列显示切换按钮 */}
                <DropdownMenu>
                  <DropdownMenuTrigger asChild>
                    <Button variant="outline" className="h-9 gap-2 whitespace-nowrap">
                      <Settings2 className="h-4 w-4" />
                      {t('pnlMonitor.columnToggle')}
                    </Button>
                  </DropdownMenuTrigger>
                  <DropdownMenuContent align="end" className="w-56">
                    <DropdownMenuLabel>{t('pnlMonitor.showColumns')}</DropdownMenuLabel>
                    <DropdownMenuSeparator />
                    {Object.entries(columnVisibility).map(([columnId, isVisible]) => {
                    const columnLabels: Record<string, string> = {
                      client_id: tz('clientPnl.columns.clientId', '客户ID', 'Client ID'),
                      client_name: tz('clientPnl.columns.clientName', '客户名称', 'Client Name'),
                      primary_server: tz('clientPnl.columns.zipcodeServer', 'Zipcode/服务器', 'Zipcode/Server'),
                      account_count: tz('clientPnl.columns.accountCount', '账户数', 'Accounts'),
                      total_balance_usd: tz('clientPnl.columns.totalBalanceUsd', '总余额 (USD)', 'Total Balance (USD)'),
                      total_floating_pnl_usd: tz('clientPnl.columns.totalFloatingUsd', '总浮动盈亏 (USD)', 'Total Floating PnL (USD)'),
                      total_equity_usd: tz('clientPnl.columns.totalEquityUsd', '总净值 (USD)', 'Total Equity (USD)'),
                      total_closed_profit_usd: tz('clientPnl.columns.totalClosedProfitUsd', '总平仓盈亏 (USD)', 'Total Closed Profit (USD)'),
                      total_ib_commission_income_usd: tz('clientPnl.columns.totalCommissionUsd', 'IB佣金 (USD)', 'IB Commission (USD)'),
                      total_net_profit_with_ib_usd: tz('clientPnl.columns.totalNetProfitWithIbUsd', '净盈亏(含佣金) (USD)', 'Net PnL (w/ Comm) (USD)'),
                      total_deposit_usd: tz('clientPnl.columns.totalDepositUsd', '总入金 (USD)', 'Total Deposit (USD)'),
                      total_withdrawal_usd: tz('clientPnl.columns.totalWithdrawalUsd', '总出金 (USD)', 'Total Withdrawal (USD)'),
                      net_deposit_usd: tz('clientPnl.columns.netDepositUsd', '净入金 (USD)', 'Net Deposit (USD)'),
                      total_volume_lots: tz('clientPnl.columns.totalVolumeLots', '总交易手数', 'Total Volume (lots)'),
                      auto_swap_free_status: tz('clientPnl.columns.autoSwapFreeStatus', 'auto_swap_free_status', 'Auto Swap Free Status'),
                      is_enabled: tz('clientPnl.columns.isEnabled', '是否启用', 'Enabled'),
                      last_updated: tz('pnlMonitor.columns.lastUpdated', '最后更新', 'Last Updated'),
                    }
                      return (
                        <DropdownMenuCheckboxItem
                          key={columnId}
                          checked={isVisible}
                          onSelect={(e) => { e.preventDefault() }}
                          onCheckedChange={(value: boolean) => 
                            {
                              try { gridApi?.setColumnsVisible([columnId], !!value) } catch {}
                              setColumnVisibility(prev => ({ ...prev, [columnId]: !!value }))
                              saveGridState()
                            }
                          }
                        >
                          {columnLabels[columnId] || columnId}
                        </DropdownMenuCheckboxItem>
                      )
                    })}
                  </DropdownMenuContent>
                </DropdownMenu>
              </div>
            </div>

            {/* 移动端：信息分行展示（记录/页码/排序/更新时间） */}
            <div className="sm:hidden text-xs text-muted-foreground space-y-1">
              <div>{t('pnlMonitor.totalRecords', { count: totalCount })}</div>
              <div>{t('pnlMonitor.currentPage', { current: pageIndex + 1, total: totalPages })}</div>
              {sortModel.length > 0 && (
                <div>
                  {t('pnlMonitor.sortBy', { sort: sortModel.map(s => `${s.colId} ${s.sort === 'desc' ? '↓' : '↑'}`).join(', ') })}
                </div>
              )}
              {lastUpdated && (
                <div>
                  {t('pnlMonitor.dataUpdateTime', { time: new Intl.DateTimeFormat('zh-CN', {
                    timeZone: 'Asia/Shanghai',
                    year: 'numeric', month: '2-digit', day: '2-digit',
                    hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false
                  }).format(lastUpdated) })}
                </div>
              )}
            </div>
            
            {/* 已应用筛选条件展示 */}
            {appliedFilters && appliedFilters.rules.length > 0 && (
              <div className="flex flex-wrap items-center gap-2 pt-2 border-t">
                <span className="text-xs text-muted-foreground">{t('pnlMonitor.filterConditions', { join: appliedFilters.join })}</span>
                {appliedFilters.rules.map((rule, index) => {
                  const colMeta = CLIENT_FILTER_COLUMNS.find(c => c.id === rule.field)
                  const opLabel = getOperatorLabel(rule.op)
                  let valueDisplay = ''
                  if (operatorNeedsValue(rule.op)) {
                    if (operatorNeedsTwoValues(rule.op)) {
                      valueDisplay = ` ${rule.value ?? ''} ~ ${rule.value2 ?? ''}`
                    } else {
                      valueDisplay = ` ${rule.value ?? ''}`
                    }
                  }
                  return (
                    <Badge 
                      key={index} 
                      variant="outline" 
                      className="gap-1.5 pr-1 bg-blue-50 dark:bg-blue-950/30 border-blue-200 dark:border-blue-800 text-blue-700 dark:text-blue-300"
                    >
                      <span className="text-xs">
                        {(colMeta?.label || rule.field)} {opLabel}{valueDisplay}
                      </span>
                      <Button
                        variant="ghost"
                        size="sm"
                        onClick={() => handleRemoveFilter(index)}
                        className="h-4 w-4 p-0 hover:bg-blue-200 dark:hover:bg-blue-900"
                      >
                        <X className="h-3 w-3" />
                      </Button>
                    </Badge>
                  )
                })}
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={handleClearFilters}
                  className="h-7 text-xs text-red-600 dark:text-red-400 hover:text-red-700 dark:hover:text-red-300"
                >
                  {t('pnlMonitor.clearAll')}
                </Button>
              </div>
            )}
          </div>
        </CardContent>
      </Card>
      
      {/* V2 刷新结果 Banner */}
      {v2Banner && (
        <div className="px-1 sm:px-0">
          <div className="flex items-center gap-2 px-4 py-3 bg-blue-50 dark:bg-blue-900/20 border border-blue-200 dark:border-blue-800 rounded-lg">
            <svg className="w-4 h-4 text-blue-600 dark:text-blue-400" fill="currentColor" viewBox="0 0 20 20"><path fillRule="evenodd" d="M10 18a8 8 0 100-16 8 8 0 000 16zm-1-8.414l-1.293-1.293a1 1 0 10-1.414 1.414l2 2a1 1 0 001.414 0l5-5a1 1 0 10-1.414-1.414L9 9.586z" clipRule="evenodd"/></svg>
            <p className="text-sm text-blue-800 dark:text-blue-200">{v2Banner}</p>
          </div>
        </div>
      )}

      {/* client 刷新结果 Banner */}
      {refreshBanner && (
        <div className="px-1 sm:px-0">
          <div className="flex items-center gap-2 px-4 py-3 bg-green-50 dark:bg-green-900/20 border border-green-200 dark:border-green-800 rounded-lg">
            <svg className="w-4 h-4 text-green-600 dark:text-green-400" fill="currentColor" viewBox="0 0 20 20"><path fillRule="evenodd" d="M10 18a8 8 0 100-16 8 8 0 000 16zm-1-8.414l-1.293-1.293a1 1 0 10-1.414 1.414l2 2a1 1 0 001.414 0l5-5a1 1 0 10-1.414-1.414L9 9.586z" clipRule="evenodd"/></svg>
            <p className="text-sm text-green-800 dark:text-green-200">{refreshBanner}</p>
          </div>
        </div>
      )}

      {/* AG Grid 表格 */}
      <div className="flex-1">
        <div
          ref={gridContainerRef}
          className={`${isDarkMode ? 'ag-theme-quartz-dark' : 'ag-theme-quartz'} clientpnl-theme h-[600px] w-full min-h-[400px] relative`}
          style={{
            // Indigo 主题主色/对比色（shadcn 变量格式：H S L）
            ['--primary' as any]: '243 75% 59%',             // indigo-600
            ['--primary-foreground' as any]: '0 0% 100%',     // 白色文字
            ['--accent' as any]: '243 75% 65%',               // 略浅的 indigo，用于子行底色
            ['--accent-foreground' as any]: '0 0% 14%',

            // 表头：浅黑底白字（light），白底黑字（dark）
            ['--ag-header-background-color' as any]: isDarkMode ? 'hsl(0 0% 100% / 1)' : 'hsl(0 0% 8% / 1)',
            ['--ag-header-foreground-color' as any]: isDarkMode ? 'hsl(0 0% 0% / 1)' : 'hsl(0 0% 100% / 1)',
            ['--ag-header-column-separator-color' as any]: isDarkMode ? 'hsl(0 0% 0% / 1)' : 'hsl(0 0% 100% / 1)',
            ['--ag-header-column-separator-width' as any]: '1px',

            // 表格前景/背景/边框颜色使用 shadcn 语义色
            ['--ag-background-color' as any]: 'hsl(var(--card))',
            ['--ag-foreground-color' as any]: 'hsl(var(--foreground))',
            ['--ag-row-border-color' as any]: 'hsl(var(--border))',
            // 斑马纹（备用，不作为主逻辑）：primary 的极浅层次
            ['--ag-odd-row-background-color' as any]: 'hsl(var(--primary) / 0.04)'
          }}
        >
          <AgGridReact
            rowData={flatRows}
            columnDefs={columnDefs}
            gridOptions={{ theme: 'legacy' }}
            defaultColDef={{
              sortable: true,
              filter: true,
              resizable: true,
              minWidth: 100,
              wrapHeaderText: true,
              autoHeaderHeight: true,
            }}
            getRowId={(params) => {
              const d = params.data as any
              if (d && d._rowType === 'detail') {
                return `detail-${String(d._parentClientId)}-${String(d.login)}`
              }
              return `main-${String(d?.client_id)}`
            }}
            suppressScrollOnNewData={true}
            onGridReady={onGridReady}
            onSortChanged={onSortChanged}
            onColumnResized={(e: any) => { if (e.finished) throttledSaveGridState() }}
            onColumnMoved={() => throttledSaveGridState()}
            onColumnVisible={() => throttledSaveGridState()}
            onColumnPinned={() => throttledSaveGridState()}
            postSortRows={postSortRows}
            animateRows={true}
            rowSelection="multiple"
            suppressRowClickSelection={true}
            enableCellTextSelection={true}
            domLayout="normal"
            getRowStyle={(params: any) => {
              const rowType = params.data?._rowType
              
              // 明细行样式：使用 accent 的浅底，整体缩进 + 左侧细色条
              if (rowType === 'detail') {
                return {
                  backgroundColor: 'hsl(var(--accent) / 0.12)',
                  paddingLeft: 8,
                  borderLeft: '2px solid hsl(var(--accent) / 0.50)'
                }
              }
              
              // 主行：基于 primary 的弱对比底色（两档深浅区分）
              // fresh grad note: ensure index 0 is treated as even; guard for non-number
              const idx = typeof params.node.rowIndex === 'number' ? params.node.rowIndex : -1
              if (idx % 2 === 0) {
                return { backgroundColor: 'hsl(var(--primary) / 0.03)', paddingLeft: 0, borderLeft: 'none' }
              }
              return { backgroundColor: 'hsl(var(--primary) / 0.06)', paddingLeft: 0, borderLeft: 'none' }
            }}
          />
        </div>
        {/* 仅表头边框：light=白边框，dark=黑边框 */}
        <style>{`
          .clientpnl-theme .ag-header {
            border: 1px solid ${isDarkMode ? '#000' : '#fff'};
            border-bottom-width: 1px;
          }
        `}</style>
      </div>
      
      {/* 分页控件 */}
      <Card>
        <CardContent className="py-4">
          <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
            {/* 左侧：显示信息和每页条数选择 */}
            <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:space-x-4">
              <div className="text-sm text-muted-foreground">
                {t("pnlMonitor.totalRecordsDisplay", { 
                  start: pageIndex * pageSize + 1,
                  end: Math.min((pageIndex + 1) * pageSize, totalCount),
                  total: totalCount
                })}
              </div>
              
              {/* 每页条数选择 */}
              <div className="flex items-center space-x-2">
                <span className="text-sm text-muted-foreground">{t('pnlMonitor.perPage')}</span>
                <Select
                  value={pageSize.toString()}
                  onValueChange={(value: string) => {
                    const newSize = Number(value)
                    setPageSize(newSize)
                    setPageIndex(0)
                  }}
                >
                  <SelectTrigger className="h-8 w-20">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {[50, 100, 200, 300, 500].map((size) => (
                      <SelectItem key={size} value={size.toString()}>
                        {size}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
                <span className="text-sm text-muted-foreground">{t('pnlMonitor.records')}</span>
              </div>
            </div>

            {/* 右侧：分页按钮 */}
            <div className="flex items-center flex-wrap gap-2 w-full sm:w-auto justify-center sm:justify-end">
              <Button
                variant="outline"
                size="sm"
                onClick={() => setPageIndex(0)}
                disabled={pageIndex === 0}
              >
                {t('pnlMonitor.firstPage')}
              </Button>
              <Button
                variant="outline"
                size="sm"
                onClick={() => setPageIndex(Math.max(0, pageIndex - 1))}
                disabled={pageIndex === 0}
              >
                {t('pnlMonitor.prevPage')}
              </Button>
              
              {/* 页码显示 */}
              <div className="flex items-center space-x-1">
                <span className="text-sm text-muted-foreground">
                  {t('pnlMonitor.pageInfo', { current: pageIndex + 1, total: totalPages })}
                </span>
              </div>
              
              <Button
                variant="outline"
                size="sm"
                onClick={() => setPageIndex(Math.min(totalPages - 1, pageIndex + 1))}
                disabled={pageIndex >= totalPages - 1}
              >
                {t('pnlMonitor.nextPage')}
              </Button>
              <Button
                variant="outline"
                size="sm"
                onClick={() => setPageIndex(totalPages - 1)}
                disabled={pageIndex >= totalPages - 1}
              >
                {t('pnlMonitor.lastPage')}
              </Button>
            </div>
          </div>
        </CardContent>
      </Card>
      {/* Filter Builder Dialog/Drawer */}
      <FilterBuilder
        open={filterBuilderOpen}
        onOpenChange={setFilterBuilderOpen}
        initialFilters={appliedFilters || undefined}
        onApply={handleApplyFilters}
        columns={CLIENT_FILTER_COLUMNS}
      />
    </div>
  )
}

