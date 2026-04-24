/**
 * Login IP Monitor — main page.
 *
 * Four tabs:
 *   1. 日报告   — daily correlation report (read-only)
 *   2. 监控账户  — watchlist CRUD
 *   3. 手动搜索  — on-demand search + async CSV export
 *   4. 运维    — scheduler timeline + mail recipients
 *
 * Each Tab is a self-contained component in ./login-ip/. This file is just
 * the shell — routing, sidebar, i18n are already wired up in App.tsx /
 * app-sidebar.tsx / site-header.tsx for the existing `/login-ips` route.
 */

import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import {
  IconReport,
  IconUsers,
  IconSearch,
  IconSettings,
} from "@tabler/icons-react";

import { ReportTab } from "./login-ip/ReportTab";
import { WatchlistTab } from "./login-ip/WatchlistTab";
import { SearchTab } from "./login-ip/SearchTab";
import { OperationsTab } from "./login-ip/OperationsTab";

export default function LoginIPsPage() {
  return (
    <div className="flex-1 space-y-4 p-4 md:p-6">
      <Tabs defaultValue="report" className="w-full">
        <TabsList className="grid w-full max-w-2xl grid-cols-4">
          <TabsTrigger value="report" className="gap-1.5">
            <IconReport className="h-4 w-4" />
            每日报告
          </TabsTrigger>
          <TabsTrigger value="watchlist" className="gap-1.5">
            <IconUsers className="h-4 w-4" />
            监控账户
          </TabsTrigger>
          <TabsTrigger value="search" className="gap-1.5">
            <IconSearch className="h-4 w-4" />
            手动搜索
          </TabsTrigger>
          <TabsTrigger value="ops" className="gap-1.5">
            <IconSettings className="h-4 w-4" />
            运维
          </TabsTrigger>
        </TabsList>

        <TabsContent value="report" className="mt-4">
          <ReportTab />
        </TabsContent>
        <TabsContent value="watchlist" className="mt-4">
          <WatchlistTab />
        </TabsContent>
        <TabsContent value="search" className="mt-4">
          <SearchTab />
        </TabsContent>
        <TabsContent value="ops" className="mt-4">
          <OperationsTab />
        </TabsContent>
      </Tabs>
    </div>
  );
}
