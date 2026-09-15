/**
 * The usage endpoint as the dashboard uses it.
 *
 * The recurring assertion is about money: the backend sends an exact decimal as
 * a **string**, and nothing in this client may turn it into a number. A
 * JavaScript number is an IEEE double, and the whole point of carrying a
 * `Decimal` from the price book is thrown away by one `parseFloat`.
 *
 * The other half is what the response cannot contain - no prompt, no answer, no
 * tool argument, no tool result, no idempotency key - and that nothing in the
 * request can ask about another organization's usage.
 */

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import { acme, FAKE_TOKEN, usageResponse } from "../support/fixtures";
import { errorResponse, jsonResponse, stubFetch } from "../support/fetch-stub";
import {
  ApiError,
  createAuthenticatedApi,
  describeError,
  getUsage,
} from "@/lib/api";
import { isPriced } from "@/types/ai";

const caller = createAuthenticatedApi({
  getToken: () => FAKE_TOKEN,
  getOrganizationId: () => acme.id,
});

// -- The request --------------------------------------------------------------

test("usage is read from the AI surface", async () => {
  const fetch = stubFetch(() => jsonResponse(usageResponse()));
  try {
    await getUsage(caller);

    assert.equal(fetch.last.method, "GET");
    assert.equal(new URL(fetch.last.url).pathname, "/api/v1/ai/usage");
  } finally {
    fetch.restore();
  }
});

test("the request is authenticated and names the acting organization", async () => {
  const fetch = stubFetch(() => jsonResponse(usageResponse()));
  try {
    await getUsage(caller);

    assert.equal(fetch.last.headers.authorization, `Bearer ${FAKE_TOKEN}`);
    assert.equal(fetch.last.headers["x-organization-id"], acme.id);
  } finally {
    fetch.restore();
  }
});

test("nothing in the request can ask about another organization", async () => {
  // Structural: there is no parameter for it. The backend takes the tenant
  // from the verified membership, and this client has no way to override it.
  const fetch = stubFetch(() => jsonResponse(usageResponse()));
  try {
    await getUsage(caller, { since: "2026-09-01T00:00:00Z" });

    const query = new URL(fetch.last.url).searchParams;
    for (const forbidden of ["organization_id", "organization", "tenant", "user_id"]) {
      assert.equal(query.has(forbidden), false, `"${forbidden}" must not be a parameter`);
    }
  } finally {
    fetch.restore();
  }
});

test("a window is sent as given", async () => {
  const fetch = stubFetch(() => jsonResponse(usageResponse()));
  try {
    await getUsage(caller, { since: "2026-09-01T00:00:00Z", until: "2026-09-15T00:00:00Z" });

    const query = new URL(fetch.last.url).searchParams;
    assert.equal(query.get("since"), "2026-09-01T00:00:00Z");
    assert.equal(query.get("until"), "2026-09-15T00:00:00Z");
  } finally {
    fetch.restore();
  }
});

test("the per-day breakdown is opt-in", async () => {
  const fetch = stubFetch(() => jsonResponse(usageResponse()));
  try {
    await getUsage(caller);
    assert.equal(new URL(fetch.last.url).search, "", "no flags unless asked");

    await getUsage(caller, { includeDays: true });
    assert.equal(new URL(fetch.last.url).searchParams.get("include_days"), "true");
  } finally {
    fetch.restore();
  }
});

// -- What comes back ----------------------------------------------------------

test("a report carries counts and token totals", async () => {
  const fetch = stubFetch(() => jsonResponse(usageResponse()));
  try {
    const report = await getUsage(caller);

    assert.equal(report.llm_calls, 3);
    assert.equal(report.tokens.total_tokens, 180);
    assert.equal(report.agent_runs.total, 2);
    assert.equal(report.agent_runs.by_status.completed, 2);
  } finally {
    fetch.restore();
  }
});

test("money arrives as a string and stays one", async () => {
  const fetch = stubFetch(() => jsonResponse(usageResponse()));
  try {
    const report = await getUsage(caller);

    assert.equal(typeof report.cost.amount, "string");
    assert.equal(report.cost.amount, "0.052500");
  } finally {
    fetch.restore();
  }
});

