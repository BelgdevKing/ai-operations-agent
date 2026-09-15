/** The session: signing in, staying in, and being put out. */

import assert from "node:assert/strict";
import test from "node:test";

import { acme, currentUser, FAKE_TOKEN, globex, tokenResponse, user } from "../support/fixtures";
import { errorResponse, jsonResponse, stubFetch } from "../support/fetch-stub";
import { ApiError } from "@/lib/api";
import { SessionStore } from "@/lib/auth/session-store";

const CREDENTIALS = { email: "ada@example.com", password: "correct-horse-battery" };

/** Answers login and /auth/me the way the backend does. */
function backend(me = currentUser()) {
  return stubFetch((request) => {
    if (request.url.endsWith("/auth/login")) return jsonResponse(tokenResponse);
    if (request.url.endsWith("/auth/me")) return jsonResponse(me);
    return jsonResponse({});
  });
}

test("a session starts anonymous", () => {
  const store = new SessionStore();
  assert.equal(store.getSnapshot().status, "anonymous");
});

test("the server snapshot is always anonymous, so hydration matches", () => {
  const store = new SessionStore();
  assert.equal(store.getServerSnapshot().status, "anonymous");
});

test("signing in exchanges credentials and then loads the real user", async () => {
  const fetch = backend();
  try {
    const store = new SessionStore();
    await store.signIn(CREDENTIALS);

    const state = store.getSnapshot();
    assert.equal(state.status, "authenticated");
    assert.equal(state.status === "authenticated" && state.user.email, user.email);

    // Two calls, in order: the backend's login returns a token and nothing else.
    assert.equal(fetch.calls.length, 2);
    assert.match(fetch.calls[0]!.url, /\/auth\/login$/);
    assert.match(fetch.calls[1]!.url, /\/auth\/me$/);
  } finally {
    fetch.restore();
  }
});

test("the token from sign-in is used on subsequent requests", async () => {
  const fetch = backend();
  try {
    const store = new SessionStore();
    await store.signIn(CREDENTIALS);

    assert.equal(fetch.calls[1]!.headers.authorization, `Bearer ${FAKE_TOKEN}`);
  } finally {
    fetch.restore();
  }
});

test("the token is never exposed on the session state", async () => {
  const fetch = backend();
  try {
    const store = new SessionStore();
    await store.signIn(CREDENTIALS);

    // Whatever a component renders, it cannot render the token from here.
    const serialised = JSON.stringify(store.getSnapshot());
    assert.equal(serialised.includes(FAKE_TOKEN), false);
    assert.equal(Object.keys(store).includes("token"), false);
  } finally {
    fetch.restore();
  }
});

test("wrong credentials leave the session anonymous and re-throw for the form", async () => {
  const fetch = stubFetch(() => errorResponse(401, "unauthorized", "Incorrect email or password."));
  try {
    const store = new SessionStore();

    await assert.rejects(
      () => store.signIn(CREDENTIALS),
      (error: unknown) => error instanceof ApiError && error.status === 401,
    );
    assert.equal(store.getSnapshot().status, "anonymous");
  } finally {
    fetch.restore();
  }
});

test("a failed sign-in is not reported as an expired session", async () => {
  // Otherwise the sign-in page would tell a first-time visitor their session
  // had ended.
  const fetch = stubFetch(() => errorResponse(401, "unauthorized", "Incorrect email or password."));
  try {
    const store = new SessionStore();
    await assert.rejects(() => store.signIn(CREDENTIALS));

    const state = store.getSnapshot();
    assert.equal(state.status === "anonymous" && state.reason, null);
  } finally {
    fetch.restore();
  }
});

test("a network failure during sign-in leaves no half-open session", async () => {
  const fetch = stubFetch(() => {
    throw new TypeError("fetch failed");
  });
  try {
    const store = new SessionStore();
    await assert.rejects(() => store.signIn(CREDENTIALS));
    assert.equal(store.getSnapshot().status, "anonymous");
  } finally {
    fetch.restore();
  }
});

test("a 401 on any authenticated request ends the session", async () => {
  const fetch = backend();
  const store = new SessionStore();
  try {
    await store.signIn(CREDENTIALS);
    assert.equal(store.getSnapshot().status, "authenticated");
  } finally {
    fetch.restore();
  }

  const expired = stubFetch(() =>
    errorResponse(401, "unauthorized", "Could not validate credentials."),
  );
  try {
    await assert.rejects(() => store.api.get("/api/v1/organization"));

    const state = store.getSnapshot();
    assert.equal(state.status, "anonymous");
    assert.equal(state.status === "anonymous" && state.reason, "expired");
  } finally {
    expired.restore();
  }
});

test("after a 401 the token is gone, so nothing else goes out authenticated", async () => {
  const fetch = backend();
  const store = new SessionStore();
  try {
    await store.signIn(CREDENTIALS);
  } finally {
    fetch.restore();
  }

  const expired = stubFetch(() => errorResponse(401, "unauthorized", "No."));
  try {
    await assert.rejects(() => store.api.get("/api/v1/organization"));
    await assert.rejects(() => store.api.get("/api/v1/organization/members"));

    assert.equal("authorization" in expired.last.headers, false);
  } finally {
    expired.restore();
  }
});

test("a 403 does not end the session", async () => {
  const fetch = backend();
  const store = new SessionStore();
  try {
    await store.signIn(CREDENTIALS);
  } finally {
    fetch.restore();
  }

  const denied = stubFetch(() => errorResponse(403, "permission_denied", "No."));
  try {
    await assert.rejects(() => store.api.patch("/api/v1/organization/members/x", { role: "owner" }));
    assert.equal(store.getSnapshot().status, "authenticated");
  } finally {
    denied.restore();
  }
});

