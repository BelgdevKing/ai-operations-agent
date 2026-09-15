/**
 * AI generation contracts, mirroring `app/schemas/ai.py`,
 * `app/schemas/agent.py` and `app/schemas/approval.py`.
 *
 * The request cannot name a provider: the backend rejects unknown fields, and
 * which vendor serves a call is deployment configuration rather than contract.
 *
 * Nothing here carries a tool's arguments or a tool's output, in either
 * direction. The backend does not publish them, and these types are how that
 * stays true as the interface grows - a field that does not exist cannot be
 * rendered by accident.
 */

/** `app/ai/models.py::LLMRole` */
export type LLMRole = "system" | "user" | "assistant";

export interface AIMessage {
  role: LLMRole;
  content: string;
}

export interface AIUsage {
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
}

/** Body of `POST /api/v1/ai/generate`. */
export interface GenerateRequest {
  messages: AIMessage[];
  /** Omit for the deployment's configured model; a named one must be allowed. */
  model?: string | null;
  temperature?: number | null;
  max_output_tokens?: number | null;
}

export interface GenerateResponse {
  content: string;
  /** The model that served the call. The provider is deliberately not returned. */
  model: string;
  usage: AIUsage;
  latency_ms: number;
}

// -- Agent runs ---------------------------------------------------------------

/**
 * `app/agents/models.py::AgentRunStatus`
 *
 * `awaiting_approval` is not an ending: the run is paused because a person was
 * asked about a tool, and it continues - as the same run - once they answer.
 */
export type AgentRunStatus =
  | "pending"
  | "running"
  | "awaiting_approval"
  | "completed"
  | "failed"
  | "cancelled";

/** `app/tools/models.py::ToolOutcome` */
export type ToolOutcome =
  | "succeeded"
  | "failed"
  | "timed_out"
  | "cancelled"
  | "approval_required"
  | "rejected";

/**
 * One turn of an agent conversation.
 *
 * No system role: an agent's instructions are server-controlled, and the schema
 * accepts user and assistant only.
 */
export interface AgentMessage {
  role: "user" | "assistant";
  content: string;
}

/**
 * Body of `POST /api/v1/ai/agents/{agent_id}/run`.
 *
 * With `conversation_id`, `messages` must carry exactly one new user turn: the
 * history belongs to the server and a client cannot rewrite it.
 */
export interface AgentRunRequest {
  messages: AgentMessage[];
  conversation_id?: string;
}

/** A tool the agent used. Name and outcome only - never arguments or results. */
export interface ToolCallSummary {
  tool_name: string;
  outcome: ToolOutcome | null;
}

/**
 * What a paused run is waiting for.
 *
 * Enough to understand the decision being asked for - which kind of action, and
 * why it is gated. Deliberately not what the tool was asked to do: the
 * arguments are business data and the backend does not publish them.
 */
/**
 * One labelled value from a tool's declared approval summary.
 *
 * Not a piece of the argument payload. A tool declares, in its own code, which
 * of its fields a reviewer may see; the projection was taken once when the
 * approval was requested, and the rest of the payload was never part of it.
 * Both halves are plain text of bounded length by the time they arrive here.
 */
export interface ApprovalField {
  label: string;
  value: string;
}

export interface PendingApproval {
  id: string;
  tool_name: string | null;
  action: string;
  /** Why a person has to agree. The framework's reason, not the model's. */
  reason: string | null;
  /** What is being proposed, in one line: "Cancel shipment ABC123". */
  summary: string | null;
  /** The labelled values behind it. An allow-list, never the arguments. */
  summary_fields: ApprovalField[];
  /** When it stops being decidable. Null where nothing expires. */
  expires_at: string | null;
  requested_at: string;
  requested_by: string;
}

export interface AgentRunResponse {
  run_id: string;
  agent_id: string;
  conversation_id: string | null;
  status: AgentRunStatus;
  /** The answer. Null while the run is unfinished or waiting on a person. */
  final_response: string | null;
  tool_calls: ToolCallSummary[];
  /** Set when the run is awaiting approval, and only then. */
  approval: PendingApproval | null;
  /** Stable code when the run failed, for example `agent_run_abandoned`. */
  error_code: string | null;
  step_count: number;
  usage: AIUsage;
  latency_ms: number;
}

/** An agent this organization may run. The system prompt is never included. */
export interface AgentSummary {
  id: string;
  name: string;
  description: string | null;
}

// -- Stored conversations -----------------------------------------------------

/** `GET /api/v1/ai/conversations` */
export interface ConversationSummary {
  id: string;
  title: string | null;
  created_at: string;
  updated_at: string;
}

/**
 * One stored turn.
 *
 * The two tool roles carry a name and an outcome and no content: what a tool
 * was asked and what it returned reach the reader through the agent's answer,
 * which is the turn that was written for them.
 */
export interface ConversationTurn {
  id: string;
  role: "user" | "assistant" | "tool_request" | "tool_result";
  content: string | null;
  tool_name: string | null;
  outcome: string | null;
  created_at: string;
}

/** `GET /api/v1/ai/conversations/{id}` */
export interface ConversationDetail {
  id: string;
  title: string | null;
  created_at: string;
  updated_at: string;
  turns: ConversationTurn[];
}

