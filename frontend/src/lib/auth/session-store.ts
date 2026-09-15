/**
 * The one place the frontend knows it is signed in.
 *
 * Plain TypeScript with no React in it, for two reasons. It can be tested
 * without a DOM, and it makes the token's home obvious: a private field of one
 * object, reachable only through the API caller this store hands out.
 *
 * **Where the token lives, and why.** In memory, for the lifetime of the page.
 * The backend issues a bearer token in the login response body and supports
 * neither an HttpOnly cookie nor a refresh token, so the only two options are
 * a JavaScript-readable store or nothing that survives a reload. `localStorage`
 * and `sessionStorage` would both hand the token to any script that manages to
 * run on the page, and would keep handing it over after the tab is gone. This
 * keeps it in a `#private` field instead: never serialised, never in React
 * state, never in the rendered tree, and gone the moment the page unloads.
 *
 * The cost is real and deliberate: **a reload signs the user out.** Fixing that
 * properly needs a change on the backend - an HttpOnly cookie, or a refresh
 * token - which is not this part's to make.
 */

import { createAuthenticatedApi, type ApiCaller } from "@/lib/api/authenticated";
import { getCurrentUser, login } from "@/lib/api/endpoints";
import type { CallOptions } from "@/lib/api/authenticated";
import type {
  CurrentUserResponse,
  LoginRequest,
  MembershipSummary,
  TokenResponse,
  UserProfile,
} from "@/types/auth";

/** Why a session is not active. Lets the sign-in page explain itself. */
export type SignedOutReason = "expired" | "signed-out" | null;

export interface AnonymousSession {
  status: "anonymous";
  reason: SignedOutReason;
}

export interface AuthenticatingSession {
  status: "authenticating";
}

export interface AuthenticatedSession {
  status: "authenticated";
  user: UserProfile;
  /** Every organization the user actively belongs to, as the backend reports them. */
  memberships: MembershipSummary[];
  /**
   * The membership this session is acting as.
   *
   * Null when the user belongs to no active organization - possible, and a
   * state the interface has to show rather than crash on.
   */
  active: MembershipSummary | null;
}

export type SessionState = AnonymousSession | AuthenticatingSession | AuthenticatedSession;

/**
 * The starting state, shared by the server render and the first client render.
 *
 * One frozen object so both sides see the identical reference and React has no
 * hydration mismatch to report.
 */
export const INITIAL_SESSION: SessionState = Object.freeze({
  status: "anonymous",
  reason: null,
} as const);

export class SessionStore {
  /**
   * The access token.
   *
   * A true private field: unreachable from outside this class even at runtime,
   * so "who can read the token" has a one-word answer.
   */
  #token: string | null = null;

  #state: SessionState = INITIAL_SESSION;

  readonly #listeners = new Set<() => void>();

  /**
   * The authenticated caller, bound to this store.
   *
   * Handed to components so they can make authenticated requests without ever
   * touching the token.
   */
  readonly api: ApiCaller;

  constructor() {
    this.api = createAuthenticatedApi({
      getToken: () => this.#token,
      getOrganizationId: () => this.activeOrganizationId,
      // The backend no longer accepts these credentials, so the session is
      // over. Only 401 reaches here; a 403 leaves the session alone.
      onUnauthorized: () => this.#end("expired"),
    });
  }

  // -- Reading ----------------------------------------------------------------

  /** Stable between changes, as `useSyncExternalStore` requires. */
  getSnapshot = (): SessionState => this.#state;

  /** The server never has a token, so it always renders the signed-out state. */
  getServerSnapshot = (): SessionState => INITIAL_SESSION;

  subscribe = (listener: () => void): (() => void) => {
    this.#listeners.add(listener);
    return () => {
      this.#listeners.delete(listener);
    };
  };

  get activeOrganizationId(): string | null {
    const state = this.#state;
    return state.status === "authenticated" ? (state.active?.organization.id ?? null) : null;
  }

  // -- Signing in -------------------------------------------------------------

  /**
   * Exchange credentials for a session.
   *
   * Two calls, because the backend splits them: login returns a token and
   * nothing else, so who the user actually is comes from `/auth/me`.
   *
   * @throws The error from whichever call failed, already normalised by the API
   *   client, for the form to present. The store resets itself first, so a
   *   failure never leaves a half-open session behind.
   */
  async signIn(credentials: LoginRequest, options?: CallOptions): Promise<void> {
    this.#set({ status: "authenticating" });

    try {
      const token = await login(credentials, options);
      await this.#adopt(token, options);
    } catch (error) {
      this.#token = null;
      this.#set({ status: "anonymous", reason: null });
      throw error;
    }
  }

  /**
   * Start a session from a token the backend has already issued.
   *
   * Registration returns one, so a new user is signed in without being asked
   * for the password they just typed.
   */
  async adoptToken(token: TokenResponse, options?: CallOptions): Promise<void> {
    this.#set({ status: "authenticating" });

    try {
      await this.#adopt(token, options);
    } catch (error) {
      this.#token = null;
      this.#set({ status: "anonymous", reason: null });
      throw error;
    }
  }

  async #adopt(token: TokenResponse, options?: CallOptions): Promise<void> {
    this.#token = token.access_token;

    const me = await getCurrentUser(this.api, options);
    this.#set(this.#authenticatedFrom(me, null));
  }

  /**
   * Re-read the current user from the backend.
   *
   * The backend is the source of truth for the profile and for which
   * organizations the user is in, so anything that might have changed either -
   * a role change, a removal - is followed by this rather than by patching
   * local state and hoping.
   */
  async refreshUser(options?: CallOptions): Promise<void> {
    if (this.#state.status !== "authenticated") return;

    const me = await getCurrentUser(this.api, options);
    this.#set(this.#authenticatedFrom(me, this.activeOrganizationId));
  }

  #authenticatedFrom(me: CurrentUserResponse, preferredOrganizationId: string | null) {
    const memberships = me.memberships;

    // Keep acting as the same organization across a refresh when it is still
    // one of the user's; otherwise fall back to the first, which is the oldest.
    const active =
      memberships.find((m) => m.organization.id === preferredOrganizationId) ??
      memberships[0] ??
      null;

    return { status: "authenticated", user: me.user, memberships, active } as const;
  }

  // -- Switching organization -------------------------------------------------

  /**
   * Act as a different organization from now on.
   *
   * Only one of the user's own memberships is accepted, and an unknown id is
   * ignored rather than sent. That is a guard against a UI bug, not a security
   * control: the header is checked against membership by the backend on every
   * request, which is the only check that counts.
   */
  selectOrganization(organizationId: string): void {
    const state = this.#state;
    if (state.status !== "authenticated") return;

    const membership = state.memberships.find((m) => m.organization.id === organizationId);
    if (!membership || membership === state.active) return;

    this.#set({ ...state, active: membership });
  }

  // -- Signing out ------------------------------------------------------------

  /**
   * End the session.
   *
   * There is no backend logout endpoint - the token is stateless and simply
   * expires - so this drops the only copy of it. Subsequent requests carry no
   * credentials and are refused.
   */
  signOut(): void {
    this.#end("signed-out");
  }

  #end(reason: Exclude<SignedOutReason, null>): void {
    this.#token = null;
    this.#set({ status: "anonymous", reason });
  }

  // -- Notification -----------------------------------------------------------

  #set(state: SessionState): void {
    this.#state = state;
    for (const listener of this.#listeners) listener();
  }
}
