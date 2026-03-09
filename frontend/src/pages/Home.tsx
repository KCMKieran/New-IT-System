import { lazy, Suspense } from "react";
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { CreditCard } from "lucide-react";

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
      {/* Left column: 1/4 - CN payment success rate placeholder */}
      <div className="lg:col-span-1 self-start lg:sticky lg:top-4">
        <Card>
          <CardHeader>
            <div className="flex items-center gap-2">
              <div className="rounded-lg bg-primary/10 p-2">
                <CreditCard className="h-5 w-5 text-primary" />
              </div>
              <div>
                <CardTitle className="text-lg">CN渠道支付成功率</CardTitle>
                <CardDescription>实时监控各支付渠道</CardDescription>
              </div>
            </div>
          </CardHeader>
          <CardContent className="flex flex-1 items-center justify-center">
            <div className="text-center space-y-2 py-12">
              <div className="text-4xl text-muted-foreground/30">📊</div>
              <p className="text-sm text-muted-foreground">Coming Soon</p>
              <p className="text-xs text-muted-foreground/60">功能开发中</p>
            </div>
          </CardContent>
        </Card>
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
