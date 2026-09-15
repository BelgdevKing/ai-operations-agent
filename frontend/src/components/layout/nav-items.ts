/** The application's primary destinations, in one place. */
export interface NavItem {
  href: string;
  label: string;
  description: string;
}

export const NAV_ITEMS: readonly NavItem[] = [
  {
    href: "/dashboard",
    label: "Dashboard",
    description: "Service health and, later, agent activity.",
  },
  {
    href: "/ai",
    label: "Agent console",
    description: "Supervise the agents: conversations, tool activity and execution.",
  },
  {
    href: "/approvals",
    label: "Approvals",
    description: "Actions waiting on a person.",
  },
  {
    href: "/workflows",
    label: "Workflows",
    description: "Business processes the platform runs, and what they are waiting on.",
  },
  {
    href: "/organization",
    label: "Organization",
    description: "The tenant this session acts on, and its members.",
  },
  {
    href: "/settings",
    label: "Settings",
    description: "How this browser session is configured.",
  },
] as const;
