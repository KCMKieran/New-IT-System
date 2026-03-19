import type React from "react";
import { useEffect } from "react";
import { Outlet, useLocation } from "react-router-dom";
import { SidebarProvider, SidebarInset } from "@/components/ui/sidebar";
import { AppSidebar } from "@/components/app-sidebar";
import { SiteHeader } from "@/components/site-header";

/**
 * Cloudflare Access issues CF_Authorization JWT with ~10s TTL.
 * Navigation requests (page load/refresh) auto-renew it, but JS fetch() cannot.
 * This hook uses a hidden iframe to periodically trigger a navigation request,
 * which makes CF Access reissue CF_Authorization before it expires.
 * Only activates when accessed via HTTPS (i.e. through Cloudflare Tunnel).
 */
function useCfTokenRefresh(intervalMs = 8000) {
  useEffect(() => {
    if (window.location.protocol !== "https:") return;

    const iframe = document.createElement("iframe");
    iframe.style.cssText = "display:none;width:0;height:0;border:0";
    iframe.setAttribute("aria-hidden", "true");
    document.body.appendChild(iframe);

    const refresh = () => {
      iframe.src = "/cf-refresh.html?t=" + Date.now();
    };
    refresh();
    const timer = setInterval(refresh, intervalMs);

    return () => {
      clearInterval(timer);
      iframe.remove();
    };
  }, [intervalMs]);
}

// Persistent app shell: sidebar + header + content outlet
export default function DashboardLayout() {
  useCfTokenRefresh();
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
