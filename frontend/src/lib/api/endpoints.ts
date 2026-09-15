/**
 * Typed calls for the endpoints this application uses.
 *
 * Two groups. The health probes need nothing and call `api` directly. The rest
 * need credentials, so they take an `ApiCaller` from the session instead of
 * reaching for a token themselves - which is what stops a component from
 * assembling its own authenticated request.
 */

import type { ApiCaller, CallOptions } from "./authenticated";
import { api } from "./client";
import type {
  CurrentUserResponse,
  LoginRequest,
  RegisterRequest,
  RegisterResponse,
  TokenResponse,
} from "@/types/auth";
import type { HealthResponse, ReadinessResponse } from "@/types/health";
import type {
  MemberResponse,
  MemberRole,
  OrganizationResponse,
} from "@/types/organization";
import type {
  AgentRunRequest,
  AgentRunResponse,
  AgentSummary,
  ApprovalDecisionResponse,
  ApprovalResponse,
  ConversationDetail,
  ConversationSummary,
  GenerateRequest,
  GenerateResponse,
  WorkflowRunResponse,
  WorkflowStepRunResponse,
  WorkflowSummary,
} from "@/types/ai";

/** Mounted prefix of the versioned API. Matches `Settings.api_v1_prefix`. */
const V1 = "/api/v1";

// -- Health, unauthenticated --------------------------------------------------

/** Liveness. Answers as long as the process is serving. */
export function getHealth(signal?: AbortSignal): Promise<HealthResponse> {
  return api.get<HealthResponse>("/health", { signal });
}

/**
 * Readiness, including each dependency's verdict.
 *
 * 503 is a real answer here rather than a failure - it carries the same payload
 * and says which dependency is down - so it is allowed through.
 */
export function getReadiness(signal?: AbortSignal): Promise<ReadinessResponse> {
  return api.get<ReadinessResponse>("/health/ready", { signal, allowStatus: [503] });
}

// -- Authentication -----------------------------------------------------------

/**
 * Exchange credentials for an access token.
 *
 * Unauthenticated by definition, and a POST so the password travels in the
 * body. A wrong password, an unknown address and a suspended account all come
 * back as the same 401, which is the backend refusing to say which.
 */
export function login(credentials: LoginRequest, options?: CallOptions): Promise<TokenResponse> {
  return api.post<TokenResponse>(`${V1}/auth/login`, credentials, options);
}

/** Create a user, their organization, and their owner membership. */
export function register(payload: RegisterRequest, options?: CallOptions): Promise<RegisterResponse> {
  return api.post<RegisterResponse>(`${V1}/auth/register`, payload, options);
}

/** The caller's own profile and the organizations they belong to. */
export function getCurrentUser(
  caller: ApiCaller,
  options?: CallOptions,
): Promise<CurrentUserResponse> {
  return caller.get<CurrentUserResponse>(`${V1}/auth/me`, options);
}

// -- Organization -------------------------------------------------------------

/**
 * The organization this request acts on.
 *
 * Which one that is comes from the caller's verified membership. There is no
 * organization id in the path for a client to change.
 */
export function getOrganization(
  caller: ApiCaller,
  options?: CallOptions,
): Promise<OrganizationResponse> {
  return caller.get<OrganizationResponse>(`${V1}/organization`, options);
}

export interface ListMembersOptions extends CallOptions {
  limit?: number;
  offset?: number;
}

/** Members of the current organization. Readable by any active member. */
export function listMembers(
  caller: ApiCaller,
  { limit, offset, ...options }: ListMembersOptions = {},
): Promise<MemberResponse[]> {
  return caller.get<MemberResponse[]>(`${V1}/organization/members`, {
    ...options,
    query: { limit, offset },
  });
}

/**
 * Change one member's role.
 *
 * Requires admin or owner, and several narrower rules the backend enforces on
 * top - see `canChangeRole` for the copy of them the UI uses to decide what to
 * offer.
 */
export function changeMemberRole(
  caller: ApiCaller,
  userId: string,
  role: MemberRole,
  options?: CallOptions,
): Promise<MemberResponse> {
  return caller.patch<MemberResponse>(
    `${V1}/organization/members/${encodeURIComponent(userId)}`,
    { role },
    options,
  );
}