// -- Approvals ----------------------------------------------------------------

/** `app/models/enums.py::ApprovalStatus` */
export type ApprovalStatus = "pending" | "approved" | "rejected" | "expired" | "cancelled";

/** `GET /api/v1/approvals/{id}` - one approval, as a reviewer may see it. */
export interface ApprovalResponse {
  id: string;
  organization_id: string;
  status: ApprovalStatus;
  run_id: string | null;
  conversation_id: string | null;
  workflow_run_id: string | null;
  workflow_step_run_id: string | null;
  tool_execution_id: string | null;
  tool_name: string | null;
  action: string;
  /** What is being proposed, in one line. Null where the tool declared none. */
  summary: string | null;
  summary_fields: ApprovalField[];
  /** Why a person has to agree. Never the arguments, never model prose. */
  reason: string | null;
  /** What a yes does, in plain words. */
  effect_if_approved: string;
  /** What a no does. */
  effect_if_rejected: string;
  requested_by: string;
  requested_at: string;
  /** When it stops being decidable, after which the action never runs. */
  expires_at: string | null;
  decided_by: string | null;
  decided_at: string | null;
  /** What the deciding person wrote, if anything. Rendered as text. */
  decision_reason: string | null;
}

/**
 * `GET /api/v1/approvals` - one page of the inbox.
 *
 * Paged from the last row seen rather than by offset: approvals arrive at the
 * front of the order the queue is read in, so an offset would skip items that
 * were never shown to anybody. A full page always carries a cursor, because
 * whether anything follows it cannot be known without asking.
 */
export interface ApprovalQueue {
  approvals: ApprovalResponse[];
  next_cursor: string | null;
}

/**
 * The body of a decision.
 *
 * One optional field, and the backend forbids any other - so there is no
 * request a client can send that changes *what* is being approved. Everything
 * about the action is read from the persisted approval.
 */
export interface ApprovalDecisionRequest {
  reason?: string;
}

// -- Workflows ----------------------------------------------------------------

/** `app/models/enums.py::WorkflowStatus` - the lifecycle of one version. */
export type WorkflowStatus = "draft" | "active" | "inactive";

/**
 * `app/models/enums.py::RunStatus` - a *workflow* run.
 *
 * Near-twin of `AgentRunStatus`, and it says "succeeded" where that one says
 * "completed". Both carry `awaiting_approval`, which is the state that matters:
 * a run paused because a person was asked is not a run that finished.
 */
export type WorkflowRunStatus =
  | "pending"
  | "running"
  | "awaiting_approval"
  | "succeeded"
  | "failed"
  | "cancelled";

/** `app/models/enums.py::StepRunStatus` */
export type WorkflowStepStatus =
  | "pending"
  | "running"
  | "awaiting_approval"
  | "succeeded"
  | "failed"
  | "skipped"
  | "cancelled";

/** `app/models/enums.py::WorkflowStepType` */
export type WorkflowStepType = "tool_call" | "agent_step" | "condition" | "approval";

/** One version of a workflow. The definition is fetched separately. */
export interface WorkflowSummary {
  id: string;
  name: string;
  description: string | null;
  version: number;
  status: WorkflowStatus;
  created_at: string;
  updated_at: string;
}

/**
 * One step of a run.
 *
 * Which step, of what kind, how it ended and when - and deliberately not what it
 * produced. The records a tool read reach the reader through the run's own
 * result, not through a list of its working.
 */
export interface WorkflowStepRunResponse {
  id: string;
  step_key: string;
  step_type: WorkflowStepType;
  position: number;
  status: WorkflowStepStatus;
  error_code: string | null;
  started_at: string | null;
  completed_at: string | null;
}

/**
 * What a paused workflow is waiting for.
 *
 * Which step, which kind of action and why it is gated. Never the arguments -
 * the backend does not publish them, so there is nothing here to render.
 */
export interface WorkflowPendingApproval {
  id: string;
  step_key: string | null;
  tool_name: string | null;
  action: string;
  reason: string | null;
  summary: string | null;
  summary_fields: ApprovalField[];
  expires_at: string | null;
  requested_at: string;
  requested_by: string;
}

export interface WorkflowRunResponse {
  run_id: string;
  workflow_id: string;
  workflow_version: number;
  status: WorkflowRunStatus;
  current_step: string | null;
  step_count: number;
  steps: WorkflowStepRunResponse[];
  /** What the run produced, once it has succeeded. */
  output: Record<string, unknown> | null;
  error_code: string | null;
  approval: WorkflowPendingApproval | null;
  created_at: string;
  started_at: string | null;
  completed_at: string | null;
}

/** Body of `POST /api/v1/ai/workflows/{id}/runs`. */
export interface WorkflowRunRequest {
  input: Record<string, unknown>;
}

/**
 * What a decision did.
 *
 * Two kinds of process pause on an approval, so the response says which one was
 * carried forward rather than leaving a client to infer it. Both are set when an
 * agent step inside a workflow was waiting.
 */
export interface ApprovalDecisionResponse {
  approval: ApprovalResponse;
  agent_run: AgentRunResponse | null;
  workflow_run: WorkflowRunResponse | null;
}
