/**
 * The one place the frontend talks to the backend.
 *
 * Every page and component goes through `api`, so URL building, the error
 * envelope, timeouts and cancellation are decided once. Ad-hoc `fetch` calls
 * scattered through components are how three slightly different definitions of
 * "the request failed" get born.
 *
 * It holds no credentials and reads no storage. Attaching an access token is
 * the caller's choice via `RequestOptions.token`; nothing in this part supplies
 * one, because authentication arrives in a later part.
 */

import { apiUrl } from "@/lib/config";
import type { ErrorResponse } from "@/types/api";

import { ApiError, NetworkError, UNKNOWN_ERROR_CODE } from "./errors";

/** Correlation id the backend sets on every response. */
export const REQUEST_ID_HEADER = "x-request-id";

/**
 * Header naming the organization a request acts on.
 *
 * The backend verifies it against the caller's memberships, so sending it is a
 * choice of tenant, never a claim of access.
 */
export const ORGANIZATION_HEADER = "X-Organization-ID";

/** Long enough for a slow cold start, short enough that a hung call still ends. */
export const DEFAULT_TIMEOUT_MS = 15_000;

export type QueryValue = string | number | boolean | null | undefined;

export interface RequestOptions {
  method?: "GET" | "POST" | "PATCH" | "PUT" | "DELETE";
  /** Serialised as JSON. Omit for a body-less request. */
  body?: unknown;
  /** Query parameters. Null and undefined entries are dropped. */
  query?: Record<string, QueryValue>;
  /** Extra headers, merged over the defaults. */
  headers?: Record<string, string>;
  /** Bearer token. Supplied per call; this module never stores one. */
  token?: string;
  /** Tenant to act as, sent as `X-Organization-ID`. */
  organizationId?: string;
  /** Caller's cancellation signal, honoured alongside the timeout. */
  signal?: AbortSignal;
  /** Overrides {@link DEFAULT_TIMEOUT_MS}. Pass 0 to wait indefinitely. */
  timeoutMs?: number;
  /** Next.js fetch cache mode. Defaults to "no-store": this data is live. */
  cache?: RequestCache;
  /**
   * Non-2xx statuses to treat as success and parse as `T`.
   *
   * For endpoints that answer with a domain payload rather than an error
   * envelope - `GET /health/ready` returns its report with 503 when degraded.
   */
  allowStatus?: readonly number[];
}

/**
 * Call the backend and return the parsed body.
 *
 * @throws {ApiError} The backend answered with a status that is not success.
 * @throws {NetworkError} No response arrived, including on timeout.
 * @throws {DOMException} Named "AbortError", when the caller's own signal
 *   cancelled the request. Deliberate cancellation is not a failure, so it is
 *   re-thrown untouched for callers to ignore - see `isAbortError`.
 */
export async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const {
    method = "GET",
    body,
    query,
    headers,
    token,
    organizationId,
    signal,
    timeoutMs = DEFAULT_TIMEOUT_MS,
    cache = "no-store",
    allowStatus,
  } = options;

  const response = await send(buildUrl(path, query), {
    method,
    cache,
    headers: buildHeaders({ body, headers, token, organizationId }),
    body: body === undefined ? undefined : JSON.stringify(body),
    signal,
    timeoutMs,
  });

  if (!response.ok && !allowStatus?.includes(response.status)) {
    throw await readError(response);
  }

  return (await readBody(response)) as T;
}

export const api = {
  get: <T>(path: string, options?: Omit<RequestOptions, "method" | "body">) =>
    request<T>(path, { ...options, method: "GET" }),

  post: <T>(path: string, body?: unknown, options?: Omit<RequestOptions, "method" | "body">) =>
    request<T>(path, { ...options, method: "POST", body }),

  patch: <T>(path: string, body?: unknown, options?: Omit<RequestOptions, "method" | "body">) =>
    request<T>(path, { ...options, method: "PATCH", body }),

  delete: <T = void>(path: string, options?: Omit<RequestOptions, "method" | "body">) =>
    request<T>(path, { ...options, method: "DELETE" }),
} as const;

