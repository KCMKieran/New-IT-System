import { useEffect, useMemo, useState } from "react"
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import {
  Bar,
  BarChart,
  CartesianGrid,
  Tooltip,
  XAxis,
  YAxis,
  ResponsiveContainer,
} from "recharts"
import { Button } from "@/components/ui/button"
import { Label } from "@/components/ui/label"
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover"
import { Calendar } from "@/components/ui/calendar"
import { DateRange } from "react-day-picker"
import { Input } from "@/components/ui/input"
import { Separator } from "@/components/ui/separator"

type ProfitRow = {
  date: string // e.g. "2025-05-01"
  hour: number // 0-23 (source timezone: UTC+3)
  profit: number
}

type AggKey = "timeline" | "hourOfDay"
type TzKey = "+3" | "+8"

// fresh grad: simple date formatting helper
function formatLabel(dt: Date) {
  const mm = String(dt.getUTCMonth() + 1).padStart(2, "0")
  const dd = String(dt.getUTCDate()).padStart(2, "0")
  const hh = String(dt.getUTCHours()).padStart(2, "0")
  return `${mm}-${dd} ${hh}:00`
}

export default function ProfitPage() {
  const [rows, setRows] = useState<ProfitRow[]>([])
  const [loading, setLoading] = useState(true)
  // fresh grad: date range via Popover + Calendar
  const [startOpen, setStartOpen] = useState(false)
  const [endOpen, setEndOpen] = useState(false)
  const [range, setRange] = useState<DateRange | undefined>({
    from: new Date(2025, 4, 1), // 2025-05-01
    to: new Date(2025, 7, 18),  // 2025-08-18
  })
  const [agg, setAgg] = useState<AggKey>("timeline")
  const [tz, setTz] = useState<TzKey>("+8")

  // fresh grad: state for custom range input
  const [customRangeOpen, setCustomRangeOpen] = useState(false)
  const [startDateStr, setStartDateStr] = useState("")
  const [endDateStr, setEndDateStr] = useState("")
  const [rangeHistory, setRangeHistory] = useState<{ from: string; to: string }[]>([])

  // fresh grad: load history from localStorage on mount
  useEffect(() => {
    try {
      const item = window.localStorage.getItem("profitPageRangeHistory")
      if (item) {
        const parsed = JSON.parse(item)
        if (Array.isArray(parsed)) setRangeHistory(parsed)
      }
    } catch (error) {
      console.error("Failed to load range history:", error)
    }
  }, [])

  // fresh grad: sync range state to input strings and update history
  useEffect(() => {
    const formatDate = (date: Date) => {
      const y = date.getFullYear()
      const m = String(date.getMonth() + 1).padStart(2, "0")
      const d = String(date.getDate()).padStart(2, "0")
      return `${y}-${m}-${d}`
    }

    if (range?.from) setStartDateStr(formatDate(range.from))
    if (range?.to) setEndDateStr(formatDate(range.to))

    if (range?.from && range?.to) {
      setRangeHistory((prev) => {
        const newEntry = { from: formatDate(range.from!), to: formatDate(range.to!) }
        // Avoid adding a duplicate of the most recent entry
        if (prev.length > 0 && prev[0].from === newEntry.from && prev[0].to === newEntry.to) {
          return prev
        }
        const updated = [newEntry, ...prev.filter((h) => h.from !== newEntry.from || h.to !== newEntry.to)].slice(
          0,
          5,
        )
        try {
          window.localStorage.setItem("profitPageRangeHistory", JSON.stringify(updated))
        } catch (error) {
          console.error("Failed to save range history:", error)
        }
        return updated
      })
    }
  }, [range])

  // fresh grad: apply custom date range from inputs
  const handleApplyCustomRange = () => {
    const dateRegex = /^\d{4}-\d{2}-\d{2}$/
    if (!dateRegex.test(startDateStr) || !dateRegex.test(endDateStr)) {
      alert("日期格式不正确，请输入 YYYY-MM-DD 格式")
      return
    }

    const [fromY, fromM, fromD] = startDateStr.split("-").map(Number)
    const [toY, toM, toD] = endDateStr.split("-").map(Number)

    // Create dates in local timezone to be consistent with calendar component
    const fromDate = new Date(fromY, fromM - 1, fromD)
    const toDate = new Date(toY, toM - 1, toD)

    // Validate that the created date is the same as the input date
    if (
      fromDate.getFullYear() !== fromY ||
      fromDate.getMonth() !== fromM - 1 ||
      fromDate.getDate() !== fromD ||
      toDate.getFullYear() !== toY ||
      toDate.getMonth() !== toM - 1 ||
      toDate.getDate() !== toD
    ) {
      alert("日期无效，请重新输入")
      return
    }

    setRange({ from: fromDate, to: toDate })
    setCustomRangeOpen(false)
  }

  // fresh grad: source file is NDJSON (one JSON object per line), not a JSON array
  useEffect(() => {
    let cancelled = false
    async function load() {
      setLoading(true)
      const res = await fetch("/profit_xauusd_hourly.json")
      const text = await res.text()
      if (cancelled) return
      const lines = text.split(/\r?\n/).filter(Boolean)
      const data: ProfitRow[] = []
      for (const line of lines) {
        try {
          const obj = JSON.parse(line)
          if (
            typeof obj?.date === "string" &&
            typeof obj?.hour === "number" &&
            typeof obj?.profit === "number"
          ) {
            data.push({ date: obj.date, hour: obj.hour, profit: obj.profit })
          }
        } catch {
          // skip bad line
        }
      }
      setRows(data)
      setLoading(false)
    }
    load()
    return () => {
      cancelled = true
    }
  }, [])

  // fresh grad: memoized rows with UTC timestamp
  const withUtc = useMemo(
    () =>
      rows.map((r) => {
        const [y, m, d] = r.date.split("-").map((v) => parseInt(v, 10))
        const tsUtc = Date.UTC(y, m - 1, d, r.hour - 3, 0, 0) // shift from UTC+3 → UTC
        return { ...r, tsUtc }
      }),
    [rows],
  )

  // fresh grad: filter rows by selected date range and timezone
  const inRangeRows = useMemo(() => {
    if (!range?.from || !range?.to || withUtc.length === 0) {
      return withUtc
    }
    const tzOffsetHours = tz === "+8" ? 8 : 3

    // fresh grad: compute start/end UTC timestamp from local date range in chosen tz
    const getTimestamp = (d: Date, atEndOfDay: boolean) => {
      // 1. Get Y/M/D in browser's local timezone
      const y = d.getFullYear()
      const m = d.getMonth()
      const day = d.getDate()
      // 2. Construct UTC timestamp for start/end of that day in chosen timezone
      if (atEndOfDay) {
        return Date.UTC(y, m, day, 23, 59, 59, 999) - tzOffsetHours * 3600000
      }
      return Date.UTC(y, m, day, 0, 0, 0) - tzOffsetHours * 3600000
    }

    let startUtc = getTimestamp(range.from, false)
    let endUtc = getTimestamp(range.to, true)

    // fresh grad: handle case where user selects end date before start date
    if (startUtc > endUtc) {
      ;[startUtc, endUtc] = [endUtc, startUtc]
    }

    return withUtc.filter((x) => x.tsUtc >= startUtc && x.tsUtc <= endUtc)
  }, [withUtc, range, tz])

  // Convert source (UTC+3) to UTC epoch ms, then label in chosen tz
  const prepared = useMemo(() => {
    const tzOffsetHours = tz === "+8" ? 8 : 3

    if (agg === "timeline") {
      // label by chosen tz within selected date range
      const timeline = inRangeRows
        .map((x) => {
          const dt = new Date(x.tsUtc + tzOffsetHours * 3600000)
          return {
            label: formatLabel(dt),
            profit: x.profit,
            ts: x.tsUtc, // for stable sorting
          }
        })
        .sort((a, b) => a.ts - b.ts)

      // merge same label (unlikely but safe if multiple rows map to same local hour)
      const merged = new Map<string, number>()
      for (const it of timeline) {
        merged.set(it.label, (merged.get(it.label) ?? 0) + it.profit)
      }
      return Array.from(merged.entries()).map(([label, profit]) => ({ label, profit }))
    }

    // hour-of-day aggregation in chosen tz (0-23) within selected date range
    const buckets = new Array(24).fill(0) as number[]
    for (const x of inRangeRows) {
      const local = new Date(x.tsUtc + tzOffsetHours * 3600000)
      const hour = local.getUTCHours()
      buckets[hour] += x.profit
    }
    return buckets.map((profit, hour) => ({ label: `${String(hour).padStart(2, "0")}:00`, profit }))
  }, [inRangeRows, agg, tz])

  // fresh grad: totals (within selected date range + chosen tz)
  const { totalProfit, totalLoss, pnl } = useMemo(() => {
    let profit = 0
    let loss = 0
    for (const x of inRangeRows) {
      if (x.profit >= 0) profit += x.profit
      else loss += Math.abs(x.profit)
    }
    const pnl = profit - loss
    return { totalProfit: profit, totalLoss: loss, pnl }
  }, [inRangeRows])

  return (
    <div className="space-y-4 px-4 pb-6 lg:px-6">
      {/* Toolbar */}
      <Card>
        <CardHeader>
          <CardTitle className="text-2xl font-bold">筛选与视图</CardTitle>
        </CardHeader>
        <CardContent className="flex flex-col gap-3 md:flex-row md:items-center md:gap-6">
          {/* 时间范围 (separate start/end calendars) */}
          <div className="flex flex-col items-start gap-2 sm:flex-row sm:items-center">
            <Label className="text-sm text-muted-foreground">时间范围</Label>
            <Popover open={startOpen} onOpenChange={setStartOpen}>
              <PopoverTrigger asChild>
                <Button variant="outline" className="w-[130px] justify-start font-normal">
                  {range?.from ? range.from.toLocaleDateString() : <span>选择开始</span>}
                </Button>
              </PopoverTrigger>
              <PopoverContent className="w-auto overflow-hidden p-0" align="start">
                <Calendar
                  mode="single"
                  selected={range?.from}
                  onSelect={(d) => {
                    setRange((prev) => ({ from: d, to: prev?.to }))
                    setStartOpen(false)
                  }}
                  initialFocus
                />
              </PopoverContent>
            </Popover>
            <span className="text-muted-foreground">-</span>
            <Popover open={endOpen} onOpenChange={setEndOpen}>
              <PopoverTrigger asChild>
                <Button variant="outline" className="w-[130px] justify-start font-normal">
                  {range?.to ? range.to.toLocaleDateString() : <span>选择结束</span>}
                </Button>
              </PopoverTrigger>
              <PopoverContent className="w-auto overflow-hidden p-0" align="start">
                <Calendar
                  mode="single"
                  selected={range?.to}
                  onSelect={(d) => {
                    setRange((prev) => ({ from: prev?.from, to: d }))
                    setEndOpen(false)
                  }}
                  initialFocus
                />
              </PopoverContent>
            </Popover>
            <Popover open={customRangeOpen} onOpenChange={setCustomRangeOpen}>
              <PopoverTrigger asChild>
                <Button variant="outline" size="default" className="w-[250px] justify-center font-normal">
                  自定义范围
                </Button>
              </PopoverTrigger>
              <PopoverContent className="w-auto p-4" align="start">
                <div className="grid gap-4">
                  <div className="space-y-1">
                    <p className="text-sm text-muted-foreground">请输入 YYYY-MM-DD 格式</p>
                  </div>
                  <div className="grid gap-2">
                    <Label htmlFor="start-date">开始日期</Label>
                    <Input
                      id="start-date"
                      value={startDateStr}
                      onChange={(e) => setStartDateStr(e.target.value)}
                    />
                  </div>
                  <div className="grid gap-2">
                    <Label htmlFor="end-date">结束日期</Label>
                    <Input
                      id="end-date"
                      value={endDateStr}
                      onChange={(e) => setEndDateStr(e.target.value)}
                    />
                  </div>
                  <Button onClick={handleApplyCustomRange}>应用</Button>
                  {rangeHistory.length > 0 && (
                    <>
                      <Separator />
                      <div className="space-y-2">
                        <h4 className="font-medium leading-none text-sm">历史记录</h4>
                        <div className="flex flex-col items-stretch gap-1">
                          {rangeHistory.map((item, index) => (
                            <Button
                              key={index}
                              variant="ghost"
                              size="sm"
                              className="justify-start text-xs"
                              onClick={() => {
                                // Parse as local time to be consistent with calendar
                                const fromDate = new Date(`${item.from}T00:00:00`)
                                const toDate = new Date(`${item.to}T00:00:00`)
                                if (!isNaN(fromDate.getTime()) && !isNaN(toDate.getTime())) {
                                  setRange({ from: fromDate, to: toDate })
                                  setCustomRangeOpen(false)
                                }
                              }}
                            >
                              {item.from} → {item.to}
                            </Button>
                          ))}
                        </div>
                      </div>
                    </>
                  )}
                </div>
              </PopoverContent>
            </Popover>
          </div>

          {/* 聚合维度 */}
          <div className="flex items-center gap-3">
            <span className="text-sm text-muted-foreground">聚合维度</span>
            <ToggleGroup
              type="single"
              value={agg}
              onValueChange={(v) => v && setAgg(v as AggKey)}
              className="inline-flex rounded-md border border-border overflow-hidden"
            >
              <ToggleGroupItem
                value="timeline"
                className="px-3 py-1 text-sm border-l border-border first:border-l-0
                           data-[state=on]:bg-primary/10 data-[state=on]:text-primary"
              >
                时间轴（小时）
              </ToggleGroupItem>
              <ToggleGroupItem
                value="hourOfDay"
                className="px-3 py-1 text-sm border-l border-border first:border-l-0
                           data-[state=on]:bg-primary/10 data-[state=on]:text-primary"
              >
                小时段(0-23)
              </ToggleGroupItem>
            </ToggleGroup>
          </div>

          {/* 时区 */}
          <div className="flex items-center gap-3">
            <span className="text-sm text-muted-foreground">时区</span>
            <Select value={tz} onValueChange={(v) => setTz(v as TzKey)}>
              <SelectTrigger className="w-[240px] justify-center">
                <SelectValue placeholder="选择时区" />
              </SelectTrigger>
              <SelectContent className="rounded-lg">
                <SelectItem value="+3">UTC+3 (MT Server Time)</SelectItem>
                <SelectItem value="+8">UTC+8 (HKT)</SelectItem>
              </SelectContent>
            </Select>
          </div>
        </CardContent>
      </Card>

      {/* Chart */}
      <Card>
        <CardContent className="pt-6">
          {loading ? (
            <div className="text-sm text-muted-foreground px-2 py-8">Loading…</div>
          ) : (
            <div className="flex flex-col gap-4 sm:flex-row">
              <div className="w-full h-[360px] lg:w-4/5">
                <ResponsiveContainer width="100%" height="100%">
                  <BarChart data={prepared}>
                    <CartesianGrid vertical={false} />
                    <XAxis dataKey="label" tickMargin={8} minTickGap={24} tick={{ fontSize: 10 }}/>
                    <YAxis tickFormatter={(v) => new Intl.NumberFormat().format(v)} tick={{ fontSize: 10 }} />
                    <Tooltip
                      formatter={(value: number) => new Intl.NumberFormat().format(value)}
                      labelFormatter={(label: string) => label}
                    />
                    <Bar dataKey="profit" fill="var(--primary)" />
                  </BarChart>
                </ResponsiveContainer>
              </div>
              <div className="w-full lg:w-1/5">
                <div className="grid grid-rows-3 gap-2 h-[360px]">
                  <Card className="h-full">
                    <CardContent className="h-full min-w-0 p-2 flex items-center justify-between">
                      <span className="ps-2 sm:ps-3 md:ps-4 text-2xl font-bold text-muted-foreground">盈利</span>
                      <span className="truncate text-right text-base lg:text-lg font-semibold text-green-600">
                        {new Intl.NumberFormat("en-US", { style: "currency", currency: "USD" }).format(totalProfit)}
                      </span>
                    </CardContent>
                  </Card>
                  <Card className="h-full">
                    <CardContent className="h-full min-w-0 p-2 flex items-center justify-between">
                      <span className="ps-2 sm:ps-3 md:ps-4 text-2xl font-bold text-muted-foreground">亏损</span>
                      <span className="truncate text-right text-base lg:text-lg font-semibold text-red-600">
                        {new Intl.NumberFormat("en-US", { style: "currency", currency: "USD" }).format(totalLoss)}
                      </span>
                    </CardContent>
                  </Card>
                  <Card className="h-full">
                    <CardContent className="h-full min-w-0 p-2 flex items-center justify-between">
                      <span className="ps-2 sm:ps-3 md:ps-4 text-2xl font-bold text-muted-foreground">净利润</span>
                      <span
                        className={`truncate text-right text-base lg:text-lg font-semibold ${
                          pnl >= 0 ? "text-green-600" : "text-red-600"
                        }`}
                      >
                        {new Intl.NumberFormat("en-US", { style: "currency", currency: "USD" }).format(pnl)}
                      </span>
                    </CardContent>
                  </Card>
                </div>
              </div>
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  )
}

// export default function ProfitPage() {
//   return (
//     <div className="flex min-h-svh items-center justify-center text-3xl font-semibold">
//       利润分析 开发ing
//     </div>
//   )
// }


