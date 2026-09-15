import type { ComponentProps, ReactNode } from "react";

import { cn } from "@/lib/cn";

const TONES = {
  info: "border-line bg-surface-muted text-ink-muted",
  warn: "border-warn/30 bg-warn/10 text-warn",
  danger: "border-danger/30 bg-danger/10 text-danger",
} as const;

export type NoticeTone = keyof typeof TONES;

// `title` is overridden rather than inherited: the DOM attribute of that name
// is a tooltip string, and this one is a heading.
export interface NoticeProps extends Omit<ComponentProps<"div">, "title"> {
  tone?: NoticeTone;
  title?: ReactNode;
}

/** A callout: what is not built yet, or what went wrong. */
export function Notice({ className, tone = "info", title, children, ...props }: NoticeProps) {
  return (
    <div
      className={cn("rounded-lg border px-4 py-3 text-sm", TONES[tone], className)}
      {...props}
    >
      {title && <p className="font-medium text-ink">{title}</p>}
      {children && <div className={cn(title ? "mt-1" : undefined)}>{children}</div>}
    </div>
  );
}
