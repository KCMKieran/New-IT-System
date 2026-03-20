import { useState, useCallback, useMemo, useEffect } from "react";
import { useTheme } from "@/components/theme-provider";
import { apiFetch } from "@/lib/fetch";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Label } from "@/components/ui/label";
import {
  Search,
  BarChart3,
  Calendar as CalendarIcon,
  Filter,
  Settings2,
  Star,
  ExternalLink,
  Clock,
  Square,
  CheckSquare,
} from "lucide-react";
import { AgGridReact } from "ag-grid-react";
import { ColDef, GridApi, ICellRendererParams } from "ag-grid-community";
import { format, subDays, subMonths, startOfMonth, endOfMonth } from "date-fns";
import { cn } from "@/lib/utils";
import { Calendar } from "@/components/ui/calendar";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";
import { DateRange } from "react-day-picker";
import { Checkbox } from "@/components/ui/checkbox";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  DropdownMenu,
  DropdownMenuCheckboxItem,
  DropdownMenuContent,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
} from "@/components/ui/dialog";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Badge } from "@/components/ui/badge";

// --- Constants ---
const GRID_STATE_STORAGE_KEY = "IB_REPORT_GRID_STATE_V1";
const FAV_GROUPS_STORAGE_KEY = "IB_REPORT_FAV_GROUPS_V1";

// --- Types ---

interface IBValue {
  range_val: number;
  month_val: number;
}

interface IBReportRow {
  group: string;
  user_name: string;
  time_range: string;
  deposit: IBValue;
  withdrawal: IBValue;
  ib_withdrawal: IBValue;
  net_deposit: IBValue;
  volume: IBValue;
  adjustments: IBValue;
  commission: IBValue;
  ib_commission: IBValue;
  swap: IBValue;
  profit: IBValue;
  new_clients: IBValue;
  new_agents: IBValue;
}

interface GroupMetadata {
  tag_id: string;
  tag_name: string;
  user_count: number;
}

interface GroupsApiResponse {
  group_list: GroupMetadata[];
  last_update_time: string;
  previous_update_time: string | null;
  total_groups: number;
}

// --- Components ---

/**
 * Custom Cell Renderer for dual-row display (Range Value / Monthly Value)
 */
const DoubleValueRenderer = (params: ICellRendererParams) => {
  const value = params.value as IBValue;
  if (!value) return null;

  const isPinned = params.node.rowPinned === "top";
  const isPositive = (val: number) => val > 0;
  const isNegative = (val: number) => val < 0;

  const getClassName = (val: number) => {
    if (isPositive(val)) return "text-emerald-600 dark:text-emerald-400";
    if (isNegative(val)) return "text-red-600 dark:text-red-400";
    return "text-muted-foreground";
  };

  const showMonth = params.context?.includeMonthly ?? true;

  return (
    <div
      className={cn(
        "flex flex-col leading-tight py-1",
        isPinned && "scale-[1.02] origin-left"
      )}
    >
      <span
        className={cn(
          isPinned ? "font-bold text-[14px]" : "font-medium",
          getClassName(value.range_val)
        )}
      >
        {value.range_val.toLocaleString(undefined, {
          minimumFractionDigits: 2,
          maximumFractionDigits: 2,
        })}
      </span>
      {showMonth && (
        <span
          className={cn(
            "text-muted-foreground opacity-70 border-t border-dashed mt-0.5",
            isPinned ? "text-[11px] font-semibold" : "text-[10px]"
          )}
        >
          Month:{" "}
          {value.month_val.toLocaleString(undefined, {
            minimumFractionDigits: 2,
            maximumFractionDigits: 2,
          })}
        </span>
      )}
    </div>
  );
};

/**
 * Custom Cell Renderer for Time Range (splits into two lines with smaller font)
 */
const TimeRangeRenderer = (params: ICellRendererParams) => {
  const val = params.value as string;
  if (!val) return null;

  const parts = val.split(" ~ ");
  if (parts.length < 2)
    return <span className="text-[11px] font-mono">{val}</span>;

  return (
    <div className="flex flex-col leading-[1.1] py-1.5 text-[10px] font-mono text-muted-foreground">
      <span>{parts[0]}</span>
      <span className="opacity-40 text-[9px] py-0.5 text-center">to</span>
      <span>{parts[1]}</span>
    </div>
  );
};

const PREDEFINED_GROUPS = ["HZL", "CCX", "szs"];

