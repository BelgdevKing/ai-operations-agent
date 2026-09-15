/**
 * The organization and member calls: the right method, the right path, and no
 * organization id anywhere a client could choose it.
 */

import assert from "node:assert/strict";
import test from "node:test";

import { acme, FAKE_TOKEN, member } from "../support/fixtures";
import { errorResponse, jsonResponse, noContentResponse, stubFetch } from "../support/fetch-stub";
import {
  ApiError,
  changeMemberRole,
  createAuthenticatedApi,
  getOrganization,
  listMembers,
  removeMember,
} from "@/lib/api";

const caller = createAuthenticatedApi({
  getToken: () => FAKE_TOKEN,
  getOrganizationId: () => acme.id,
});

test("the organization is fetched without an id in the path", () => {
  // Which organization this returns is the backend's decision, taken from the
  // caller's verified membership. There is nothing here to tamper with.
  const fetch = stubFetch(() => jsonResponse({ ...acme, created_at: "2026-01-01T00:00:00Z" }));
  return getOrganization(caller)
    .then(() => {
      const url = new URL(fetch.last.url);
      assert.equal(url.pathname, "/api/v1/organization");
      assert.equal(url.search, "");
    })
    .finally(() => fetch.restore());
});

test("members are listed with an explicit page size", async () => {
  const fetch = stubFetch(() => jsonResponse([member({ userId: "a", role: "owner" })]));
  try {
    const members = await listMembers(caller, { limit: 200 });

    const url = new URL(fetch.last.url);
    assert.equal(url.pathname, "/api/v1/organization/members");
    assert.equal(url.searchParams.get("limit"), "200");
    assert.equal(members.length, 1);
  } finally {
    fetch.restore();
  }
});

test("a role change is a PATCH carrying only the new role", async () => {
  const fetch = stubFetch(() => jsonResponse(member({ userId: "u-1", role: "admin" })));
  try {
    await changeMemberRole(caller, "u-1", "admin");

    assert.equal(fetch.last.method, "PATCH");
    assert.equal(new URL(fetch.last.url).pathname, "/api/v1/organization/members/u-1");
    assert.deepEqual(fetch.last.body, { role: "admin" });
  } finally {
    fetch.restore();
  }
});

test("a user id is escaped into the path", async () => {
  const fetch = stubFetch(() => noContentResponse());
  try {
    await removeMember(caller, "a/../b");
    assert.equal(new URL(fetch.last.url).pathname.endsWith("a%2F..%2Fb"), true);
  } finally {
    fetch.restore();
  }
});

test("removal is a DELETE and expects no body back", async () => {
  const fetch = stubFetch(() => noContentResponse());
  try {
    assert.equal(await removeMember(caller, "u-1"), undefined);
    assert.equal(fetch.last.method, "DELETE");
  } finally {
    fetch.restore();
  }
});

test("every organization call carries the acting tenant header", async () => {
  const fetch = stubFetch(() => jsonResponse([]));
  try {
    await listMembers(caller);
    assert.equal(fetch.last.headers["x-organization-id"], acme.id);
  } finally {
    fetch.restore();
  }
});

test("the backend's refusals arrive intact for the UI to present", async () => {
  const cases = [
    [403, "permission_denied", "You do not have permission to manage members."],
    [404, "not_found", "That user is not a member of this organization."],
    [409, "conflict", "This is the organization's only owner. Promote another owner first."],
  ] as const;

  for (const [status, code, message] of cases) {
    const fetch = stubFetch(() => errorResponse(status, code, message));
    try {
      await assert.rejects(
        () => changeMemberRole(caller, "u-1", "member"),
        (error: unknown) => {
          assert.ok(error instanceof ApiError);
          assert.equal(error.status, status);
          assert.equal(error.code, code);
          assert.equal(error.message, message);
          return true;
        },
      );
    } finally {
      fetch.restore();
    }
  }
});

test("a failed load surfaces as an error rather than an empty list", async () => {
  // Rendering "no members" for a request that failed would be inventing data.
  const fetch = stubFetch(() => errorResponse(500, "internal_error", "Server error."));
  try {
    await assert.rejects(() => listMembers(caller), ApiError);
  } finally {
    fetch.restore();
  }
});
