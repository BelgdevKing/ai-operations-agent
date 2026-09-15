# Frontend

Next.js dashboard for the platform.

Next.js (App Router) · TypeScript · Tailwind CSS v4 · shadcn/ui (later)

**Implemented so far:** the application shell and its routes, a shared API
client, TypeScript types mirroring the backend contracts, a working
authenticated session — sign in, sign out, the current user, organization
membership administration — and an AI workspace that talks to the platform's
own generation endpoint.

## Routes

| Route | Purpose |
| --- | --- |
| `/` | Landing page, with a live backend reachability indicator |
| `/login` | Sign in |
| `/register` | Create an account and the organization you will own |
| `/dashboard` | Your session and live service health; agent activity later |
| `/ai` | Ask the configured model a question |
| `/organization` | The tenant, its members, and role administration |
| `/settings` | Your account, and what this build points at |

`/dashboard`, `/organization` and `/settings` share the application shell
through the `(app)` route group, which gives them one sidebar and one footer
without appearing in any URL, and sit behind `<RequireAuth>`. `/`, `/login` and
`/register` stay outside it. `not-found.tsx` and `error.tsx` cover the 404 and
the render-error cases.

## Layout

```
src/
├── app/          routes (App Router)
│   └── (app)/    routes sharing the application shell
├── components/   feature components
│   ├── layout/   shell, navigation, page header
│   └── ui/       primitives (shadcn/ui-compatible conventions)
├── hooks/        reusable client-side behaviour
├── lib/
│   ├── ai/       the conversation, as pure data and functions
│   ├── api/      the API client
│   ├── auth/     the session, permissions, form validation
│   └── config.ts build-time configuration
└── types/        TypeScript mirrors of the backend contracts
tests/            node:test suites for the session and the API client
public/           static assets
```

## Talking to the backend

Everything goes through `src/lib/api`. Import from there, never from the
modules inside it, and do not write a bare `fetch` in a component:

```ts
import { api, ApiError, isApiError } from "@/lib/api";

const health = await api.get<HealthResponse>("/health");
```

The client owns URL building, JSON encoding, a request timeout, cancellation,
and the error envelope. A failed call throws one of two things, because a page
should say different words for each:

- `ApiError` — the backend answered and refused. Carries `status`, the stable
  `code`, a safe `message`, `details`, and the `requestId` to quote in a bug
  report. A 5xx deliberately carries a generic message; the detail is in the
  server log.
- `NetworkError` — nothing answered: the backend is down, or the call timed out.

A deliberate cancellation is neither, and is re-thrown untouched so an unmounted
component renders no error. `usePolledResource` in `src/hooks` wraps all of that
for client components that need to load or poll something.

Types in `src/types` mirror the current FastAPI schemas field for field. When a
backend contract changes, change them here rather than working around the
difference at a call site.

## Authentication

One session object, `SessionStore` in `src/lib/auth/session-store.ts`, is the
only thing that knows the browser is signed in. React reaches it through
`<SessionProvider>` in the root layout and the hooks beside it; nothing else
handles a token.

Signing in takes two calls, because the backend splits them:

1. `POST /api/v1/auth/login` returns an access token and nothing else.
2. `GET /api/v1/auth/me` returns the user and the organizations they belong to.

From then on `useApi()` gives components a caller that attaches
`Authorization: Bearer <token>` by itself. Components never build that header,
and cannot: the option is not on the type they are handed.

```ts
const api = useApi();                       // authenticated caller
const { user, active } = useAuthenticatedSession();
const members = await listMembers(api, { limit: 200 });
```

**Where the token is kept, and what that costs.** In memory, in a `#private`
field, for the lifetime of the page. Never in `localStorage`, `sessionStorage`,
a cookie, React state, or the rendered HTML.

That is not a preference, it is the only safe option the backend currently
offers. It issues a bearer token in the login response body, with no HttpOnly
cookie and no refresh token, so the browser must either hold the token where
JavaScript can read it or not hold it at all. Browser storage would hand it to
any script that manages to run on the page, and would keep handing it over long
after the tab closed.

**Reloading the browser tab ends the current frontend session because the
access token is intentionally held only in memory.** This is deliberate and
visible, not a bug. Removing it needs a backend change - an HttpOnly cookie set
on login, or a refresh token - and that decision belongs to the backend, so the
limitation is documented here rather than worked around.

A `401` on any authenticated request means the token is no longer accepted: the
session clears itself and `<RequireAuth>` sends the visitor to `/login`, which
says the session ended. A `403` does not - the token was fine and the answer was
still no, and signing someone out for opening a page they may not see would
teach them the wrong lesson.

### Organizations

`GET /auth/me` reports every organization the user actively belongs to. The
session acts as one of them and sends its id as `X-Organization-ID` on every
authenticated request; the switcher in the sidebar appears when there is more
than one to choose from. The backend *requires* the header once a user belongs
to several - without it the API answers `400 organization_required` rather than
guessing - so it is always sent rather than only sometimes.

The header is a request, not a grant. The backend resolves it against the
caller's own memberships and refuses anything else with a `403`. Nothing in the
browser decides what a user may reach.

### What the UI decides, and what it does not

