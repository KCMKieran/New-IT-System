import { Suspense, useEffect } from "react"
import { BrowserRouter, Routes, Route, Navigate } from "react-router-dom"
import { AuthProvider, useAuth } from "@/providers/auth-provider"
import { LazyErrorBoundary, PageLoader, lazyWithRetry } from "@/components/LazyErrorBoundary"
import { pruneStaleGridKeys } from "@/hooks/useGridColumnPersist"
import { ensureDeviceId } from "@/lib/view-profiles/device-id"
import { Toaster } from "@/components/ui/sonner"

const DashboardLayout = lazyWithRetry(() => import("@/layouts/DashboardLayout"))
const LoginPage = lazyWithRetry(() => import("@/pages/Login"))
const HomePage = lazyWithRetry(() => import("@/pages/Home"))
const DashboardTemplatePage = lazyWithRetry(() => import("@/pages/Dashboard"))
const DashboardPnlHistoryPage = lazyWithRetry(() => import("@/pages/DashboardPnlHistory"))
// [HIDDEN] Basis page - 10.6.20.138:8050 service disabled
// const BasisPage = lazyWithRetry(() => import("@/pages/Basis"))
const GoldQuotePage = lazyWithRetry(() => import("@/pages/GoldQuote"))
// [REMOVED] Downloads page deprecated
// const DownloadsPage = lazyWithRetry(() => import("@/pages/Downloads"))
const WarehousePage = lazyWithRetry(() => import("@/pages/Warehouse"))
// [REMOVED] EquityMonitor page deleted
// const EquityMonitorPage = lazyWithRetry(() => import("@/pages/EquityMonitor"))
const PositionPage = lazyWithRetry(() => import("@/pages/Position"))
const WarehouseProductsPage = lazyWithRetry(() => import("@/pages/WarehouseProducts"))
const WarehouseOthersPage = lazyWithRetry(() => import("@/pages/WarehouseOthers"))
const IBDataPage = lazyWithRetry(() => import("@/pages/IBData"))
const LoginIPsPage = lazyWithRetry(() => import("@/pages/LoginIPs"))
const ProfitPage = lazyWithRetry(() => import("@/pages/Profit"))
const AgentGlobalPage = lazyWithRetry(() => import("@/pages/AgentGlobal"))
const IbidLotsPage = lazyWithRetry(() => import("@/pages/IbidLots"))
const SwapFreeControlPage = lazyWithRetry(() => import("@/pages/SwapFreeControl"))
// [HIDDEN] ClientPnLMonitor page hidden
// const ClientPnLMonitorPage = lazyWithRetry(() => import("@/pages/ClientPnLMonitor"))
const ClientPnLAnalysisPage = lazyWithRetry(() => import("@/pages/ClientPnLAnalysis"))
const ClientReturnRatePage = lazyWithRetry(() => import("@/pages/ClientReturnRate"))
const ConfigPlaceholder = lazyWithRetry(() => import("@/pages/ConfigPlaceholder"))
const IBReportPage = lazyWithRetry(() => import("@/pages/IBReport"))
const IBFinancialMonitorPage = lazyWithRetry(() => import("@/pages/IBFinancialMonitor"))
const RiskMonitorPage = lazyWithRetry(() => import("@/pages/RiskMonitor"))
const RiskAlertMailCenterPage = lazyWithRetry(() => import("@/pages/RiskAlertMailCenter"))
const RiskWatchlistPage = lazyWithRetry(() => import("@/pages/RiskWatchlist"))
const FundFlowMonitorPage = lazyWithRetry(() => import("@/pages/cs/FundFlowMonitor"))
const IBTreeQueryPage = lazyWithRetry(() => import("@/pages/cs/IBTreeQuery"))
const SettingsPage = lazyWithRetry(() => import("@/pages/Settings"))
const SearchPage = lazyWithRetry(() => import("@/pages/Search"))

function PrivateRoute({ children }: { children: React.ReactElement }) {
  if (import.meta.env.VITE_DISABLE_AUTH === 'true') return children
  const { isAuthenticated } = useAuth()
  return isAuthenticated ? children : <Navigate to="/login" replace />
}

