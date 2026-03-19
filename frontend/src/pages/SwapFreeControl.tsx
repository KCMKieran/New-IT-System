import { useEffect, useState, useCallback } from "react"
import { apiFetch } from "@/lib/fetch"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { Input } from "@/components/ui/input"
import { Button } from "@/components/ui/button"
import { Separator } from "@/components/ui/separator"
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover"
import { Calendar } from "@/components/ui/calendar"
import { Calendar as CalendarIcon, ArrowUp, ArrowDown } from "lucide-react"
import type { DateRange } from "react-day-picker"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"

// Swap Free Control page component
export default function SwapFreeControlPage() {
  // fresh grad: mobile detection to adjust calendar months & stacking
  const [isMobile, setIsMobile] = useState(false)
  // fresh grad: zipcode distribution rows from backend API
  type DistRow = { zipcode: string; client_count: number; client_ids?: number[] }
  const [distRows, setDistRows] = useState<DistRow[]>([])
  const [distLoading, setDistLoading] = useState(false)
  const [distError, setDistError] = useState<string | null>(null)
  // fresh grad: zipcode change logs state
  type ChangeLogRow = {
    client_id: number
    zipcode_before: string
    zipcode_after: string
    change_reason: string
    change_time: string
  }
  const [logRows, setLogRows] = useState<ChangeLogRow[]>([])
  const [logsLoading, setLogsLoading] = useState(false)
  const [logsError, setLogsError] = useState<string | null>(null)
  // fresh grad: client change frequency state (API aggregates last 30 days)
  type FrequencyRow = { client_id: number; changes: number; last_change: string | null }
  const [freqRows, setFreqRows] = useState<FrequencyRow[]>([])
  const [freqLoading, setFreqLoading] = useState(false)
  const [freqError, setFreqError] = useState<string | null>(null)
  // fresh grad: exclusions list state
  type ExclusionRow = {
    id: number
    client_id: number
    reason_code: string
    // fresh grad: manual note from UI for MANUAL entries
    note: string | null
    added_by: string
    added_at: string
    expires_at: string | null
    is_active: boolean
  }
  const [exRows, setExRows] = useState<ExclusionRow[]>([])
  const [exLoading, setExLoading] = useState(false)
  const [exError, setExError] = useState<string | null>(null)
  // fresh grad: exclusions filter/sort
  type ReasonKey = "ALL" | "PERM_LOSS" | "MANUAL" | "OTHER"
  const [exReasonFilter, setExReasonFilter] = useState<ReasonKey>("MANUAL")
  const [exAddedAtSort, setExAddedAtSort] = useState<"desc" | "asc">("desc")
  // fresh grad: manual add form state
  const [exClientIdInput, setExClientIdInput] = useState("")
  const [exNoteInput, setExNoteInput] = useState("")
  const [exAddLoading, setExAddLoading] = useState(false)
  const [exAddError, setExAddError] = useState<string | null>(null)
  const [exAddSuccess, setExAddSuccess] = useState<string | null>(null)
  useEffect(() => {
    const onResize = () => setIsMobile(window.innerWidth < 640) // sm breakpoint
    onResize()
    window.addEventListener("resize", onResize)
    return () => window.removeEventListener("resize", onResize)
  }, [])

  // fresh grad: Zipcode logs date range (Popover + Calendar, similar to Profit.tsx)
  const [range, setRange] = useState<DateRange | undefined>(undefined)
  // fresh grad: client id filter input for zipcode logs
  const [logClientIdInput, setLogClientIdInput] = useState("")
  const rangeLabel = (() => {
    if (!range?.from || !range?.to) return "Select date range"
    const opts: Intl.DateTimeFormatOptions = { month: "short", day: "2-digit", year: "numeric" }
    return `${range.from.toLocaleDateString("en-US", opts)} - ${range.to.toLocaleDateString("en-US", opts)}`
  })()

  // fresh grad: fetch zipcode distribution on mount
  // AbortController cancels the request when component unmounts (React 18 StrictMode cleanup)
  useEffect(() => {
    const controller = new AbortController()
    ;(async () => {
      setDistLoading(true)
      setDistError(null)
      try {
        const res = await apiFetch("/api/v1/zipcode/distribution", { signal: controller.signal })
        if (!res.ok) throw new Error(`HTTP ${res.status}`)
        const payload = await res.json()
        const data: DistRow[] = payload?.data ?? []
        setDistRows(data)
      } catch (e: any) {
        if (e instanceof DOMException && e.name === "AbortError") return
        setDistRows([])
        setDistError(e?.message ?? "Failed to load distribution")
      } finally {
        setDistLoading(false)
      }
    })()
    return () => controller.abort()
  }, [])
  
  // fresh grad: helper to format Date -> "YYYY-MM-DD HH:MM:SS" in UTC (backend uses UTC+0)
  const formatDateTime = (d: Date, endOfDay: boolean) => {
    const copy = new Date(d)
    if (endOfDay) {
      copy.setHours(23, 59, 59, 999)
    } else {
      copy.setHours(0, 0, 0, 0)
    }
    const iso = copy.toISOString()
    return iso.replace("T", " ").slice(0, 19)
  }

  // fresh grad: format ISO timestamptz to UTC+8 string "YYYY-MM-DD HH:mm:ss"
  const formatIsoToUtc8 = (iso: string | null | undefined) => {
    if (!iso) return ""
    try {
      // use fixed timezone Asia/Shanghai for UTC+8; sv-SE gives ISO-like format
      return new Date(iso).toLocaleString("sv-SE", {
        timeZone: "Asia/Shanghai",
        hour12: false,
      }).replace(",", "")
    } catch {
      return iso
    }
  }

  // fresh grad: load zipcode change logs with optional time window, limit 100
  // signal param allows cancelling the request on unmount (React 18 StrictMode cleanup)
  const loadLogs = async (options?: { start?: string; end?: string; clientId?: number; signal?: AbortSignal }) => {
    const { start, end, clientId, signal } = options ?? {}
    setLogsLoading(true)
    setLogsError(null)
    try {
      const params = new URLSearchParams()
      params.set("page", "1")
      params.set("page_size", "100")
      if (start) params.set("start", start)
      if (end) params.set("end", end)
      if (clientId) params.set("client_id", String(clientId))
      const res = await apiFetch(`/api/v1/zipcode/changes?${params.toString()}`, { signal })
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      const payload = await res.json()
      const data: ChangeLogRow[] = payload?.data ?? []
      setLogRows(data)
    } catch (e: any) {
      if (e instanceof DOMException && e.name === "AbortError") return
      setLogRows([])
      setLogsError(e?.message ?? "Failed to load change logs")
    } finally {
      setLogsLoading(false)
    }
  }

  // fresh grad: on mount, load default logs (backend defaults to last 25h)
  useEffect(() => {
    const controller = new AbortController()
    loadLogs({ signal: controller.signal })
    return () => controller.abort()
  }, [])

  // fresh grad: trigger zipcode logs fetch by client id only
  const onSearchLogs = () => {
    setLogsError(null)
    const trimmed = logClientIdInput.trim()
    if (!trimmed) {
      setLogsError("Client ID is required")
      return
    }
    const parsed = Number(trimmed)
    if (!Number.isInteger(parsed) || parsed <= 0) {
      setLogsError("Client ID must be a positive integer")
      return
    }
    loadLogs({ clientId: parsed })
  }

  // fresh grad: clear client id filter and restore logs based on date range (if set) or default
  const onResetLogSearch = () => {
    setLogClientIdInput("")
    setLogsError(null)
    // If date range is set, use it; otherwise use default (last 25h)
    if (range?.from && range?.to) {
      const start = formatDateTime(range.from, false)
      const end = formatDateTime(range.to, true)
      loadLogs({ start, end })
    } else {
      loadLogs()
    }
  }

  // fresh grad: apply date range independent from client id filter
  const onApplyDateRange = () => {
    setLogsError(null)
    // Clear client ID when applying date range to keep them independent
    setLogClientIdInput("")
    if (!range?.from || !range?.to) {
      loadLogs()
      return
    }
    const start = formatDateTime(range.from, false)
    const end = formatDateTime(range.to, true)
    loadLogs({ start, end })
  }

  // fresh grad: fetch exclusions (active only) so we can reuse after manual add
  // signal param allows cancelling the request on unmount (React 18 StrictMode cleanup)
  const loadExclusions = useCallback(async (signal?: AbortSignal) => {
    setExLoading(true)
    setExError(null)
    try {
      const res = await apiFetch(`/api/v1/zipcode/exclusions?is_active=true`, { signal })
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      const payload = await res.json()
      const data: ExclusionRow[] = payload?.data ?? []
      setExRows(data)
    } catch (e: any) {
      if (e instanceof DOMException && e.name === "AbortError") return
      setExRows([])
      setExError(e?.message ?? "Failed to load exclusions")
    } finally {
      setExLoading(false)
    }
  }, [])

  useEffect(() => {
    const controller = new AbortController()
    loadExclusions(controller.signal)
    return () => controller.abort()
  }, [loadExclusions])

  // fresh grad: manual add handler (validates input then POST to backend)
  const onAddExclusion = useCallback(async () => {
    setExAddError(null)
    setExAddSuccess(null)

    const clientIdRaw = exClientIdInput.trim()
    if (!clientIdRaw) {
      setExAddError("Client ID is required")
      return
    }
    const clientId = Number(clientIdRaw)
    if (!Number.isInteger(clientId) || clientId <= 0) {
      setExAddError("Client ID must be a positive integer")
      return
    }

    const note = exNoteInput.trim()
    if (!note) {
      setExAddError("Reason is required")
      return
    }

    setExAddLoading(true)
    try {
      const res = await apiFetch(`/api/v1/zipcode/exclusions`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ client_id: clientId, note }),
      })
      if (!res.ok) {
        const payload = await res.json().catch(() => null)
        const message =
          typeof payload?.detail === "string"
            ? payload.detail
            : `HTTP ${res.status}`
        setExAddError(message)
        return
      }
      await loadExclusions()
      setExClientIdInput("")
      setExNoteInput("")
      setExAddSuccess("Client added to exclusion list")
    } catch (e: any) {
      setExAddError(e?.message ?? "Failed to add exclusion")
    } finally {
      setExAddLoading(false)
    }
  }, [exClientIdInput, exNoteInput, loadExclusions])
  
  // fresh grad: derived exclusions view (filter by reason, sort by added_at)
  const displayedExRows = (() => {
    const filtered =
      exReasonFilter === "ALL"
        ? exRows
        : exRows.filter((r) => r.reason_code === exReasonFilter)
    const sorted = [...filtered].sort((a, b) => {
      const ta = new Date(a.added_at).getTime()
      const tb = new Date(b.added_at).getTime()
      return exAddedAtSort === "desc" ? tb - ta : ta - tb
    })
    return sorted
  })()

  // fresh grad: soft grey gradient突出日志卡片层次，兼容亮/暗模式
  const logsGradient =
    "bg-gradient-to-t from-zinc-200/70 via-zinc-100/30 to-transparent dark:from-zinc-900/70 dark:via-zinc-800/40 dark:to-transparent"

  // fresh grad: load client change frequency (last 30 days, only >=2 changes)
  // signal param allows cancelling the request on unmount (React 18 StrictMode cleanup)
  const loadFrequency = useCallback(async (signal?: AbortSignal) => {
    setFreqLoading(true)
    setFreqError(null)
    try {
      const res = await apiFetch(`/api/v1/zipcode/change-frequency?window_days=30&page=1&page_size=200`, { signal })
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      const payload = await res.json()
      const data: FrequencyRow[] = Array.isArray(payload?.data) ? payload.data : []
      const filtered = data.filter((row) => Number(row.changes) >= 2)
      setFreqRows(filtered)
    } catch (e: any) {
      if (e instanceof DOMException && e.name === "AbortError") return
      setFreqRows([])
      setFreqError(e?.message ?? "Failed to load change frequency")
    } finally {
      setFreqLoading(false)
    }
  }, [])

  useEffect(() => {
    const controller = new AbortController()
    loadFrequency(controller.signal)
    return () => controller.abort()
  }, [loadFrequency])
  
  return (
    <div className="px-3 py-4 sm:px-4 lg:px-6">
      {/* Layout: 2x2 grid on desktop, stacked on mobile */}
      <div className="grid grid-cols-1 gap-4 md:grid-cols-2 lg:grid-cols-8 lg:gap-6">
        {/* Zipcode Distribution (borderless, blended with background) */}
        <div className="h-[600px] rounded-lg border border-border/60 bg-card shadow-sm overflow-hidden lg:col-span-2">
          <CardHeader className="px-4 pt-4 pb-2">
            <CardTitle>Current Zipcode Distribution</CardTitle>
            <CardDescription>Count of enabled clients by zipcode</CardDescription>
          </CardHeader>
          <div className="px-4 pb-4">
            {/* Table area */}
            <div className="min-h-0 mt-2 h-[520px] overflow-auto rounded-md">
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead className="w-[160px]">Zipcode</TableHead>
                    <TableHead className="w-[160px]">Client count</TableHead>
                    <TableHead>
                      {distRows.some((r) => Array.isArray(r.client_ids) && r.client_ids.length > 0)
                        ? "Client IDs (<10)"
                        : "Notes"
                      }
                    </TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {distLoading ? (
                    <TableRow>
                      <TableCell colSpan={3} className="text-center text-muted-foreground py-8">
                        Loading...
                      </TableCell>
                    </TableRow>
                  ) : distError ? (
                    <TableRow>
                      <TableCell colSpan={3} className="text-center text-destructive py-8">
                        {distError}
                      </TableCell>
                    </TableRow>
                  ) : distRows.length === 0 ? (
                    <TableRow>
                      <TableCell colSpan={3} className="text-center text-muted-foreground py-8">
                        No data
                      </TableCell>
                    </TableRow>
                  ) : (
                    distRows.map((r) => {
                      const ids = Array.isArray(r.client_ids) ? r.client_ids : []
                      return (
                        <TableRow key={r.zipcode} className="odd:bg-muted/50">
                          <TableCell>{r.zipcode}</TableCell>
                          <TableCell>{r.client_count}</TableCell>
                          <TableCell className="text-sm">
                            {ids.length > 0 ? ids.join(", ") : ""}
                          </TableCell>
                        </TableRow>
                      )
                    })
                  )}
                </TableBody>
              </Table>
            </div>
          </div>
        </div>

        {/* Zipcode Change Logs */}
        <Card className="relative z-0 h-[600px] flex flex-col overflow-hidden lg:col-span-3 border border-border/60 bg-transparent shadow-sm">
          {/* fresh grad: grey gradient从底部渐隐，强调日志列表 */}
          <div className={`pointer-events-none absolute inset-0 z-0 ${logsGradient}`} aria-hidden="true" />
          <CardHeader className="relative z-10">
            <CardTitle>Zipcode Change Logs</CardTitle>
            <CardDescription>
              Only display 100 records of zipcode changes (for more details, plz contact Kieran)
            </CardDescription>
          </CardHeader>
          <CardContent className="relative z-10 flex-1 flex flex-col min-h-0 overflow-hidden">
            {/* Controls: responsive layout - stack on small screens, side-by-side on larger screens */}
            <div className="grid grid-cols-1 gap-4 lg:grid-cols-12">
              {/* Client ID Search Section */}
              <div className="lg:col-span-6">
                <label className="mb-1 block text-sm font-medium">Client ID</label>
                <div className="flex flex-col gap-2 sm:flex-row sm:items-center">
                  <Input
                    placeholder="e.g. 100001"
                    value={logClientIdInput}
                    onChange={(e) => setLogClientIdInput(e.target.value)}
                    disabled={logsLoading}
                    inputMode="numeric"
                    className="flex-1 min-w-0"
                  />
                  <div className="flex gap-2 shrink-0">
                    <Button size="sm" onClick={onSearchLogs} disabled={logsLoading} className="whitespace-nowrap">
                      Search
                    </Button>
                    <Button size="sm" variant="outline" onClick={onResetLogSearch} disabled={logsLoading} className="whitespace-nowrap">
                      Reset
                    </Button>
                  </div>
                </div>
              </div>
              {/* Date Range Section */}
              <div className="lg:col-span-6">
                <label className="mb-1 block text-sm font-medium">Date range</label>
                <div className="flex flex-col gap-2 sm:flex-row sm:items-center">
                  <Popover>
                    <PopoverTrigger asChild>
                      <Button variant="outline" className="flex-1 justify-start gap-2 font-normal min-w-0">
                        <CalendarIcon className="h-4 w-4 shrink-0" />
                        <span className="truncate">{rangeLabel}</span>
                      </Button>
                    </PopoverTrigger>
                    <PopoverContent className="w-auto p-0" align="start">
                      <Calendar
                        mode="range"
                        selected={range}
                        onSelect={setRange}
                        numberOfMonths={isMobile ? 1 : 2}
                        initialFocus
                      />
                    </PopoverContent>
                  </Popover>
                  <Button size="sm" onClick={onApplyDateRange} disabled={logsLoading} className="whitespace-nowrap shrink-0">
                    Apply
                  </Button>
                </div>
              </div>
            </div>
            <Separator className="my-4" />
            {/* Table area with zebra stripes */}
            <div className="min-h-0 flex-1 overflow-auto">
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead className="w-[120px]">Client ID</TableHead>
                    <TableHead>Zipcode Before</TableHead>
                    <TableHead>Zipcode After</TableHead>
                    <TableHead className="w-[180px]">Change Time</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {logsLoading ? (
                    <TableRow>
                      <TableCell colSpan={4} className="text-center text-muted-foreground py-8">
                        Loading...
                      </TableCell>
                    </TableRow>
                  ) : logsError ? (
                    <TableRow>
                      <TableCell colSpan={4} className="text-center text-destructive py-8">
                        {logsError}
                      </TableCell>
                    </TableRow>
                  ) : logRows.length === 0 ? (
                    <TableRow>
                      <TableCell colSpan={4} className="text-center text-muted-foreground py-8">
                        No data
                      </TableCell>
                    </TableRow>
                  ) : (
                    logRows.map((r, idx) => (
                      <TableRow key={`${r.client_id}-${r.change_time}-${idx}`} className="odd:bg-muted/50">
                        <TableCell>{r.client_id}</TableCell>
                        <TableCell>{r.zipcode_before}</TableCell>
                        <TableCell>{r.zipcode_after}</TableCell>
                        <TableCell>{formatIsoToUtc8(r.change_time)}</TableCell>
                      </TableRow>
                    ))
                  )}
                </TableBody>
              </Table>
            </div>
          </CardContent>
        </Card>

        {/* Exclude Group */}
        <Card className="h-[600px] flex flex-col overflow-hidden lg:col-span-3">
          <CardHeader>
            <CardTitle>Exclude Group</CardTitle>
            <CardDescription>
              PERM_LOSS means cumulative net loss exceeds 5000 USD; clients remain swap-free (zipcode=90). MANUAL entries are added from this page and require both client ID and note.
            </CardDescription>
          </CardHeader>
          <CardContent className="flex-1 flex flex-col min-h-0 overflow-hidden">
            {/* Controls (Client ID, Note, Add) - stack on mobile */}
            <div className="grid grid-cols-1 sm:grid-cols-12 items-end gap-3">
              <div className="sm:col-span-4">
                <label className="mb-1 block text-sm font-medium">Client ID</label>
                <Input
                  placeholder="e.g. 100001"
                  value={exClientIdInput}
                  onChange={(e) => setExClientIdInput(e.target.value)}
                  disabled={exAddLoading}
                  inputMode="numeric"
                />
              </div>
              <div className="sm:col-span-6">
                <label className="mb-1 block text-sm font-medium">Note</label>
                <Input
                  placeholder="Add context for MANUAL exclusion"
                  value={exNoteInput}
                  onChange={(e) => setExNoteInput(e.target.value)}
                  disabled={exAddLoading}
                  maxLength={500}
                />
              </div>
              <div className="sm:col-span-2">
                <Button className="w-full" onClick={onAddExclusion} disabled={exAddLoading}>
                  {exAddLoading ? "Adding..." : "Add"}
                </Button>
              </div>
            </div>
            {exAddError && (
              <p className="mt-2 text-sm text-destructive">{exAddError}</p>
            )}
            {exAddSuccess && (
              <p className="mt-2 text-sm text-emerald-600">{exAddSuccess}</p>
            )}
            {/* Filters & sort for display */}
            <div className="grid grid-cols-1 sm:grid-cols-12 items-end gap-3 mt-3">
              <div className="sm:col-span-6">
                <label className="mb-1 block text-sm font-medium">Filter by Reason</label>
                <Select value={exReasonFilter} onValueChange={(v) => setExReasonFilter(v as ReasonKey)}>
                  <SelectTrigger className="w-full">
                    <SelectValue placeholder="Select reason" />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="ALL">ALL</SelectItem>
                    <SelectItem value="PERM_LOSS">PERM_LOSS</SelectItem>
                    <SelectItem value="MANUAL">MANUAL</SelectItem>
                    <SelectItem value="OTHER">OTHER</SelectItem>
                  </SelectContent>
                </Select>
              </div>
              <div className="sm:col-span-6">
                <label className="mb-1 block text-sm font-medium">Sort by Added At</label>
                <Button
                  variant="outline"
                  className="w-full justify-between"
                  onClick={() => setExAddedAtSort((p) => (p === "desc" ? "asc" : "desc"))}
                >
                  {exAddedAtSort === "desc" ? "Newest first" : "Oldest first"}
                  {exAddedAtSort === "desc" ? <ArrowDown className="ml-1 h-4 w-4" /> : <ArrowUp className="ml-1 h-4 w-4" />}
                </Button>
              </div>
            </div>
            <Separator className="my-4" />
            {/* Table area with zebra stripes */}
            <div className="min-h-0 flex-1 overflow-auto">
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead className="w-[120px]">Client ID</TableHead>
                    <TableHead className="w-[110px]">Reason</TableHead>
                    <TableHead>Note</TableHead>
                    <TableHead className="w-[180px]">Added At</TableHead>
                    <TableHead className="w-[100px]">Active</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {exLoading ? (
                    <TableRow>
                      <TableCell colSpan={5} className="text-center text-muted-foreground py-8">
                        Loading...
                      </TableCell>
                    </TableRow>
                  ) : exError ? (
                    <TableRow>
                      <TableCell colSpan={5} className="text-center text-destructive py-8">
                        {exError}
                      </TableCell>
                    </TableRow>
                  ) : exRows.length === 0 ? (
                    <TableRow>
                      <TableCell colSpan={5} className="text-center text-muted-foreground py-8">
                        No data
                      </TableCell>
                    </TableRow>
                  ) : (
                    displayedExRows.map((r) => (
                      <TableRow key={r.id} className="odd:bg-muted/50">
                        <TableCell>{r.client_id}</TableCell>
                        <TableCell>{r.reason_code}</TableCell>
                        <TableCell>{r.note ?? ""}</TableCell>
                        <TableCell>{formatIsoToUtc8(r.added_at)}</TableCell>
                        <TableCell>{String(r.is_active)}</TableCell>
                      </TableRow>
                    ))
                  )}
                </TableBody>
              </Table>
            </div>
          </CardContent>
        </Card>

        {/* Client Change Frequency */}
        <Card className="h-[600px] flex flex-col overflow-hidden lg:col-span-8">
          <CardHeader>
            <CardTitle>Client Change Frequency</CardTitle>
            <CardDescription>
              Clients with 2+ swap-free zipcode changes in the last 30 days
            </CardDescription>
          </CardHeader>
          <CardContent className="flex-1 flex flex-col min-h-0 overflow-hidden">
            {/* Table area with zebra stripes */}
            <div className="min-h-0 flex-1 overflow-auto">
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead className="w-[120px]">Client ID</TableHead>
                    <TableHead className="w-[120px]">Changes</TableHead>
                    <TableHead>Window</TableHead>
                    <TableHead className="w-[180px]">Last Change</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {freqLoading ? (
                    <TableRow>
                      <TableCell colSpan={4} className="text-center text-muted-foreground py-8">
                        Loading...
                      </TableCell>
                    </TableRow>
                  ) : freqError ? (
                    <TableRow>
                      <TableCell colSpan={4} className="text-center text-destructive py-8">
                        {freqError}
                      </TableCell>
                    </TableRow>
                  ) : freqRows.length === 0 ? (
                    <TableRow>
                      <TableCell colSpan={4} className="text-center text-muted-foreground py-8">
                        No clients with 2+ changes in last 30 days
                      </TableCell>
                    </TableRow>
                  ) : (
                    freqRows.map((row) => (
                      <TableRow key={row.client_id} className="odd:bg-muted/50">
                        <TableCell>{row.client_id}</TableCell>
                        <TableCell>{row.changes}</TableCell>
                        <TableCell>Last 30 days</TableCell>
                        <TableCell>{row.last_change ? formatIsoToUtc8(row.last_change) : "-"}</TableCell>
                      </TableRow>
                    ))
                  )}
                </TableBody>
              </Table>
            </div>
          </CardContent>
        </Card>
      </div>
    </div>
  )
}
