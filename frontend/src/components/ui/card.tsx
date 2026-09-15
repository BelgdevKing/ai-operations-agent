import type { ComponentProps } from "react";

import { cn } from "@/lib/cn";

/** A bordered panel. The unit most of the dashboard is built from. */
export function Card({ className, ...props }: ComponentProps<"div">) {
  return (
    <div
      className={cn("rounded-xl border border-line bg-surface-muted p-5", className)}
      {...props}
    />
  );
}

export function CardHeader({ className, ...props }: ComponentProps<"div">) {
  return <div className={cn("flex items-start justify-between gap-3", className)} {...props} />;
}

/** An `h2`: cards sit directly beneath the page heading, with nothing between. */
export function CardTitle({ className, ...props }: ComponentProps<"h2">) {
  return <h2 className={cn("text-sm font-semibold tracking-tight", className)} {...props} />;
}

export function CardDescription({ className, ...props }: ComponentProps<"p">) {
  return <p className={cn("mt-1 text-sm text-ink-muted", className)} {...props} />;
}

export function CardContent({ className, ...props }: ComponentProps<"div">) {
  return <div className={cn("mt-4 text-sm", className)} {...props} />;
}
