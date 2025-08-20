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
import { ChevronDown } from "lucide-react"
import { DateRange } from "react-day-picker"

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
  return `${mm}-${dd} ${hh}`
}

export default function ProfitPage() {
  const [rows, setRows] = useState<ProfitRow[]>([])
  const [loading, setLoading] = useState(true)
  // fresh grad: date range via Popover + Calendar
  const [rangeOpen, setRangeOpen] = useState(false)
  const [range, setRange] = useState<DateRange | undefined>({
    from: new Date(2025, 4, 1), // 2025-05-01
    to: new Date(2025, 7, 18),  // 2025-08-18
  })
  const [agg, setAgg] = useState<AggKey>("timeline")
  const [tz, setTz] = useState<TzKey>("+8")

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

  // Convert source (UTC+3) to UTC epoch ms, then label in chosen tz
  const prepared = useMemo(() => {
    // build utc epoch for each row based on date+hour in UTC+3
    const withUtc = rows.map((r) => {
      const [y, m, d] = r.date.split("-").map((v) => parseInt(v, 10))
      const tsUtc = Date.UTC(y, m - 1, d, r.hour - 3, 0, 0) // shift from UTC+3 → UTC
      return { ...r, tsUtc }
    })

    const tzOffsetHours = tz === "+8" ? 8 : 3

    // fresh grad: compute inclusive UTC window for selected local dates in chosen tz
    const toYmdInTz = (d: Date) => {
      const shifted = new Date(d.getTime() + tzOffsetHours * 3600000)
      const y = shifted.getUTCFullYear()
      const m = shifted.getUTCMonth() + 1
      const day = shifted.getUTCDate()
      return { y, m, day }
    }
    let inRange = withUtc
    if (range?.from && range?.to && withUtc.length > 0) {
      const s = toYmdInTz(range.from)
      const e = toYmdInTz(range.to)
      let startUtc = Date.UTC(s.y, s.m - 1, s.day, 0, 0, 0) - tzOffsetHours * 3600000
      let endUtc = Date.UTC(e.y, e.m - 1, e.day, 23, 59, 59, 999) - tzOffsetHours * 3600000
      if (startUtc > endUtc) [startUtc, endUtc] = [endUtc, startUtc]
      inRange = withUtc.filter((x) => x.tsUtc >= startUtc && x.tsUtc <= endUtc)
    }

    if (agg === "timeline") {
      // label by chosen tz within selected date range
      const timeline = inRange
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
    for (const x of inRange) {
      const local = new Date(x.tsUtc + tzOffsetHours * 3600000)
      const hour = local.getUTCHours()
      buckets[hour] += x.profit
    }
    return buckets.map((profit, hour) => ({ label: String(hour).padStart(2, "0"), profit }))
  }, [rows, range, agg, tz])

  // fresh grad: totals (within selected date range + chosen tz)
  const { totalProfit, totalLoss } = useMemo(() => {
    const withUtc = rows.map((r) => {
      const [y, m, d] = r.date.split("-").map((v) => parseInt(v, 10))
      const tsUtc = Date.UTC(y, m - 1, d, r.hour - 3, 0, 0)
      return { ...r, tsUtc }
    })
    const tzOffsetHours = tz === "+8" ? 8 : 3
    const toYmdInTz = (d: Date) => {
      const shifted = new Date(d.getTime() + tzOffsetHours * 3600000)
      return {
        y: shifted.getUTCFullYear(),
        m: shifted.getUTCMonth() + 1,
        day: shifted.getUTCDate(),
      }
    }
    let inRange = withUtc
    if (range?.from && range?.to && withUtc.length > 0) {
      const s = toYmdInTz(range.from)
      const e = toYmdInTz(range.to)
      let startUtc = Date.UTC(s.y, s.m - 1, s.day, 0, 0, 0) - tzOffsetHours * 3600000
      let endUtc = Date.UTC(e.y, e.m - 1, e.day, 23, 59, 59, 999) - tzOffsetHours * 3600000
      if (startUtc > endUtc) [startUtc, endUtc] = [endUtc, startUtc]
      inRange = withUtc.filter((x) => x.tsUtc >= startUtc && x.tsUtc <= endUtc)
    }
    let profit = 0
    let loss = 0
    for (const x of inRange) {
      if (x.profit >= 0) profit += x.profit
      else loss += Math.abs(x.profit)
    }
    return { totalProfit: profit, totalLoss: loss }
  }, [rows, range, tz])

  return (
    <div className="space-y-4 px-4 pb-6 lg:px-6">
      {/* Toolbar */}
      <Card>
        <CardHeader>
          <CardTitle>筛选与视图</CardTitle>
        </CardHeader>
        <CardContent className="flex flex-col gap-3 md:flex-row md:items-center md:gap-6">
          {/* 时间范围（Popover + Calendar 范围选择） */}
          <div className="flex items-center gap-3">
            <Label className="text-sm text-muted-foreground">时间范围</Label>
            <Popover open={rangeOpen} onOpenChange={setRangeOpen}>
              <PopoverTrigger asChild>
                <Button variant="outline" className="w-[260px] justify-between font-normal">
                  {range?.from && range?.to
                    ? `${range.from.toLocaleDateString()} — ${range.to.toLocaleDateString()}`
                    : "选择日期"}
                  <ChevronDown className="h-4 w-4" />
                </Button>
              </PopoverTrigger>
              <PopoverContent className="w-auto overflow-hidden p-0" align="start">
                <Calendar
                  mode="range"
                  selected={range}
                  onSelect={(r) => {
                    setRange(r)
                    if (r?.from && r?.to) setRangeOpen(false)
                  }}
                  numberOfMonths={2}
                  captionLayout="dropdown"
                  initialFocus
                />
              </PopoverContent>
            </Popover>
            <Button
              variant="outline"
              size="sm"
              onClick={() => {
                // fresh grad: quick set to "today" in chosen tz (same day range)
                const tzOffsetHours = tz === "+8" ? 8 : 3
                const now = Date.now()
                const local = new Date(now + tzOffsetHours * 3600000)
                const y = local.getUTCFullYear()
                const m = local.getUTCMonth()
                const d = local.getUTCDate()
                const today = new Date(Date.UTC(y, m, d))
                setRange({ from: today, to: today })
              }}
            >
              今天
            </Button>
          </div>

          {/* 聚合维度 */}
          <div className="flex items-center gap-3">
            <span className="text-sm text-muted-foreground">聚合维度</span>
            <ToggleGroup type="single" value={agg} onValueChange={(v) => v && setAgg(v as AggKey)}>
              <ToggleGroupItem value="timeline">时间轴小时</ToggleGroupItem>
              <ToggleGroupItem value="hourOfDay">小时段(0-23)</ToggleGroupItem>
            </ToggleGroup>
          </div>

          {/* 时区 */}
          <div className="flex items-center gap-3">
            <span className="text-sm text-muted-foreground">时区</span>
            <Select value={tz} onValueChange={(v) => setTz(v as TzKey)}>
              <SelectTrigger className="w-[120px]">
                <SelectValue placeholder="选择时区" />
              </SelectTrigger>
              <SelectContent className="rounded-lg">
                <SelectItem value="+3">UTC+3 (源数据)</SelectItem>
                <SelectItem value="+8">UTC+8 (常用)</SelectItem>
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
            <div className="flex gap-4">
              <div className="w-4/5 h-[360px]">
                <ResponsiveContainer width="100%" height="100%">
                  <BarChart data={prepared}>
                    <CartesianGrid vertical={false} />
                    <XAxis dataKey="label" tickMargin={8} minTickGap={24} />
                    <YAxis tickFormatter={(v) => new Intl.NumberFormat().format(v)} />
                    <Tooltip
                      formatter={(value: number) => new Intl.NumberFormat().format(value)}
                      labelFormatter={(label: string) => label}
                    />
                    <Bar dataKey="profit" fill="var(--primary)" />
                  </BarChart>
                </ResponsiveContainer>
              </div>
              <div className="w-1/5">
                <div className="grid grid-rows-2 gap-4 h-[360px]">
                  <Card className="h-full">
                    <CardHeader className="py-3">
                      <CardTitle className="text-sm">盈利</CardTitle>
                    </CardHeader>
                    <CardContent className="h-full flex items-center justify-center">
                      <div className="text-3xl font-semibold text-green-600">
                        {new Intl.NumberFormat().format(totalProfit)}
                      </div>
                    </CardContent>
                  </Card>
                  <Card className="h-full">
                    <CardHeader className="py-3">
                      <CardTitle className="text-sm">亏损</CardTitle>
                    </CardHeader>
                    <CardContent className="h-full flex items-center justify-center">
                      <div className="text-3xl font-semibold text-red-600">
                        {new Intl.NumberFormat().format(totalLoss)}
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


