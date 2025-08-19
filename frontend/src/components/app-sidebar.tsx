import * as React from "react"
import {
  IconCamera,
  IconChartBar,
  IconDashboard,
  IconDatabase,
  IconFileAi,
  IconFileDescription,
  IconFileWord,
  IconFolder,
  IconHelp,
  IconInnerShadowTop,
  IconListDetails,
  IconReport,
  IconSearch,
  IconSettings,
  IconUsers,
} from "@tabler/icons-react"

import { NavDocuments } from "@/components/nav-documents"
import { NavMain } from "@/components/nav-main"
import { NavSecondary } from "@/components/nav-secondary"
import { NavUser } from "@/components/nav-user"
import {
  Sidebar,
  SidebarContent,
  SidebarFooter,
  SidebarHeader,
  SidebarMenu,
  SidebarMenuButton,
  SidebarMenuItem,
} from "@/components/ui/sidebar"

const data = {
  user: {
    name: "shadcn",
    email: "m@example.com",
    avatar: "/avatars/shadcn.jpg",
  },
  navMain: [
    { title: "模板", url: "/template", icon: IconDashboard },
    { title: "基差分析", url: "/basis", icon: IconDashboard },
    { title: "数据下载", url: "/downloads", icon: IconListDetails },
    { title: "报仓数据", url: "/warehouse", icon: IconChartBar },
    { title: "实时全仓报表", url: "/positions", icon: IconFolder },
    { title: "Login IP监测", url: "/login-ips", icon: IconUsers },
    { title: "利润分析", url: "/profit", icon: IconReport },
  ],
  navClouds: [
    {
      title: "Capture",
      icon: IconCamera,
      isActive: true,
      url: "#",
      items: [
        {
          title: "Active Proposals",
          url: "#",
        },
        {
          title: "Archived",
          url: "#",
        },
      ],
    },
    {
      title: "Proposal",
      icon: IconFileDescription,
      url: "#",
      items: [
        {
          title: "Active Proposals",
          url: "#",
        },
        {
          title: "Archived",
          url: "#",
        },
      ],
    },
    {
      title: "Prompts",
      icon: IconFileAi,
      url: "#",
      items: [
        {
          title: "Active Proposals",
          url: "#",
        },
        {
          title: "Archived",
          url: "#",
        },
      ],
    },
  ],
  navSecondary: [
    { title: "Settings", url: "/settings", icon: IconSettings },
    { title: "Get Help", url: "https://ui.shadcn.com/docs/installation", icon: IconHelp }, 
    { title: "Search", url: "/search", icon: IconSearch },
  ],
  documents: [
    { name: "Managers", url: "/cfg/managers", icon: IconDatabase },
    { name: "Reports", url: "/cfg/reports", icon: IconReport },
    { name: "Financial", url: "/cfg/financial", icon: IconFileWord },
    { name: "Clients", url: "/cfg/clients", icon: IconDatabase },
    { name: "Tasks", url: "/cfg/tasks", icon: IconReport },
    { name: "Marketing", url: "/cfg/marketing", icon: IconFileWord },
  ],
}

export function AppSidebar({ ...props }: React.ComponentProps<typeof Sidebar>) {
  return (
    <Sidebar collapsible="offcanvas" {...props}>
      <SidebarHeader>
        <div className="flex items-center gap-2 px-3 py-2">
          <img src="/Logo.svg" alt="Company" className="h-24 w-auto block" />
          {/* <span className="text-base font-semibold">KCM Trade</span> */}
        </div>
      </SidebarHeader>
      <SidebarContent>
        <NavMain items={data.navMain} />
        <NavDocuments items={data.documents} />
        <NavSecondary items={data.navSecondary} className="mt-auto" />
      </SidebarContent>
      <SidebarFooter>
        <NavUser user={data.user} />
      </SidebarFooter>
    </Sidebar>
  )
}
