/**
 * The error envelope every FastAPI endpoint returns.
 *
 * Mirrors `app/schemas/common.py`. Every failure has this shape, which is why
 * the client can parse one thing rather than guessing per endpoint.
 */
export interface ErrorDetail {
  /** Stable machine-readable identifier, e.g. "unauthorized". Branch on this, not the message. */
  code: string;
  /** Human-readable description, safe to show. 5xx responses carry a generic one by design. */
  message: string;
  /** Structured context, such as which fields failed validation. */
  details?: Record<string, unknown>;
}

export interface ErrorResponse {
  error: ErrorDetail;
  /** Correlation id, matching the X-Request-ID response header. */
  request_id?: string | null;
}
