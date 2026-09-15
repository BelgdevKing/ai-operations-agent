/**
 * Typed calls for the endpoints this application uses.
 *
 * Two groups. The health probes need nothing and call `api` directly. The rest
 * need credentials, so they take an `ApiCaller` from the session instead of
 * reaching for a token themselves - which is what stops a component from
 * assembling its own authenticated request.
 */

import type { ApiCaller, CallOptions } from "./authenticated";
import { api } from "./client";
import type {
  CurrentUserResponse,
  LoginRequest,
  RegisterRequest,
  RegisterResponse,
  TokenResponse,
} from "@/types/auth";
import type { HealthResponse, ReadinessResponse } from "@/types/health";
import type {
  MemberResponse,
  MemberRole,
  OrganizationResponse,
} from "@/types/organization";
import type { GenerateRequest, GenerateResponse } from "@/types/ai";

/** Mounted prefix of the versioned API. Matches `Settings.api_v1_prefix`. */
const V1 = "/api/v1";

// -- Health, unauthenticated --------------------------------------------------

/** Liveness. Answers as long as the process is serving. */
export function getHealth(signal?: AbortSignal): Promise<HealthResponse> {
  return api.get<HealthResponse>("/health", { signal });
}

/**
 * Readiness, including each dependency's verdict.
 *
 * 503 is a real answer here rather than a failure - it carries the same payload
 * and says which dependency is down - so it is allowed through.
 */
export function getReadiness(signal?: AbortSignal): Promise<ReadinessResponse> {
  return api.get<ReadinessResponse>("/health/ready", { signal, allowStatus: [503] });
}

// -- Authentication -----------------------------------------------------------

/**
 * Exchange credentials for an access token.
 *
 * Unauthenticated by definition, and a POST so the password travels in the
 * body. A wrong password, an unknown address and a suspended account all come
 * back as the same 401, which is the backend refusing to say which.
 */
export function login(credentials: LoginRequest, options?: CallOptions): Promise<TokenResponse> {
  return api.post<TokenResponse>(`${V1}/auth/login`, credentials, options);
}

/** Create a user, their organization, and their owner membership. */
export function register(payload: RegisterRequest, options?: CallOptions): Promise<RegisterResponse> {
  return api.post<RegisterResponse>(`${V1}/auth/register`, payload, options);
}

/** The caller's own profile and the organizations they belong to. */
export function getCurrentUser(
  caller: ApiCaller,
  options?: CallOptions,
): Promise<CurrentUserResponse> {
  return caller.get<CurrentUserResponse>(`${V1}/auth/me`, options);
}

// -- Organization -------------------------------------------------------------

/**
 * The organization this request acts on.
 *
 * Which one that is comes from the caller's verified membership. There is no
 * organization id in the path for a client to change.
 */
export function getOrganization(
  caller: ApiCaller,
  options?: CallOptions,
): Promise<OrganizationResponse> {
  return caller.get<OrganizationResponse>(`${V1}/organization`, options);
}

export interface ListMembersOptions extends CallOptions {
  limit?: number;
  offset?: number;
}

/** Members of the current organization. Readable by any active member. */
export function listMembers(
  caller: ApiCaller,
  { limit, offset, ...options }: ListMembersOptions = {},
): Promise<MemberResponse[]> {
  return caller.get<MemberResponse[]>(`${V1}/organization/members`, {
    ...options,
    query: { limit, offset },
  });
}

/**
 * Change one member's role.
 *
 * Requires admin or owner, and several narrower rules the backend enforces on
 * top - see `canChangeRole` for the copy of them the UI uses to decide what to
 * offer.
 */
export function changeMemberRole(
  caller: ApiCaller,
  userId: string,
  role: MemberRole,
  options?: CallOptions,
): Promise<MemberResponse> {
  return caller.patch<MemberResponse>(
    `${V1}/organization/members/${encodeURIComponent(userId)}`,
    { role },
    options,
  );
}

/** Remove a member. Answers 204, so there is no body to read. */
export function removeMember(
  caller: ApiCaller,
  userId: string,
  options?: CallOptions,
): Promise<void> {
  return caller.delete<void>(`${V1}/organization/members/${encodeURIComponent(userId)}`, options);
}

// -- AI ------------------------------------------------------------------------

/** Comfortably past the backend's own provider timeout, which answers 504. */
export const AI_TIMEOUT_MS = 90_000;

/**
 * Ask the configured model for a completion.
 *
 * The provider is fixed by server configuration and the organization comes
 * from the caller's verified membership, so neither appears here. What the
 * frontend knows is that it is calling the platform's AI endpoint.
 *
 * A generation can take a while, so the deadline is longer than the client's
 * default - but still a deadline, because a request that never ends leaves the
 * composer disabled forever.
 */
export function generate(
  caller: ApiCaller,
  body: GenerateRequest,
  options?: CallOptions,
): Promise<GenerateResponse> {
  return caller.post<GenerateResponse>(`${V1}/ai/generate`, body, {
    timeoutMs: AI_TIMEOUT_MS,
    ...options,
  });
}
