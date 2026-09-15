/**
 * Organization and membership contracts, mirroring `app/schemas/organization.py`.
 */

/** `app/models/enums.py::OrganizationStatus` */
export type OrganizationStatus = "active" | "suspended" | "archived";

/** `app/models/enums.py::MemberRole`, ordered from most to least privileged. */
export type MemberRole = "owner" | "admin" | "member";

/** `app/models/enums.py::MembershipStatus` */
export type MembershipStatus = "invited" | "active" | "suspended";

/** An organization as it appears inside another payload. */
export interface OrganizationSummary {
  id: string;
  name: string;
  slug: string;
  status: OrganizationStatus;
}

/** Response of `GET /api/v1/organizations/current`. */
export interface OrganizationResponse extends OrganizationSummary {
  /** ISO 8601 timestamp. */
  created_at: string;
}

/** The user behind a membership. Narrower than UserProfile: no status. */
export interface MemberUser {
  id: string;
  email: string;
  first_name: string | null;
  last_name: string | null;
}

/** One row of `GET /api/v1/organizations/current/members`. `id` is the membership. */
export interface MemberResponse {
  id: string;
  user: MemberUser;
  role: MemberRole;
  status: MembershipStatus;
  /** ISO 8601 timestamp. */
  created_at: string;
}

export interface MemberRoleUpdate {
  role: MemberRole;
}
