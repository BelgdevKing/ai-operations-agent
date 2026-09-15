/**
 * Turning a thrown error into something a person can read.
 *
 * One mapping, so every screen says the same thing about the same failure. The
 * rule underneath it: show the backend's message only where the backend writes
 * that message for a client. It does so for 4xx, which describe what the caller
 * did. It deliberately does not for 5xx - those carry a generic message and the
 * detail stays in the server log - so this substitutes its own text there
 * rather than echoing whatever arrived.
 *
 * Three 5xx statuses get their own words, because the advice genuinely differs:
 * 502 and 504 are the model provider, and 503 is a dependency this service
 * needs. All three are temporary and worth retrying; a plain 500 is not.
 */

import { ApiError, NetworkError } from "./errors";

export interface ErrorPresentation {
  title: string;
  message: string;
  tone: "warn" | "danger";
  /** Worth quoting in a bug report. Null when the response did not carry one. */
  requestId: string | null;
  /** Per-field messages from a 422, keyed by field name. */
  fieldErrors: Record<string, string>;
}

/** One entry of the backend's `details.errors` on a 422. */
interface ValidationEntry {
  field?: unknown;
  message?: unknown;
}

/**
 * Pull field errors out of a 422 envelope.
 *
 * The backend sends `{"errors": [{"field": "body.password", "message": ...}]}`.
 * The `body.` prefix is an artefact of where the value sat in the request, not
 * something to show a user, so it is dropped.
 */
export function fieldErrorsOf(error: unknown): Record<string, string> {
  if (!(error instanceof ApiError) || error.status !== 422) return {};

  const entries = error.details.errors;
  if (!Array.isArray(entries)) return {};

  const result: Record<string, string> = {};
  for (const entry of entries as ValidationEntry[]) {
    if (typeof entry?.field !== "string" || typeof entry.message !== "string") continue;
    const name = entry.field.replace(/^body\./, "");
    // First message wins: later ones for the same field are usually a
    // restatement, and a stack of them under one input reads as noise.
    result[name] ??= entry.message;
  }
  return result;
}

/**
 * The same mapping, for a sign-in attempt.
 *
 * A 401 means something different here than anywhere else. Everywhere else it
 * means the token stopped working and the session is over; on the sign-in form
 * it means the credentials were wrong, and the backend's own message - the same
 * one for a wrong password, an unknown address and a suspended account - is
 * exactly what to show.
 */
export function describeSignInError(error: unknown): ErrorPresentation {
  if (error instanceof ApiError && error.status === 401) {
    return {
      title: "Sign-in failed",
      message: error.message,
      tone: "danger",
      requestId: error.requestId,
      fieldErrors: {},
    };
  }

  return describeError(error);
}

export function describeError(error: unknown): ErrorPresentation {
  if (error instanceof NetworkError) {
    return {
      title: "Cannot reach the server",
      message: "The API did not respond. Check that the backend is running, then try again.",
      tone: "warn",
      requestId: null,
      fieldErrors: {},
    };
  }

  if (!(error instanceof ApiError)) {
    return {
      title: "Something went wrong",
      message: "The action could not be completed.",
      tone: "danger",
      requestId: null,
      fieldErrors: {},
    };
  }

  const requestId = error.requestId;
  const fieldErrors = fieldErrorsOf(error);

  switch (error.status) {
    case 401:
      return {
        title: "Your session has ended",
        message: "Sign in again to continue.",
        tone: "warn",
        requestId,
        fieldErrors,
      };

    case 403:
      return {
        title: "Access denied",
        message: error.message,
        tone: "warn",
        requestId,
        fieldErrors,
      };

    case 404:
      return {
        title: "Not found",
        message: error.message,
        tone: "warn",
        requestId,
        fieldErrors,
      };

    case 409:
      return {
        title: "That conflicts with the current state",
        message: error.message,
        tone: "warn",
        requestId,
        fieldErrors,
      };

    case 422:
      return {
        title: "Check the details you entered",
        message: error.message,
        tone: "warn",
        requestId,
        fieldErrors,
      };

    case 429:
      return {
        title: "Too many requests",
        message: "You have made too many requests. Wait a moment and try again.",
        tone: "warn",
        requestId,
        fieldErrors,
      };

    default:
      break;
  }

  // The two provider failures the AI endpoint distinguishes. Worth their own
  // words - "try again" and "try again shorter" are different advice - but
  // written here rather than echoed from the response, so the 5xx rule above
  // holds even if a future backend change made one of these messages specific.
  if (error.status === 502) {
    return {
      title: "The model could not answer",
      message: "The AI provider returned an error. Trying again usually works.",
      tone: "warn",
      requestId,
      fieldErrors: {},
    };
  }

  if (error.status === 504) {
    return {
      title: "The model took too long",
      message:
        "The AI provider did not respond in time. Try again, or send a shorter message.",
      tone: "warn",
      requestId,
      fieldErrors: {},
    };
  }

  // A dependency the server needs is down - the database, most often. Its own
  // case rather than the generic 5xx below, because the advice genuinely
  // differs: this one is temporary and the same request works once the
  // dependency is back, so "try again shortly" is accurate rather than
  // hopeful. The backend answers it deliberately, as `service_unavailable`,
  // instead of letting an outage look like a bug.
  if (error.status === 503) {
    return {
      title: "The service is temporarily unavailable",
      message: "Something the server depends on is not responding. Try again shortly.",
      tone: "warn",
      requestId,
      fieldErrors: {},
    };
  }

  if (error.isServerError) {
    // Never the backend's own text: 5xx detail belongs in the server log, and
    // the request id is what ties this screen to it.
    return {
      title: "The server could not complete that",
      message: "Something failed on the server. Trying again may work.",
      tone: "danger",
      requestId,
      fieldErrors: {},
    };
  }

  return {
    title: "Something went wrong",
    message: error.message,
    tone: "danger",
    requestId,
    fieldErrors,
  };
}
