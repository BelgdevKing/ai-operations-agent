/**
 * Runtime configuration for the browser bundle.
 * Only NEXT_PUBLIC_* values are available client-side.
 */
export const apiUrl =
  process.env.NEXT_PUBLIC_API_URL?.replace(/\/$/, "") ?? "http://localhost:8000";

export const appName =
  process.env.NEXT_PUBLIC_APP_NAME ?? "AI Operations Agent Platform";
