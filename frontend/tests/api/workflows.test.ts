/**
 * The workflow endpoints as the console uses them.
 *
 * Contract tests, mostly negative: the recurring assertion is that nothing the
 * backend sends carries a step's payload or a tool's arguments, so there is
 * nothing for a screen to disclose even if somebody tried to render it.
 */

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import {
  acme,
  approvalResponse,
  agentRunResponse,
  FAKE_TOKEN,
  workflowRunResponse,
  workflowSummary,
  WORKFLOW_ID,
  WORKFLOW_RUN_ID,
} from "../support/fixtures";
import { errorResponse, jsonResponse, stubFetch } from "../support/fetch-stub";
import {
  ApiError,
  approveAction,
  createAuthenticatedApi,
  describeError,
  getWorkflowRun,
  listWorkflowRunSteps,
  listWorkflows,
  rejectAction,
  startWorkflowRun,
} from "@/lib/api";

const caller = createAuthenticatedApi({
  getToken: () => FAKE_TOKEN,
  getOrganizationId: () => acme.id,
});

// -- Listing ------------------------------------------------------------------

test("workflows are read from the AI surface, not a second API", async () => {
  const fetch = stubFetch(() => jsonResponse([workflowSummary()]));
  try {
    await listWorkflows(caller);

    assert.equal(fetch.last.method, "GET");
    assert.equal(new URL(fetch.last.url).pathname, "/api/v1/ai/workflows");
  } finally {
    fetch.restore();
  }
});

test("the request is authenticated and names the acting organization", async () => {
  const fetch = stubFetch(() => jsonResponse([]));
  try {
    await listWorkflows(caller);

    assert.equal(fetch.last.headers.authorization, `Bearer ${FAKE_TOKEN}`);
    assert.equal(fetch.last.headers["x-organization-id"], acme.id);
  } finally {
    fetch.restore();
  }
});

test("a workflow version says which edition it is", async () => {
  const fetch = stubFetch(() => jsonResponse([workflowSummary({ version: 3 })]));
  try {
    const [workflow] = await listWorkflows(caller);

    assert.equal(workflow?.version, 3);
    assert.equal(workflow?.status, "active");
  } finally {
    fetch.restore();
  }
});

// -- Starting -----------------------------------------------------------------

test("starting a run posts the input and nothing else", async () => {
  const fetch = stubFetch(() => jsonResponse(workflowRunResponse()));
  try {
    await startWorkflowRun(caller, WORKFLOW_ID, { shipment_reference: "ABC123" });

    assert.equal(fetch.last.method, "POST");
    assert.equal(
      new URL(fetch.last.url).pathname,
      `/api/v1/ai/workflows/${WORKFLOW_ID}/runs`,
    );
    assert.deepEqual(fetch.last.body, { input: { shipment_reference: "ABC123" } });
  } finally {
    fetch.restore();
  }
});

test("nothing in the request names an organization, a user or a provider", async () => {
  const fetch = stubFetch(() => jsonResponse(workflowRunResponse()));
  try {
    await startWorkflowRun(caller, WORKFLOW_ID, { shipment_reference: "ABC123" });

    const sent = JSON.stringify(fetch.last.body);
    for (const forbidden of ["organization", "user_id", "provider", "anthropic", "api_key"]) {
      assert.equal(sent.includes(forbidden), false, `"${forbidden}" must not be in the body`);
    }
  } finally {
    fetch.restore();
  }
});

test("a key travels as the Idempotency-Key header", async () => {
  const fetch = stubFetch(() => jsonResponse(workflowRunResponse()));
  try {
    await startWorkflowRun(caller, WORKFLOW_ID, {}, { idempotencyKey: "start-1" });

    assert.equal(fetch.last.headers["idempotency-key"], "start-1");
  } finally {
    fetch.restore();
  }
});

test("a start without a key sends no such header", async () => {
  const fetch = stubFetch(() => jsonResponse(workflowRunResponse()));
  try {
    await startWorkflowRun(caller, WORKFLOW_ID, {});

    assert.equal("idempotency-key" in fetch.last.headers, false);
  } finally {
    fetch.restore();
  }
});

