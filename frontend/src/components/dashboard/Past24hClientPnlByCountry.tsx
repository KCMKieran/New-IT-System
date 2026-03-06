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
import { ChevronRight, Clock, RefreshCw } from "lucide-react";
import { cn } from "@/lib/utils";

/** One row from API: sales_team + today/yesterday PnL & IB commission + country */
interface SalesTeamPnlRow {
  sales_team: string;
  net_pnl_today: number;
  net_pnl_yesterday: number;
  net_pnl_total: number;
  ib_commission_today: number;
  ib_commission_yesterday: number;
  country: string;
}

/** Country group: country name + aggregated PnL & IB commission + list of sales team rows */
interface CountryGroup {
  country: string;
  net_pnl_today: number;
  net_pnl_yesterday: number;
  net_pnl_total: number;
  ib_commission_today: number;
  ib_commission_yesterday: number;
  teams: SalesTeamPnlRow[];
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

const IB_COMM_COLOR = "text-muted-foreground";

/**
 * Dashboard widget: 近两日客户平仓净盈亏.
 * Fetches per-sales-team PnL from API, groups by country (Plan A), shows expandable rows.
 * Time scope: MT Server natural day (今日 / 昨日).
 */
export default function Past24hClientPnlByCountry() {
  const [items, setItems] = useState<SalesTeamPnlRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [expandedCountry, setExpandedCountry] = useState<string | null>(null);
  const [fetchedAt, setFetchedAt] = useState<Date | null>(null);

  const fetchData = useCallback(() => {
    setLoading(true);
    setError(null);
    fetch("/api/v1/dashboard/pnl-by-sales-team")
      .then((res) => {
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        return res.json();
      })
      .then((data: { items: SalesTeamPnlRow[] }) => {
        setItems(data.items ?? []);
        setFetchedAt(new Date());
      })
      .catch((e) => {
        setError(e instanceof Error ? e.message : String(e));
      })
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => {
    fetchData();
  }, [fetchData]);

  // Group by country and sum; sort by net_pnl_yesterday ascending (min to max)
  const countryGroups = useMemo(() => {
    const map = new Map<string, CountryGroup>();
    for (const row of items) {
      const c = row.country || "Unknown";
      const g = map.get(c);
      if (g) {
        g.net_pnl_today += row.net_pnl_today;
        g.net_pnl_yesterday += row.net_pnl_yesterday;
        g.net_pnl_total += row.net_pnl_total;
        g.ib_commission_today += row.ib_commission_today;
        g.ib_commission_yesterday += row.ib_commission_yesterday;
        g.teams.push(row);
      } else {
        map.set(c, {
          country: c,
          net_pnl_today: row.net_pnl_today,
          net_pnl_yesterday: row.net_pnl_yesterday,
          net_pnl_total: row.net_pnl_total,
          ib_commission_today: row.ib_commission_today,
          ib_commission_yesterday: row.ib_commission_yesterday,
          teams: [row],
        });
      }
    }
    const groups = Array.from(map.values());
    groups.sort((a, b) => a.net_pnl_yesterday - b.net_pnl_yesterday);
    groups.forEach((g) => g.teams.sort((a, b) => a.net_pnl_yesterday - b.net_pnl_yesterday));
    return groups;
  }, [items]);

  return (
    <Card className="flex h-full min-h-[320px] flex-col gap-2">
      <CardHeader className="shrink-0 flex flex-row flex-wrap items-start justify-between gap-x-2 gap-y-1 pb-0">
        <div className="min-w-0">
          <CardTitle className="text-lg">近两日客户平仓净盈亏</CardTitle>
          <CardDescription className="text-xs">时间口径：MT Server 时间</CardDescription>
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
              数据获取时间: {fetchedAt.toLocaleString("zh-CN", { hour12: false })}
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
                <TableHead className="text-xs">国家/地区</TableHead>
                <TableHead className="text-left text-xs">今日Profit Excl.comm</TableHead>
                <TableHead className="text-left text-xs">今日IB佣金</TableHead>
                <TableHead className="text-left text-xs">昨日Profit Excl.comm</TableHead>
                <TableHead className="text-left text-xs">昨日IB佣金</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {loading && (
                <TableRow>
                  <TableCell colSpan={6} className="text-muted-foreground text-center py-8">
                    加载中…
                  </TableCell>
                </TableRow>
              )}
              {error && (
                <TableRow>
                  <TableCell colSpan={6} className="text-destructive text-center py-4">
                    {error}
                  </TableCell>
                </TableRow>
              )}
              {!loading && !error && countryGroups.length === 0 && (
                <TableRow>
                  <TableCell colSpan={6} className="text-muted-foreground text-center py-8">
                    暂无数据
                  </TableCell>
                </TableRow>
              )}
              {!loading && !error &&
                countryGroups.map((group, rowIndex) => {
                  const isExpanded = expandedCountry === group.country;
                  return (
                    <Fragment key={group.country}>
                      <TableRow
                        className={cn(
                          "cursor-pointer hover:bg-muted/50",
                          rowIndex % 2 === 1 && "bg-muted/30",
                        )}
                        onClick={() =>
                          setExpandedCountry((c) => (c === group.country ? null : group.country))
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
                        <TableCell className="font-medium">{group.country}</TableCell>
                        <TableCell className={cn("text-left", pnlColor(group.net_pnl_today))}>
                          {formatPnl(group.net_pnl_today)}
                        </TableCell>
                        <TableCell className={cn("text-left", IB_COMM_COLOR)}>
                          {formatPnl(group.ib_commission_today)}
                        </TableCell>
                        <TableCell className={cn("text-left", pnlColor(group.net_pnl_yesterday))}>
                          {formatPnl(group.net_pnl_yesterday)}
                        </TableCell>
                        <TableCell className={cn("text-left", IB_COMM_COLOR)}>
                          {formatPnl(group.ib_commission_yesterday)}
                        </TableCell>
                      </TableRow>
                      {isExpanded && (
                        <TableRow className="bg-muted/20 hover:bg-muted/20">
                          <TableCell colSpan={6} className="p-0 align-top">
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
                                    <td className={cn("text-left py-1", pnlColor(team.net_pnl_today))}>
                                      {formatPnl(team.net_pnl_today)}
                                    </td>
                                    <td className={cn("text-left py-1", IB_COMM_COLOR)}>
                                      {formatPnl(team.ib_commission_today)}
                                    </td>
                                    <td className={cn("text-left py-1", pnlColor(team.net_pnl_yesterday))}>
                                      {formatPnl(team.net_pnl_yesterday)}
                                    </td>
                                    <td className={cn("text-left py-1", IB_COMM_COLOR)}>
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
