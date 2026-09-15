/**
 * The member-management rules the UI mirrors.
 *
 * Each case here has a counterpart in `app/services/membership.py`. If the two
 * drift, the interface offers a button the backend refuses - annoying, but not
 * dangerous, which is exactly the status these rules have.
 */

import assert from "node:assert/strict";
import test from "node:test";

import {
  assignableRoles,
  canChangeRole,
  canRemoveMember,
  countOwners,
  isAdministrator,
} from "@/lib/auth/permissions";
import { member } from "../support/fixtures";

const OWNER = { userId: "owner-1", role: "owner" as const };
const OTHER_OWNER = { userId: "owner-2", role: "owner" as const };
const ADMIN = { userId: "admin-1", role: "admin" as const };
const MEMBER = { userId: "member-1", role: "member" as const };

test("owners and admins administer membership; members do not", () => {
  assert.equal(isAdministrator("owner"), true);
  assert.equal(isAdministrator("admin"), true);
  assert.equal(isAdministrator("member"), false);
});

test("owners are counted from the member list", () => {
  const members = [
    member({ userId: "a", role: "owner" }),
    member({ userId: "b", role: "owner" }),
    member({ userId: "c", role: "member" }),
  ];
  assert.equal(countOwners(members), 2);
});

// -- Changing a role ----------------------------------------------------------

test("a plain member may not change anyone's role", () => {
  const result = canChangeRole(MEMBER, ADMIN, "member", 2);
  assert.equal(result.allowed, false);
});

test("nobody changes their own role", () => {
  // Self-promotion is the obvious attack; self-demotion is usually a mistake.
  const result = canChangeRole(OWNER, { userId: OWNER.userId, role: "owner" }, "member", 2);
  assert.equal(result.allowed, false);
  assert.match(result.allowed === false ? result.reason : "", /your own role/);
});

test("an admin may promote a member to admin", () => {
  assert.equal(canChangeRole(ADMIN, MEMBER, "admin", 1).allowed, true);
});

test("an admin may not grant the owner role", () => {
  // Otherwise an admin could make themselves an owner through a second account.
  const result = canChangeRole(ADMIN, MEMBER, "owner", 1);
  assert.equal(result.allowed, false);
  assert.match(result.allowed === false ? result.reason : "", /Only an owner/);
});

test("an admin may not revoke the owner role", () => {
  assert.equal(canChangeRole(ADMIN, OTHER_OWNER, "member", 2).allowed, false);
});

test("an owner may demote another owner while a second owner remains", () => {
  assert.equal(canChangeRole(OWNER, OTHER_OWNER, "admin", 2).allowed, true);
});

test("the last owner cannot be demoted", () => {
  const result = canChangeRole(OWNER, OTHER_OWNER, "admin", 1);
  assert.equal(result.allowed, false);
  assert.match(result.allowed === false ? result.reason : "", /only owner/);
});

test("assigning a member the role they already hold is a no-op, not a refusal", () => {
  // The backend returns early on this before reaching its owner check.
  assert.equal(canChangeRole(OWNER, OTHER_OWNER, "owner", 1).allowed, true);
});

test("the offered roles are exactly the permitted ones", () => {
  assert.deepEqual(assignableRoles(ADMIN, MEMBER, 1), ["admin", "member"]);
  assert.deepEqual(assignableRoles(OWNER, MEMBER, 1), ["owner", "admin", "member"]);
  // Nothing to offer: an admin cannot touch an owner at all.
  assert.deepEqual(assignableRoles(ADMIN, OTHER_OWNER, 2), []);
});

// -- Removing a member --------------------------------------------------------

test("a plain member may remove themselves", () => {
  // Leaving is not an administrative act.
  assert.equal(canRemoveMember(MEMBER, { userId: MEMBER.userId, role: "member" }, 1).allowed, true);
});

test("a plain member may not remove anyone else", () => {
  assert.equal(canRemoveMember(MEMBER, ADMIN, 1).allowed, false);
});

test("an admin may remove a member", () => {
  assert.equal(canRemoveMember(ADMIN, MEMBER, 1).allowed, true);
});

test("an admin may not remove an owner", () => {
  const result = canRemoveMember(ADMIN, OTHER_OWNER, 2);
  assert.equal(result.allowed, false);
  assert.match(result.allowed === false ? result.reason : "", /Only an owner/);
});

test("an owner may remove another owner while a second remains", () => {
  assert.equal(canRemoveMember(OWNER, OTHER_OWNER, 2).allowed, true);
});

test("the last owner cannot be removed, not even by themselves", () => {
  const result = canRemoveMember(OWNER, { userId: OWNER.userId, role: "owner" }, 1);
  assert.equal(result.allowed, false);
  assert.match(result.allowed === false ? result.reason : "", /only owner/);
});
