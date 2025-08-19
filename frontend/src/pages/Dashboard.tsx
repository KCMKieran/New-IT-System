import { SectionCards } from "@/components/section-cards"
import { ChartAreaInteractive } from "@/components/chart-area-interactive"

// Dashboard template content rendered inside the persistent layout
export default function DashboardPage() {
  return (
    <div className="space-y-4 pb-6">
      <SectionCards />
      <div className="px-4 lg:px-6">
        <ChartAreaInteractive />
      </div>
    </div>
  )
}


