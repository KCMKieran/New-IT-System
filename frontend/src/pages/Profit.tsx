import { useEffect, useMemo, useRef, useState } from "react"
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group"
// removed Select in favor of capsule toggle for timezone
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
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover"
import { Calendar } from "@/components/ui/calendar"
import { Calendar as CalendarIcon } from "lucide-react"
import { DateRange } from "react-day-picker"
// removed Input/Separator from old custom range UI

type ProfitRow = {
  date: string // e.g. "2025-05-01"
  hour: number // 0-23 (source timezone: UTC+3)
  profit: number
}

type AggKey = "timeline" | "hourOfDay"
type TzKey = "+3" | "+8"
type AggTypeKey = "open" | "close"

// fresh grad: simple date formatting helper
function formatLabel(dt: Date) {
  const mm = String(dt.getUTCMonth() + 1).padStart(2, "0")
  const dd = String(dt.getUTCDate()).padStart(2, "0")
  const hh = String(dt.getUTCHours()).padStart(2, "0")
  return `${mm}-${dd} ${hh}:00`
}

// fresh grad: simple animated number hook for smooth value changes
function useAnimatedNumber(target: number, durationMs = 600) {
  const [displayValue, setDisplayValue] = useState(target)
  const previousRef = useRef(target)
  const rafRef = useRef<number | null>(null)

  useEffect(() => {
    const startValue = previousRef.current
    const delta = target - startValue
    if (delta === 0) return

    const startTime = performance.now()

    const tick = (now: number) => {
      const elapsed = now - startTime
      const t = Math.min(1, elapsed / durationMs)
      // ease-out cubic
      const eased = 1 - Math.pow(1 - t, 3)
      setDisplayValue(startValue + delta * eased)
      if (t < 1) {
        rafRef.current = requestAnimationFrame(tick)
      }
    }

    rafRef.current = requestAnimationFrame(tick)
    previousRef.current = target

    return () => {
      if (rafRef.current) cancelAnimationFrame(rafRef.current)
    }
  }, [target, durationMs])

  return displayValue
}

