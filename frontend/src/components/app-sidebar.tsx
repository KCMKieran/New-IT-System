import * as React from "react";
import { Link } from "react-router-dom";
import {
  IconBook,
  IconDashboard,
  IconDatabase,
  IconFileWord,
  IconHelp,
  IconHome,
  IconListDetails,
  IconReport,
  IconSearch,
  IconSettings,
  IconUsers,
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

export function AppSidebar({ ...props }: React.ComponentProps<typeof Sidebar>) {
  const { t } = useI18n();

  // Navigation data with translations
  const data = React.useMemo(
    () => ({
      user: {
        name: "shadcn",
        email: "m@example.com",
        avatar: "/avatars/shadcn.jpg",
      },
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
            { title: t("nav.ibFinancialMonitor"), url: "/ib-financial-monitor" },
            { title: t("nav.warehouseProducts"), url: "/warehouse/products" },
            { title: t("nav.position"), url: "/position" },
            { title: t("nav.ibData"), url: "/warehouse/ib-data" },
            { title: t("nav.ibReport"), url: "/ib-report" },
          ],
        },
        {
          title: t("nav.riskControlDepartment"),
          icon: IconDashboard,
          children: [
            { title: "交易实时监控", url: "/risk-monitor" },
            { title: t("nav.riskAlertMail"), url: "/risk-alert-mail" },
            { title: t("nav.clientReturnRate"), url: "/client-return-rate" },
            // [HIDDEN] Client PnL Analysis - temporarily hidden
            // { title: "盈亏监控 (Preview)", url: "/client-pnl-analysis" },
            { title: t("nav.swapFreeControl"), url: "/swap-free-control" },
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
        {
          name: t("config.managers"),
          url: "/cfg/managers",
          icon: IconDatabase,
        },
        {
          name: t("config.customGroups"),
          url: "/cfg/custom-groups",
          icon: IconSettings,
        },
        { name: t("config.reports"), url: "/cfg/reports", icon: IconReport },
        {
          name: t("config.financial"),
          url: "/cfg/financial",
          icon: IconFileWord,
        },
        { name: t("config.clients"), url: "/cfg/clients", icon: IconDatabase },
        { name: t("config.tasks"), url: "/cfg/tasks", icon: IconReport },
        {
          name: t("config.marketing"),
          url: "/cfg/marketing",
          icon: IconFileWord,
        },
      ],
    }),
    [t],
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
        <NavUser user={data.user} />
      </SidebarFooter>
    </Sidebar>
  );
}
