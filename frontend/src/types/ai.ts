/**
 * AI generation contracts, mirroring `app/schemas/ai.py`.
 *
 * The request cannot name a provider: the backend rejects unknown fields, and
 * which vendor serves a call is deployment configuration rather than contract.
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
