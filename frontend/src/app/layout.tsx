import type { Metadata } from "next";

import { SessionProvider } from "@/lib/auth/session-context";
import { appName } from "@/lib/config";

import "./globals.css";

export const metadata: Metadata = {
  // Pages set only their own name; the template appends the product.
  title: { default: appName, template: `%s · ${appName}` },
  description: "Multi-tenant platform for AI agents that operate on business data.",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en">
      <body className="min-h-screen antialiased">
        {/*
          The session lives above the router so it survives navigation between
          sign-in and the authenticated pages. It holds the access token, so a
          remount would be a sign-out.
        */}
        <SessionProvider>{children}</SessionProvider>
      </body>
    </html>
  );
}
