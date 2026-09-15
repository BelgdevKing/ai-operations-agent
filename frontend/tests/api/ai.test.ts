/**
 * The AI endpoint as the workspace uses it: the request it sends, the answer
 * it records, and every refusal it has to explain.
 */

import assert from "node:assert/strict";
import test from "node:test";

import { acme, FAKE_TOKEN, generateResponse, tokenResponse, currentUser } from "../support/fixtures";
import { errorResponse, jsonResponse, stubFetch } from "../support/fetch-stub";
import {
  ApiError,
  createAuthenticatedApi,
  describeError,
  generate,
  isAbortError,
} from "@/lib/api";
import { SessionStore } from "@/lib/auth/session-store";
import {
  EMPTY_CONVERSATION,
  conversationReducer as reduce,
  toGenerateRequest,
} from "@/lib/ai/conversation";

const caller = createAuthenticatedApi({
  getToken: () => FAKE_TOKEN,
  getOrganizationId: () => acme.id,
});

const ASK = { messages: [{ role: "user" as const, content: "What is a refund policy?" }] };

// -- The request --------------------------------------------------------------

test("generation posts to the platform's own AI endpoint", async () => {
  const fetch = stubFetch(() => jsonResponse(generateResponse("An answer.")));
  try {
    await generate(caller, ASK);

    assert.equal(fetch.last.method, "POST");
    assert.equal(new URL(fetch.last.url).pathname, "/api/v1/ai/generate");
  } finally {
    fetch.restore();
  }
});

test("the request is authenticated and names the acting organization", async () => {
  const fetch = stubFetch(() => jsonResponse(generateResponse("An answer.")));
  try {
    await generate(caller, ASK);

    assert.equal(fetch.last.headers.authorization, `Bearer ${FAKE_TOKEN}`);
    assert.equal(fetch.last.headers["x-organization-id"], acme.id);
  } finally {
    fetch.restore();
  }
});

test("an unauthenticated caller sends no credentials", async () => {
  // The route is guarded, but the client must not invent a token either.
  const anonymous = createAuthenticatedApi({ getToken: () => null });
  const fetch = stubFetch(() => errorResponse(401, "unauthorized", "Not authenticated."));
  try {
    await assert.rejects(() => generate(anonymous, ASK));
    assert.equal("authorization" in fetch.last.headers, false);
  } finally {
    fetch.restore();
  }
});

test("the body carries only the conversation", async () => {
  const fetch = stubFetch(() => jsonResponse(generateResponse("An answer.")));
  try {
    await generate(caller, ASK);
    assert.deepEqual(fetch.last.body, ASK);
  } finally {
    fetch.restore();
  }
});

test("nothing in the request names a provider, model or organization", async () => {
  const fetch = stubFetch(() => jsonResponse(generateResponse("An answer.")));
  try {
    await generate(caller, ASK);

    const sent = JSON.stringify(fetch.last.body);
    for (const forbidden of ["anthropic", "openai", "provider", "model", "organization", "api_key"]) {
      assert.equal(sent.includes(forbidden), false, `"${forbidden}" must not be in the body`);
    }
  } finally {
    fetch.restore();
  }
});

// -- A whole exchange ---------------------------------------------------------

test("a message goes out and the answer lands in the conversation", async () => {
  const fetch = stubFetch(() => jsonResponse(generateResponse("Refunds take five days.")));
  try {
    const asked = reduce(EMPTY_CONVERSATION, {
      type: "send",
      id: "u1",
      content: "How long do refunds take?",
    });
    assert.equal(asked.status, "generating");

    const response = await generate(caller, toGenerateRequest(asked.turns));
    const answered = reduce(asked, { type: "received", id: "a1", response });

    assert.deepEqual(fetch.last.body, {
      messages: [{ role: "user", content: "How long do refunds take?" }],
    });
    assert.equal(answered.turns.length, 2);
    assert.equal(answered.turns[1]!.content, "Refunds take five days.");
    assert.equal(answered.status, "idle");
  } finally {
    fetch.restore();
  }
});