// -- What comes back ----------------------------------------------------------

test("a finished run reports its steps and its result", async () => {
  const fetch = stubFetch(() => jsonResponse(workflowRunResponse()));
  try {
    const run = await startWorkflowRun(caller, WORKFLOW_ID, {});

    assert.equal(run.status, "succeeded");
    assert.deepEqual(
      run.steps.map((step) => step.step_key),
      ["look", "charges"],
    );
    assert.equal(run.output?.step, "charges");
  } finally {
    fetch.restore();
  }
});

test("a step carries no payload to render", async () => {
  // Structural: there is no field for it, in the type or the body.
  const fetch = stubFetch(() => jsonResponse(workflowRunResponse()));
  try {
    const run = await startWorkflowRun(caller, WORKFLOW_ID, {});

    for (const step of run.steps) {
      assert.equal("output" in step, false);
      assert.equal("input" in step, false);
      assert.equal("arguments" in step, false);
    }
  } finally {
    fetch.restore();
  }
});

test("a paused run says what it is waiting for, and not what it would do", async () => {
  const fetch = stubFetch(() =>
    jsonResponse(
      workflowRunResponse({
        status: "awaiting_approval",
        output: null,
        approval: {
          id: "99999999-9999-4999-8999-999999999999",
          step_key: "cancel",
          tool_name: "cancel_shipment",
          action: "cancel_shipment",
          reason: "It is classified destructive.",
          requested_at: "2026-01-01T00:00:00Z",
          requested_by: "11111111-1111-4111-8111-111111111111",
        },
      }),
    ),
  );
  try {
    const run = await startWorkflowRun(caller, WORKFLOW_ID, {});

    assert.equal(run.status, "awaiting_approval");
    assert.equal(run.approval?.tool_name, "cancel_shipment");
    assert.equal(run.approval?.step_key, "cancel");
    assert.equal("parameters" in (run.approval ?? {}), false);
    assert.equal("arguments" in (run.approval ?? {}), false);
  } finally {
    fetch.restore();
  }
});

test("a stopped run is reported with a code rather than as an error", async () => {
  // A tool that found no record is an answer about the business process, so it
  // arrives as a 200 and a run to read.
  const fetch = stubFetch(() =>
    jsonResponse(
      workflowRunResponse({ status: "failed", error_code: "shipment_not_found", output: null }),
    ),
  );
  try {
    const run = await startWorkflowRun(caller, WORKFLOW_ID, {});

    assert.equal(run.status, "failed");
    assert.equal(run.error_code, "shipment_not_found");
  } finally {
    fetch.restore();
  }
});

test("a run is readable again after the page is reopened", async () => {
  const fetch = stubFetch(() => jsonResponse(workflowRunResponse()));
  try {
    const run = await getWorkflowRun(caller, WORKFLOW_ID, WORKFLOW_RUN_ID);

    assert.equal(fetch.last.method, "GET");
    assert.equal(
      new URL(fetch.last.url).pathname,
      `/api/v1/ai/workflows/${WORKFLOW_ID}/runs/${WORKFLOW_RUN_ID}`,
    );
    assert.equal(run.run_id, WORKFLOW_RUN_ID);
  } finally {
    fetch.restore();
  }
});

test("the steps endpoint is its own path", async () => {
  const fetch = stubFetch(() => jsonResponse(workflowRunResponse().steps));
  try {
    const steps = await listWorkflowRunSteps(caller, WORKFLOW_ID, WORKFLOW_RUN_ID);

    assert.match(new URL(fetch.last.url).pathname, /\/runs\/[^/]+\/steps$/);
    assert.equal(steps.length, 2);
  } finally {
    fetch.restore();
  }
});

// -- Refusals -----------------------------------------------------------------

