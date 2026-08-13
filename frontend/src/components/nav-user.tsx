import {
  IconDotsVertical,
  IconLogout,
  IconShieldCheck,
  IconUserCircle,
} from "@tabler/icons-react"

import {
  Avatar,
  AvatarFallback,
} from "@/components/ui/avatar"
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuGroup,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import {
  SidebarMenu,
  SidebarMenuButton,
  SidebarMenuItem,
  useSidebar,
} from "@/components/ui/sidebar"
import { useI18n } from "@/components/i18n-provider"
import { useAuth } from "@/providers/auth-provider"

/**
 * The signed-in user chip (auth design P3).
 *
 * Reads the real session rather than a prop: this used to render a hardcoded
 * "shadcn / m@example.com" from app-sidebar, and the Log out item had no
 * onClick at all — logout() was unreachable from the UI entirely.
 */
export function NavUser() {
  const { isMobile } = useSidebar()
  const { t } = useI18n()
  const { user, authEnabled, logout } = useAuth()

  // With the AUTH_ENABLED kill switch thrown there is no session to describe.
  // Say so plainly instead of inventing a name.
  const name = user?.displayName || user?.email?.split("@")[0] || t("auth.account")
  const email = user?.email ?? (authEnabled ? "" : "auth disabled")
  const initials = (user?.displayName || user?.email || "?")
    .split(/[\s.@]+/)
    .filter(Boolean)
    .slice(0, 2)
    .map((part) => part[0]?.toUpperCase() ?? "")
    .join("")

  return (
    <SidebarMenu>
      <SidebarMenuItem>
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <SidebarMenuButton
              size="lg"
              className="data-[state=open]:bg-sidebar-accent data-[state=open]:text-sidebar-accent-foreground"
            >
              <Avatar className="h-8 w-8 rounded-lg grayscale">
                <AvatarFallback className="rounded-lg">{initials}</AvatarFallback>
              </Avatar>
              <div className="grid flex-1 text-left text-sm leading-tight">
                <span className="truncate font-medium">{name}</span>
                <span className="text-muted-foreground truncate text-xs">
                  {email}
                </span>
              </div>
              <IconDotsVertical className="ml-auto size-4" />
            </SidebarMenuButton>
          </DropdownMenuTrigger>
          <DropdownMenuContent
            className="w-(--radix-dropdown-menu-trigger-width) min-w-56 rounded-lg"
            side={isMobile ? "bottom" : "right"}
            align="end"
            sideOffset={4}
          >
            <DropdownMenuLabel className="p-0 font-normal">
              <div className="flex items-center gap-2 px-1 py-1.5 text-left text-sm">
                <Avatar className="h-8 w-8 rounded-lg">
                  <AvatarFallback className="rounded-lg">{initials}</AvatarFallback>
                </Avatar>
                <div className="grid flex-1 text-left text-sm leading-tight">
                  <span className="truncate font-medium">{name}</span>
                  <span className="text-muted-foreground truncate text-xs">
                    {email}
                  </span>
                </div>
              </div>
            </DropdownMenuLabel>
            <DropdownMenuSeparator />
            {/* Account / Billing / Notifications were shadcn template stubs with
                no destinations. Role is the one thing worth showing here today;
                the user-management page arrives in P4. */}
            <DropdownMenuGroup>
              <DropdownMenuItem disabled>
                {user?.role === "manager" ? <IconShieldCheck /> : <IconUserCircle />}
                {user?.role ?? (authEnabled ? "—" : "auth disabled")}
              </DropdownMenuItem>
            </DropdownMenuGroup>
            <DropdownMenuSeparator />
            <DropdownMenuItem onClick={() => void logout()}>
              <IconLogout />
              {t("auth.signOut")}
            </DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      </SidebarMenuItem>
    </SidebarMenu>
  )
}
