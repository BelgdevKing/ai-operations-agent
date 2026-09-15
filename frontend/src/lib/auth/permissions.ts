/**
 * What the member interface should offer.
 *
 * A mirror of the rules in `app/services/membership.py`, kept here so the UI
 * can grey out an action instead of letting someone click it and read a 403.
 *
 * **This is not a security boundary.** Every rule below is enforced by the
 * backend, against the database, on every request. If the two ever disagree
 * the backend wins, and the only consequence of a mistake here is a button
 * that looked available and was not. Nothing may be granted on the strength of
 * this file.
 */

import type { MemberResponse, MemberRole } from "@/types/organization";

/** Roles allowed to administer membership at all. */
const ADMINISTRATIVE_ROLES: readonly MemberRole[] = ["owner", "admin"];

export type Permission = { allowed: true } | { allowed: false; reason: string };

const ALLOWED: Permission = { allowed: true };

function denied(reason: string): Permission {
  return { allowed: false, reason };
}

export interface Actor {
  userId: string;
  role: MemberRole;
}

/** Whoever is being acted on, as the member list reports them. */
export interface Target {
  userId: string;
  role: MemberRole;
}

export function isAdministrator(role: MemberRole): boolean {
  return ADMINISTRATIVE_ROLES.includes(role);
}

/**
 * How many owners the organization has.
 *
 * Counted from the member list, so it is only right if the whole list was
 * fetched - which is why the page asks for the backend's maximum page size.
 * A wrong count here can only mislead the UI; the backend does its own count
 * before allowing a demotion or removal.
 */
export function countOwners(members: readonly MemberResponse[]): number {
  return members.filter((member) => member.role === "owner").length;
}

/** Whether *actor* may change *target*'s role to `newRole`. */
export function canChangeRole(
  actor: Actor,
  target: Target,
  newRole: MemberRole,
  ownerCount: number,
): Permission {
  if (!isAdministrator(actor.role)) {
    return denied("You do not have permission to manage members.");
  }

  if (target.userId === actor.userId) {
    return denied("You cannot change your own role.");
  }

  // Granting or revoking ownership is the one privilege an admin lacks.
  if ((target.role === "owner" || newRole === "owner") && actor.role !== "owner") {
    return denied("Only an owner can grant or revoke the owner role.");
  }

  // Same role is a no-op the backend accepts without reaching its owner check,
  // so it is not refused here either.
  if (target.role === newRole) return ALLOWED;

  if (target.role === "owner" && ownerCount <= 1) {
    return denied("This is the organization's only owner. Promote another owner first.");
  }

  return ALLOWED;
}

/** Whether *actor* may remove *target* from the organization. */
export function canRemoveMember(actor: Actor, target: Target, ownerCount: number): Permission {
  const isSelf = target.userId === actor.userId;

  // Leaving is not an administrative act, so any member may remove themselves.
  if (!isSelf && !isAdministrator(actor.role)) {
    return denied("You do not have permission to manage members.");
  }

  if (!isSelf && target.role === "owner" && actor.role !== "owner") {
    return denied("Only an owner can remove another owner.");
  }

  if (target.role === "owner" && ownerCount <= 1) {
    return denied("This is the organization's only owner. Promote another owner first.");
  }

  return ALLOWED;
}

/** The roles *actor* may assign to *target*, for building a role selector. */
export function assignableRoles(
  actor: Actor,
  target: Target,
  ownerCount: number,
): readonly MemberRole[] {
  const roles: readonly MemberRole[] = ["owner", "admin", "member"];
  return roles.filter((role) => canChangeRole(actor, target, role, ownerCount).allowed);
}
