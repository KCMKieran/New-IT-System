import * as React from "react";
import { Link } from "react-router-dom";
import {
  IconBook,
  IconDashboard,
  IconDatabase,
  IconHelp,
  IconHome,
  IconLayoutColumns,
  IconListDetails,
  IconSearch,
  IconSettings,
  IconUsers,
  IconUsersGroup,
} from "@tabler/icons-react";

import { NavDocuments } from "@/components/nav-documents";
import { NavMain } from "@/components/nav-main";
import { NavSecondary } from "@/components/nav-secondary";
import { NavUser } from "@/components/nav-user";
import {
  Sidebar,
  SidebarContent,
  SidebarFooter,
  SidebarHeader,
} from "@/components/ui/sidebar";
import { useI18n } from "@/components/i18n-provider";
import { useAuth } from "@/providers/auth-provider";

export function AppSidebar({ ...props }: React.ComponentProps<typeof Sidebar>) {
  const { t } = useI18n();
  const { user, authEnabled } = useAuth();

  // Auth P4a: /cfg/managers is manager-only, so ordinary users should not even
  // see the entry — the page itself and every /admin endpoint refuse them
  // anyway, and a menu item that always errors is worse than no menu item.
  // ⚠ `!authEnabled` has to pass: with the kill switch thrown the backend lets
  // everyone through and /auth/me reports no user, so a bare role check would
  // hide the page precisely when auth is off (kill switch in reverse).
  const isManager = !authEnabled || user?.role === "manager";

  // Navigation data with translations
  const data = React.useMemo(
    () => ({
      // The hardcoded shadcn/m@example.com placeholder is gone — <NavUser />
      // reads the real session from useAuth() itself (auth P3).
      navSections: [
        {
          // Dashboard - direct link to home page
          title: "Dashboard",
          icon: IconHome,
          url: "/",
        },
        {
          title: t("nav.csDepartment"),
          icon: IconUsers,
          children: [
            // { title: t("nav.clientTrading"), url: "/client-trading" },
            { title: t("nav.loginIPs"), url: "/login-ips" },
            { title: t("nav.ibidLots"), url: "/ibid-lots" },
            { title: t("nav.fundFlowMonitor"), url: "/cs/fund-flow-monitor" },
            { title: t("nav.ibTreeQuery"), url: "/cs/ib-tree" },
            // [HIDDEN] ClientPnLMonitor page hidden
            // { title: t("nav.clientPnLMonitor"), url: "/client-pnl-monitor" },
          ],
        },
        {
          // Data Query section - data lookup and report pages
          title: t("nav.dataQuery"),
          icon: IconDatabase,
          children: [
            { title: t("nav.holdBucketReport"), url: "/hold-bucket-report" },
            { title: t("nav.ibFinancialMonitor"), url: "/ib-financial-monitor" },
            { title: t("nav.warehouseProducts"), url: "/warehouse/products" },
            { title: t("nav.position"), url: "/position" },
            { title: t("nav.ibData"), url: "/warehouse/ib-data" },
          ],
        },
        {
          title: t("nav.riskControlDepartment"),
          icon: IconDashboard,
          children: [
            { title: t("nav.riskMonitor"), url: "/risk-monitor" },
            { title: t("nav.riskWatchlist"), url: "/risk-watchlist" },
            { title: t("nav.windowScan"), url: "/window-scan" },
            // [HIDDEN] Client PnL Analysis - temporarily hidden
            // { title: "盈亏监控 (Preview)", url: "/client-pnl-analysis" },
            { title: t("nav.swapFreeControl"), url: "/swap-free-control" },
            { title: t("nav.clientReturnRate"), url: "/client-return-rate" },
            { title: t("nav.riskAlertMail"), url: "/risk-alert-mail" },
            // [HIDDEN] Basis page - 10.6.20.138:8050 service disabled
            // { title: t("nav.basisAnalysis"), url: "/basis" },
            // [HIDDEN] Profit Analysis - temporarily hidden
            // { title: t("nav.profitAnalysis"), url: "/profit" },
          ],
        },
        {
          title: t("nav.otherSection"),
          icon: IconListDetails,
          children: [
            // [REMOVED] Downloads page deprecated
            // { title: t("nav.downloads"), url: "/downloads" },
            { title: t("nav.template"), url: "/template" },
            // [HIDDEN] AgentGlobal - static JSON page, not using backend API
            // { title: t("nav.agentGlobal"), url: "/warehouse/agent-global" },
            // [DEPRECATED] CustomerPnLMonitor - removed, use ClientPnLAnalysis instead
            // { title: t("nav.customerPnLMonitor"), url: "/customer-pnl-monitor" },
            // [REMOVED] EquityMonitor page deleted
            // { title: t("nav.equityMonitor"), url: "/equity-monitor" },
          ],
        },
      ],
      navSecondary: [
        { title: t("common.settings"), url: "/settings", icon: IconSettings },
        {
          title: t("nav.getHelp"),
          url: "https://ui.shadcn.com/docs/installation",
          icon: IconHelp,
        },
        { title: t("common.search"), url: "/search", icon: IconSearch },
      ],
      documents: [
        // OPT-0026: docs portal — external link to MkDocs site at /docs/.
        // Marked `external` so NavDocuments renders an <a target="_blank">
        // instead of a SPA <Link> (mkdocs lives outside React Router).
        // In dev there's no local mkdocs container, so a relative /docs/ would
        // 404 on the Vite dev server — point it at the prod docs site instead
        // (it sits behind Cloudflare Access, so SSO/office-IP still applies).
        // In prod the relative path is served by the same Nginx.
        {
          name: t("config.docs"),
          url: import.meta.env.DEV
            ? "https://analysis.kohleservices.com/docs/"
            : "/docs/",
          icon: IconBook,
          external: true,
        },
        ...(isManager
          ? [
              {
                name: t("config.managers"),
                url: "/cfg/managers",
                icon: IconUsersGroup,
              },
            ]
          : []),
        // The six placeholder entries (Custom Groups / Reports / Financial /
        // Clients / Tasks / Marketing) were dropped from the sidebar — every one
        // of them opened the same empty <ConfigPlaceholder />. Their routes are
        // still registered in App.tsx, so old bookmarks keep working.
        {
          name: t("config.viewProfiles"),
          url: "/cfg/view-profiles",
          icon: IconLayoutColumns,
        },
      ],
    }),
    // isManager belongs here: /auth/me resolves after first paint, so the menu
    // has to be rebuilt when the role finally arrives.
    [t, isManager],
  );

  return (
    <Sidebar collapsible="offcanvas" {...props}>
      <SidebarHeader>
        {/* Logo links to home page */}
        <Link to="/" className="flex items-center gap-2 px-3 py-2">
          <img src="/logo.svg" alt="Company" className="h-24 w-auto block" />
        </Link>
      </SidebarHeader>
      <SidebarContent>
        <NavMain items={data.navSections} />
        <NavDocuments items={data.documents} />
        <NavSecondary items={data.navSecondary} className="mt-auto" />
      </SidebarContent>
      <SidebarFooter>
        <NavUser />
      </SidebarFooter>
    </Sidebar>
  );
}
