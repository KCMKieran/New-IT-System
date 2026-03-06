import { useState, useEffect, useMemo, Fragment, useCallback } from "react";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Button } from "@/components/ui/button";
import {
  ArrowDown,
  ArrowUp,
  ArrowUpDown,
  ChevronRight,
  Clock,
  RefreshCw,
} from "lucide-react";
import { cn } from "@/lib/utils";

const COL_SPAN = 6; // expand + group + 4 data columns
const IB_COMM_COLOR = "text-muted-foreground";

type SortKey = "pnl_today" | "ib_today" | "pnl_yesterday" | "ib_yesterday";

interface ApiRow {
  account_group: string;
  sales_team: string;
  net_pnl_today: number;
  net_pnl_yesterday: number;
  ib_commission_today: number;
  ib_commission_yesterday: number;
}

interface TeamPnl {
  sales_team: string;
  net_pnl_today: number;
  net_pnl_yesterday: number;
  ib_commission_today: number;
  ib_commission_yesterday: number;
}

interface GroupData {
  account_group: string;
  net_pnl_today: number;
  net_pnl_yesterday: number;
  ib_commission_today: number;
  ib_commission_yesterday: number;
  teams: TeamPnl[];
}

function formatPnl(value: number): string {
  const sign = value >= 0 ? "" : "-";
  return `${sign}$${Math.abs(value).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

function pnlColor(value: number): string {
  if (value > 0) return "text-green-600 dark:text-green-400";
  if (value < 0) return "text-red-600 dark:text-red-400";
  return "text-muted-foreground";
}

function getSortValue(row: GroupData | TeamPnl, key: SortKey): number {
  switch (key) {
    case "pnl_today":
      return row.net_pnl_today;
    case "ib_today":
      return row.ib_commission_today;
    case "pnl_yesterday":
      return row.net_pnl_yesterday;
    case "ib_yesterday":
      return row.ib_commission_yesterday;
  }
}

function SortIcon({ active, asc }: { active: boolean; asc: boolean }) {
  if (!active)
    return <ArrowUpDown className="inline ml-0.5 h-3 w-3 opacity-40" />;
  return asc ? (
    <ArrowUp className="inline ml-0.5 h-3 w-3" />
  ) : (
    <ArrowDown className="inline ml-0.5 h-3 w-3" />
  );
}

/**
 * Dashboard widget: 近两日客户平仓净盈亏 (Group).
 * Rows = mt4_users.GROUP (expandable -> sales team).
 * Columns = 今日Profit(Excl.comm) / 今日IB佣金 / 昨日Profit(Excl.comm) / 昨日IB佣金.
 * All 4 data columns support click-to-sort.
 */
export default function Past24hClientPnlByGroup() {
  const [items, setItems] = useState<ApiRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [expandedGroup, setExpandedGroup] = useState<string | null>(null);
  const [fetchedAt, setFetchedAt] = useState<Date | null>(null);
  const [sortKey, setSortKey] = useState<SortKey>("pnl_yesterday");
  const [sortAsc, setSortAsc] = useState(true);

  const handleSort = useCallback(
    (key: SortKey) => {
      if (key === sortKey) {
        setSortAsc((a) => !a);
      } else {
        setSortKey(key);
        setSortAsc(true);
      }
    },
    [sortKey],
  );

  const fetchData = useCallback(() => {
    setLoading(true);
    setError(null);
    fetch("/api/v1/dashboard/pnl-by-group")
      .then((res) => {
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        return res.json();
      })
      .then((data: { items: ApiRow[] }) => {
        setItems(data.items ?? []);
        setFetchedAt(new Date());
      })
      .catch((e) => setError(e instanceof Error ? e.message : String(e)))
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => {
    fetchData();
  }, [fetchData]);

  const groupData = useMemo(() => {
    const map = new Map<string, GroupData>();
    for (const row of items) {
      const g = row.account_group || "Unknown";
      const existing = map.get(g);
      if (existing) {
        existing.net_pnl_today += row.net_pnl_today;
        existing.net_pnl_yesterday += row.net_pnl_yesterday;
        existing.ib_commission_today += row.ib_commission_today;
        existing.ib_commission_yesterday += row.ib_commission_yesterday;
        existing.teams.push({
          sales_team: row.sales_team,
          net_pnl_today: row.net_pnl_today,
          net_pnl_yesterday: row.net_pnl_yesterday,
          ib_commission_today: row.ib_commission_today,
          ib_commission_yesterday: row.ib_commission_yesterday,
        });
      } else {
        map.set(g, {
          account_group: g,
          net_pnl_today: row.net_pnl_today,
          net_pnl_yesterday: row.net_pnl_yesterday,
          ib_commission_today: row.ib_commission_today,
          ib_commission_yesterday: row.ib_commission_yesterday,
          teams: [
            {
              sales_team: row.sales_team,
              net_pnl_today: row.net_pnl_today,
              net_pnl_yesterday: row.net_pnl_yesterday,
              ib_commission_today: row.ib_commission_today,
              ib_commission_yesterday: row.ib_commission_yesterday,
            },
          ],
        });
      }
    }
    const groups = Array.from(map.values());
    const cmp = (a: GroupData | TeamPnl, b: GroupData | TeamPnl) => {
      const diff = getSortValue(a, sortKey) - getSortValue(b, sortKey);
      return sortAsc ? diff : -diff;
    };
    groups.sort(cmp);
    groups.forEach((g) => g.teams.sort(cmp));
    return groups;
  }, [items, sortKey, sortAsc]);

  const thSortClass =
    "text-left text-xs cursor-pointer select-none hover:text-foreground";

  return (
    <Card className="flex h-full min-h-[320px] flex-col gap-2">
      <CardHeader className="shrink-0 flex flex-row flex-wrap items-start justify-between gap-x-2 gap-y-1 pb-0">
        <div className="min-w-0">
          <CardTitle className="text-lg">
            近两日客户平仓净盈亏 (Group)
          </CardTitle>
          <CardDescription className="text-xs">
            时间口径：MT Server 时间 · 按账户组分组
          </CardDescription>
        </div>
        <div className="flex flex-shrink-0 flex-wrap items-center gap-2">
          <Button
            size="sm"
            variant="outline"
            className="h-7 gap-1 text-xs"
            onClick={fetchData}
            disabled={loading}
          >
            {loading ? (
              <RefreshCw className="h-3 w-3 animate-spin" />
            ) : (
              <RefreshCw className="h-3 w-3" />
            )}
            刷新
          </Button>
          {fetchedAt && (
            <span className="flex items-center gap-1.5 text-xs text-muted-foreground">
              <Clock className="h-3 w-3" />
              数据获取时间:{" "}
              {fetchedAt.toLocaleString("zh-CN", { hour12: false })}
            </span>
          )}
        </div>
      </CardHeader>
      <CardContent className="min-h-0 flex-1 overflow-hidden">
        <div className="h-[260px] overflow-auto rounded-md border text-xs">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead className="w-7" aria-label="展开" />
                <TableHead className="text-xs">账户组</TableHead>
                <TableHead
                  className={thSortClass}
                  onClick={() => handleSort("pnl_today")}
                >
                  今日Profit (Excl.rbt)
                  <SortIcon active={sortKey === "pnl_today"} asc={sortAsc} />
                </TableHead>
                <TableHead
                  className={thSortClass}
                  onClick={() => handleSort("ib_today")}
                >
                  今日IB佣金
                  <SortIcon active={sortKey === "ib_today"} asc={sortAsc} />
                </TableHead>
                <TableHead
                  className={thSortClass}
                  onClick={() => handleSort("pnl_yesterday")}
                >
                  昨日Profit (Excl.rbt)
                  <SortIcon
                    active={sortKey === "pnl_yesterday"}
                    asc={sortAsc}
                  />
                </TableHead>
                <TableHead
                  className={thSortClass}
                  onClick={() => handleSort("ib_yesterday")}
                >
                  昨日IB佣金
                  <SortIcon active={sortKey === "ib_yesterday"} asc={sortAsc} />
                </TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {loading && (
                <TableRow>
                  <TableCell
                    colSpan={COL_SPAN}
                    className="text-muted-foreground text-center py-8"
                  >
                    加载中…
                  </TableCell>
                </TableRow>
              )}
              {error && (
                <TableRow>
                  <TableCell
                    colSpan={COL_SPAN}
                    className="text-destructive text-center py-4"
                  >
                    {error}
                  </TableCell>
                </TableRow>
              )}
              {!loading && !error && groupData.length === 0 && (
                <TableRow>
                  <TableCell
                    colSpan={COL_SPAN}
                    className="text-muted-foreground text-center py-8"
                  >
                    暂无数据
                  </TableCell>
                </TableRow>
              )}
              {!loading &&
                !error &&
                groupData.map((group, rowIndex) => {
                  const isExpanded = expandedGroup === group.account_group;
                  return (
                    <Fragment key={group.account_group}>
                      <TableRow
                        className={cn(
                          "cursor-pointer hover:bg-muted/50",
                          rowIndex % 2 === 1 && "bg-muted/30",
                        )}
                        onClick={() =>
                          setExpandedGroup((prev) =>
                            prev === group.account_group
                              ? null
                              : group.account_group,
                          )
                        }
                      >
                        <TableCell className="w-7 p-0 align-middle">
                          <span
                            className={cn(
                              "inline-flex size-6 items-center justify-center rounded",
                              isExpanded && "rotate-90",
                            )}
                          >
                            <ChevronRight className="size-3.5 transition-transform duration-200" />
                          </span>
                        </TableCell>
                        <TableCell className="font-medium">
                          {group.account_group}
                        </TableCell>
                        <TableCell
                          className={cn(
                            "text-left",
                            pnlColor(group.net_pnl_today),
                          )}
                        >
                          {formatPnl(group.net_pnl_today)}
                        </TableCell>
                        <TableCell className={cn("text-left", IB_COMM_COLOR)}>
                          {formatPnl(group.ib_commission_today)}
                        </TableCell>
                        <TableCell
                          className={cn(
                            "text-left",
                            pnlColor(group.net_pnl_yesterday),
                          )}
                        >
                          {formatPnl(group.net_pnl_yesterday)}
                        </TableCell>
                        <TableCell className={cn("text-left", IB_COMM_COLOR)}>
                          {formatPnl(group.ib_commission_yesterday)}
                        </TableCell>
                      </TableRow>
                      {isExpanded && (
                        <TableRow className="bg-muted/20 hover:bg-muted/20">
                          <TableCell
                            colSpan={COL_SPAN}
                            className="p-0 align-top"
                          >
                            <table className="w-full text-xs">
                              <tbody>
                                {group.teams.map((team, teamIndex) => (
                                  <tr
                                    key={team.sales_team}
                                    className={cn(
                                      "border-t border-border/50",
                                      teamIndex % 2 === 1 && "bg-muted/20",
                                    )}
                                  >
                                    <td className="w-7 py-1" />
                                    <td className="pl-6 py-1 text-muted-foreground">
                                      {team.sales_team}
                                    </td>
                                    <td
                                      className={cn(
                                        "text-left py-1",
                                        pnlColor(team.net_pnl_today),
                                      )}
                                    >
                                      {formatPnl(team.net_pnl_today)}
                                    </td>
                                    <td
                                      className={cn(
                                        "text-left py-1",
                                        IB_COMM_COLOR,
                                      )}
                                    >
                                      {formatPnl(team.ib_commission_today)}
                                    </td>
                                    <td
                                      className={cn(
                                        "text-left py-1",
                                        pnlColor(team.net_pnl_yesterday),
                                      )}
                                    >
                                      {formatPnl(team.net_pnl_yesterday)}
                                    </td>
                                    <td
                                      className={cn(
                                        "text-left py-1",
                                        IB_COMM_COLOR,
                                      )}
                                    >
                                      {formatPnl(team.ib_commission_yesterday)}
                                    </td>
                                  </tr>
                                ))}
                              </tbody>
                            </table>
                          </TableCell>
                        </TableRow>
                      )}
                    </Fragment>
                  );
                })}
            </TableBody>
          </Table>
        </div>
      </CardContent>
    </Card>
  );
}