test("a follow-up sends the whole conversation, because the endpoint is stateless", async () => {
  const fetch = stubFetch(() => jsonResponse(generateResponse("Second answer.")));
  try {
    let state = reduce(EMPTY_CONVERSATION, { type: "send", id: "u1", content: "First" });
    state = reduce(state, { type: "received", id: "a1", response: generateResponse("One.") });
    state = reduce(state, { type: "send", id: "u2", content: "Second" });

    await generate(caller, toGenerateRequest(state.turns));

    assert.deepEqual(fetch.last.body, {
      messages: [
        { role: "user", content: "First" },
        { role: "assistant", content: "One." },
        { role: "user", content: "Second" },
      ],
    });
  } finally {
    fetch.restore();
  }
});

// -- Refusals -----------------------------------------------------------------

test("403 is an access problem, in the backend's words", async () => {
  const fetch = stubFetch(() =>
    errorResponse(403, "permission_denied", "You do not have access to an active organization."),
  );
  try {
    await assert.rejects(
      () => generate(caller, ASK),
      (error: unknown) => {
        const { title, message } = describeError(error);
        assert.equal(title, "Access denied");
        assert.match(message, /active organization/);
        return true;
      },
    );
  } finally {
    fetch.restore();
  }
});

test("422 explains that the request was not acceptable", async () => {
  const fetch = stubFetch(() =>
    errorResponse(422, "validation_error", "The request payload is invalid.", {
      errors: [{ field: "body.messages", message: "List should have at most 50 items" }],
    }),
  );
  try {
    await assert.rejects(
      () => generate(caller, ASK),
      (error: unknown) => {
        const { title, fieldErrors } = describeError(error);
        assert.match(title, /Check the details/);
        assert.equal(fieldErrors.messages, "List should have at most 50 items");
        return true;
      },
    );
  } finally {
    fetch.restore();
  }
});

test("422 also covers a model the deployment does not allow", async () => {
  // The workspace never names a model, so this is the backend refusing
  // something the browser could not have asked for - and it still reads safely.
  const fetch = stubFetch(() =>
    errorResponse(422, "validation_error", "Model 'gpt-5.5' is not available."),
  );
  try {
    await assert.rejects(
      () => generate(caller, ASK),
      (error: unknown) => error instanceof ApiError && error.status === 422,
    );
  } finally {
    fetch.restore();
  }
});

test("429 tells the user to wait", async () => {
  const fetch = stubFetch(() =>
    errorResponse(429, "llm_rate_limited", "The language model provider is rate limiting."),
  );
  try {
    await assert.rejects(
      () => generate(caller, ASK),
      (error: unknown) => {
        assert.match(describeError(error).title, /Too many requests/);
        return true;
      },
    );
  } finally {
    fetch.restore();
  }
});

test("502 is reported as a provider failure worth retrying", async () => {
  const fetch = stubFetch(() =>
    errorResponse(502, "llm_provider_error", "The language model provider returned an error."),
  );
  try {
    await assert.rejects(
      () => generate(caller, ASK),
      (error: unknown) => {
        const { title, message } = describeError(error);
        assert.match(title, /could not answer/);
        assert.match(message, /Trying again/);
        return true;
      },
    );
  } finally {
    fetch.restore();
  }
});

test("504 suggests trying again or asking for less", async () => {
  const fetch = stubFetch(() =>
    errorResponse(504, "llm_timeout", "The language model provider did not respond in time."),
  );
  try {
    await assert.rejects(
      () => generate(caller, ASK),
      (error: unknown) => {
        const { title, message } = describeError(error);
        assert.match(title, /took too long/);
        assert.match(message, /shorter message/);
        return true;
      },
    );
  } finally {
    fetch.restore();
  }
});

