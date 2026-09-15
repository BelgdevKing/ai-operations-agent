import type { ComponentProps } from "react";

import { cn } from "@/lib/cn";

const VARIANTS = {
  primary: "bg-accent text-white hover:brightness-110",
  secondary: "border border-line bg-surface hover:bg-surface-muted",
  ghost: "hover:bg-surface-muted",
} as const;

export type ButtonVariant = keyof typeof VARIANTS;

const BASE =
  "inline-flex items-center justify-center gap-2 rounded-lg px-3.5 py-2 text-sm font-medium " +
  "transition-[filter,background-color] outline-none focus-visible:ring-2 focus-visible:ring-accent " +
  "focus-visible:ring-offset-2 focus-visible:ring-offset-surface " +
  "disabled:cursor-not-allowed disabled:opacity-50";

/**
 * The button's classes, for elements that must not be a `<button>`.
 *
 * A navigation control has to be an anchor - `next/link` renders one - and
 * wrapping it in a button would break middle-click, copy-link and keyboard
 * behaviour. Sharing the classes keeps the two looking identical anyway.
 */
export function buttonClasses(variant: ButtonVariant = "primary", className?: string): string {
  return cn(BASE, VARIANTS[variant], className);
}

export function Button({
  className,
  variant = "primary",
  type = "button",
  ...props
}: ComponentProps<"button"> & { variant?: ButtonVariant }) {
  // Defaulting to type="button" so a button inside a form does not submit it by
  // accident; a real submit says so explicitly.
  return <button type={type} className={buttonClasses(variant, className)} {...props} />;
}
