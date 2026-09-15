"use client";

import { createContext, useContext, useState, useSyncExternalStore, type ReactNode } from "react";

import { SessionStore, type AuthenticatedSession, type SessionState } from "./session-store";

const SessionContext = createContext<SessionStore | null>(null);

/**
 * Holds the session for the whole application.
 *
 * Mounted once in the root layout so the session survives navigation between
 * sign-in and the authenticated pages. It is not a cache that can be rebuilt:
 * the token lives inside it, so a remount is a sign-out.
 */
export function SessionProvider({ children }: { children: ReactNode }) {
  // A lazy `useState` initialiser rather than `useMemo`, which React is free
  // to discard and recompute - and recomputing would silently drop the token.
  const [store] = useState(() => new SessionStore());

  return <SessionContext.Provider value={store}>{children}</SessionContext.Provider>;
}

/**
 * The session object itself: actions, and the authenticated API caller.
 *
 * Returned rather than wrapped in bound functions so method calls keep their
 * receiver and the token stays reachable only from inside the store.
 */
export function useSessionStore(): SessionStore {
  const store = useContext(SessionContext);
  if (store === null) {
    throw new Error("useSessionStore must be used inside <SessionProvider>.");
  }
  return store;
}

/** The current session state, re-rendering the component when it changes. */
export function useSession(): SessionState {
  const store = useSessionStore();
  return useSyncExternalStore(store.subscribe, store.getSnapshot, store.getServerSnapshot);
}

/**
 * The session, when the caller already knows it is signed in.
 *
 * Only valid below `<RequireAuth>`, which renders its children exclusively in
 * the authenticated state. Throwing rather than returning null keeps the
 * authenticated pages free of impossible branches.
 */
export function useAuthenticatedSession(): AuthenticatedSession {
  const session = useSession();
  if (session.status !== "authenticated") {
    throw new Error("useAuthenticatedSession must be used below <RequireAuth>.");
  }
  return session;
}

/** The authenticated API caller for the current session. */
export function useApi() {
  return useSessionStore().api;
}