/** Remove a member. Answers 204, so there is no body to read. */
export function removeMember(
  caller: ApiCaller,
  userId: string,
  options?: CallOptions,
): Promise<void> {
  return caller.delete<void>(`${V1}/organization/members/${encodeURIComponent(userId)}`, options);
}

// -- AI ------------------------------------------------------------------------

/** Comfortably past the backend's own provider timeout, which answers 504. */
export const AI_TIMEOUT_MS = 90_000;

/**
 * Ask the configured model for a completion.
 *
 * The provider is fixed by server configuration and the organization comes
 * from the caller's verified membership, so neither appears here. What the
 * frontend knows is that it is calling the platform's AI endpoint.
 *
 * A generation can take a while, so the deadline is longer than the client's
 * default - but still a deadline, because a request that never ends leaves the
 * composer disabled forever.
 */
export function generate(
  caller: ApiCaller,
  body: GenerateRequest,
  options?: CallOptions,
): Promise<GenerateResponse> {
  return caller.post<GenerateResponse>(`${V1}/ai/generate`, body, {
    timeoutMs: AI_TIMEOUT_MS,
    ...options,
  });
}

/**
 * The agents this organization may run.
 *
 * Scoped by the backend to the caller's own tenant, so there is no agent id for
 * a client to guess at.
 */
export function listAgents(caller: ApiCaller, options?: CallOptions): Promise<AgentSummary[]> {
  return caller.get<AgentSummary[]>(`${V1}/ai/agents`, options);
}

/** Header making a run repeatable. Matches the backend's own constant. */
export const IDEMPOTENCY_HEADER = "Idempotency-Key";

export interface RunAgentOptions extends CallOptions {
  /**
   * Opaque token making this run repeatable.
   *
   * Sending the same key again returns the run it already created rather than
   * starting a second one - which is what makes retrying safe when the first
   * answer never arrived. Scoped per organization by the backend.
   */
  idempotencyKey?: string;
}

/**
 * Run an agent over a conversation.
 *
 * The agent decides which tools to use and the server runs them; this sends one
 * new message and, for a conversation already under way, which one. Model,
 * provider, instructions and limits are all server configuration, and the body
 * has no field for any of them.
 *
 * **The run may come back unfinished.** A `status` of `awaiting_approval` means
 * a person has been asked about a tool; the run continues, under the same id,
 * once somebody decides.
 *
 * Shares the generation deadline: an agent run makes several model calls and a
 * database query or two, so it is the slower of the two paths.
 */
export function runAgent(
  caller: ApiCaller,
  agentId: string,
  body: AgentRunRequest,
  { idempotencyKey, ...options }: RunAgentOptions = {},
): Promise<AgentRunResponse> {
  return caller.post<AgentRunResponse>(
    `${V1}/ai/agents/${encodeURIComponent(agentId)}/run`,
    body,
    {
      timeoutMs: AI_TIMEOUT_MS,
      ...options,
      headers: {
        ...options.headers,
        ...(idempotencyKey ? { [IDEMPOTENCY_HEADER]: idempotencyKey } : {}),
      },
    },
  );
}

/**
 * One durable run.
 *
 * What makes a run survive the page that started it: an interface closed while
 * an approval was pending can come back and find it where it was left.
 */
export function getRun(
  caller: ApiCaller,
  runId: string,
  options?: CallOptions,
): Promise<AgentRunResponse> {
  return caller.get<AgentRunResponse>(`${V1}/ai/runs/${encodeURIComponent(runId)}`, options);
}

// -- Stored conversations -----------------------------------------------------

/** This organization's conversations, newest first. */
export function listConversations(
  caller: ApiCaller,
  options?: CallOptions,
): Promise<ConversationSummary[]> {
  return caller.get<ConversationSummary[]>(`${V1}/ai/conversations`, options);
}

/**
 * One stored conversation and its turns.
 *
 * Tool turns come back as a name and an outcome. The records the tools read
 * reach the reader through the agent's answer.
 */
export function getConversation(
  caller: ApiCaller,
  conversationId: string,
  options?: CallOptions,
): Promise<ConversationDetail> {
  return caller.get<ConversationDetail>(
    `${V1}/ai/conversations/${encodeURIComponent(conversationId)}`,
    options,
  );
}

