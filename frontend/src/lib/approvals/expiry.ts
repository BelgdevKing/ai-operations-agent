/**
 * How long an approval has left.
 *
 * Pure functions in their own module for two reasons. The interface needs them
 * in two places - the panel inside a conversation and the inbox - and a shared
 * helper is how those two stop disagreeing about what "2 days" means. And the
 * test runner imports `.ts` but not `.tsx`, so anything worth asserting on
 * directly has to live outside a component file.
 *
 * **Nothing here decides anything.** Whether an approval may still be decided is
 * settled by the backend, in the same statement that records the decision - a
 * countdown on a screen is a courtesy to whoever is reading it, and a screen
 * that has been open for a day is wrong about the time either way.
 */

/** Milliseconds until the deadline. Null when there is no deadline or no date. */
export function remainingMs(expiresAt: string | null, now: number = Date.now()): number | null {
  if (expiresAt === null) return null;

  const deadline = new Date(expiresAt).getTime();
  return Number.isNaN(deadline) ? null : deadline - now;
}

/**
 * "3 days", "5 hours", "12 minutes", "under a minute".
 *
 * The largest unit that still says something useful. A reviewer deciding
 * whether to deal with something now does not benefit from "2 days, 7 hours and
 * 13 minutes", and a queue full of ticking seconds is a queue that re-renders
 * forever.
 */
export function describeRemaining(milliseconds: number): string {
  const minutes = Math.floor(milliseconds / 60_000);
  if (minutes < 1) return "under a minute";
  if (minutes < 60) return plural(minutes, "minute");

  const hours = Math.floor(minutes / 60);
  if (hours < 24) return plural(hours, "hour");

  return plural(Math.floor(hours / 24), "day");
}

function plural(count: number, unit: string): string {
  return `${count} ${unit}${count === 1 ? "" : "s"}`;
}