export default function IBReport() {
  const { theme } = useTheme();
  const isDarkMode = theme === "dark";

  // --- State ---
  const [date, setDate] = useState<DateRange | undefined>({
    from: new Date(2026, 0, 4),
    to: new Date(2026, 0, 8),
  });
  // Quick range selector: same UI as client-return-rate page (Select dropdown)
  const [quickRange, setQuickRange] = useState<string>("");
  const [selectedGroups, setSelectedGroups] = useState<string[]>(() => {
    // 默认加载常用组别 (从 localStorage 或 PREDEFINED_GROUPS)
    const saved = localStorage.getItem(FAV_GROUPS_STORAGE_KEY);
    return saved ? JSON.parse(saved) : PREDEFINED_GROUPS;
  });
  const [includeMonthly, setIncludeMonthly] = useState(true);
  const [loading, setLoading] = useState(false);
  const [searchError, setSearchError] = useState<string | null>(null);
  const [rows, setRows] = useState<IBReportRow[]>([]);
  const [gridApi, setGridApi] = useState<GridApi | null>(null);
  const [columnState, setColumnState] = useState<any[]>([]);

  useEffect(() => {
    if (gridApi) {
      gridApi.refreshCells({ force: true });
    }
  }, [includeMonthly, gridApi]);

  // --- 组别动态加载与收藏状态 ---
  const [allGroups, setAllGroups] = useState<GroupMetadata[]>([]);
  const [favGroups, setFavGroups] = useState<string[]>(() => {
    // 初始加载：从 localStorage 读取，如果没有则使用硬编码的初始组别
    const saved = localStorage.getItem(FAV_GROUPS_STORAGE_KEY);
    return saved ? JSON.parse(saved) : PREDEFINED_GROUPS;
  });
  const [isGroupsDialogOpen, setIsGroupsDialogOpen] = useState(false);
  const [groupsMetadata, setGroupsMetadata] = useState<{
    last_update: string;
    prev_update: string | null;
  } | null>(null);
  const [groupSearchQuery, setGroupSearchQuery] = useState("");

  // --- 获取所有组别数据 (后端 7 天缓存) ---
  // AbortSignal allows cancelling the request on unmount (React 18 StrictMode cleanup)
  const fetchAllGroups = useCallback(async (signal?: AbortSignal) => {
    try {
      const res = await apiFetch("/api/v1/ib-report/groups", { signal });
      if (!res.ok) throw new Error("Failed to fetch groups");
      const data: GroupsApiResponse = await res.json();
      setAllGroups(data.group_list);
      setGroupsMetadata({
        last_update: data.last_update_time,
        prev_update: data.previous_update_time,
      });
    } catch (err) {
      if (err instanceof DOMException && err.name === "AbortError") return;
      console.error("Error fetching IB groups:", err);
    }
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    fetchAllGroups(controller.signal);
    return () => controller.abort();
  }, [fetchAllGroups]);

  // 收藏状态持久化
  useEffect(() => {
    localStorage.setItem(FAV_GROUPS_STORAGE_KEY, JSON.stringify(favGroups));
  }, [favGroups]);

  const toggleFavorite = (groupName: string) => {
    setFavGroups((prev) => {
      const isFav = prev.some(
        (g) => g.toLowerCase() === groupName.toLowerCase()
      );
      if (isFav) {
        return prev.filter((g) => g.toLowerCase() !== groupName.toLowerCase());
      } else {
        return [...prev, groupName];
      }
    });
  };

  const isFavorite = (groupName: string) => {
    return favGroups.some((g) => g.toLowerCase() === groupName.toLowerCase());
  };

  const filteredGroupsForDialog = useMemo(() => {
    return allGroups.filter((g) =>
      g.tag_name.toLowerCase().includes(groupSearchQuery.toLowerCase())
    );
  }, [allGroups, groupSearchQuery]);

  // --- Popover 展示组别：常用组别 + 当前已选但非常用的组别 ---
  const popoverDisplayGroups = useMemo(() => {
    const groups = [...favGroups];
    selectedGroups.forEach((sg) => {
      if (!groups.some((g) => g.toLowerCase() === sg.toLowerCase())) {
        groups.push(sg);
      }
    });
    return groups;
  }, [favGroups, selectedGroups]);

  // --- Grid State Persistence ---
  const refreshColumnState = useCallback(
    (api?: GridApi | null) => {
      const a = api || gridApi;
      if (!a) return;
      try {
        const state = (a as any).getColumnState?.();
        if (Array.isArray(state)) setColumnState(state);
      } catch {}
    },
    [gridApi]
  );

  const saveGridState = useCallback(() => {
    if (!gridApi) return;
    try {
      const state = (gridApi as any).getColumnState?.();
      if (!Array.isArray(state)) return;
      localStorage.setItem(GRID_STATE_STORAGE_KEY, JSON.stringify(state));
      setColumnState(state);
    } catch {}
  }, [gridApi]);

  const throttledSaveGridState = useMemo(() => {
    let last = 0;
    let timer: any;
    return () => {
      const now = Date.now();
      const run = () => {
        last = Date.now();
        saveGridState();
      };
      if (now - last >= 300) {
        run();
      } else {
        clearTimeout(timer);
        timer = setTimeout(run, 300 - (now - last));
      }
    };
  }, [saveGridState]);

  // --- Data Fetching ---
  // Optional dateOverride: when using quick presets, pass range so request uses it and calendar updates.
  const handleSearch = useCallback(
    async (signal?: AbortSignal, dateOverride?: DateRange | undefined) => {
      const range = dateOverride ?? date;
      if (!range?.from || !range?.to) {
        setSearchError("请先选择日期范围");
        return;
      }
      if (dateOverride) setDate(dateOverride);

      setLoading(true);
      setSearchError(null);
      try {
        const response = await apiFetch("/api/v1/ib-report/search", {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
          },
          body: JSON.stringify({
            start_date: format(range.from, "yyyy-MM-dd"),
            end_date: format(range.to, "yyyy-MM-dd"),
            groups: selectedGroups,
          }),
          signal,
        });

        if (!response.ok) {
          const msg = await response.text();
          throw new Error(msg || `请求失败 (${response.status})`);
        }

        const data: IBReportRow[] = await response.json();
        setRows(data);
      } catch (error) {
        if (error instanceof DOMException && error.name === "AbortError") return;
        console.error("Error loading IB report data:", error);
        const message =
          error instanceof Error ? error.message : "网络或服务器错误，请稍后重试";
        setSearchError(message);
      } finally {
        setLoading(false);
      }
    },
    [date, selectedGroups]
  );

  // Apply quick preset: set date range and run search (same behavior as client-return-rate)
  const applyQuickRange = useCallback(
    (preset: string) => {
      const today = new Date();
      let from: Date;
      let to: Date;
      switch (preset) {
        case "1w":
          to = today;
          from = subDays(today, 6);
          break;
        case "1m":
          to = today;
          from = subDays(today, 29);
          break;
        case "thisMonth":
          from = startOfMonth(today);
          to = endOfMonth(today);
          break;
        case "lastMonth": {
          const last = subMonths(today, 1);
          from = startOfMonth(last);
          to = endOfMonth(last);
          break;
        }
        default:
          return;
      }
      setDate({ from, to });
      handleSearch(undefined, { from, to });
    },
    [handleSearch]
  );

  useEffect(() => {
    const controller = new AbortController();
    handleSearch(controller.signal);
    return () => controller.abort();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // --- Table Configuration ---

  const defaultColDef = useMemo(
    () => ({
      sortable: true,
      resizable: true,
      filter: true,
      minWidth: 50,
      wrapHeaderText: true,
      autoHeaderHeight: true,
    }),
    []
  );

  const ibValueComparator = (valA: IBValue, valB: IBValue) => {
    if (!valA) return -1;
    if (!valB) return 1;
    return valA.range_val - valB.range_val;
  };

  const columnDefs = useMemo<ColDef[]>(
    () => [
      {
        field: "group",
        headerName: "组别",
        pinned: "left",
        width: 80,
        cellStyle: { backgroundColor: "rgba(255, 215, 0, 0.12)" },
        sortable: false,
        filter: false,
      },
      {
        field: "user_name",
        headerName: "User Name",
        pinned: "left",
        width: 150,
        cellStyle: { backgroundColor: "rgba(255, 215, 0, 0.12)" },
        sortable: false,
        filter: false,
      },
      {
        field: "time_range",
        headerName: "时间段",
        width: 100,
        cellRenderer: TimeRangeRenderer,
        sortable: false,
        filter: false,
      },
      {
        field: "deposit",
        headerName: "入金 (USD)",
        cellRenderer: DoubleValueRenderer,
        comparator: ibValueComparator,
        type: "numericColumn",
      },
      {
        field: "withdrawal",
        headerName: "出金 (USD)",
        cellRenderer: DoubleValueRenderer,
        comparator: ibValueComparator,
        type: "numericColumn",
      },
      {
        field: "ib_withdrawal",
        headerName: "IB出金 (USD)",
        cellRenderer: DoubleValueRenderer,
        comparator: ibValueComparator,
        type: "numericColumn",
      },
      {
        field: "net_deposit",
        headerName: "净入金 (USD)",
        cellRenderer: DoubleValueRenderer,
        comparator: ibValueComparator,
        type: "numericColumn",
      },
      {
        field: "volume",
        headerName: "平仓交易量 (lots)",
        cellRenderer: DoubleValueRenderer,
        comparator: ibValueComparator,
        type: "numericColumn",
      },
      {
        field: "adjustments",
        headerName: "交易调整",
        cellRenderer: DoubleValueRenderer,
        comparator: ibValueComparator,
        type: "numericColumn",
      },
      {
        field: "commission",
        headerName: "佣金 (Commission)",
        cellRenderer: DoubleValueRenderer,
        comparator: ibValueComparator,
        type: "numericColumn",
      },
      {
        field: "ib_commission",
        headerName: "IB 佣金",
        cellRenderer: DoubleValueRenderer,
        comparator: ibValueComparator,
        type: "numericColumn",
      },
      {
        field: "swap",
        headerName: "平仓利息 (Swap)",
        cellRenderer: DoubleValueRenderer,
        comparator: ibValueComparator,
        type: "numericColumn",
      },
      {
        field: "profit",
        headerName: "平仓盈亏 (Profit)",
        cellRenderer: DoubleValueRenderer,
        comparator: ibValueComparator,
        type: "numericColumn",
      },
      {
        field: "new_clients",
        headerName: "当天新开客户",
        cellRenderer: DoubleValueRenderer,
        comparator: ibValueComparator,
        type: "numericColumn",
      },
      {
        field: "new_agents",
        headerName: "当天新开代理",
        cellRenderer: DoubleValueRenderer,
        comparator: ibValueComparator,
        type: "numericColumn",
      },
    ],
    []
  );

  const toggleColumns = useMemo(() => {
    return (columnDefs || [])
      .map((c: any) => ({ colId: c.field, label: c.headerName }))
      .filter((x) => x.colId);
  }, [columnDefs]);

  const columnVisibilityMap = useMemo(() => {
    const m: Record<string, boolean> = {};
    (columnState || []).forEach((s: any) => {
      if (s && typeof s.colId === "string") {
        m[s.colId] = !s.hide;
      }
    });
    return m;
  }, [columnState]);

  const pinnedTopRowData = useMemo(() => {
    if (rows.length === 0) return [];
    const sum = (field: keyof IBReportRow) => {
      return rows.reduce(
        (acc, row) => {
          const val = row[field] as IBValue;
          return {
            range_val: acc.range_val + (val?.range_val || 0),
            month_val: acc.month_val + (val?.month_val || 0),
          };
        },
        { range_val: 0, month_val: 0 }
      );
    };

    return [
      {
        group: "汇总",
        user_name: "ALL GROUPS",
        time_range: "-",
        deposit: sum("deposit"),
        withdrawal: sum("withdrawal"),
        ib_withdrawal: sum("ib_withdrawal"),
        net_deposit: sum("net_deposit"),
        volume: sum("volume"),
        adjustments: sum("adjustments"),
        commission: sum("commission"),
        ib_commission: sum("ib_commission"),
        swap: sum("swap"),
        profit: sum("profit"),
        new_clients: sum("new_clients"),
        new_agents: sum("new_agents"),
      },
    ];
  }, [rows]);

  return (
    <div className="flex flex-col gap-4 p-4 min-h-svh bg-background">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold tracking-tight">
          IB 报表 (IB Report)
        </h1>
      </div>

      {/* Data freshness banner */}
      <div className="bg-amber-100 dark:bg-amber-900/30 border border-amber-200 dark:border-amber-800 rounded px-4 py-2 text-amber-800 dark:text-amber-200 text-sm">
        <span className="font-semibold">提示：</span>
        当前数据截止至 <span className="font-semibold">2026-01-08</span>。
      </div>

      {/* Search error feedback (e.g. network/CORS/403) */}
      {searchError && (
        <div className="bg-destructive/10 border border-destructive/30 rounded px-4 py-2 text-destructive text-sm flex items-center justify-between gap-2">
          <span>{searchError}</span>
          <Button
            type="button"
            variant="ghost"
            size="sm"
            className="h-7 px-2 text-destructive hover:bg-destructive/20"
            onClick={() => setSearchError(null)}
          >
            关闭
          </Button>
        </div>
      )}

      <Card>
        <CardContent className="py-3">
          <div className="flex flex-col sm:flex-row gap-3 items-center justify-between">
            <div className="flex flex-col sm:flex-row items-stretch sm:items-center gap-3 w-full sm:w-auto">
              <Popover>
                <PopoverTrigger asChild>
                  <Button
                    variant="outline"
                    className={cn(
                      "w-full sm:w-[260px] justify-start text-left font-normal h-9",
                      !date && "text-muted-foreground"
                    )}
                  >
                    <CalendarIcon className="mr-2 h-4 w-4" />
                    {date?.from ? (
                      date.to ? (
                        <>
                          {format(date.from, "yyyy-MM-dd")} - {format(date.to, "yyyy-MM-dd")}
                        </>
                      ) : (
                        format(date.from, "yyyy-MM-dd")
                      )
                    ) : (
                      <span>选择日期范围</span>
                    )}
                  </Button>
                </PopoverTrigger>
                <PopoverContent className="w-auto p-0" align="start">
                  <Calendar
                    initialFocus
                    mode="range"
                    defaultMonth={date?.from}
                    selected={date}
                    onSelect={(newDate) => {
                      setDate(newDate);
                      if (newDate?.from) setQuickRange("");
                    }}
                    numberOfMonths={2}
                  />
                </PopoverContent>
              </Popover>

              {/* Quick range selector: same UI as client-return-rate (Select dropdown, responsive) */}
              <Select
                value={quickRange}
                onValueChange={(val) => {
                  setQuickRange(val);
                  applyQuickRange(val);
                }}
              >
                <SelectTrigger className="w-full sm:w-[130px] h-9">
                  <SelectValue placeholder="快捷选项" />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="1w">过去 1 周</SelectItem>
                  <SelectItem value="1m">过去 1 个月</SelectItem>
                  <SelectItem value="thisMonth">本月</SelectItem>
                  <SelectItem value="lastMonth">上月</SelectItem>
                </SelectContent>
              </Select>

              <Popover>
                <PopoverTrigger asChild>
                  <Button
                    variant="outline"
                    className="w-full sm:w-[260px] justify-between h-9 text-foreground"
                  >
                    <div className="flex items-center gap-2 overflow-hidden">
                      <Filter className="h-4 w-4 shrink-0" />
                      <span className="truncate">
                        {selectedGroups.length === 0
                          ? "全部组别"
                          : `已选 ${selectedGroups.length} 个`}
                      </span>
                    </div>
                  </Button>
                </PopoverTrigger>
                <PopoverContent className="w-[320px] p-0" align="start">
                  <div className="p-3">
                    <div className="flex items-center justify-between mb-2">
                      <span className="text-xs font-semibold text-muted-foreground uppercase tracking-wider">
                        快捷选择 (Favorites & Selected)
                      </span>
                      <Badge
                        variant="secondary"
                        className="text-[10px] h-4 px-1"
                      >
                        Local
                      </Badge>
                    </div>
                    <div className="grid grid-cols-3 gap-1.5 max-h-[200px] overflow-y-auto pr-1">
                      {popoverDisplayGroups.length > 0 ? (
                        popoverDisplayGroups.map((g) => {
                          // Try to get the latest casing from DB for display
                          const dbGroup = allGroups.find(
                            (ag) =>
                              ag.tag_name.toLowerCase() === g.toLowerCase()
                          );
                          const displayName = dbGroup ? dbGroup.tag_name : g;
                          const isSelected = selectedGroups.some(
                            (sg) => sg.toLowerCase() === g.toLowerCase()
                          );
                          const isFav = favGroups.some(
                            (fg) => fg.toLowerCase() === g.toLowerCase()
                          );

                          return (
                            <Button
                              key={g}
                              variant={isSelected ? "default" : "outline"}
                              size="sm"
                              className={cn(
                                "text-xs px-1 h-8 truncate justify-center relative",
                                isSelected && !isFav && "border-primary/50"
                              )}
                              onClick={() => {
                                setSelectedGroups((prev) => {
                                  const exists = prev.some(
                                    (sg) => sg.toLowerCase() === g.toLowerCase()
                                  );
                                  if (exists) {
                                    return prev.filter(
                                      (sg) =>
                                        sg.toLowerCase() !== g.toLowerCase()
                                    );
                                  } else {
                                    return [...prev, displayName];
                                  }
                                });
                              }}
                            >
                              {isFav && (
                                <Star className="absolute -top-1 -right-1 h-2 w-2 fill-yellow-500 text-yellow-500" />
                              )}
                              {displayName}
                            </Button>
                          );
                        })
                      ) : (
                        <div className="col-span-3 py-4 text-center text-xs text-muted-foreground">
                          暂无常用或选中组别
                        </div>
                      )}
                    </div>
                  </div>
                  <div className="border-t p-2 flex items-center justify-between bg-muted/30">
                    <div className="flex gap-1">
                      <Button
                        variant="ghost"
                        size="sm"
                        className="text-xs h-7 px-2"
                        onClick={() => setSelectedGroups([])}
                      >
                        清空
                      </Button>
                      <Button
                        variant="ghost"
                        size="sm"
                        className="text-xs h-7 px-2"
                        onClick={() => setSelectedGroups([...favGroups])}
                      >
                        全选常用
                      </Button>
                    </div>
                    <Button
                      variant="outline"
                      size="sm"
                      className="text-xs h-7 px-2 gap-1 border-primary/30 hover:border-primary"
                      onClick={() => setIsGroupsDialogOpen(true)}
                    >
                      <ExternalLink className="h-3 w-3" />
                      查看所有组别
                    </Button>
                  </div>
                </PopoverContent>
              </Popover>

              <div className="flex items-center space-x-2 h-10">
                <Checkbox
                  id="monthly-data"
                  checked={includeMonthly}
                  onCheckedChange={(checked) => setIncludeMonthly(!!checked)}
                />
                <Label
                  htmlFor="monthly-data"
                  className="text-sm cursor-pointer select-none whitespace-nowrap"
                >
                  展示当月数据
                </Label>
              </div>
            </div>

            <div className="flex flex-col sm:flex-row gap-2 w-full sm:w-auto">
              <Button
                type="button"
                onClick={() => handleSearch()}
                disabled={loading}
                className="w-full sm:w-[140px]"
              >
                <Search
                  className={cn("h-4 w-4 mr-2", loading && "animate-spin")}
                />
                {loading ? "查询中..." : "查询"}
              </Button>

              <DropdownMenu>
                <DropdownMenuTrigger asChild>
                  <Button
                    variant="outline"
                    className="w-full sm:w-[140px] whitespace-nowrap gap-2"
                  >
                    <Settings2 className="h-4 w-4" />
                    列显示
                  </Button>
                </DropdownMenuTrigger>
                <DropdownMenuContent
                  align="end"
                  className="w-72 max-h-[60vh] overflow-auto"
                >
                  <DropdownMenuLabel>显示列</DropdownMenuLabel>
                  <DropdownMenuSeparator />
                  <div className="px-2 pb-2 flex items-center gap-2">
                    <Button
                      variant="ghost"
                      size="sm"
                      className="h-8 px-2"
                      onClick={() => {
                        if (!gridApi) return;
                        try {
                          const ids = toggleColumns.map((c) => c.colId);
                          (gridApi as any).setColumnsVisible?.(ids, true);
                          throttledSaveGridState();
                          setTimeout(() => refreshColumnState(gridApi), 0);
                        } catch {}
                      }}
                    >
                      全选
                    </Button>
                    <Button
                      variant="ghost"
                      size="sm"
                      className="h-8 px-2"
                      onClick={() => {
                        if (!gridApi) return;
                        try {
                          localStorage.removeItem(GRID_STATE_STORAGE_KEY);
                          (gridApi as any).resetColumnState?.();
                          setTimeout(() => refreshColumnState(gridApi), 0);
                        } catch {}
                      }}
                    >
                      重置
                    </Button>
                  </div>
                  <DropdownMenuSeparator />
                  {toggleColumns.map(({ colId, label }) => {
                    const checked = columnVisibilityMap[colId] ?? true;
                    return (
                      <DropdownMenuCheckboxItem
                        key={colId}
                        checked={checked}
                        onSelect={(e) => {
                          e.preventDefault();
                        }}
                        onCheckedChange={(value: boolean) => {
                          if (!gridApi) return;
                          try {
                            (gridApi as any).setColumnsVisible?.(
                              [colId],
                              !!value
                            );
                            throttledSaveGridState();
                            setTimeout(() => refreshColumnState(gridApi), 0);
                          } catch {}
                        }}
                      >
                        {label}
                      </DropdownMenuCheckboxItem>
                    );
                  })}
                </DropdownMenuContent>
              </DropdownMenu>

              <Button variant="outline" className="w-full sm:w-[140px]">
                <BarChart3 className="h-4 w-4 mr-2" />
                可视化图表
              </Button>
            </div>
          </div>
        </CardContent>
      </Card>

      <div className="flex-1 relative">
        <div
          className={`${
            isDarkMode ? "ag-theme-quartz-dark" : "ag-theme-quartz"
          } ibreport-theme w-full h-[750px] relative`}
          style={{
            ["--primary" as any]: "243 75% 59%",
            ["--ag-header-background-color" as any]: isDarkMode
              ? "hsl(0 0% 100% / 1)"
              : "hsl(0 0% 8% / 1)",
            ["--ag-header-foreground-color" as any]: isDarkMode
              ? "hsl(0 0% 0% / 1)"
              : "hsl(0 0% 100% / 1)",
            ["--ag-header-column-separator-color" as any]: isDarkMode
              ? "hsl(0 0% 0% / 1)"
              : "hsl(0 0% 100% / 1)",
            ["--ag-header-column-separator-width" as any]: "1px",
            ["--ag-icon-font-color" as any]: isDarkMode
              ? "hsl(0 0% 0% / 1)"
              : "hsl(0 0% 100% / 1)",
          }}
        >
          <AgGridReact
            rowData={rows}
            columnDefs={columnDefs}
            defaultColDef={defaultColDef}
            pinnedTopRowData={pinnedTopRowData}
            context={{ includeMonthly }}
            gridOptions={{ theme: "legacy" }}
            rowHeight={50}
            headerHeight={40}
            animateRows={true}
            getRowStyle={(params: any) => {
              if (params.node.rowPinned === "top") {
                return {
                  backgroundColor: "rgba(37, 99, 235, 0.15)",
                  fontWeight: "bold",
                };
              }
              const idx =
                typeof params.node.rowIndex === "number"
                  ? params.node.rowIndex
                  : -1;
              if (idx % 2 === 0) {
                return {
                  backgroundColor: "hsl(var(--primary) / 0.03)",
                  fontWeight: "normal",
                };
              }
              return {
                backgroundColor: "hsl(var(--primary) / 0.06)",
                fontWeight: "normal",
              };
            }}
            onGridReady={(params) => {
              setGridApi(params.api);
              try {
                const raw = localStorage.getItem(GRID_STATE_STORAGE_KEY);
                if (raw) {
                  const saved = JSON.parse(raw);
                  if (Array.isArray(saved) && saved.length > 0) {
                    (params.api as any).applyColumnState?.({
                      state: saved,
                      applyOrder: true,
                    });
                  }
                }
              } catch {}
              setTimeout(() => refreshColumnState(params.api), 0);
            }}
            onColumnResized={(e: any) => {
              if (e?.finished) throttledSaveGridState();
            }}
            onColumnMoved={() => throttledSaveGridState()}
            onColumnVisible={() => throttledSaveGridState()}
            onColumnPinned={() => throttledSaveGridState()}
          />
        </div>
        <style>{`
          .ibreport-theme .ag-header {
            border: 1px solid ${isDarkMode ? "#000" : "#fff"};
            border-bottom-width: 1px;
          }
        `}</style>
      </div>

      {/* --- 所有组别详情弹窗 (Dialog) --- */}
      <Dialog open={isGroupsDialogOpen} onOpenChange={setIsGroupsDialogOpen}>
        <DialogContent className="max-w-2xl max-h-[90vh] flex flex-col p-0 overflow-hidden">
          <DialogHeader className="p-6 pb-2">
            <DialogTitle className="flex items-center gap-2 text-xl">
              所有组别全览
              <Badge variant="outline" className="font-normal">
                Total: {allGroups.length}
              </Badge>
            </DialogTitle>
            <DialogDescription className="flex flex-col gap-1 pt-2">
              <div className="flex items-center gap-2 text-xs text-muted-foreground">
                <Clock className="h-3 w-3" />
                {/* Fresh grad note: This timestamp represents when the background service last synced data from the MetaTrader server. */}
                数据更新于: {groupsMetadata?.last_update || "加载中..."}
                {groupsMetadata?.prev_update &&
                  groupsMetadata.prev_update !== "N/A" &&
                  ` (上一次: ${groupsMetadata.prev_update})`}
                <span className="ml-1 px-1.5 py-0.5 bg-muted rounded-sm font-mono text-[10px] opacity-80">
                  MT Server Time
                </span>
              </div>
              <p className="text-sm">
                在这里您可以查看所有组别的用户量统计，并快速将其加入常用列表。
              </p>
            </DialogDescription>
          </DialogHeader>

          <div className="px-6 py-2">
            <div className="relative">
              <Search className="absolute left-2.5 top-2.5 h-4 w-4 text-muted-foreground" />
              <input
                type="text"
                placeholder="搜索组别名称..."
                className="w-full bg-muted/50 rounded-md py-2 pl-9 pr-4 text-sm focus:outline-none focus:ring-1 focus:ring-primary"
                value={groupSearchQuery}
                onChange={(e) => setGroupSearchQuery(e.target.value)}
              />
            </div>
          </div>

          <div className="flex-1 overflow-y-auto px-6 py-2">
            {allGroups.length === 0 ? (
              <div className="py-20 text-center space-y-3">
                <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-primary mx-auto"></div>
                <p className="text-sm text-muted-foreground">
                  正在从服务器获取组别列表...
                </p>
              </div>
            ) : (
              <Table>
                <TableHeader className="sticky top-0 bg-background z-10">
                  <TableRow>
                    <TableHead>组别名称 (Tag Name)</TableHead>
                    <TableHead className="text-right">
                      用户数量 (Users)
                    </TableHead>
                    <TableHead className="w-[100px] text-center">
                      选中
                    </TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {filteredGroupsForDialog.map((g) => {
                    const isSelected = selectedGroups.some(
                      (sg) => sg.toLowerCase() === g.tag_name.toLowerCase()
                    );
                    const isFav = isFavorite(g.tag_name);

                    return (
                      <TableRow
                        key={g.tag_id}
                        className={cn(isSelected && "bg-primary/5")}
                      >
                        <TableCell className="font-medium">
                          <div className="flex items-center gap-2">
                            <Button
                              variant="ghost"
                              size="sm"
                              className={cn(
                                "h-6 w-6 p-0 hover:text-yellow-500 transition-colors",
                                isFav
                                  ? "text-yellow-500"
                                  : "text-muted-foreground/30"
                              )}
                              onClick={(e) => {
                                e.stopPropagation();
                                toggleFavorite(g.tag_name);
                              }}
                              title={isFav ? "从常用中移除" : "加入常用"}
                            >
                              <Star
                                className={cn(
                                  "h-3.5 w-3.5",
                                  isFav && "fill-current"
                                )}
                              />
                            </Button>
                            {g.tag_name}
                          </div>
                        </TableCell>
                        <TableCell className="text-right font-mono text-muted-foreground">
                          {g.user_count.toLocaleString()}
                        </TableCell>
                        <TableCell className="text-center">
                          <Button
                            variant="ghost"
                            size="sm"
                            className={cn(
                              "h-8 w-8 p-0 hover:text-primary",
                              isSelected && "text-primary"
                            )}
                            onClick={() => {
                              setSelectedGroups((prev) => {
                                const exists = prev.some(
                                  (sg) =>
                                    sg.toLowerCase() ===
                                    g.tag_name.toLowerCase()
                                );
                                if (exists) {
                                  return prev.filter(
                                    (sg) =>
                                      sg.toLowerCase() !==
                                      g.tag_name.toLowerCase()
                                  );
                                } else {
                                  return [...prev, g.tag_name];
                                }
                              });
                            }}
                            title={isSelected ? "取消选中" : "选中加入报表"}
                          >
                            {isSelected ? (
                              <CheckSquare className="h-5 w-5 fill-current" />
                            ) : (
                              <Square className="h-5 w-5" />
                            )}
                          </Button>
                        </TableCell>
                      </TableRow>
                    );
                  })}
                </TableBody>
              </Table>
            )}
            {allGroups.length > 0 && filteredGroupsForDialog.length === 0 && (
              <div className="py-10 text-center text-muted-foreground">
                未找到匹配的组别
              </div>
            )}
          </div>

          <div className="p-4 border-t bg-muted/20 flex justify-end">
            <Button onClick={() => setIsGroupsDialogOpen(false)}>
              完成并关闭
            </Button>
          </div>
        </DialogContent>
      </Dialog>
    </div>
  );
}
