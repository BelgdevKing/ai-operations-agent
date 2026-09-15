/**
 * Payloads shaped exactly like the backend's, so a contract drift shows up as
 * a failing test rather than as a screen that renders nothing.
 *
 * No real credentials anywhere: the tokens below are obvious fakes.
 */

import type { GenerateResponse } from "@/types/ai";
import type { CurrentUserResponse, TokenResponse, UserProfile } from "@/types/auth";
import type { MemberResponse, MemberRole, OrganizationSummary } from "@/types/organization";

/** Not a JWT, and not valid anywhere. Only ever compared as a string. */
export const FAKE_TOKEN = "test-access-token-not-a-real-jwt";

export const tokenResponse: TokenResponse = {
  access_token: FAKE_TOKEN,
  token_type: "bearer",
  expires_in: 1800,
};

export const user: UserProfile = {
  id: "11111111-1111-4111-8111-111111111111",
  email: "ada@example.com",
  first_name: "Ada",
  last_name: "Lovelace",
  status: "active",
};

export const acme: OrganizationSummary = {
  id: "22222222-2222-4222-8222-222222222222",
  name: "Acme Operations",
  slug: "acme-operations",
  status: "active",
};

export const globex: OrganizationSummary = {
  id: "33333333-3333-4333-8333-333333333333",
  name: "Globex",
  slug: "globex",
  status: "active",
};

export function currentUser(
  memberships: { organization: OrganizationSummary; role: MemberRole }[] = [
    { organization: acme, role: "owner" },
  ],
): CurrentUserResponse {
  return { user, memberships };
}

export function member(
  overrides: Partial<MemberResponse> & { role: MemberRole; userId: string },
): MemberResponse {
  const { role, userId, ...rest } = overrides;

  return {
    id: `membership-${userId}`,
    user: {
      id: userId,
      email: `${userId}@example.com`,
      first_name: null,
      last_name: null,
    },
    role,
    status: "active",
    created_at: "2026-01-01T00:00:00Z",
    ...rest,
  };
}

/** An answer from the AI endpoint, shaped exactly like `GenerateResponse`. */
export function generateResponse(content: string): GenerateResponse {
  return {
    content,
    model: "claude-opus-5",
    usage: { input_tokens: 12, output_tokens: 18, total_tokens: 30 },
    latency_ms: 412.5,
  };
}
