/**
 * The authenticated caller: what it attaches, and what it does when the
 * backend refuses.
 */

import assert from "node:assert/strict";
import test from "node:test";

import { FAKE_TOKEN } from "../support/fixtures";
import { errorResponse, jsonResponse, stubFetch } from "../support/fetch-stub";
import { ApiError, createAuthenticatedApi } from "@/lib/api";

function caller(options: {
  token?: string | null;
  organizationId?: string | null;
  onUnauthorized?: () => void;
}) {
  return createAuthenticatedApi({
    getToken: () => options.token ?? null,
    getOrganizationId: () => options.organizationId ?? null,
    onUnauthorized: options.onUnauthorized,
  });
}

test("the access token is attached as a bearer credential", async () => {
  const fetch = stubFetch(() => jsonResponse({}));
  try {
    await caller({ token: FAKE_TOKEN }).get("/api/v1/auth/me");
    assert.equal(fetch.last.headers.authorization, `Bearer ${FAKE_TOKEN}`);
  } finally {
    fetch.restore();
  }
});

test("no Authorization header is sent when there is no token", async () => {
  const fetch = stubFetch(() => jsonResponse({}));
  try {
    await caller({ token: null }).get("/api/v1/auth/me");
    assert.equal("authorization" in fetch.last.headers, false);
  } finally {
    fetch.restore();
  }
});

test("the acting organization travels in the X-Organization-ID header", async () => {
  const fetch = stubFetch(() => jsonResponse({}));
  try {
    await caller({ token: FAKE_TOKEN, organizationId: "org-1" }).get("/api/v1/organization");
    assert.equal(fetch.last.headers["x-organization-id"], "org-1");
  } finally {
    fetch.restore();
  }
});

test("no organization header is sent when the session is acting as none", async () => {
  const fetch = stubFetch(() => jsonResponse({}));
  try {
    await caller({ token: FAKE_TOKEN, organizationId: null }).get("/api/v1/organization");
    assert.equal("x-organization-id" in fetch.last.headers, false);
  } finally {
    fetch.restore();
  }
});

test("the token is read at call time, so a sign-out takes effect immediately", async () => {
  let token: string | null = FAKE_TOKEN;
  const api = createAuthenticatedApi({ getToken: () => token });

  const fetch = stubFetch(() => jsonResponse({}));
  try {
    await api.get("/api/v1/auth/me");
    assert.equal(fetch.last.headers.authorization, `Bearer ${FAKE_TOKEN}`);

    token = null;
    await api.get("/api/v1/auth/me");
    assert.equal("authorization" in fetch.last.headers, false);
  } finally {
    fetch.restore();
  }
});

test("a 401 notifies the session that its credentials are no longer accepted", async () => {
  let notified = 0;
  const api = caller({ token: FAKE_TOKEN, onUnauthorized: () => (notified += 1) });

  const fetch = stubFetch(() =>
    errorResponse(401, "unauthorized", "Could not validate credentials."),
  );
  try {
    await assert.rejects(
      () => api.get("/api/v1/auth/me"),
      (error: unknown) => error instanceof ApiError && error.status === 401,
    );
    assert.equal(notified, 1);
  } finally {
    fetch.restore();
  }
});

test("a 403 leaves the session alone", async () => {
  // The token was accepted and the answer is still no. Signing the user out
  // for opening a page they may not see would be the wrong lesson to draw.
  let notified = 0;
  const api = caller({ token: FAKE_TOKEN, onUnauthorized: () => (notified += 1) });

  const fetch = stubFetch(() =>
    errorResponse(403, "permission_denied", "You do not have permission to manage members."),
  );
  try {
    await assert.rejects(
      () => api.patch("/api/v1/organization/members/abc", { role: "owner" }),
      (error: unknown) => error instanceof ApiError && error.status === 403,
    );
    assert.equal(notified, 0);
  } finally {
    fetch.restore();
  }
});

test("a 404 and a 409 also leave the session alone", async () => {
  let notified = 0;
  const api = caller({ token: FAKE_TOKEN, onUnauthorized: () => (notified += 1) });

  for (const [status, code] of [
    [404, "not_found"],
    [409, "conflict"],
  ] as const) {
    const fetch = stubFetch(() => errorResponse(status, code, "No."));
    try {
      await assert.rejects(() => api.delete("/api/v1/organization/members/abc"));
    } finally {
      fetch.restore();
    }
  }

  assert.equal(notified, 0);
});

test("a network failure does not end the session", async () => {
  // The backend never answered, so nothing was learned about the token.
  let notified = 0;
  const api = caller({ token: FAKE_TOKEN, onUnauthorized: () => (notified += 1) });

  const fetch = stubFetch(() => {
    throw new TypeError("fetch failed");
  });
  try {
    await assert.rejects(() => api.get("/api/v1/auth/me"));
    assert.equal(notified, 0);
  } finally {
    fetch.restore();
  }
});
