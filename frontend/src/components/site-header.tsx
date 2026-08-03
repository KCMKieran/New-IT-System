import { Button } from "@/components/ui/button"
import { Separator } from "@/components/ui/separator"
import { SidebarTrigger } from "@/components/ui/sidebar"
import { useLocation } from "react-router-dom"
import { useEffect, useMemo } from "react"
import { ModeToggle } from "@/components/mode-toggle"
import { LanguageToggle } from "@/components/language-toggle"
import { useI18n } from "@/components/i18n-provider"

// Route to translation key mapping
const routeToKeyMap: Record<string, string> = {
  "/home": "pages.home",
  "/template": "pages.template",
  // "/equity-monitor": "pages.equityMonitor", // [REMOVED]
  "/gold": "pages.goldQuote",
  // "/basis": "pages.basisAnalysis", // [HIDDEN] - service disabled
  "/downloads": "pages.downloads",
  "/warehouse/agent-global": "pages.warehouseAgentGlobal",
  "/warehouse": "pages.warehouse",
  "/warehouse/products": "pages.warehouseProducts",
  "/warehouse/ib-data": "pages.ibData",
  "/warehouse/others": "pages.warehouseOthers",
  "/position": "pages.position",
  "/login-ips": "pages.loginIPs",
  "/profit": "pages.profitAnalysis",
  "/client-trading": "pages.clientTrading",
  "/ibid-lots": "pages.ibidLots",
  "/risk-monitor": "pages.riskMonitor",
  "/risk-alert-mail": "pages.riskAlertMail",
  "/risk-watchlist": "pages.riskWatchlist",
  "/swap-free-control": "pages.swapFreeControl",
  "/hold-bucket-report": "pages.holdBucketReport",
  "/ib-financial-monitor": "pages.ibFinancialMonitor",
  "/customer-pnl-monitor": "pages.customerPnLMonitor",
  "/customer-pnl-monitor-v2": "pages.customerPnLMonitorV2",
  "/settings": "pages.settings",
  "/search": "pages.search",
  "/cs/ib-tree": "pages.ibTreeQuery",
}

export function SiteHeader() {
  const location = useLocation()
  const { t } = useI18n()
  
  const pageTitle = useMemo(() => {
    const path = location.pathname
    // Handle root path - show home page title
    if (path === "/") return t("pages.home")
    // Handle configuration routes
    if (path.startsWith("/cfg")) return t("pages.configuration")
    // Get translation key for current route
    const key = routeToKeyMap[path]
    return key ? t(key) : t("header.title")
  }, [location.pathname, t])

  const fullTitle = `${t("header.title")} | ${pageTitle}`

  // Keep browser tab title in sync
  useEffect(() => {
    document.title = fullTitle
  }, [fullTitle])

  return (
    <header className="flex h-(--header-height) shrink-0 items-center gap-2 border-b transition-[width,height] ease-linear group-has-data-[collapsible=icon]/sidebar-wrapper:h-(--header-height)">
      <div className="flex w-full items-center gap-1 px-4 lg:gap-2 lg:px-6">
        <SidebarTrigger className="-ml-1" />
        <Separator
          orientation="vertical"
          className="mx-2 data-[orientation=vertical]:h-4"
        />
        <h1 className="text-base font-medium">{fullTitle}</h1>
        <div className="ml-auto flex items-center gap-2">
          <LanguageToggle />
          <ModeToggle />
          <Button variant="ghost" asChild size="sm" className="hidden sm:flex">
            <a
              href="https://github.com/shadcn-ui/ui/tree/main/apps/v4/app/(examples)/dashboard"
              rel="noopener noreferrer"
              target="_blank"
              className="dark:text-foreground"
            >
              GitHub
            </a>
          </Button>
        </div>
      </div>
    </header>
  )
}