test("a 500 never shows the server's own explanation", async () => {
  // A misconfiguration message is the one thing a 5xx must not put on screen.
  const fetch = stubFetch(() =>
    errorResponse(
      500,
      "llm_configuration_error",
      "LLM_PROVIDER is 'openai' but OPENAI_API_KEY is not set.",
    ),
  );
  try {
    await assert.rejects(
      () => generate(caller, ASK),
      (error: unknown) => {
        const { message, requestId } = describeError(error);
        assert.equal(message.includes("OPENAI_API_KEY"), false);
        assert.equal(message.includes("LLM_PROVIDER"), false);
        assert.equal(requestId, "test-request-id", "but the correlation id is kept");
        return true;
      },
    );
  } finally {
    fetch.restore();
  }
});

test("a network failure is a connection problem, not a model problem", async () => {
  const fetch = stubFetch(() => {
    throw new TypeError("fetch failed");
  });
  try {
    await assert.rejects(
      () => generate(caller, ASK),
      (error: unknown) => {
        assert.match(describeError(error).title, /Cannot reach the server/);
        return true;
      },
    );
  } finally {
    fetch.restore();
  }
});

test("every refusal offers a request id to quote", async () => {
  for (const [status, code] of [
    [403, "permission_denied"],
    [429, "llm_rate_limited"],
    [502, "llm_provider_error"],
    [504, "llm_timeout"],
  ] as const) {
    const fetch = stubFetch(() => errorResponse(status, code, "No."));
    try {
      await assert.rejects(
        () => generate(caller, ASK),
        (error: unknown) => {
          assert.equal(describeError(error).requestId, "test-request-id");
          return true;
        },
      );
    } finally {
      fetch.restore();
    }
  }
});

// -- 401 goes through the session, not the workspace --------------------------

test("a 401 during generation ends the session", async () => {
  const signedIn = stubFetch((request) => {
    if (request.url.endsWith("/auth/login")) return jsonResponse(tokenResponse);
    if (request.url.endsWith("/auth/me")) return jsonResponse(currentUser());
    return jsonResponse({});
  });
  const store = new SessionStore();
  try {
    await store.signIn({ email: "ada@example.com", password: "correct-horse-battery" });
  } finally {
    signedIn.restore();
  }

  const expired = stubFetch(() => errorResponse(401, "unauthorized", "Could not validate credentials."));
  try {
    await assert.rejects(() => generate(store.api, ASK), ApiError);

    const state = store.getSnapshot();
    assert.equal(state.status, "anonymous");
    assert.equal(state.status === "anonymous" && state.reason, "expired");
  } finally {
    expired.restore();
  }
});

// -- Cancellation -------------------------------------------------------------

test("cancelling in flight raises an abort, not an error to display", async () => {
  const controller = new AbortController();
  const fetch = stubFetch(() => new Promise<Response>(() => {}));
  try {
    const pending = generate(caller, ASK, { signal: controller.signal });
    controller.abort();

    await assert.rejects(
      () => pending,
      (error: unknown) => {
        // The workspace branches on exactly this to avoid an error banner.
        assert.equal(isAbortError(error), true);
        assert.equal(error instanceof ApiError, false);
        return true;
      },
    );
  } finally {
    fetch.restore();
  }
});

test("a cancelled generation leaves the conversation intact and retryable", async () => {
  const controller = new AbortController();
  const fetch = stubFetch(() => new Promise<Response>(() => {}));
  try {
    let state = reduce(EMPTY_CONVERSATION, { type: "send", id: "u1", content: "Hello" });

    const pending = generate(caller, toGenerateRequest(state.turns), {
      signal: controller.signal,
    });
    controller.abort();

    try {
      await pending;
    } catch (error) {
      state = isAbortError(error)
        ? reduce(state, { type: "cancelled" })
        : reduce(state, { type: "failed", error });
    }

    assert.equal(state.status, "idle");
    assert.equal(state.error, null);
    assert.equal(state.turns.length, 1, "no fabricated assistant turn");
  } finally {
    fetch.restore();
  }
});
