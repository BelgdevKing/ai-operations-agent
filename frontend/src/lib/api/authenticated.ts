/**
 * The authenticated half of the API client.
 *
 * Components never build an `Authorization` header. They ask the session for a
 * caller, and the caller attaches whatever credentials the session is holding
 * at the moment of the call. That indirection is the point: the token has one
 * home, and a component that wanted to misuse it would have to go looking.
 *
 * Everything underneath - timeout, cancellation, `ApiError`, `NetworkError`,
 * the request id, the error envelope - is the existing `request`, unchanged.
 */

import { request, type RequestOptions } from "./client";
import { ApiError } from "./errors";

/**
 * What a caller may control per request.
 *
 * `token` and `organizationId` are deliberately absent: both are the session's
 * to decide, and a component that could override them could act as a different
 * tenant. Not that the backend would let it - it checks membership either way -
 * but the frontend should not be the place that tries.
 */
export type CallOptions = Omit<RequestOptions, "method" | "body" | "token" | "organizationId">;

export interface ApiCaller {
  get<T>(path: string, options?: CallOptions): Promise<T>;
  post<T>(path: string, body?: unknown, options?: CallOptions): Promise<T>;
  patch<T>(path: string, body?: unknown, options?: CallOptions): Promise<T>;
  delete<T = void>(path: string, options?: CallOptions): Promise<T>;
}

export interface AuthenticatedApiConfig {
  /** The access token to send, or null when there is none. */
  getToken: () => string | null;
  /**
   * The organization to act as, sent as `X-Organization-ID`.
   *
   * A request, not a grant: the backend resolves it against the caller's own
   * memberships and refuses anything else.
   */
  getOrganizationId?: () => string | null;
  /**
   * Called when the backend rejects the credentials.
   *
   * Only on 401. A 403 means the token was accepted and the answer is still
   * no - ending the session over one would log a user out for opening a page
   * they are simply not allowed to see.
   */
  onUnauthorized?: () => void;
}

export function createAuthenticatedApi({
  getToken,
  getOrganizationId,
  onUnauthorized,
}: AuthenticatedApiConfig): ApiCaller {
  const call = async <T>(path: string, options: RequestOptions): Promise<T> => {
    const token = getToken();
    const organizationId = getOrganizationId?.();

    try {
      return await request<T>(path, {
        ...options,
        token: token ?? undefined,
        organizationId: organizationId ?? undefined,
      });
    } catch (error) {
      if (error instanceof ApiError && error.status === 401) {
        onUnauthorized?.();
      }
      throw error;
    }
  };

  return {
    get: (path, options) => call(path, { ...options, method: "GET" }),
    post: (path, body, options) => call(path, { ...options, method: "POST", body }),
    patch: (path, body, options) => call(path, { ...options, method: "PATCH", body }),
    delete: (path, options) => call(path, { ...options, method: "DELETE" }),
  };
}
