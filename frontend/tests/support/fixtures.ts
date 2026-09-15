/**
 * Payloads shaped exactly like the backend's, so a contract drift shows up as
 * a failing test rather than as a screen that renders nothing.
 *
 * No real credentials anywhere: the tokens below are obvious fakes.
 */

import type {
  AgentRunResponse,
  ApprovalQueue,
  ApprovalResponse,
  ConversationDetail,
  GenerateResponse,
  WorkflowRunResponse,
  WorkflowSummary,
} from "@/types/ai";
import type { CurrentUserResponse, TokenResponse, UserProfile } from "@/types/auth";
import type { MemberResponse, MemberRole, OrganizationSummary } from "@/types/organization";

/** Not a JWT, and not valid anywhere. Only ever compared as a string. */
export const FAKE_TOKEN = "test-access-token-not-a-real-jwt";

export const tokenResponse: TokenResponse = {
  access_token: FAKE_TOKEN,
  token_type: "bearer",
  expires_in: 1800,
};

export const user: UserProfile = {
  id: "11111111-1111-4111-8111-111111111111",
  email: "ada@example.com",
  first_name: "Ada",
  last_name: "Lovelace",
  status: "active",
};

export const acme: OrganizationSummary = {
  id: "22222222-2222-4222-8222-222222222222",
  name: "Acme Operations",
  slug: "acme-operations",
  status: "active",
};

export const globex: OrganizationSummary = {
  id: "33333333-3333-4333-8333-333333333333",
  name: "Globex",
  slug: "globex",
  status: "active",
};

export function currentUser(
  memberships: { organization: OrganizationSummary; role: MemberRole }[] = [
    { organization: acme, role: "owner" },
  ],
): CurrentUserResponse {
  return { user, memberships };
}

export function member(
  overrides: Partial<MemberResponse> & { role: MemberRole; userId: string },
): MemberResponse {
  const { role, userId, ...rest } = overrides;

  return {
    id: `membership-${userId}`,
    user: {
      id: userId,
      email: `${userId}@example.com`,
      first_name: null,
      last_name: null,
    },
    role,
    status: "active",
    created_at: "2026-01-01T00:00:00Z",
    ...rest,
  };
}

/** An answer from the AI endpoint, shaped exactly like `GenerateResponse`. */
export function generateResponse(content: string): GenerateResponse {
  return {
    content,
    model: "claude-opus-5",
    usage: { input_tokens: 12, output_tokens: 18, total_tokens: 30 },
    latency_ms: 412.5,
  };
}

export const RUN_ID = "44444444-4444-4444-8444-444444444444";
export const AGENT_ID = "55555555-5555-4555-8555-555555555555";
export const CONVERSATION_ID = "66666666-6666-4666-8666-666666666666";
export const APPROVAL_ID = "77777777-7777-4777-8777-777777777777";

/** A completed agent run, shaped exactly like `AgentRunResponse`. */
export function agentRunResponse(content: string, tools: string[] = []): AgentRunResponse {
  return {
    run_id: RUN_ID,
    agent_id: AGENT_ID,
    conversation_id: CONVERSATION_ID,
    status: "completed",
    final_response: content,
    tool_calls: tools.map((tool_name) => ({ tool_name, outcome: "succeeded" as const })),
    approval: null,
    error_code: null,
    step_count: tools.length + 1,
    usage: { input_tokens: 12, output_tokens: 18, total_tokens: 30 },
    latency_ms: 412.5,
  };
}

/**
 * A run that has stopped to ask somebody about a destructive tool.
 *
 * No `final_response`: it has not answered anything. The approval says which
 * kind of action and why it is gated, and - as the backend does - nothing about
 * what the tool was asked to do.
 */
export function awaitingApproval(): AgentRunResponse {
  return {
    ...agentRunResponse(""),
    status: "awaiting_approval",
    final_response: null,
    tool_calls: [{ tool_name: "cancel_shipment", outcome: "approval_required" }],
    approval: {
      id: APPROVAL_ID,
      tool_name: "cancel_shipment",
      action: "cancel_shipment",
      reason:
        "The agent asked to run cancel_shipment, which is classified destructive and cannot be performed without a person agreeing to it.",
      summary: "Cancel shipment ABC123",
      summary_fields: [
        { label: "Shipment reference", value: "ABC123" },
        { label: "Reason", value: "The customer asked us to stop it." },
      ],
      expires_at: "2026-01-02T00:00:00Z",
      requested_at: "2026-01-01T00:00:00Z",
      requested_by: user.id,
    },
    step_count: 1,
  };
}

