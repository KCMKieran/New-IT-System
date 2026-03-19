import type React from "react";
import { Outlet, useLocation } from "react-router-dom";
import { SidebarProvider, SidebarInset } from "@/components/ui/sidebar";
import { AppSidebar } from "@/components/app-sidebar";
import { SiteHeader } from "@/components/site-header";

// Persistent app shell: sidebar + header + content outlet
export default function DashboardLayout() {
  const location = useLocation();
  return (
    <SidebarProvider
      style={{ "--header-height": "3.5rem" } as React.CSSProperties}
    >
      <AppSidebar />
      <SidebarInset className="relative">
        <SiteHeader />

        {/* Routed page content renders here */}
        <div
          key={location.pathname}
          className="animate-fade-in duration-300 px-4 lg:px-6 pt-4"
        >
          <Outlet />
        </div>
      </SidebarInset>
    </SidebarProvider>
  );
}
