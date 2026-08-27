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
import { ALL_MODULES, canAccessPath, type ModuleAccess } from "@/lib/modules";

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

  // Auth P4b: what this user may open. Same shape the route guard uses, and the
  // same shared predicate — written twice, a sidebar filter and a route guard
  // drift, and the symptom is either a menu entry that leads straight to a 403
  // or a page reachable only through an entry that was hidden.
  //
  // ⚠ `allowedModules` passed through untouched: `[]` (nothing) and `["*"]`
  // (everything) are opposite grants, and the provider is the single place
  // that decides which one a response means. `authEnabled` is inside the
  // predicate for the same reason `isManager` handles it above — with the kill
  // switch thrown /auth/me reports no user, and a bare grant check would empty
  // the sidebar precisely when auth is off. The no-user fallback is permissive
  // for the same reason: an empty menu is how "the app is broken" looks.
  const access = React.useMemo<ModuleAccess>(
    () => ({
      authEnabled,
      isManager,
      allowedModules: user ? user.allowedModules : [ALL_MODULES],
    }),
    [authEnabled, isManager, user],
  );

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
            { title: t("nav.csIbDeposits"), url: "/cs/ib-deposits" },
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
          // Auth P4b: manager-only, enforced by nginx's auth_request
          // (?require=manager) — this flag only stops the click. Shown rather
          // than hidden because the portal has been linked to internally for
          // months; a vanished entry reads as "the docs were deleted", a greyed
          // one reads as "I need access", which is the true statement.
          disabled: !isManager,
          disabledReason: t("nav.docsManagerOnly"),
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
    // has to be rebuilt when the role finally arrives. The module grant lands in
    // the same response but is not a dependency of THIS memo — it filters the
    // finished list below rather than changing what is built.
    [t, isManager],
  );

  // Auth P4b: drop what this user cannot open.
  //
  // Filtered per CHILD rather than per group, even though every group happens
  // to be single-module today: the table in lib/modules.ts is per page, so a
  // group that later mixes modules (or gains one page that is manager-only)
  // filters correctly without anyone remembering to revisit this.
  //
  // ⚠ A group whose children all disappear is removed entirely, heading and
  // all. Leaving the title behind renders a department name that expands into
  // nothing, which looks like a loading bug rather than a permission.
  const visibleNavSections = React.useMemo(
    () =>
      data.navSections
        .map((section) =>
          section.children
            ? { ...section, children: section.children.filter((c) => canAccessPath(access, c.url)) }
            : section,
        )
        .filter((section) =>
          section.children
            ? section.children.length > 0
            : !section.url || canAccessPath(access, section.url),
        ),
    [data.navSections, access],
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
        <NavMain items={visibleNavSections} />
        <NavDocuments items={data.documents} />
        <NavSecondary items={data.navSecondary} className="mt-auto" />
      </SidebarContent>
      <SidebarFooter>
        <NavUser />
      </SidebarFooter>
    </Sidebar>
  );
}
