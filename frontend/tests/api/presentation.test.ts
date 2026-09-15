/** Turning failures into words, and keeping the server's private ones private. */

import assert from "node:assert/strict";
import test from "node:test";

import { ApiError, NetworkError, describeError, describeSignInError, fieldErrorsOf } from "@/lib/api";

function apiError(status: number, code: string, message: string, details = {}) {
  return new ApiError({ status, code, message, details, requestId: "req-1" });
}

test("a network failure is named as one, without inventing a server reason", () => {
  const { title, message } = describeError(new NetworkError("boom"));
  assert.match(title, /Cannot reach the server/);
  assert.match(message, /backend is running/);
});

test("401 says the session ended", () => {
  const { title } = describeError(apiError(401, "unauthorized", "Not authenticated."));
  assert.match(title, /session has ended/);
});

test("403 shows the backend's own explanation", () => {
  const { title, message } = describeError(
    apiError(403, "permission_denied", "You do not have permission to manage members."),
  );
  assert.equal(title, "Access denied");
  assert.equal(message, "You do not have permission to manage members.");
});

test("404 and 409 keep the backend's message", () => {
  assert.equal(
    describeError(apiError(404, "not_found", "That user is not a member of this organization."))
      .message,
    "That user is not a member of this organization.",
  );
  assert.match(
    describeError(apiError(409, "conflict", "This is the organization's only owner.")).message,
    /only owner/,
  );
});

test("429 says to wait", () => {
  assert.match(describeError(apiError(429, "rate_limited", "Slow down.")).title, /Too many/);
});

test("a 5xx message from the server is never shown", () => {
  // The backend already replaces 5xx detail with a generic line. This refuses
  // to echo whatever arrived, so a misconfigured deployment cannot leak one.
  const leaky = apiError(500, "internal_error", "LLM_PROVIDER is 'openai' but OPENAI_API_KEY is not set.");
  const { message, requestId } = describeError(leaky);

  assert.equal(message.includes("OPENAI_API_KEY"), false);
  assert.equal(message.includes("LLM_PROVIDER"), false);
  assert.equal(requestId, "req-1", "the request id is what ties this to the server log");
});

test("the request id is carried through for quoting in a report", () => {
  assert.equal(describeError(apiError(403, "permission_denied", "No.")).requestId, "req-1");
});

test("an unknown throwable gets a safe generic message", () => {
  const { message } = describeError(new Error("internal detail nobody should read"));
  assert.equal(message.includes("internal detail"), false);
});

// -- Validation ---------------------------------------------------------------

test("422 details become per-field messages, with the body prefix removed", () => {
  const error = apiError(422, "validation_error", "The request payload is invalid.", {
    errors: [
      { field: "body.password", message: "String should have at least 12 characters" },
      { field: "body.email", message: "value is not a valid email address" },
    ],
  });

  assert.deepEqual(fieldErrorsOf(error), {
    password: "String should have at least 12 characters",
    email: "value is not a valid email address",
  });
});

test("the first message for a field wins", () => {
  const error = apiError(422, "validation_error", "Invalid.", {
    errors: [
      { field: "body.password", message: "too short" },
      { field: "body.password", message: "also bad" },
    ],
  });
  assert.deepEqual(fieldErrorsOf(error), { password: "too short" });
});

test("malformed validation details are ignored rather than crashing a form", () => {
  assert.deepEqual(fieldErrorsOf(apiError(422, "validation_error", "Invalid.", { errors: "nope" })), {});
  assert.deepEqual(fieldErrorsOf(apiError(422, "validation_error", "Invalid.", {})), {});
  assert.deepEqual(fieldErrorsOf(new NetworkError("boom")), {});
});

// -- Sign-in is the exception -------------------------------------------------

test("a 401 while signing in means wrong credentials, not an expired session", () => {
  const { title, message } = describeSignInError(
    apiError(401, "unauthorized", "Incorrect email or password."),
  );
  assert.equal(title, "Sign-in failed");
  assert.equal(message, "Incorrect email or password.");
});

test("other statuses read the same on the sign-in form as anywhere else", () => {
  const conflict = apiError(409, "conflict", "Email already registered.");
  assert.deepEqual(describeSignInError(conflict), describeError(conflict));
});

// -- A dependency outage reads as temporary -----------------------------------

test("503 says the outage is temporary rather than that the app is broken", () => {
  // Reachable for any endpoint since Part 22: the backend answers
  // `service_unavailable` when it cannot reach PostgreSQL, instead of letting
  // an outage arrive as the same 500 a null dereference produces.
  const { title, message, tone } = describeError(
    apiError(503, "service_unavailable", "A required dependency is unavailable."),
  );

  assert.match(title, /temporarily unavailable/);
  assert.match(message, /try again shortly/i);
  // Warn, not danger: nothing the person did is wrong and nothing is lost.
  assert.equal(tone, "warn");
});

test("503 does not read like the generic server failure", () => {
  const dependency = describeError(apiError(503, "service_unavailable", "A required dependency is unavailable."));
  const bug = describeError(apiError(500, "internal_error", "An unexpected error occurred."));

  assert.notEqual(dependency.title, bug.title);
  assert.notEqual(dependency.tone, bug.tone);
});

test("no 5xx echoes the server's own message", () => {
  // The rule the whole 5xx branch exists for: server-side detail stays in the
  // server log, and the request id is what ties this screen to it.
  const secret = "psycopg: FATAL password authentication failed for user aiops";

  for (const status of [500, 502, 503, 504]) {
    const shown = describeError(apiError(status, "some_code", secret));
    assert.ok(!shown.message.includes(secret), `${status} echoed the server message`);
    assert.equal(shown.requestId, "req-1");
  }
});
