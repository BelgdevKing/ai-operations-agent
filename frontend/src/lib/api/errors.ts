/**
 * What a failed API call throws.
 *
 * Two kinds, because the UI treats them differently: the backend answered and
 * refused (`ApiError`), or it never answered at all (`NetworkError`). Telling a
 * user "your session expired" and "the server is unreachable" are different
 * jobs, so they are different types rather than one error with a flag.
 */

/** Code used when the backend answered but sent no parseable envelope. */
export const UNKNOWN_ERROR_CODE = "unknown_error";

export interface ApiErrorInit {
  status: number;
  code: string;
  message: string;
  details?: Record<string, unknown>;
  requestId?: string | null;
}

/** The backend answered with a non-2xx status. */
export class ApiError extends Error {
  /** HTTP status code. */
  readonly status: number;
  /** Stable identifier from the error envelope, e.g. "unauthorized". */
  readonly code: string;
  /** Structured context, such as which fields failed validation. */
  readonly details: Record<string, unknown>;
  /**
   * Correlation id for this call, worth quoting in a bug report.
   *
   * Null when it could not be read: cross-origin responses only expose the
   * `X-Request-ID` header if the backend lists it in `Access-Control-Expose-Headers`.
   */
  readonly requestId: string | null;

  constructor({ status, code, message, details, requestId }: ApiErrorInit) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.details = details ?? {};
    this.requestId = requestId ?? null;
  }

  /** No credentials, or credentials the backend would not accept. */
  get isUnauthorized(): boolean {
    return this.status === 401;
  }

  /** Authenticated, but not allowed to do this. */
  get isForbidden(): boolean {
    return this.status === 403;
  }

  /**
   * The backend blames itself.
   *
   * These carry a deliberately generic message - the backend does not return
   * internal detail on 5xx - so the request id is the only thing worth showing.
   */
  get isServerError(): boolean {
    return this.status >= 500;
  }
}

/** The request never produced a response: server down, DNS, CORS, or a timeout. */
export class NetworkError extends Error {
  constructor(message: string, options?: { cause?: unknown }) {
    super(message, options);
    this.name = "NetworkError";
  }
}

export function isApiError(error: unknown): error is ApiError {
  return error instanceof ApiError;
}

export function isNetworkError(error: unknown): error is NetworkError {
  return error instanceof NetworkError;
}

/**
 * True when a request was cancelled deliberately - an unmounted component or a
 * superseded keystroke - rather than having failed. Callers should stay silent
 * for these instead of rendering an error.
 */
export function isAbortError(error: unknown): boolean {
  return error instanceof DOMException && error.name === "AbortError";
}

/** A message safe to put in front of a user, whatever was thrown. */
export function errorMessage(error: unknown): string {
  if (isApiError(error) || isNetworkError(error)) return error.message;
  if (error instanceof Error) return error.message;
  return "Something went wrong.";
}
