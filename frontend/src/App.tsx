import { lazy, Suspense } from "react"
import { BrowserRouter, Routes, Route, Navigate } from "react-router-dom"
import { AuthProvider, useAuth } from "@/providers/auth-provider"

const DashboardLayout = lazy(() => import("@/layouts/DashboardLayout"))
const LoginPage = lazy(() => import("@/pages/Login"))
const DashboardTemplatePage = lazy(() => import("@/pages/Dashboard"))
const BasisPage = lazy(() => import("@/pages/Basis"))
const GoldQuotePage = lazy(() => import("@/pages/GoldQuote"))
const DownloadsPage = lazy(() => import("@/pages/Downloads"))
const WarehousePage = lazy(() => import("@/pages/Warehouse"))
const PositionsPage = lazy(() => import("@/pages/Positions"))
const LoginIPsPage = lazy(() => import("@/pages/LoginIPs"))
const ProfitPage = lazy(() => import("@/pages/Profit"))
const ConfigPlaceholder = lazy(() => import("@/pages/ConfigPlaceholder"))
const SettingsPage = lazy(() => import("@/pages/Settings"))
const SearchPage = lazy(() => import("@/pages/Search"))

function PrivateRoute({ children }: { children: React.ReactElement }) {
  const { isAuthenticated } = useAuth()
  return isAuthenticated ? children : <Navigate to="/login" replace />
}

function App() {
  return (
    <AuthProvider>
      <BrowserRouter>
        <Suspense fallback={<div className="p-4">Loading...</div>}>
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
              <Route index element={<BasisPage />} />
              <Route path="template" element={<DashboardTemplatePage />} />
              <Route path="gold" element={<GoldQuotePage />} />
              <Route path="basis" element={<BasisPage />} />
              <Route path="downloads" element={<DownloadsPage />} />
              <Route path="warehouse" element={<WarehousePage />} />
              <Route path="positions" element={<PositionsPage />} />
              <Route path="login-ips" element={<LoginIPsPage />} />
              <Route path="profit" element={<ProfitPage />} />
              <Route path="settings" element={<SettingsPage />} />
              <Route path="search" element={<SearchPage />} />
              {/* Configuration routes */}
              <Route path="cfg">
                <Route path=":" element={<ConfigPlaceholder />} />
                <Route path="managers" element={<ConfigPlaceholder />} />
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
      </BrowserRouter>
    </AuthProvider>
  )
}

export default App