test("a draft cannot be started, and says so", async () => {
  const fetch = stubFetch(() =>
    errorResponse(409, "workflow_not_active", "That workflow version is not active."),
  );
  try {
    await assert.rejects(
      () => startWorkflowRun(caller, WORKFLOW_ID, {}),
      (error: unknown) => error instanceof ApiError && error.status === 409,
    );
  } finally {
    fetch.restore();
  }
});

test("an input the backend refuses is reported as a check-the-details failure", async () => {
  const fetch = stubFetch(() =>
    errorResponse(422, "workflow_invalid_input", "Workflow input cannot contain identity fields."),
  );
  try {
    await assert.rejects(
      () => startWorkflowRun(caller, WORKFLOW_ID, { organization_id: "x" }),
      (error: unknown) => {
        assert.match(describeError(error).title, /Check the details/);
        return true;
      },
    );
  } finally {
    fetch.restore();
  }
});

test("another organization's workflow is simply not found", async () => {
  const fetch = stubFetch(() =>
    errorResponse(404, "workflow_not_found", "That workflow does not exist."),
  );
  try {
    await assert.rejects(
      () => getWorkflowRun(caller, WORKFLOW_ID, WORKFLOW_RUN_ID),
      (error: unknown) => error instanceof ApiError && error.status === 404,
    );
  } finally {
    fetch.restore();
  }
});

// -- Deciding a workflow approval ---------------------------------------------

test("approving a workflow step resumes the workflow, not a conversation", async () => {
  const fetch = stubFetch(() =>
    jsonResponse({
      approval: approvalResponse({ status: "approved" }),
      agent_run: null,
      workflow_run: workflowRunResponse(),
    }),
  );
  try {
    const decision = await approveAction(caller, "99999999-9999-4999-8999-999999999999");

    assert.equal(decision.agent_run, null);
    assert.equal(decision.workflow_run?.run_id, WORKFLOW_RUN_ID);
    assert.equal(decision.workflow_run?.status, "succeeded");
  } finally {
    fetch.restore();
  }
});

test("a refusal is an outcome of the process, not a failure of the platform", async () => {
  const fetch = stubFetch(() =>
    jsonResponse({
      approval: approvalResponse({ status: "rejected" }),
      agent_run: null,
      workflow_run: workflowRunResponse({
        status: "failed",
        error_code: "workflow_approval_rejected",
        output: null,
      }),
    }),
  );
  try {
    const decision = await rejectAction(caller, "99999999-9999-4999-8999-999999999999");

    assert.equal(decision.approval.status, "rejected");
    assert.equal(decision.workflow_run?.error_code, "workflow_approval_rejected");
  } finally {
    fetch.restore();
  }
});

test("an agent step inside a workflow resumes both", async () => {
  const fetch = stubFetch(() =>
    jsonResponse({
      approval: approvalResponse({ status: "approved" }),
      agent_run: agentRunResponse("Cancelled.", ["cancel_shipment"]),
      workflow_run: workflowRunResponse(),
    }),
  );
  try {
    const decision = await approveAction(caller, "99999999-9999-4999-8999-999999999999");

    assert.notEqual(decision.agent_run, null);
    assert.notEqual(decision.workflow_run, null);
  } finally {
    fetch.restore();
  }
});

// -- Storage ------------------------------------------------------------------

test("the workflow console reaches for no browser storage", () => {
  // The whole durability story is server-side. Read from source rather than
  // exercised, because the guarantee is "there is no such call" - and the only
  // way to test the absence of a call is to look.
  const root = new URL("../../src/", import.meta.url);

  for (const file of ["components/workflows/workflow-console.tsx", "app/(app)/workflows/page.tsx"]) {
    const source = readFileSync(new URL(file, root), "utf8")
      .replace(/\/\*[\s\S]*?\*\//g, "")
      .replace(/(^|[^:])\/\/.*$/gm, "$1");

    for (const forbidden of ["localStorage", "sessionStorage", "indexedDB", "document.cookie"]) {
      assert.equal(source.includes(forbidden), false, `${file} must not use ${forbidden}`);
    }
  }
});