`src/lib/auth/permissions.ts` mirrors the membership rules from
`app/services/membership.py` so the interface can grey out an action instead of
letting someone click it and read a refusal. **It is not a security boundary.**
Every rule is enforced by the backend against the database on every request; if
the two ever disagree the backend wins, and the worst a mistake here can do is
offer a button that then fails.

## The AI workspace

`/ai` sends a conversation to `POST /api/v1/ai/generate` and shows what comes
back. Everything that makes that interesting happens on the server:

```
/ai  ->  API client  ->  POST /api/v1/ai/generate  ->  AIService
     ->  LLMGateway  ->  configured provider  ->  Claude / OpenAI
```

The browser knows none of it. It does not know which provider served the call,
whether the gateway retried, or what credentials were used — **Claude and
OpenAI credentials are backend-only** and no provider SDK is installed here.
The request body is the conversation and nothing else:

- **No `model`.** Nothing exposes which models a deployment allows, so the
  browser has no legitimate way to name one. Omitting it uses the configured
  model, and `LLM_ALLOWED_MODELS` cannot be routed around from here.
- **No system message.** The schema has the role, but a system prompt the
  browser controls is one an end user controls. If the product needs one it
  belongs on the server.
- **No `temperature` or `max_output_tokens`.** Server policy; sending them
  would only let a client widen them.
- **No organization field.** The tenant comes from the verified membership, the
  same way every other authenticated call works.

Answers are rendered as **plain text**, preserving line breaks. No Markdown and
no HTML: rendering a model's output as markup is how an injected instruction
becomes an injected element, and no Markdown dependency is worth that.

### Conversations are saved on the server

A run started through an agent writes a conversation and its messages to the
database, so the console can list earlier conversations and reopen one — see
`GET /api/v1/ai/conversations`. The transcript on screen is still React state
for the session you are in; what makes it durable is the server's copy, not the
browser's.

The stateless `POST /ai/generate` path is unchanged and still takes the whole
message list every time. It is the lower-level endpoint the workspace was first
built on, and it persists nothing by design.

The size limits from `app/schemas/ai.py` are mirrored in
`src/lib/ai/conversation.ts` so a conversation that has outgrown the endpoint
says so instead of failing with a 422. When it fills up, start a new one.

A generation in flight can be stopped with the Stop button, which aborts the
request through the API client's existing cancellation. Stopping is not a
failure: no error is shown and no assistant message is invented, and the
message can be sent again.

## Running

Through the Compose stack, from the repository root:

```bash
docker compose up --build      # http://localhost:3000
```

Natively, with no Docker:

```powershell
cd frontend
npm install
copy .env.example .env.local   # optional - defaults point at localhost:8000
npm run dev                    # http://localhost:3000
```

Other scripts:

```powershell
npm run lint                   # eslint
npm run typecheck              # tsc --noEmit
npm test                       # node:test, no browser and no extra dependencies
npm run build                  # production build
```

To use the authenticated pages you need the backend running and an account.
Create one at <http://localhost:3000/register> - registration makes you the
owner of a new organization - or sign in at `/login` with an existing one.
There is no invitation endpoint yet, so a second member has to be added
directly in the database.

### Environment

Two variables, both public by design, both optional - the defaults point at a
backend on `localhost:8000`:

| Variable | Default | Purpose |
| --- | --- | --- |
| `NEXT_PUBLIC_API_URL` | `http://localhost:8000` | Backend address **as the browser sees it** |
| `NEXT_PUBLIC_APP_NAME` | `AI Operations Agent Platform` | Name shown in the interface |

There is no frontend secret, and there must never be one: `NEXT_PUBLIC_*` values
are inlined into the JavaScript the browser downloads. API keys, the JWT signing
secret and database credentials belong to the backend environment only.

`.env.local` is git-ignored and is where local overrides belong. Values are
inlined at build time - restart the dev server after changing one. Full setup
notes: [docs/development-windows.md](../docs/development-windows.md).

## Screens

| Screen | Route | Status |
| --- | --- | --- |
| Agent console | `/ai` | Built — agent and conversation pickers, transcript, execution detail, tool activity, approval panel |
| Approvals | `/approvals` | Built — pending queue, safe action summary, approve or reject |
| Workflows | `/workflows` | Built — definitions and run state |
| Dashboard | `/dashboard` | Built — the usage and cost summary |
| Organization | `/organization` | Built — members and roles |
| Settings | `/settings` | Built — the build configuration this bundle was compiled with |
| Documents | – | Not built; the backend has no ingestion or retrieval yet |
| Audit | – | Not built; events are recorded but no API reads them back |

## Rules

- Only `NEXT_PUBLIC_*` environment variables reach the browser. Never put a
  secret in one: they are inlined into the JavaScript anyone can download.
- The browser talks to the backend API; it never talks to Postgres, Redis, or
  the Claude API directly.
- Server components by default; client components only where interactivity
  requires them.
- One API client. No ad-hoc `fetch` calls in pages or components.
- One session. No component reads or stores a token, and no component builds an
  `Authorization` header.
- The backend is the authority on who may do what. Anything the UI decides is
  presentation.

See [../docs/architecture.md](../docs/architecture.md).
