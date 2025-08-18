import type React from "react"
import { SidebarProvider, SidebarInset } from "@/components/ui/sidebar"
import { AppSidebar } from "@/components/app-sidebar"
import { SiteHeader } from "@/components/site-header"
import { SectionCards } from "@/components/section-cards"
import { ChartAreaInteractive } from "@/components/chart-area-interactive"
import { ModeToggle } from "@/components/mode-toggle"

// Dashboard page composed from generated components
export default function DashboardPage() {
  return (
    <SidebarProvider style={{ "--header-height": "3.5rem" } as React.CSSProperties}>
      <AppSidebar />
      <SidebarInset>
        <SiteHeader />
        <div className="flex items-center justify-end px-4 py-2 lg:px-6">
          <ModeToggle />
        </div>
        <div className="space-y-4 pb-6">
          <SectionCards />
          <div className="px-4 lg:px-6">
            <ChartAreaInteractive />
          </div>
        </div>
      </SidebarInset>
    </SidebarProvider>
  )
}


