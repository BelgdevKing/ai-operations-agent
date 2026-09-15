import type { ReactNode } from "react";

export interface PageHeaderProps {
  title: ReactNode;
  description?: ReactNode;
  /** Actions aligned to the right of the title. */
  children?: ReactNode;
}

export function PageHeader({ title, description, children }: PageHeaderProps) {
  return (
    <header className="mb-6 flex flex-wrap items-start justify-between gap-3">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">{title}</h1>
        {description && <p className="mt-1.5 text-sm text-ink-muted">{description}</p>}
      </div>
      {children}
    </header>
  );
}