/** One page of the inbox, as `GET /approvals` answers it. */
export function approvalQueue(
  approvals: ApprovalResponse[] = [approvalResponse()],
  nextCursor: string | null = null,
): ApprovalQueue {
  return { approvals, next_cursor: nextCursor };
}

/** One pending approval, as the queue reports it. */
export function approvalResponse(
  overrides: Partial<ApprovalResponse> = {},
): ApprovalResponse {
  return {
    id: APPROVAL_ID,
    organization_id: acme.id,
    status: "pending",
    run_id: RUN_ID,
    conversation_id: CONVERSATION_ID,
    workflow_run_id: null,
    workflow_step_run_id: null,
    tool_execution_id: "88888888-8888-4888-8888-888888888888",
    tool_name: "cancel_shipment",
    action: "cancel_shipment",
    summary: "Cancel shipment ABC123",
    summary_fields: [
      { label: "Shipment reference", value: "ABC123" },
      { label: "Reason", value: "The customer asked us to stop it." },
    ],
    reason: "It is classified destructive.",
    effect_if_approved:
      "cancel_shipment runs once, as the exact execution this approval names, and the paused run continues from where it stopped.",
    effect_if_rejected:
      "cancel_shipment is never run. The paused run continues and reports the refusal - a refusal is an outcome of the process, not a failure of the platform.",
    requested_by: user.id,
    requested_at: "2026-01-01T00:00:00Z",
    expires_at: "2026-01-02T00:00:00Z",
    decision_reason: null,
    decided_by: null,
    decided_at: null,
    ...overrides,
  };
}

/**
 * A stored conversation: a question, a tool the agent used, and the answer.
 *
 * The tool turns carry a name and an outcome and no content, exactly as the
 * backend returns them - there is nothing in the payload for an interface to
 * leak even if it tried.
 */
export function storedConversation(): ConversationDetail {
  return {
    id: CONVERSATION_ID,
    title: "Where is ABC123?",
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:05Z",
    turns: [
      {
        id: "m1",
        role: "user",
        content: "Where is ABC123?",
        tool_name: null,
        outcome: null,
        created_at: "2026-01-01T00:00:00Z",
      },
      {
        id: "m2",
        role: "tool_request",
        content: null,
        tool_name: "get_shipment",
        outcome: null,
        created_at: "2026-01-01T00:00:01Z",
      },
      {
        id: "m3",
        role: "tool_result",
        content: null,
        tool_name: "get_shipment",
        outcome: "succeeded",
        created_at: "2026-01-01T00:00:02Z",
      },
      {
        id: "m4",
        role: "assistant",
        content: "It is in transit to Felixstowe.",
        tool_name: null,
        outcome: null,
        created_at: "2026-01-01T00:00:03Z",
      },
    ],
  };
}

// -- Workflows ----------------------------------------------------------------

export const WORKFLOW_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
export const WORKFLOW_RUN_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";

/** One active workflow version, as the list reports it. */
export function workflowSummary(
  overrides: Partial<WorkflowSummary> = {},
): WorkflowSummary {
  return {
    id: WORKFLOW_ID,
    name: "Shipment exception review",
    description: "Look a shipment up and decide whether to cancel it.",
    version: 1,
    status: "active",
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
    ...overrides,
  };
}

/**
 * A finished workflow run, shaped exactly like `WorkflowRunResponse`.
 *
 * Its steps carry a name, a kind and an outcome and no payload - which is the
 * backend's shape, and the property most of the workflow tests assert.
 */
export function workflowRunResponse(
  overrides: Partial<WorkflowRunResponse> = {},
): WorkflowRunResponse {
  return {
    run_id: WORKFLOW_RUN_ID,
    workflow_id: WORKFLOW_ID,
    workflow_version: 1,
    status: "succeeded",
    current_step: "charges",
    step_count: 2,
    steps: [
      {
        id: "s1",
        step_key: "look",
        step_type: "tool_call",
        position: 1,
        status: "succeeded",
        error_code: null,
        started_at: "2026-01-01T00:00:00Z",
        completed_at: "2026-01-01T00:00:01Z",
      },
      {
        id: "s2",
        step_key: "charges",
        step_type: "tool_call",
        position: 2,
        status: "succeeded",
        error_code: null,
        started_at: "2026-01-01T00:00:01Z",
        completed_at: "2026-01-01T00:00:02Z",
      },
    ],
    output: { step: "charges", output: { total_outstanding: "0.00" } },
    error_code: null,
    approval: null,
    created_at: "2026-01-01T00:00:00Z",
    started_at: "2026-01-01T00:00:00Z",
    completed_at: "2026-01-01T00:00:02Z",
    ...overrides,
  };
}
