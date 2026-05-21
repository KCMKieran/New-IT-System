"use client"

import * as React from "react"
import {
  IconDots,
  IconShare3,
  IconTrash,
  IconChevronDown,
  type Icon,
} from "@tabler/icons-react"

import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import {
  SidebarGroup,
  SidebarGroupLabel,
  SidebarMenu,
  SidebarMenuAction,
  SidebarMenuButton,
  SidebarMenuItem,
  useSidebar,
} from "@/components/ui/sidebar"
import { Link } from "react-router-dom"

type NavDocItem = {
  name: string
  url: string
  icon: Icon
  // External URLs (e.g. the docs portal at /docs/) need a real <a> so the
  // browser does a full navigation instead of the SPA router catching it.
  external?: boolean
}

// SidebarMenuButton's asChild path uses Radix Slot, which forwards a ref to
// the child element — so this wrapper must use forwardRef instead of being
// a plain function component, otherwise the ref handoff silently breaks.
const NavLink = React.forwardRef<
  HTMLAnchorElement,
  { item: NavDocItem; children: React.ReactNode }
>(({ item, children, ...rest }, ref) => {
  if (item.external) {
    return (
      <a
        ref={ref}
        href={item.url}
        target="_blank"
        rel="noopener noreferrer"
        {...rest}
      >
        {children}
      </a>
    )
  }
  return (
    <Link ref={ref} to={item.url} {...rest}>
      {children}
    </Link>
  )
})
NavLink.displayName = "NavLink"

export function NavDocuments({ items }: { items: NavDocItem[] }) {
  const { isMobile } = useSidebar()
  const [expanded, setExpanded] = React.useState(false)
  // Visible-vs-collapsed split: top 4 always shown, the rest fold under
  // "More". Bumped from 3 to 4 (OPT-0026) so adding the docs portal entry
  // doesn't push Reports below the fold for existing users.
  const firstItems = items.slice(0, 4)
  const restItems = items.slice(4)

  return (
    <SidebarGroup className="group-data-[collapsible=icon]:hidden">
      <SidebarGroupLabel>Configuration</SidebarGroupLabel>
      <SidebarMenu>
        {firstItems.map((item) => (
          <SidebarMenuItem key={item.name}>
            <SidebarMenuButton asChild>
              <NavLink item={item}>
                <item.icon />
                <span>{item.name}</span>
              </NavLink>
            </SidebarMenuButton>
            <DropdownMenu>
              <DropdownMenuTrigger asChild>
                <SidebarMenuAction
                  showOnHover
                  className="data-[state=open]:bg-accent rounded-sm"
                >
                  <IconDots />
                  <span className="sr-only">More</span>
                </SidebarMenuAction>
              </DropdownMenuTrigger>
              <DropdownMenuContent
                className="w-24 rounded-lg"
                side={isMobile ? "bottom" : "right"}
                align={isMobile ? "end" : "start"}
              >
                <DropdownMenuItem>
                  <span>Open</span>
                </DropdownMenuItem>
                <DropdownMenuItem>
                  <IconShare3 />
                  <span>Share</span>
                </DropdownMenuItem>
                <DropdownMenuSeparator />
                <DropdownMenuItem variant="destructive">
                  <IconTrash />
                  <span>Delete</span>
                </DropdownMenuItem>
              </DropdownMenuContent>
            </DropdownMenu>
          </SidebarMenuItem>
        ))}

        {restItems.length > 0 && (
          <>
            {/* Expandable content ABOVE the trigger so the button moves down when opened */}
            <SidebarMenuItem className="p-0">
              <ul
                className={`grid gap-1 overflow-hidden transition-all duration-300 ${expanded ? "max-h-96 opacity-100" : "max-h-0 opacity-0"}`}
              >
                {restItems.map((item) => (
                  <li key={item.name}>
                    <SidebarMenuButton asChild>
                      <NavLink item={item}>
                        <item.icon />
                        <span>{item.name}</span>
                      </NavLink>
                    </SidebarMenuButton>
                  </li>
                ))}
              </ul>
            </SidebarMenuItem>

            {/* Trigger BELOW content so it gets pushed down on expand */}
            <SidebarMenuItem>
              <SidebarMenuButton
                className="text-sidebar-foreground/70"
                onClick={() => setExpanded((v) => !v)}
                aria-expanded={expanded}
              >
                <IconChevronDown
                  className={`text-sidebar-foreground/70 transition-transform duration-300 ${expanded ? "rotate-180" : "rotate-0"}`}
                />
                <span>{expanded ? "Less" : "More"}</span>
              </SidebarMenuButton>
            </SidebarMenuItem>
          </>
        )}
      </SidebarMenu>
    </SidebarGroup>
  )
}
