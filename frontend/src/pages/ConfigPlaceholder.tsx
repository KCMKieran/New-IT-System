import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { useI18n } from "@/components/i18n-provider"
import { useNavigate } from "react-router-dom"
import {
  IconChartBar,
  IconDashboard,
  IconListDetails,
  IconUsers,
} from "@tabler/icons-react"

export default function ConfigPlaceholder() {
  const { t } = useI18n()
  const navigate = useNavigate()

  const sections = [
    {
      title: t("nav.csDepartment"),
      icon: IconUsers,
      items: [
        { title: t("nav.loginIPs"), url: "/login-ips" },
        { title: t("nav.ibidLots"), url: "/ibid-lots" },
      ],
    },
    {
      title: t("nav.dataQuery"),
      icon: IconChartBar,
      items: [
        { title: t("nav.warehouseProducts"), url: "/warehouse/products" },
        { title: t("nav.position"), url: "/position" },
        { title: t("nav.ibData"), url: "/warehouse/ib-data" },
        { title: t("nav.ibReport"), url: "/ib-report" },
      ],
    },
    {
      title: t("nav.riskControlDepartment"),
      icon: IconDashboard,
      items: [
        { title: t("nav.clientReturnRate"), url: "/client-return-rate" },
        { title: "盈亏监控 (Preview)", url: "/client-pnl-analysis" },
        { title: t("nav.swapFreeControl"), url: "/swap-free-control" },
        { title: t("nav.ibFinancialMonitor"), url: "/ib-financial-monitor" },
        { title: t("nav.profitAnalysis"), url: "/profit" },
      ],
    },
    {
      title: t("nav.otherSection"),
      icon: IconListDetails,
      items: [
        { title: t("nav.template"), url: "/template" },
      ],
    },
  ]

  return (
    <div className="p-8 bg-white min-h-[calc(100vh-4rem)]">
      <div className="max-w-6xl mx-auto space-y-8">
        <div className="text-center space-y-2">
          <h1 className="text-4xl font-bold tracking-tight text-gray-900">
            欢迎来到 KCM Trade Analytic Web
          </h1>
          <p className="text-lg text-gray-500">
            Select a module to get started
          </p>
        </div>

        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-6">
          {sections.map((section, idx) => (
            <Card key={idx} className="hover:shadow-lg transition-shadow border-gray-200">
              <CardHeader className="flex flex-row items-center gap-4 space-y-0 pb-2">
                <div className="p-2 bg-primary/10 rounded-lg">
                  <section.icon className="w-6 h-6 text-primary" />
                </div>
                <CardTitle className="text-base font-medium text-gray-900">
                  {section.title}
                </CardTitle>
              </CardHeader>
              <CardContent className="pt-4">
                <ul className="space-y-2">
                  {section.items.map((item, itemIdx) => (
                    <li key={itemIdx}>
                      <button
                        onClick={() => navigate(item.url)}
                        className="text-sm text-gray-600 hover:text-primary hover:underline text-left w-full py-1"
                      >
                        {item.title}
                      </button>
                    </li>
                  ))}
                </ul>
              </CardContent>
            </Card>
          ))}
        </div>
      </div>
    </div>
  )
}
