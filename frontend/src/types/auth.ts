/**
 * Authentication contracts, mirroring `app/schemas/auth.py`.
 *
 * Types only. Part 11A implements no authentication; these exist so the API
 * client and Part 11B share one definition of the wire format.
 */
import type { MemberRole, OrganizationSummary } from "./organization";

/** `app/models/enums.py::UserStatus` */
export type UserStatus = "pending" | "active" | "suspended" | "deactivated";

/** The caller's own profile. The backend never returns a password hash. */
export interface UserProfile {
  id: string;
  email: string;
  first_name: string | null;
  last_name: string | null;
  status: UserStatus;
}

export interface MembershipSummary {
  organization: OrganizationSummary;
  role: MemberRole;
}

/** Response of `GET /api/v1/auth/me`. */
export interface CurrentUserResponse {
  user: UserProfile;
  memberships: MembershipSummary[];
}

/** Response of `POST /api/v1/auth/login`. */
export interface TokenResponse {
  access_token: string;
  /** Always "bearer". */
  token_type?: string;
  /** Lifetime in seconds from issue. */
  expires_in: number;
}

export interface LoginRequest {
  email: string;
  password: string;
}

export interface RegisterRequest {
  email: string;
  password: string;
  organization_name: string;
  first_name?: string | null;
  last_name?: string | null;
}

/** Response of `POST /api/v1/auth/register`. */
export interface RegisterResponse {
  user: UserProfile;
  organization: OrganizationSummary;
  role: MemberRole;
  token: TokenResponse;
}
