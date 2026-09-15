/**
 * A stand-in for `fetch`, so the tests exercise the real client without a
 * network or a running backend.
 *
 * Every response is built to look like the backend's: the error envelope, the
 * request id in the body, the JSON content type. A stub that answered in some
 * simpler shape would test the stub.
 */

export interface RecordedRequest {
  url: string;
  method: string;
  headers: Record<string, string>;
  body: unknown;
}

export interface FetchStub {
  /** Every call made since the stub was installed, oldest first. */
  readonly calls: RecordedRequest[];
  /** The most recent call. Throws if there was none. */
  readonly last: RecordedRequest;
  restore(): void;
}

type Handler = (request: RecordedRequest) => Response | Promise<Response>;

/**
 * Install a fake `fetch` for the duration of a test.
 *
 * The caller decides what each request answers; the stub only records what was
 * asked. Always paired with `restore()` in a `finally` or `t.after`.
 */
export function stubFetch(handler: Handler): FetchStub {
  const original = globalThis.fetch;
  const calls: RecordedRequest[] = [];

  globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    const headers: Record<string, string> = {};
    for (const [key, value] of Object.entries(
      (init?.headers as Record<string, string> | undefined) ?? {},
    )) {
      headers[key.toLowerCase()] = value;
    }

    const recorded: RecordedRequest = {
      url: String(input),
      method: init?.method ?? "GET",
      headers,
      body: typeof init?.body === "string" ? JSON.parse(init.body) : undefined,
    };
    calls.push(recorded);

    // Honour an abort that has already happened, the way fetch does.
    init?.signal?.throwIfAborted();

    const answer = Promise.resolve(handler(recorded));
    const signal = init?.signal;
    if (!signal) return answer;

    // A real fetch rejects when its signal aborts, however long the response
    // was going to take. Without this, a handler that never settles hangs the
    // client's own timeout instead of being cut short by it.
    return Promise.race([
      answer,
      new Promise<never>((_resolve, reject) => {
        signal.addEventListener("abort", () => reject(signal.reason), { once: true });
      }),
    ]);
  }) as typeof fetch;

  return {
    calls,
    get last() {
      const call = calls.at(-1);
      if (!call) throw new Error("No request was made.");
      return call;
    },
    restore() {
      globalThis.fetch = original;
    },
  };
}

/** A successful JSON response, as the backend would send it. */
export function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json", "x-request-id": "test-request-id" },
  });
}

/** An error in the backend's envelope, request id included in the body. */
export function errorResponse(
  status: number,
  code: string,
  message: string,
  details: Record<string, unknown> = {},
): Response {
  return new Response(
    JSON.stringify({ error: { code, message, details }, request_id: "test-request-id" }),
    {
      status,
      headers: { "content-type": "application/json", "x-request-id": "test-request-id" },
    },
  );
}

/** 204, which the member removal endpoint answers with. */
export function noContentResponse(): Response {
  return new Response(null, { status: 204, headers: { "x-request-id": "test-request-id" } });
}