export default function ProfitPage() {
  const [rows, setRows] = useState<ProfitRow[]>([])
  const [loading, setLoading] = useState(true)
  // fresh grad: date range via single Popover + range Calendar
  const [range, setRange] = useState<DateRange | undefined>({
    from: new Date(2025, 4, 1), // 2025-05-01
    to: new Date(2025, 7, 18),  // 2025-08-18
  })
  const [agg, setAgg] = useState<AggKey>("timeline")
  const [tz, setTz] = useState<TzKey>("+8")
  const [aggType, setAggType] = useState<AggTypeKey>("open")
  // fresh grad: detect mobile to adjust layout/Chart
  const [isMobile, setIsMobile] = useState(false)

  useEffect(() => {
    const onResize = () => setIsMobile(window.innerWidth < 640) // 640px ~ tailwind sm breakpoint
    onResize()
    window.addEventListener("resize", onResize)
    return () => window.removeEventListener("resize", onResize)
  }, [])

  // removed: custom range text inputs and history

  // removed: history persistence and input sync

  // removed: custom input apply handler

  // fresh grad: source file is NDJSON (one JSON object per line), not a JSON array
  useEffect(() => {
    let cancelled = false
    async function load() {
      setLoading(true)
      const url = aggType === "open" ? "/profit_xauusd_hourly.json" : "/profit_xauusd_hourly_close.json"
      const res = await fetch(url)
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
  }, [aggType])

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
  const selectedRangeUtc = useMemo(() => {
    if (!range?.from || !range?.to) return null
    const tzOffsetHours = tz === "+8" ? 8 : 3
    const getTimestamp = (d: Date, atEndOfDay: boolean) => {
      const y = d.getFullYear()
      const m = d.getMonth()
      const day = d.getDate()
      if (atEndOfDay) return Date.UTC(y, m, day, 23, 59, 59, 999) - tzOffsetHours * 3600000
      return Date.UTC(y, m, day, 0, 0, 0) - tzOffsetHours * 3600000
    }
    let startUtc = getTimestamp(range.from, false)
    let endUtc = getTimestamp(range.to, true)
    if (startUtc > endUtc) [startUtc, endUtc] = [endUtc, startUtc]
    return { startUtc, endUtc }
  }, [range, tz])

  const inRangeRows = useMemo(() => {
    if (!selectedRangeUtc || withUtc.length === 0) return withUtc
    const { startUtc, endUtc } = selectedRangeUtc
    return withUtc.filter((x) => x.tsUtc >= startUtc && x.tsUtc <= endUtc)
  }, [withUtc, selectedRangeUtc])

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

  // fresh grad: previous period comparison removed per latest design; keep layout concise

  // fresh grad: animated numbers for better UX feedback on changes
  const animatedProfit = useAnimatedNumber(totalProfit)
  const animatedLoss = useAnimatedNumber(totalLoss)
  const animatedPnl = useAnimatedNumber(pnl)

  // fresh grad: format date range like "Jan 20, 2023 - Feb 09, 2023"
  const rangeLabel = useMemo(() => {
    if (!range?.from || !range?.to) return "选择日期范围"
    const opts: Intl.DateTimeFormatOptions = { month: "short", day: "2-digit", year: "numeric" }
    return `${range.from.toLocaleDateString("en-US", opts)} - ${range.to.toLocaleDateString("en-US", opts)}`
  }, [range])

  return (
    <div className="space-y-4 px-4 pb-6 lg:px-6">
      {/* Toolbar */}
      <Card>
        <CardHeader>
          <CardTitle className="text-2xl font-bold">筛选与视图</CardTitle>
        </CardHeader>
        <CardContent className="flex flex-col gap-3 md:flex-row md:items-center md:gap-6">
          {/* 时间范围（单按钮 + Range 日历） */}
          <div className="flex items-center gap-3">
            <span className="text-sm text-muted-foreground">时间范围</span>
            <Popover>
              <PopoverTrigger asChild>
                <Button variant="outline" className="justify-start gap-2 font-normal">
                  <CalendarIcon className="h-4 w-4" />
                  <span>{rangeLabel}</span>
                </Button>
              </PopoverTrigger>
              <PopoverContent className="w-auto p-0" align="start">
                <Calendar
                  mode="range"
                  selected={range}
                  onSelect={(v) => setRange(v)}
                  numberOfMonths={2}
                  initialFocus
                />
              </PopoverContent>
            </Popover>
          </div>
          {/* 聚合类型（与聚合维度采用一致风格与宽度） */}
          <div className="flex items-center gap-3">
            <span className="text-sm text-muted-foreground">聚合类型</span>
            <ToggleGroup
              type="single"
              value={aggType}
              onValueChange={(v) => v && setAggType(v as AggTypeKey)}
              className="inline-flex w-[240px] items-center rounded-full bg-muted p-1"
            >
              <ToggleGroupItem
                value="open"
                className="flex-1 rounded-full first:rounded-l-full last:rounded-r-full px-3 py-1 text-center text-sm text-muted-foreground
                           data-[state=on]:bg-background data-[state=on]:text-foreground data-[state=on]:shadow"
              >
                Open Time
              </ToggleGroupItem>
              <ToggleGroupItem
                value="close"
                className="flex-1 rounded-full first:rounded-l-full last:rounded-r-full px-3 py-1 text-center text-sm text-muted-foreground
                           data-[state=on]:bg-background data-[state=on]:text-foreground data-[state=on]:shadow"
              >
                Close Time
              </ToggleGroupItem>
            </ToggleGroup>
          </div>

          {/* 聚合维度（与聚合类型保持一致宽度与风格） */}
          <div className="flex items-center gap-3">
            <span className="text-sm text-muted-foreground">聚合维度</span>
            <ToggleGroup
              type="single"
              value={agg}
              onValueChange={(v) => v && setAgg(v as AggKey)}
              className="inline-flex w-[240px] items-center rounded-full bg-muted p-1"
            >
              <ToggleGroupItem
                value="timeline"
                className="flex-1 rounded-full first:rounded-l-full last:rounded-r-full px-3 py-1 text-center text-sm text-muted-foreground
                           data-[state=on]:bg-background data-[state=on]:text-foreground data-[state=on]:shadow"
              >
                时间轴（小时）
              </ToggleGroupItem>
              <ToggleGroupItem
                value="hourOfDay"
                className="flex-1 rounded-full first:rounded-l-full last:rounded-r-full px-3 py-1 text-center text-sm text-muted-foreground
                           data-[state=on]:bg-background data-[state=on]:text-foreground data-[state=on]:shadow"
              >
                小时段(0-23)
              </ToggleGroupItem>
            </ToggleGroup>
          </div>

          {/* 时区（胶囊式等宽切换） */}
          <div className="flex items-center gap-3">
            <span className="text-sm text-muted-foreground">时区</span>
            <ToggleGroup
              type="single"
              value={tz}
              onValueChange={(v) => v && setTz(v as TzKey)}
              className="inline-flex w-[240px] items-center rounded-full bg-muted p-1"
            >
              <ToggleGroupItem
                value="+3"
                className="flex-1 rounded-full first:rounded-l-full last:rounded-r-full px-3 py-1 text-center text-sm text-muted-foreground
                           data-[state=on]:bg-background data-[state=on]:text-foreground data-[state=on]:shadow"
              >
                UTC+3
              </ToggleGroupItem>
              <ToggleGroupItem
                value="+8"
                className="flex-1 rounded-full first:rounded-l-full last:rounded-r-full px-3 py-1 text-center text-sm text-muted-foreground
                           data-[state=on]:bg-background data-[state=on]:text-foreground data-[state=on]:shadow"
              >
                UTC+8
              </ToggleGroupItem>
            </ToggleGroup>
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
              <div className="w-full h-[200px] sm:h-[400px] lg:w-4/5">
                <ResponsiveContainer width="100%" height="100%">
                  <BarChart data={prepared}>
                    <CartesianGrid vertical={false} />
                    <XAxis dataKey="label" tickMargin={8} minTickGap={24} tick={{ fontSize: 10 }}/>
                    {!isMobile && (
                      <YAxis tickFormatter={(v) => new Intl.NumberFormat().format(v)} tick={{ fontSize: 10 }} />
                    )}
                    <Tooltip
                      formatter={(value: number) => new Intl.NumberFormat().format(value)}
                      labelFormatter={(label: string) => label}
                    />
                    <Bar dataKey="profit" fill="var(--primary)" />
                  </BarChart>
                </ResponsiveContainer>
              </div>
              <div className="w-full lg:w-1/6">
                <div className="flex flex-col gap-2 lg:gap-2 justify-between">
                  {/* Card 1: 盈利 */}
                  <Card>
                    <CardContent className="min-w-0 px-4 py-2">
                      <div className="flex items-start justify-between">
                        <div className="text-sm font-medium text-muted-foreground">盈利</div>
                      </div>
                      <div className="mt-1 text-xl lg:text-2xl font-extrabold text-foreground" aria-live="polite">
                        {`${animatedProfit >= 0 ? "+" : "-"}${new Intl.NumberFormat("en-US", { style: "currency", currency: "USD" }).format(Math.abs(animatedProfit))}`}
                      </div>

                    </CardContent>
                  </Card>

                  {/* Card 2: 亏损 */}
                  <Card>
                    <CardContent className="min-w-0 px-4 py-2">
                      <div className="flex items-start justify-between">
                        <div className="text-sm font-medium text-muted-foreground">亏损</div>
                      </div>
                      <div className="mt-1 text-xl lg:text-2xl font-extrabold text-foreground" aria-live="polite">
                        {`${animatedLoss <= 0 ? "+" : "-"}${new Intl.NumberFormat("en-US", { style: "currency", currency: "USD" }).format(Math.abs(animatedLoss))}`}
                      </div>
                    </CardContent>
                  </Card>

                  {/* Card 3: 净利润 */}
                  <Card>
                    <CardContent className="min-w-0 px-4 py-2">
                      <div className="flex items-start justify-between">
                        <div className="text-sm font-medium text-muted-foreground">净利润</div>
                      </div>
                      <div className="mt-1 text-xl lg:text-2xl font-extrabold text-foreground" aria-live="polite">
                        {`${animatedPnl >= 0 ? "+" : "-"}${new Intl.NumberFormat("en-US", { style: "currency", currency: "USD" }).format(Math.abs(animatedPnl))}`}
                      </div>
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