// -- Internals ----------------------------------------------------------------

function buildUrl(path: string, query?: Record<string, QueryValue>): string {
  const url = new URL(path.startsWith("/") ? path : `/${path}`, `${apiUrl}/`);

  for (const [key, value] of Object.entries(query ?? {})) {
    if (value === null || value === undefined) continue;
    url.searchParams.set(key, String(value));
  }

  return url.toString();
}

function buildHeaders({
  body,
  headers,
  token,
  organizationId,
}: Pick<RequestOptions, "body" | "headers" | "token" | "organizationId">): HeadersInit {
  const result: Record<string, string> = { Accept: "application/json" };

  if (body !== undefined) result["Content-Type"] = "application/json";
  if (token) result.Authorization = `Bearer ${token}`;
  if (organizationId) result[ORGANIZATION_HEADER] = organizationId;

  return { ...result, ...headers };
}

interface SendOptions extends Omit<RequestInit, "signal"> {
  signal?: AbortSignal;
  timeoutMs: number;
}

/**
 * `fetch`, with a deadline and with transport failures named.
 *
 * The timeout is built from an AbortController rather than `AbortSignal.any`,
 * which is newer than the Node version this project supports.
 */
async function send(url: string, { signal, timeoutMs, ...init }: SendOptions): Promise<Response> {
  if (signal?.aborted) signal.throwIfAborted();

  const controller = new AbortController();
  let timedOut = false;

  const timer =
    timeoutMs > 0
      ? setTimeout(() => {
          timedOut = true;
          controller.abort();
        }, timeoutMs)
      : undefined;

  const relayAbort = () => controller.abort();
  signal?.addEventListener("abort", relayAbort, { once: true });

  try {
    return await fetch(url, { ...init, signal: controller.signal });
  } catch (error) {
    if (timedOut) {
      throw new NetworkError(`The request timed out after ${timeoutMs} ms.`, { cause: error });
    }
    // The caller cancelled: not a failure, so it travels up unchanged.
    if (signal?.aborted) signal.throwIfAborted();

    throw new NetworkError(
      `Could not reach the API at ${apiUrl}. Check that the backend is running.`,
      { cause: error },
    );
  } finally {
    if (timer !== undefined) clearTimeout(timer);
    signal?.removeEventListener("abort", relayAbort);
  }
}

/** Parse the body, tolerating the endpoints that answer with none. */
async function readBody(response: Response): Promise<unknown> {
  if (response.status === 204 || response.headers.get("content-length") === "0") {
    return undefined;
  }

  const text = await response.text();
  if (!text) return undefined;

  try {
    return JSON.parse(text) as unknown;
  } catch (error) {
    throw new NetworkError("The API returned a response that is not valid JSON.", {
      cause: error,
    });
  }
}

/**
 * Turn a failed response into an `ApiError`.
 *
 * The request id is taken from the envelope first and the header second: a
 * cross-origin browser request can only read the header when the backend lists
 * it in `Access-Control-Expose-Headers`, while the body always carries it.
 */
async function readError(response: Response): Promise<ApiError> {
  const headerRequestId = response.headers.get(REQUEST_ID_HEADER);

  let code = UNKNOWN_ERROR_CODE;
  let message = response.statusText || `The request failed with status ${response.status}.`;
  let details: Record<string, unknown> | undefined;
  let requestId: string | null = headerRequestId;

  try {
    const payload = (await response.json()) as Partial<ErrorResponse> | null;
    if (payload?.error && typeof payload.error.message === "string") {
      code = payload.error.code || code;
      message = payload.error.message;
      details = payload.error.details;
    }
    requestId = payload?.request_id ?? headerRequestId;
  } catch {
    // Not our envelope - a proxy error page, or an empty body. The status
    // already gave us something to show.
  }

  return new ApiError({ status: response.status, code, message, details, requestId });
}
