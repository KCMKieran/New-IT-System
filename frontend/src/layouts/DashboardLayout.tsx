import type React from "react"
import { Outlet } from "react-router-dom"
import { SidebarProvider, SidebarInset } from "@/components/ui/sidebar"
import { AppSidebar } from "@/components/app-sidebar"
import { SiteHeader } from "@/components/site-header"
import { ModeToggle } from "@/components/mode-toggle"

// Persistent app shell: sidebar + header + content outlet
export default function DashboardLayout() {
  return (
    <SidebarProvider style={{ "--header-height": "3.5rem" } as React.CSSProperties}>
      <AppSidebar />
      <SidebarInset>
        <SiteHeader />
        <div className="flex items-center justify-end px-4 py-2 lg:px-6">
          <ModeToggle />
        </div>
        {/* Routed page content renders here */}
        <Outlet />
      </SidebarInset>
    </SidebarProvider>
  )
}


