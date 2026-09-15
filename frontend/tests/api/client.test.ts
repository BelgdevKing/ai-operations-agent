/** The API client: URLs, the error envelope, and the two failure kinds. */

import assert from "node:assert/strict";
import test from "node:test";

import { errorResponse, jsonResponse, noContentResponse, stubFetch } from "../support/fetch-stub";
import { ApiError, NetworkError, api, request } from "@/lib/api";

const BASE = "http://localhost:8000";

test("a path is resolved against the configured API base URL", async () => {
  const fetch = stubFetch(() => jsonResponse({ ok: true }));
  try {
    await api.get("/health");
    assert.equal(fetch.last.url, `${BASE}/health`);
  } finally {
    fetch.restore();
  }
});

test("query parameters are appended, and null ones are dropped", async () => {
  const fetch = stubFetch(() => jsonResponse([]));
  try {
    await request("/api/v1/organization/members", {
      query: { limit: 200, offset: undefined, search: null },
    });

    const url = new URL(fetch.last.url);
    assert.equal(url.searchParams.get("limit"), "200");
    assert.equal(url.searchParams.has("offset"), false);
    assert.equal(url.searchParams.has("search"), false);
  } finally {
    fetch.restore();
  }
});

test("a body is sent as JSON with the matching content type", async () => {
  const fetch = stubFetch(() => jsonResponse({}));
  try {
    await api.post("/api/v1/auth/login", { email: "ada@example.com", password: "x" });

    assert.equal(fetch.last.method, "POST");
    assert.equal(fetch.last.headers["content-type"], "application/json");
    assert.deepEqual(fetch.last.body, { email: "ada@example.com", password: "x" });
  } finally {
    fetch.restore();
  }
});

test("credentials never appear in the URL", async () => {
  const fetch = stubFetch(() => jsonResponse({}));
  try {
    await api.post("/api/v1/auth/login", {
      email: "ada@example.com",
      password: "correct-horse-battery",
    });

    assert.equal(fetch.last.url.includes("correct-horse-battery"), false);
    assert.equal(fetch.last.url.includes("ada@example.com"), false);
  } finally {
    fetch.restore();
  }
});

test("the error envelope becomes an ApiError carrying code, message and details", async () => {
  const fetch = stubFetch(() =>
    errorResponse(403, "permission_denied", "You do not have permission to manage members.", {
      hint: "ask an owner",
    }),
  );
  try {
    await assert.rejects(
      () => api.get("/api/v1/organization/members"),
      (error: unknown) => {
        assert.ok(error instanceof ApiError);
        assert.equal(error.status, 403);
        assert.equal(error.code, "permission_denied");
        assert.equal(error.message, "You do not have permission to manage members.");
        assert.deepEqual(error.details, { hint: "ask an owner" });
        assert.equal(error.isForbidden, true);
        assert.equal(error.isUnauthorized, false);
        return true;
      },
    );
  } finally {
    fetch.restore();
  }
});

test("the request id is taken from the envelope body", async () => {
  // The header is unreadable cross-origin unless the backend exposes it, so
  // the body is the dependable source.
  const fetch = stubFetch(
    () =>
      new Response(
        JSON.stringify({
          error: { code: "not_found", message: "Gone.", details: {} },
          request_id: "from-the-body",
        }),
        { status: 404, headers: { "content-type": "application/json" } },
      ),
  );
  try {
    await assert.rejects(
      () => api.get("/api/v1/organization"),
      (error: unknown) => error instanceof ApiError && error.requestId === "from-the-body",
    );
  } finally {
    fetch.restore();
  }
});

test("a response that is not our envelope still produces a usable ApiError", async () => {
  const fetch = stubFetch(
    () => new Response("<html>502 Bad Gateway</html>", { status: 502, statusText: "Bad Gateway" }),
  );
  try {
    await assert.rejects(
      () => api.get("/health"),
      (error: unknown) => {
        assert.ok(error instanceof ApiError);
        assert.equal(error.status, 502);
        assert.equal(error.isServerError, true);
        return true;
      },
    );
  } finally {
    fetch.restore();
  }
});

test("a transport failure is a NetworkError, not an ApiError", async () => {
  const fetch = stubFetch(() => {
    throw new TypeError("fetch failed");
  });
  try {
    await assert.rejects(
      () => api.get("/health"),
      (error: unknown) => error instanceof NetworkError,
    );
  } finally {
    fetch.restore();
  }
});

test("a timeout is reported as a NetworkError", async () => {
  // A response that never arrives. Only the client's own deadline can end it.
  const fetch = stubFetch(() => new Promise<Response>(() => {}));
  try {
    await assert.rejects(
      () => request("/health", { timeoutMs: 20 }),
      (error: unknown) => error instanceof NetworkError && /timed out/.test(error.message),
    );
  } finally {
    fetch.restore();
  }
});

test("a caller's own cancellation is re-thrown rather than turned into an error state", async () => {
  const controller = new AbortController();
  controller.abort();

  const fetch = stubFetch(() => jsonResponse({}));
  try {
    await assert.rejects(
      () => request("/health", { signal: controller.signal }),
      (error: unknown) => error instanceof DOMException && error.name === "AbortError",
    );
    assert.equal(fetch.calls.length, 0, "an already-cancelled request is never sent");
  } finally {
    fetch.restore();
  }
});

test("204 resolves without a body", async () => {
  const fetch = stubFetch(() => noContentResponse());
  try {
    assert.equal(await api.delete("/api/v1/organization/members/abc"), undefined);
  } finally {
    fetch.restore();
  }
});

test("a status listed in allowStatus is parsed rather than thrown", async () => {
  // Readiness answers 503 with its report when a dependency is down.
  const fetch = stubFetch(() =>
    jsonResponse({ status: "degraded", dependencies: {} }, 503),
  );
  try {
    const body = await request<{ status: string }>("/health/ready", { allowStatus: [503] });
    assert.equal(body.status, "degraded");
  } finally {
    fetch.restore();
  }
});