function App() {
  // Boot-time housekeeping: remove orphaned grid-state entries from
  // localStorage (renamed / removed grids leave dead keys behind). See
  // docs/features/grid-column-persist.md and OPT-0016.
  useEffect(() => {
    pruneStaleGridKeys();
    // OPT-0035: mint a stable device id on first load so view-profile claims
    // have a "my computer" handle from the very first request.
    ensureDeviceId();
  }, []);

  return (
    <AuthProvider>
      <BrowserRouter>
        <LazyErrorBoundary>
        <Suspense fallback={<PageLoader />}>
          <Routes>
            <Route path="/login" element={<LoginPage />} />
            <Route
              path="/"
              element={
                <PrivateRoute>
                  <DashboardLayout />
                </PrivateRoute>
              }
            >
              {/* Default route: show home page */}
              <Route index element={<HomePage />} />
              <Route path="home" element={<HomePage />} />
              <Route path="template" element={<DashboardTemplatePage />} />
              <Route path="dashboard/pnl-history" element={<DashboardPnlHistoryPage />} />
              {/* [REMOVED] EquityMonitor page deleted */}
              {/* <Route path="equity-monitor" element={<EquityMonitorPage />} /> */}
              <Route path="gold" element={<GoldQuotePage />} />
              {/* [HIDDEN] Basis page - 10.6.20.138:8050 service disabled */}
              {/* <Route path="basis" element={<BasisPage />} /> */}
              {/* [REMOVED] Downloads page deprecated */}
              {/* <Route path="downloads" element={<DownloadsPage />} /> */}
              <Route path="warehouse" element={<WarehousePage />} />
              <Route path="warehouse/products" element={<WarehouseProductsPage />} />
              <Route path="warehouse/ib-data" element={<IBDataPage />} />
              <Route path="warehouse/others" element={<WarehouseOthersPage />} />
              <Route path="warehouse/agent-global" element={<AgentGlobalPage />} />
              <Route path="position" element={<PositionPage />} />
              <Route path="login-ips" element={<LoginIPsPage />} />
              <Route path="profit" element={<ProfitPage />} />
              <Route path="ibid-lots" element={<IbidLotsPage />} />
              <Route path="swap-free-control" element={<SwapFreeControlPage />} />
              {/* [HIDDEN] ClientPnLMonitor page hidden */}
              {/* <Route path="client-pnl-monitor" element={<ClientPnLMonitorPage />} /> */}
              <Route path="client-pnl-analysis" element={<ClientPnLAnalysisPage />} />
              <Route path="client-return-rate" element={<ClientReturnRatePage />} />
              <Route path="ib-report" element={<IBReportPage />} />
              <Route path="ib-financial-monitor" element={<IBFinancialMonitorPage />} />
              <Route path="risk-monitor" element={<RiskMonitorPage />} />
              <Route path="risk-alert-mail" element={<RiskAlertMailCenterPage />} />
              <Route path="risk-watchlist" element={<RiskWatchlistPage />} />
              <Route path="cs/fund-flow-monitor" element={<FundFlowMonitorPage />} />
              <Route path="cs/ib-tree" element={<IBTreeQueryPage />} />
              {/* test page removed */}
              <Route path="settings" element={<SettingsPage />} />
              <Route path="search" element={<SearchPage />} />
              {/* Configuration routes */}
              <Route path="cfg">
                <Route path=":" element={<ConfigPlaceholder />} />
                <Route path="managers" element={<ConfigPlaceholder />} />
                <Route path="custom-groups" element={<ConfigPlaceholder />} />
                <Route path="reports" element={<ConfigPlaceholder />} />
                <Route path="financial" element={<ConfigPlaceholder />} />
                <Route path="clients" element={<ConfigPlaceholder />} />
                <Route path="tasks" element={<ConfigPlaceholder />} />
                <Route path="marketing" element={<ConfigPlaceholder />} />
              </Route>
            </Route>
            <Route path="*" element={<Navigate to="/" replace />} />
          </Routes>
        </Suspense>
        </LazyErrorBoundary>
      </BrowserRouter>
      {/* App-root toast outlet — sonner toast() calls (e.g. account-remarks
          save / 409 conflict messages) need this mounted once to render. */}
      <Toaster richColors closeButton />
    </AuthProvider>
  )
}

export default App