// -- Approvals ----------------------------------------------------------------

/** Actions waiting on a person. Readable by any active member. */
export function listApprovals(
  caller: ApiCaller,
  options?: CallOptions,
): Promise<ApprovalResponse[]> {
  return caller.get<ApprovalResponse[]>(`${V1}/approvals`, options);
}

/**
 * Approve an action and resume the run that was waiting on it.
 *
 * Requires the admin or owner role, which the **backend** enforces on every
 * request. The interface hides the control from a member so they are not shown
 * something that would fail; that is a courtesy, not the boundary.
 *
 * Answers with what it resumed - an agent run, a workflow run, or both where an
 * agent step inside a workflow was waiting.
 */
export function approveAction(
  caller: ApiCaller,
  approvalId: string,
  options?: CallOptions,
): Promise<ApprovalDecisionResponse> {
  return caller.post<ApprovalDecisionResponse>(
    `${V1}/approvals/${encodeURIComponent(approvalId)}/approve`,
    undefined,
    { timeoutMs: AI_TIMEOUT_MS, ...options },
  );
}

/**
 * Decline an action and let the agent respond to having been refused.
 *
 * The tool is never executed. The refusal goes back to the agent as an ordinary
 * outcome, so what the user is told is the agent's own words rather than an
 * error.
 */
export function rejectAction(
  caller: ApiCaller,
  approvalId: string,
  options?: CallOptions,
): Promise<ApprovalDecisionResponse> {
  return caller.post<ApprovalDecisionResponse>(
    `${V1}/approvals/${encodeURIComponent(approvalId)}/reject`,
    undefined,
    { timeoutMs: AI_TIMEOUT_MS, ...options },
  );
}

// -- Workflows -----------------------------------------------------------------

/** The workflow versions this organization has, newest first within a name. */
export function listWorkflows(
  caller: ApiCaller,
  options?: CallOptions,
): Promise<WorkflowSummary[]> {
  return caller.get<WorkflowSummary[]>(`${V1}/ai/workflows`, options);
}

export interface StartWorkflowOptions extends CallOptions {
  /** Makes the start repeatable. See `runAgent` for why that matters. */
  idempotencyKey?: string;
}

/**
 * Start a run of an active workflow version.
 *
 * **The run may come back unfinished.** `awaiting_approval` means a step needs a
 * person; the same run continues once somebody decides. `failed` with an
 * `error_code` is an answer about the business process rather than an error - a
 * tool that found no record, say - so it arrives as a 200.
 *
 * Shares the agent deadline: a workflow makes several tool calls and possibly a
 * model call, so it is among the slower paths.
 */
export function startWorkflowRun(
  caller: ApiCaller,
  workflowId: string,
  input: Record<string, unknown>,
  { idempotencyKey, ...options }: StartWorkflowOptions = {},
): Promise<WorkflowRunResponse> {
  return caller.post<WorkflowRunResponse>(
    `${V1}/ai/workflows/${encodeURIComponent(workflowId)}/runs`,
    { input },
    {
      timeoutMs: AI_TIMEOUT_MS,
      ...options,
      headers: {
        ...options.headers,
        ...(idempotencyKey ? { [IDEMPOTENCY_HEADER]: idempotencyKey } : {}),
      },
    },
  );
}

/** One durable workflow run, so a reopened page finds it where it was left. */
export function getWorkflowRun(
  caller: ApiCaller,
  workflowId: string,
  runId: string,
  options?: CallOptions,
): Promise<WorkflowRunResponse> {
  return caller.get<WorkflowRunResponse>(
    `${V1}/ai/workflows/${encodeURIComponent(workflowId)}/runs/${encodeURIComponent(runId)}`,
    options,
  );
}

/** What each step of a run did. Names and outcomes, never payloads. */
export function listWorkflowRunSteps(
  caller: ApiCaller,
  workflowId: string,
  runId: string,
  options?: CallOptions,
): Promise<WorkflowStepRunResponse[]> {
  return caller.get<WorkflowStepRunResponse[]>(
    `${V1}/ai/workflows/${encodeURIComponent(workflowId)}/runs/${encodeURIComponent(runId)}/steps`,
    options,
  );
}
