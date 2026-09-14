import type { Metadata } from "next";

import "./globals.css";

export const metadata: Metadata = {
  title: "AI Operations Agent Platform",
  description: "Multi-tenant platform for AI agents that operate on business data.",
};

export default function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en">
      <body className="min-h-screen antialiased">{children}</body>
    </html>
  );
}
