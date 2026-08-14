/**
 * `/cfg/managers` — user management (auth P4a).
 *
 * Manager-only. The gate is duplicated on purpose: the sidebar hides the entry,
 * this component refuses to render for non-managers, and every `/api/v1/admin/*`
 * endpoint is behind `require_manager` server-side. Only the last one is a
 * security boundary — the first two exist so a normal user never sees a page
 * made entirely of buttons that 403.
 *
 * ⚠ The kill switch has to pass. With `AUTH_ENABLED=false` the backend puts
 * `request.state.user = None` and lets everything through, so `/auth/me` reports
 * no user at all. Requiring `role === "manager"` unconditionally would make this
 * page unreachable exactly when auth is switched off — the same "kill switch in
 * reverse" trap that §2.1 calls out for `require_manager` itself.
 */

import { useAuth } from "@/providers/auth-provider";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import {
  IconClipboardList,
  IconLogin,
  IconShieldLock,
  IconUserPlus,
  IconUsers,
} from "@tabler/icons-react";
import { UsersTab } from "./managers/UsersTab";
import { AuditLogTab, AuthEventsTab } from "./managers/LogsTab";

export default function ManagersConfigPage() {
  const { user, authEnabled } = useAuth();
  const isManager = !authEnabled || user?.role === "manager";

  if (!isManager) {
    return (
      <div className="flex-1 p-4 md:p-6">
        <Card className="gap-3 max-w-xl">
          <CardContent className="flex items-start gap-3 pt-6">
            <IconShieldLock className="mt-0.5 h-5 w-5 shrink-0 text-muted-foreground" />
            <div className="space-y-1">
              <p className="font-medium">无权访问</p>
              <p className="text-sm text-muted-foreground">
                用户管理仅对 manager 开放。如需权限，请联系 IT。
              </p>
            </div>
          </CardContent>
        </Card>
      </div>
    );
  }

  return (
    <div className="flex-1 space-y-4 p-4 md:p-6">
      {/* Header row: description left, actions right (RiskMonitor pattern —
          lg: breakpoint so the Chinese description never gets squeezed). */}
      <div className="flex min-w-0 w-full max-w-full flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
        <div className="min-w-0 space-y-1">
          <h1 className="text-xl font-semibold tracking-tight">Managers</h1>
          <p className="text-sm text-muted-foreground">
            管理登录账号的角色、页面权限与会话。改角色或封禁会立即清除该用户的全部会话。
          </p>
        </div>
        <div className="flex min-w-0 w-full flex-wrap items-center gap-2 lg:w-auto lg:flex-nowrap lg:justify-end lg:shrink-0">
          <TooltipProvider delayDuration={150}>
            <Tooltip>
              {/* A disabled button swallows pointer events, so the tooltip has
                  to hang off a wrapper — otherwise it never opens. */}
              <TooltipTrigger asChild>
                <span tabIndex={0}>
                  <Button disabled>
                    <IconUserPlus className="mr-1.5 h-4 w-4" />
                    新建用户
                  </Button>
                </span>
              </TooltipTrigger>
              <TooltipContent side="bottom" className="max-w-xs">
                邮箱验证码建号（给没有公司 Microsoft 账号的人）属于 P4c，本轮未实现。
              </TooltipContent>
            </Tooltip>
          </TooltipProvider>
        </div>
      </div>

      <Tabs defaultValue="users" className="w-full">
        <TabsList className="grid w-full max-w-2xl grid-cols-3">
          <TabsTrigger value="users" className="gap-1.5">
            <IconUsers className="h-4 w-4" />
            用户
          </TabsTrigger>
          <TabsTrigger value="auth-events" className="gap-1.5">
            <IconLogin className="h-4 w-4" />
            登录日志
          </TabsTrigger>
          <TabsTrigger value="audit-log" className="gap-1.5">
            <IconClipboardList className="h-4 w-4" />
            操作日志
          </TabsTrigger>
        </TabsList>

        <TabsContent value="users" className="mt-4">
          <UsersTab />
        </TabsContent>
        {/* Both log tabs mount lazily via TabsContent, so their (potentially
            large, append-only) tables are not queried until someone looks. */}
        <TabsContent value="auth-events" className="mt-4">
          <AuthEventsTab />
        </TabsContent>
        <TabsContent value="audit-log" className="mt-4">
          <AuditLogTab />
        </TabsContent>
      </Tabs>
    </div>
  );
}
