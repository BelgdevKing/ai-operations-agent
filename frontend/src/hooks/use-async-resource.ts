"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { isAbortError } from "@/lib/api";

/** Loading, loaded, or failed. The raw error is kept so callers can map it. */
export type AsyncState<T> =
  | { kind: "loading" }
  | { kind: "ready"; data: T }
  | { kind: "error"; error: unknown };

export interface AsyncResourceOptions {
  /**
   * Restart the load when this changes.
   *
   * For anything the request depends on that is not in the loader's closure by
   * accident - the acting organization, most of all, since switching it makes
   * every loaded answer stale.
   */
  key?: string;
  /** Re-fetch every N ms. Zero or omitted loads once. */
  intervalMs?: number;
  /** Skip loading entirely while false, staying in the loading state. */
  enabled?: boolean;
}

export interface AsyncResource<T> {
  state: AsyncState<T>;
  /** Load again now - after a mutation, or from a "try again" button. */
  reload: () => void;
}

/**
 * Load something from the API.
 *
 * The loader is held in a ref so an inline arrow at the call site does not
 * restart the request on every render - the usual way a panel turns into a
 * request loop. In-flight requests are aborted on unmount and on reload, and a
 * deliberate abort never becomes a rendered error.
 */
export function useAsyncResource<T>(
  load: (signal: AbortSignal) => Promise<T>,
  { key = "", intervalMs = 0, enabled = true }: AsyncResourceOptions = {},
): AsyncResource<T> {
  const [state, setState] = useState<AsyncState<T>>({ kind: "loading" });
  const [nonce, setNonce] = useState(0);

  const loadRef = useRef(load);
  useEffect(() => {
    loadRef.current = load;
  });

  const reload = useCallback(() => setNonce((value) => value + 1), []);

  useEffect(() => {
    if (!enabled) return;

    const controller = new AbortController();

    const run = async (): Promise<void> => {
      try {
        const data = await loadRef.current(controller.signal);
        if (!controller.signal.aborted) setState({ kind: "ready", data });
      } catch (error) {
        if (controller.signal.aborted || isAbortError(error)) return;
        setState({ kind: "error", error });
      }
    };

    void run();

    const timer = intervalMs > 0 ? setInterval(() => void run(), intervalMs) : undefined;

    return () => {
      controller.abort();
      if (timer !== undefined) clearInterval(timer);
    };
  }, [key, nonce, intervalMs, enabled]);

  return { state, reload };
}
