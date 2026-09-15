"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

import { NAV_ITEMS } from "@/components/layout/nav-items";
import { cn } from "@/lib/cn";

/**
 * Primary navigation.
 *
 * A client component only because marking the current route needs the path;
 * nothing else here is interactive.
 */
export function MainNav() {
  const pathname = usePathname();

  return (
    <nav aria-label="Primary">
      <ul className="flex flex-wrap gap-1 md:flex-col md:flex-nowrap">
        {NAV_ITEMS.map((item) => {
          const current = pathname === item.href || pathname.startsWith(`${item.href}/`);

          return (
            <li key={item.href}>
              <Link
                href={item.href}
                aria-current={current ? "page" : undefined}
                className={cn(
                  "block rounded-lg px-3 py-2 text-sm transition-colors",
                  "outline-none focus-visible:ring-2 focus-visible:ring-accent",
                  current
                    ? "bg-accent/10 font-medium text-accent"
                    : "text-ink-muted hover:bg-surface-muted hover:text-ink",
                )}
              >
                {item.label}
              </Link>
            </li>
          );
        })}
      </ul>
    </nav>
  );
}
