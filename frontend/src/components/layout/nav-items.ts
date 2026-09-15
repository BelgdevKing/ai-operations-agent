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
    label: "AI workspace",
    description: "Ask the configured model a question.",
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
