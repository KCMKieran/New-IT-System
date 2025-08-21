import { Button } from "@/components/ui/button"
import { Separator } from "@/components/ui/separator"
import { SidebarTrigger } from "@/components/ui/sidebar"
import { useLocation } from "react-router-dom"
import { useEffect } from "react"

const titleMap: Record<string, string> = {
  "/template": "模板",
  "/gold": "黄金报价",
  "/basis": "基差分析",
  "/downloads": "数据下载",
  "/warehouse": "报仓数据",
  "/positions": "实时全仓报表",
  "/login-ips": "Login IP监测",
  "/profit": "利润分析",
  "/settings": "Settings",
  "/search": "Search",
}

export function SiteHeader() {
  const location = useLocation()
  const pageTitle = (() => {
    const path = location.pathname
    // fresh grad note: handle root and cfg/* prefix
    if (path === "/") return "基差分析"
    if (path.startsWith("/cfg")) return "Configuration"
    return titleMap[path] || "KCM Analytics System"
  })()

  const fullTitle = `KCM Analytics System | ${pageTitle}`

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