test("signing out clears the session and the token", async () => {
  const fetch = backend();
  const store = new SessionStore();
  try {
    await store.signIn(CREDENTIALS);
    store.signOut();

    const state = store.getSnapshot();
    assert.equal(state.status, "anonymous");
    assert.equal(state.status === "anonymous" && state.reason, "signed-out");
  } finally {
    fetch.restore();
  }

  const after = stubFetch(() => jsonResponse({}));
  try {
    await store.api.get("/api/v1/auth/me");
    assert.equal("authorization" in after.last.headers, false);
  } finally {
    after.restore();
  }
});

test("subscribers are notified when the session changes", async () => {
  const fetch = backend();
  try {
    const store = new SessionStore();
    let notifications = 0;
    const unsubscribe = store.subscribe(() => (notifications += 1));

    await store.signIn(CREDENTIALS);
    assert.ok(notifications >= 2, "authenticating, then authenticated");

    unsubscribe();
    const before = notifications;
    store.signOut();
    assert.equal(notifications, before, "an unsubscribed listener hears nothing");
  } finally {
    fetch.restore();
  }
});

test("the snapshot is stable between changes", async () => {
  const fetch = backend();
  try {
    const store = new SessionStore();
    await store.signIn(CREDENTIALS);

    // useSyncExternalStore re-renders forever if this is not true.
    assert.equal(store.getSnapshot(), store.getSnapshot());
  } finally {
    fetch.restore();
  }
});

// -- Organization context -----------------------------------------------------

test("the only membership becomes the acting organization", async () => {
  const fetch = backend();
  try {
    const store = new SessionStore();
    await store.signIn(CREDENTIALS);

    assert.equal(store.activeOrganizationId, acme.id);
  } finally {
    fetch.restore();
  }
});

test("with several memberships the first is used until one is chosen", async () => {
  const fetch = backend(
    currentUser([
      { organization: acme, role: "owner" },
      { organization: globex, role: "member" },
    ]),
  );
  try {
    const store = new SessionStore();
    await store.signIn(CREDENTIALS);
    assert.equal(store.activeOrganizationId, acme.id);

    store.selectOrganization(globex.id);
    assert.equal(store.activeOrganizationId, globex.id);
  } finally {
    fetch.restore();
  }
});

test("the chosen organization is sent on every authenticated request", async () => {
  const fetch = backend(
    currentUser([
      { organization: acme, role: "owner" },
      { organization: globex, role: "member" },
    ]),
  );
  try {
    const store = new SessionStore();
    await store.signIn(CREDENTIALS);
    store.selectOrganization(globex.id);

    await store.api.get("/api/v1/organization");
    assert.equal(fetch.last.headers["x-organization-id"], globex.id);
  } finally {
    fetch.restore();
  }
});

test("an organization the user does not belong to is never selected", async () => {
  // A guard against a UI bug, not a security control - the backend checks the
  // header against membership on every request regardless.
  const fetch = backend();
  try {
    const store = new SessionStore();
    await store.signIn(CREDENTIALS);

    store.selectOrganization("99999999-9999-4999-8999-999999999999");
    assert.equal(store.activeOrganizationId, acme.id);
  } finally {
    fetch.restore();
  }
});

test("a user with no active membership is signed in with no organization", async () => {
  const fetch = backend(currentUser([]));
  try {
    const store = new SessionStore();
    await store.signIn(CREDENTIALS);

    const state = store.getSnapshot();
    assert.equal(state.status, "authenticated");
    assert.equal(state.status === "authenticated" && state.active, null);
    assert.equal(store.activeOrganizationId, null);
  } finally {
    fetch.restore();
  }
});

test("refreshing the user keeps the organization it was acting as", async () => {
  const memberships = [
    { organization: acme, role: "owner" as const },
    { organization: globex, role: "member" as const },
  ];
  const fetch = backend(currentUser(memberships));
  try {
    const store = new SessionStore();
    await store.signIn(CREDENTIALS);
    store.selectOrganization(globex.id);

    await store.refreshUser();
    assert.equal(store.activeOrganizationId, globex.id);
  } finally {
    fetch.restore();
  }
});

test("refreshing falls back when the organization is no longer one of the user's", async () => {
  // What happens after leaving an organization: the session cannot keep acting
  // as one the backend no longer reports.
  let me = currentUser([
    { organization: acme, role: "owner" },
    { organization: globex, role: "member" },
  ]);

  const fetch = stubFetch((request) => {
    if (request.url.endsWith("/auth/login")) return jsonResponse(tokenResponse);
    if (request.url.endsWith("/auth/me")) return jsonResponse(me);
    return jsonResponse({});
  });
  try {
    const store = new SessionStore();
    await store.signIn(CREDENTIALS);
    store.selectOrganization(globex.id);

    me = currentUser([{ organization: acme, role: "owner" }]);
    await store.refreshUser();

    assert.equal(store.activeOrganizationId, acme.id);
  } finally {
    fetch.restore();
  }
});

test("registration's token starts a session without a second password prompt", async () => {
  const fetch = backend();
  try {
    const store = new SessionStore();
    await store.adoptToken(tokenResponse);

    assert.equal(store.getSnapshot().status, "authenticated");
    // Only /auth/me: registration already issued the token.
    assert.equal(fetch.calls.length, 1);
    assert.match(fetch.last.url, /\/auth\/me$/);
  } finally {
    fetch.restore();
  }
});
