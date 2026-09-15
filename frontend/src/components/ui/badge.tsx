import type { ComponentProps } from "react";

import { cn } from "@/lib/cn";

const TONES = {
  neutral: "border-line bg-surface text-ink-muted",
  ok: "border-ok/30 bg-ok/10 text-ok",
  warn: "border-warn/30 bg-warn/10 text-warn",
  danger: "border-danger/30 bg-danger/10 text-danger",
  accent: "border-accent/30 bg-accent/10 text-accent",
} as const;

export type BadgeTone = keyof typeof TONES;

const DOTS: Record<BadgeTone, string> = {
  neutral: "bg-ink-muted",
  ok: "bg-ok",
  warn: "bg-warn",
  danger: "bg-danger",
  accent: "bg-accent",
};

export interface BadgeProps extends ComponentProps<"span"> {
  tone?: BadgeTone;
  /** Show a coloured dot, for statuses read at a glance. */
  dot?: boolean;
}

export function Badge({ className, tone = "neutral", dot = false, children, ...props }: BadgeProps) {
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1.5 rounded-full border px-2.5 py-0.5 text-xs font-medium",
        TONES[tone],
        className,
      )}
      {...props}
    >
      {dot && (
        <span className={cn("inline-block h-1.5 w-1.5 rounded-full", DOTS[tone])} aria-hidden />
      )}
      {children}
    </span>
  );
}