test("an unpriced deployment reports null, not zero", async () => {
  // The distinction the whole cost model exists to preserve: "we do not know"
  // is not "it was free".
  const fetch = stubFetch(() =>
    jsonResponse(
      usageResponse({
        cost: {
          amount: null,
          priced_calls: 0,
          unpriced_calls: 3,
          unpriced_models: ["claude-opus-5"],
        },
      }),
    ),
  );
  try {
    const report = await getUsage(caller);

    assert.equal(report.cost.amount, null);
    assert.notEqual(report.cost.amount, 0);
    assert.equal(isPriced(report.cost), false);
  } finally {
    fetch.restore();
  }
});

test("a priced report is recognised as priced", async () => {
  const fetch = stubFetch(() => jsonResponse(usageResponse()));
  try {
    const report = await getUsage(caller);

    assert.equal(isPriced(report.cost), true);
    if (isPriced(report.cost)) {
      assert.equal(report.cost.currency, "USD");
      assert.equal(report.cost.price_version, "test-book");
    }
  } finally {
    fetch.restore();
  }
});

test("a partly-priced report says how much it could not cover", async () => {
  const fetch = stubFetch(() =>
    jsonResponse(
      usageResponse({
        cost: {
          amount: "1.000000",
          currency: "USD",
          price_version: "test-book",
          priced_calls: 2,
          unpriced_calls: 1,
          unpriced_models: ["some-other-model"],
        },
      }),
    ),
  );
  try {
    const report = await getUsage(caller);

    assert.equal(isPriced(report.cost), true);
    assert.equal(report.cost.unpriced_calls, 1);
    assert.deepEqual(report.cost.unpriced_models, ["some-other-model"]);
  } finally {
    fetch.restore();
  }
});

test("a report carries no execution payload", async () => {
  // Structural: there is no field for it, in the type or the body.
  const fetch = stubFetch(() => jsonResponse(usageResponse()));
  try {
    const report = await getUsage(caller);

    for (const forbidden of [
      "input_data",
      "output_data",
      "parameters",
      "arguments",
      "messages",
      "conversation",
      "idempotency_key",
    ]) {
      assert.equal(forbidden in report, false, `a report must not carry ${forbidden}`);
    }
  } finally {
    fetch.restore();
  }
});

// -- Refusals -----------------------------------------------------------------

test("an over-long window is reported as a check-the-details failure", async () => {
  const fetch = stubFetch(() =>
    errorResponse(422, "usage_window_invalid", "A usage window may cover at most 92 days."),
  );
  try {
    await assert.rejects(
      () => getUsage(caller, { since: "2020-01-01T00:00:00Z" }),
      (error: unknown) => {
        assert.match(describeError(error).title, /Check the details/);
        return true;
      },
    );
  } finally {
    fetch.restore();
  }
});

test("an unauthenticated request is refused", async () => {
  const fetch = stubFetch(() => errorResponse(401, "unauthorized", "Authentication required."));
  try {
    await assert.rejects(
      () => getUsage(caller),
      (error: unknown) => error instanceof ApiError && error.status === 401,
    );
  } finally {
    fetch.restore();
  }
});

// -- The screen ---------------------------------------------------------------

test("the usage view reaches for no browser storage", () => {
  // Usage is server-derived and tenant-scoped. A copy in localStorage would be
  // one organization's spend surviving a sign-out and a tenant switch.
  const root = new URL("../../src/", import.meta.url);
  const source = readFileSync(new URL("components/usage/usage-summary.tsx", root), "utf8")
    .replace(/\/\*[\s\S]*?\*\//g, "")
    .replace(/(^|[^:])\/\/.*$/gm, "$1");

  for (const forbidden of ["localStorage", "sessionStorage", "indexedDB", "document.cookie"]) {
    assert.equal(source.includes(forbidden), false, `must not use ${forbidden}`);
  }
});

test("the usage view never parses money into a number", () => {
  // The one thing that would undo the decimal, asserted by reading the source
  // because the guarantee is the absence of a call.
  const root = new URL("../../src/", import.meta.url);
  const source = readFileSync(new URL("components/usage/usage-summary.tsx", root), "utf8");

  for (const forbidden of ["parseFloat", "parseInt", "Number(", "+cost", "* cost"]) {
    assert.equal(source.includes(forbidden), false, `must not use ${forbidden} on money`);
  }
});
