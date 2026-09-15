"use client";

import { useAsyncResource } from "./use-async-resource";
import { errorMessage, isApiError } from "@/lib/api";

/** What a component needs to render: still loading, loaded, or failed. */
export type ResourceState<T> =
  | { kind: "loading" }
  | { kind: "ready"; data: T }
  | { kind: "error"; message: string; requestId: string | null };

export interface PolledResourceOptions {
  /** Re-fetch every N ms. Zero or omitted fetches once. */
  intervalMs?: number;
}

/**
 * Load something from the API on a timer, flattened to a message.
 *
 * For the small status widgets, which want a line of text rather than an error
 * to interpret. Anything that needs to tell a 403 from a 404 should use
 * `useAsyncResource` and `describeError` instead.
 */
export function usePolledResource<T>(
  load: (signal: AbortSignal) => Promise<T>,
  { intervalMs = 0 }: PolledResourceOptions = {},
): ResourceState<T> {
  const { state } = useAsyncResource(load, { intervalMs });

  if (state.kind === "error") {
    return {
      kind: "error",
      message: errorMessage(state.error),
      requestId: isApiError(state.error) ? state.error.requestId : null,
    };
  }

  return state;
}
