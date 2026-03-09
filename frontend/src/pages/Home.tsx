import { lazy, Suspense } from "react";
import { Card, CardContent, CardHeader } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";

const CnPaymentSuccessRate = lazy(() => import("@/components/dashboard/CnPaymentSuccessRate"));
const PositionSummary = lazy(() => import("@/components/dashboard/PositionSummary"));
const ReturnRateSummary = lazy(() => import("@/components/dashboard/ReturnRateSummary"));
const Past24hClientPnlByCountry = lazy(() => import("@/components/dashboard/Past24hClientPnlByCountry"));
const Past24hClientPnlByGroup = lazy(() => import("@/components/dashboard/Past24hClientPnlByGroup"));
const SuspiciousClients = lazy(() => import("@/components/dashboard/SuspiciousClients"));

function WidgetSkeleton() {
  return (
    <Card>
      <CardHeader>
        <Skeleton className="h-5 w-32" />
      </CardHeader>
      <CardContent className="space-y-3">
        <Skeleton className="h-24 w-full" />
        <Skeleton className="h-16 w-full" />
      </CardContent>
    </Card>
  );
}

export default function HomePage() {
  return (
    <div className="grid h-full grid-cols-1 gap-4 lg:grid-cols-4">
      {/* Left column: 1/4 - CN payment success rate */}
      <div className="lg:col-span-1 self-start lg:sticky lg:top-4">
        <Suspense fallback={<WidgetSkeleton />}>
          <CnPaymentSuccessRate />
        </Suspense>
      </div>

      {/* Right column: 3/4 - stacked vertically */}
      <div className="flex flex-col gap-4 lg:col-span-3">
        {/* Top: Position summary */}
        <Suspense fallback={<WidgetSkeleton />}>
          <PositionSummary />
        </Suspense>

        {/* Client return rate summary (full width) */}
        <Suspense fallback={<WidgetSkeleton />}>
          <ReturnRateSummary />
        </Suspense>

        {/* PnL by country + Suspicious clients side-by-side */}
        <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
          <Suspense fallback={<WidgetSkeleton />}>
            <Past24hClientPnlByCountry />
          </Suspense>
          <Suspense fallback={<WidgetSkeleton />}>
            <SuspiciousClients />
          </Suspense>
        </div>

        {/* PnL by account group: full width below */}
        <Suspense fallback={<WidgetSkeleton />}>
          <Past24hClientPnlByGroup />
        </Suspense>
      </div>
    </div>
  );
}
