# Deployment

How to run this platform somewhere real, what each decision costs, and what it
deliberately does not do for you.

The target is Docker Compose on a single host. That is the repository's stated
platform and this phase did not change it: there is no Kubernetes, no Helm and
no Terraform here, and adding all three for a platform that fits on one host
would be three more things to keep correct with nothing to show for it.
Everything below is arranged so that moving to an orchestrator later is a
change of manifest rather than a change of application.

---

## What you get

```
                    ┌──────────────────────────────┐
   TLS, your        │  reverse proxy (yours)       │
   certificates ───▶│  :443                        │
                    └──────┬────────────────┬──────┘
                           │                │
              127.0.0.1:3000          127.0.0.1:8000
                    ┌──────▼──────┐   ┌─────▼────────┐
                    │  frontend   │   │  backend     │
                    │  node       │   │  uvicorn     │
                    └─────────────┘   └──┬────────┬──┘
                                         │        │
                            ┌────────────▼──┐  ┌──▼────────┐
                            │  postgres     │  │  redis    │
                            │  (no host     │  │  (no host │
                            │   port)       │  │   port)   │
                            └───────────────┘  └───────────┘
```

Five services in `docker-compose.prod.yml`. Four run continuously; the fifth,
`migrate`, runs `alembic upgrade head` once and exits before the backend
starts.

Both application images are built from the same Dockerfiles the development
stack uses, at a different target:

| | development target | production target |
| --- | --- | --- |
| backend | `requirements-dev.txt`, source bind-mounted, `uvicorn --reload` | `requirements.txt`, source baked in, no reloader, graceful shutdown |
| frontend | `npm ci`, source bind-mounted, `next dev` | `output: "standalone"` server, no npm tree, no source, no toolchain |

One file per service rather than two, so the base image, the language version
and the unprivileged user cannot drift apart between them.

---

## Before you start

You need:

- A host with Docker Engine and the Compose plugin.
- A TLS-terminating reverse proxy in front (Caddy, nginx, Traefik - this
  repository does not pick one). The containers publish to `127.0.0.1` only.
- A model provider account for whichever of Anthropic or OpenAI you select.
  Only the selected one is needed.
- Somewhere to put database backups that is not this host.

Decide two things now, because both are awkward to change later:

**The public API address.** `NEXT_PUBLIC_API_URL` is compiled into the browser
bundle when the frontend image is built. Changing it means rebuilding that
image - see [Frontend configuration](#frontend-configuration).

**Where PostgreSQL lives.** The bundled `postgres` service is there so the
stack stands up on one host. A managed database is the better answer wherever
one is available: it takes backups, patches itself, and survives the host. To
use one, point `DATABASE_URL` at it and delete the `postgres` service and the
two `depends_on` entries that name it.

---

## Configuration

```bash
cp .env.production.example .env.production
$EDITOR .env.production
```

`.env.production` is git-ignored. `.env.production.example` is not, and
contains no value that works anywhere: every secret in it is the literal string
`REPLACE_ME`, with the command that generates a real one above it.

Compose reads the file and passes the values in as environment variables. **No
file of secrets is ever copied into an image** - both `.dockerignore` files
exclude `.env` and `.env.*` before Docker sees the build context, so a
developer with a populated `.env` on disk cannot bake it into a published
image.

### What must be supplied

These have no default in `docker-compose.prod.yml`. Compose refuses to start
the stack - naming the variable - rather than falling back to something
published in this repository:

| Variable | Why it cannot have a default |
| --- | --- |
| `JWT_SECRET_KEY` | Signs every access token. Anyone holding it can mint a token for any user in any organization. |
| `DATABASE_URL` | The development password is printed in this repository. |
| `POSTGRES_USER`, `POSTGRES_PASSWORD` | Same. |
| `CORS_ORIGINS` | Decides which sites may call the API as a signed-in user. |
| `NEXT_PUBLIC_API_URL` | A wrong value produces a frontend that cannot reach its backend, and a build to discover it. |

Generate the secrets, do not invent them:

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"   # JWT_SECRET_KEY
python -c "import secrets; print(secrets.token_urlsafe(32))"   # passwords, METRICS_TOKEN
```

### What the application refuses to start on

Configuration mistakes fail at start-up, with a message, rather than producing
a deployment where nothing appears to be wrong. In `production` and `staging`:

| Refusal | Because |
| --- | --- |
| `JWT_SECRET_KEY` is the development default, or shorter than 32 characters | It is in the repository, so it is not a secret; a short one is brute-forceable. |
| `DATABASE_URL` contains `aiops_dev_password` | Also in the repository. |
| `DEBUG` is true | Deployment is not development. |
| `CORS_ORIGINS` contains `*` | The API sends credentials, and a wildcard with credentials means Starlette echoes back whatever origin asked - so any site may call this API as whoever is signed in. An empty value is allowed and means no browser origin is trusted. |
| The selected provider's API key is missing | The failure would otherwise arrive on the first model call, at a customer. |
| `METRICS_ENABLED` is true and `METRICS_TOKEN` is empty | There is no unauthenticated metrics mode, in any environment. |
| `LLM_PRICING` does not parse | A malformed price book reports "cost unavailable" on every report, which is indistinguishable from the legitimate unpriced case and therefore invisible. |

---

## First deployment

```bash
# 1. Build both images. The frontend build argument is compiled in.
docker compose --env-file .env.production -f docker-compose.prod.yml build

# 2. Start. The migrate job runs first and the backend waits for it.
docker compose --env-file .env.production -f docker-compose.prod.yml up -d

# 3. Watch it come up.
docker compose --env-file .env.production -f docker-compose.prod.yml ps
docker compose --env-file .env.production -f docker-compose.prod.yml logs -f backend
```

Then check it from the host:

```bash
curl -fsS http://127.0.0.1:8000/health          # process is up
curl -fsS http://127.0.0.1:8000/health/ready    # dependencies answer
```

`/health` touches no dependency: it answers while PostgreSQL is down, which is
what makes it a liveness probe. `/health/ready` checks PostgreSQL, and Redis
when `REDIS_REQUIRED` is true, and answers `503` with
`{"status": "degraded", ...}` while either is unreachable. Neither response
contains a credential, a connection string or a hostname.

The container health check uses **`/health/ready`**, so a container that has
lost its database stops being reported healthy - and `depends_on:
service_healthy` keeps the frontend from starting in front of a backend that
cannot serve.

Set up an account through the UI or the API as you would locally; there is no
deployment-time user seeding, and no default account exists.

---

## Migrations

**One mechanism: Alembic.** The application does not migrate on start-up, and
nothing else creates tables. Migrations run as their own Compose service, once:

```yaml
migrate:
  command: ["alembic", "upgrade", "head"]
  depends_on:
    postgres: {condition: service_healthy}
```

and the backend will not start until it has exited successfully:

```yaml
backend:
  depends_on:
    migrate: {condition: service_completed_successfully}
```

That separation is the point. Application containers that migrated on start-up
would mean *N* concurrent `alembic upgrade head` runs against one database
whenever *N* containers started together, and the outcome of that race depends
on which lock Alembic happened to take.

To run it by hand - before a deployment, or to see what is pending:

```bash
C="docker compose --env-file .env.production -f docker-compose.prod.yml"

$C run --rm migrate alembic current        # where the database is
$C run --rm migrate alembic heads          # where the code is
$C run --rm migrate alembic history        # how it got there
$C run --rm migrate alembic upgrade head   # apply
```

### Rolling back

**Downgrades are not automated, and that is deliberate.** A downgrade drops
columns, and a rollback that destroys data is worse than the bug it was
reverting. There is no `alembic downgrade` anywhere in the deployment path.

If a migration must come out:

1. Take a backup first (below). Always, even when the downgrade looks trivial.
2. Stop the application: `$C stop backend`.
3. Run the downgrade explicitly, one revision at a time:
   `$C run --rm migrate alembic downgrade -1`.
4. Deploy the previous image.

For anything that has dropped a column, restoring the backup is usually the
faster and more honest path.

### Writing migrations that deploy safely

The stack stops the old container before starting the new one, so a brief
interruption is expected and old and new code do not overlap. If you move to a
rolling deployment they will overlap, and then only additive migrations are
safe in one step: add a nullable column, deploy code that writes it, backfill,
then make it `NOT NULL` in a second migration.

---

## Workers and metrics

`WEB_CONCURRENCY` defaults to **1**, and that is a decision rather than an
oversight.

The metrics registry is **in-process**. Each uvicorn worker keeps its own
counters, and a scrape of `/metrics` is answered by whichever worker the
kernel handed the connection to. Four workers therefore do not produce four
times the numbers - they produce one worker's numbers, chosen at random, every
time. Running multiple workers does not make a single-process registry globally
accurate, and nothing in this repository pretends otherwise.

So:

- **Scale by running more containers,** each with one worker, each scraped in
  its own right, and sum the series in the monitoring system. That is where a
  fleet total belongs.
- Raise `WEB_CONCURRENCY` only if you have turned `METRICS_ENABLED` off, or
  accept that the metrics are a sample of one worker.
- Per-tenant figures are *not* metrics and are unaffected by any of this. Usage
  is aggregated from the execution tables at read time by
  `GET /api/v1/usage/...`, authenticated and tenant-scoped, and is correct
  regardless of how many processes served the requests.

The same applies to nothing else: conversations, runs, approvals, workflow
state and usage all live in PostgreSQL, so every worker and every container
sees the same thing.

---

## Graceful shutdown

On `SIGTERM` the backend stops accepting connections, lets in-flight requests
finish, and then runs the lifespan shutdown that disposes the database pool and
closes the Redis client.

- `--timeout-graceful-shutdown 30` in the image: how long in-flight requests
  get. An agent step or a tool call happening at that moment needs the window.
- `stop_grace_period: 40s` on the service: how long Compose waits before
  `SIGKILL`. Longer than uvicorn's number on purpose, so the server finishes
  the drain it started rather than being killed in the middle of it.
- `stop_grace_period: 60s` on PostgreSQL. The default ten seconds is how a
  checkpoint becomes a recovery.

What a killed request costs: an agent or workflow run that was mid-flight is
left `running` with nothing to continue it, and the abandonment sweep
(`AGENT_RUN_STALE_AFTER_SECONDS`, default 900) marks it failed with
`agent_run_abandoned` or `workflow_run_abandoned`. Runs that are
`awaiting_approval` are never swept - they are paused on purpose, and a
deployment does not cancel somebody's pending decision.

---

## Maintenance jobs

**There is no background worker.** That is a design decision and it has one
operational consequence: work that would normally be done by a scheduler is
done instead by the next request that looks at the relevant rows, and two
things need a nudge for organizations that nobody has visited.

Both ship in the production image and both are safe to run repeatedly.

```bash
C="docker compose --env-file .env.production -f docker-compose.prod.yml"

# Lapse approvals nobody answered, and fail the runs they were holding.
$C run --rm backend python -m scripts.expire_approvals --dry-run
$C run --rm backend python -m scripts.expire_approvals

# Fail runs whose HTTP request died and which nothing is driving any more.
$C run --rm backend python -m scripts.sweep_abandoned_runs --dry-run
$C run --rm backend python -m scripts.sweep_abandoned_runs
```

Run both `--dry-run` first: each prints what it would change and touches
nothing.

A reasonable schedule from the host's cron is hourly for the sweep and daily
for the expiry, but neither is urgent. What each one will and will not do:

- **Nothing is resumed.** An abandoned run may have stopped inside a model call
  or inside a tool, and nothing can tell from here whether that tool's side
  effect happened. Such runs are marked `failed` with `agent_run_abandoned` and
  a person decides what to do about them.
- **Expiring is not deciding.** A lapsed approval becomes `expired`, never
  `rejected` - nobody refused it - and the gated action does not run, then or
  ever after. The run it paused fails with `approval_expired`.
- **Runs awaiting approval are never swept**, however old. They are paused
  because somebody was asked and has not answered.

`run --rm backend` starts a one-off container from the same image with the same
configuration; it does not disturb the running one.

---

## Reverse proxy and TLS

Both containers publish to `127.0.0.1` only. Terminate TLS in front of them and
forward:

| Public | Upstream |
| --- | --- |
| `https://console.example.com/` | `http://127.0.0.1:3000` |
| `https://api.example.com/` | `http://127.0.0.1:8000` |

The backend runs with `--proxy-headers`, so it reads `X-Forwarded-Proto` and
`X-Forwarded-For` to build correct URLs and to log the real client address.
Which upstreams may set those headers is `FORWARDED_ALLOW_IPS`, defaulting to
the Docker bridge range so the proxy on this host is trusted and a request that
reached the container another way is not.

**Do not set `FORWARDED_ALLOW_IPS=*`** on a host where anything else can reach
the container. `X-Forwarded-For` is what the application believes about who is
calling, and a trusted wildcard means the client decides that.

Two headers worth setting at the proxy, which this application cannot set for
you because it does not serve the browser origin: `Strict-Transport-Security`
and a `Content-Security-Policy` for the frontend host.

---

## Observability

### Logs

`LOG_FORMAT=json` in a deployment: one JSON object per line on stdout, which is
what the Docker log driver collects and what an aggregator reads. The driver is
capped at `10m × 5` files per service, so a chatty week cannot fill the disk
the database is on.

Every line carries `request_id` - returned to the client as `X-Request-ID`, so
a user's bug report can be matched to server logs - and `trace_id` when tracing
is enabled.

What the logs deliberately do not contain: prompts, model responses, tool
arguments, tool results, conversation content, credentials, authorization
headers or idempotency keys. Secrets are `SecretStr`, so a settings object
reaching a log or a traceback prints `**********` rather than a key.

uvicorn's own access log is off in the production image: the request-context
middleware already logs one line per request, with the correlation id on it,
and uvicorn's line is the same request again.

### Metrics

`GET /metrics` is off unless `METRICS_ENABLED=true`, and there is no
unauthenticated mode - enabling it without `METRICS_TOKEN` refuses to start.
The scraper presents `Authorization: Bearer <token>`, compared in constant
time.

The exposition is Prometheus text format and carries **no tenant dimension and
no per-execution identifier**. An operator watching the fleet may legitimately
not be entitled to any tenant's business data, and "which organization is
spending the most" is exactly that; per-execution ids are unbounded, which is
how a metrics process runs out of memory. Label values come from closed sets in
`app/observability/names.py`, with an `other` overflow bucket and a hard series
cap.

### Tracing

Off unless `TRACING_ENABLED=true`. When on, a request produces a tree of spans,
each emitted as a structured log record carrying OpenTelemetry's field names and
id formats - `trace_id`, `span_id`, `parent_span_id`, span name, kind and
status.

The boundaries that are instrumented are the ones that answer "where did the
time go":

```
POST /agents/{agent_id}/run          server span, from the middleware
└── agent.run                        one request's advance of a run
    ├── llm.generate_structured      one gateway call, retries included
    ├── tool.execute                 one tool, however it ended
    └── llm.generate_structured

POST /{approval_id}/approve
└── approval.decide                  which way it went
    └── agent.run                    the run that was waiting, carrying on

POST /workflows/{workflow_id}/runs
└── workflow.run
    ├── workflow.step
    │   └── tool.execute
    └── workflow.step
```

No layer was given a tracer to carry: the middleware binds one to the request's
context and every span below finds it there, which is OpenTelemetry's implicit
context propagation and is why adding this changed no constructor signature in
Parts 12-19.

- **W3C trace context is honoured.** An incoming `traceparent` continues the
  caller's trace. The header is validated strictly - version, lower-case hex,
  correct lengths, not the reserved all-zero ids - because it is the one piece
  of trace data a client controls. A malformed one starts a fresh trace rather
  than failing the request.
- **Attributes are an allow-list**, in `app/observability/names.py` - 23 keys,
  short enough that a reviewer can read all of them. They describe the service,
  the HTTP request, a lifecycle state, a token count, a tool's registered name
  and outcome, and a stable error code. A key not on that list is dropped
  before a span exists, so putting a prompt, a tool argument, a tenant id or a
  run id into a trace requires editing that file.
- **A UUID-shaped value is dropped whatever key it arrives under.** Every
  identifier in this platform is a UUID, so this holds even if the allow-list
  is later wrong - and nothing legitimately permitted can contain one.
- **Span names are bounded**: the method and the matched route *template* for
  HTTP, a fixed literal everywhere else. `GET /runs/{run_id}`, never
  `GET /runs/<a uuid>`; `workflow.step`, never the step key a workflow author
  chose.
- **Sampling is deterministic on the trace id** (`TRACING_SAMPLE_RATIO`), so
  every service that sees one trace reaches the same decision. A trace whose
  caller already decided to record it is recorded regardless: half a trace is
  worse than none.
- **A failing exporter cannot fail a request.** Emission is wrapped so no
  exporter error escapes, by construction rather than by operational care.

There is no OTLP exporter and no OpenTelemetry SDK in `requirements.txt`. That
is a deliberate trade: the platform's observability has no third-party
dependency, and this keeps it. To ship spans to a collector, either point the
collector's `filelog` receiver at these records, or add a `Tracer` subclass -
the only method it needs is `_emit`, and everything that makes a span safe is
in the base class where a new exporter cannot route around it.

---

## Secrets

- `.env.production` on the host, `600`, owned by the deploying user. Never
  committed; `.env.*` is git-ignored and the two `.example` templates are named
  back in one at a time, so a new `.env.<anything>` is ignored by default.
- Never copied into an image. Never echoed at start-up. Never in a health
  response, a metric, a span or a log.
- Nothing secret under `NEXT_PUBLIC_*`. Those values become string literals in
  JavaScript that every visitor downloads, and are also recorded in the image's
  build history.
- To rotate `JWT_SECRET_KEY`: change it and restart the backend. Everyone is
  signed out, which is the intended effect.
- To rotate a provider key: change it and restart. In-flight model calls fail
  and are retried by the gateway.
- To rotate the database password: change it in PostgreSQL, then in
  `DATABASE_URL`, then restart. There is no dual-credential window.

---

## Deploying a new version

```bash
C="docker compose --env-file .env.production -f docker-compose.prod.yml"

git pull
$C build                    # rebuild both images
$C run --rm migrate alembic upgrade head    # optional: migrate before the swap
$C up -d                    # recreate what changed
```

`up -d` recreates only the services whose image or configuration changed.
Expect a short interruption on the backend while the old container drains and
the new one passes its health check.

If the frontend's API address changed, that image must be rebuilt - and only a
rebuild will do it, because the value is compiled in.

To roll back, deploy the previous image tag. Pin `BACKEND_IMAGE` and
`FRONTEND_IMAGE` to something that identifies the build - a version or a commit
- as soon as images are published to a registry rather than built on the host;
`:prod` is fine for one host and useless for a rollback.

---

## Backups

Nothing here takes one for you. PostgreSQL holds every conversation, run,
approval, workflow and audit record; the Redis volume holds nothing that cannot
be rebuilt.

```bash
C="docker compose --env-file .env.production -f docker-compose.prod.yml"

# Dump, compressed, from the running container.
$C exec -T postgres pg_dump -U "$POSTGRES_USER" -Fc aiops > aiops-$(date +%F).dump

# Restore into an empty database.
$C exec -T postgres pg_restore -U "$POSTGRES_USER" -d aiops --clean --if-exists \
  < aiops-2026-01-01.dump
```

Put the dumps somewhere that is not this host, and restore one somewhere else
occasionally. A backup nobody has restored is a hypothesis.

---

## Frontend configuration

`NEXT_PUBLIC_*` values are substituted into the browser bundle **while it is
built**. They are build arguments, not runtime environment:

```bash
docker compose --env-file .env.production -f docker-compose.prod.yml build \
  --build-arg NEXT_PUBLIC_API_URL=https://api.example.com frontend
```

Two consequences:

- A published image points at whatever address it was built with. Re-pointing
  it means building again.
- Nothing secret may be passed this way. It ends up in the JavaScript and in
  the image's build history.

`NEXT_PUBLIC_API_URL` is the address **the browser** uses, so it is a public
URL - not `http://backend:8000`, which only exists inside the Compose network.

The access token is held in memory only. A page reload signs the user out, and
there is no token in `localStorage`, `sessionStorage`, `indexedDB` or a cookie
for a script to read.

---

## When something is wrong

| Symptom | Look at |
| --- | --- |
| `up` fails naming a variable | `.env.production` is missing that value. Compose refused rather than defaulting. |
| Backend exits immediately, log names a setting | One of the start-up refusals above. The message says which and what to do. |
| `/health/ready` is 503 | `docker compose ps`: PostgreSQL or Redis is not healthy. The response says which dependency. |
| Backend healthy, frontend cannot reach it | `NEXT_PUBLIC_API_URL` was wrong when the image was built, or the proxy is not forwarding to `127.0.0.1:8000`. |
| Browser calls blocked by CORS | `CORS_ORIGINS` must list the frontend's exact public origin, scheme included. |
| `/metrics` returns 404 | `METRICS_ENABLED` is false. |
| `/metrics` returns 401 | Wrong or missing `Authorization: Bearer <METRICS_TOKEN>`. |
| `/docs` returns 404 | `DOCS_ENABLED` is false, which is the default here. |
| Runs stuck `running` after a restart | Expected: there is no background worker. The abandonment sweep fails them after `AGENT_RUN_STALE_AFTER_SECONDS`. |
| Cost shows as unknown | `LLM_PRICING` is empty, or has no entry for that model. Usage is still reported in full. |

Correlating one report with the logs: the client's `X-Request-ID` is the
`request_id` on every line the request produced.

---

## What this deliberately does not include

- **Kubernetes, Helm, Terraform.** The repository's platform is Compose.
- **An OTLP exporter.** See [Tracing](#tracing).
- **A CD pipeline.** `.github/workflows/ci.yml` tests, type-checks, builds both
  production images and validates both manifests. It does not deploy: pushing
  an image or touching a host needs registry and host credentials, and
  inventing a pipeline for infrastructure that does not exist yet would be
  worse than this document.
- **A reverse proxy.** Deliberate: the choice belongs to the host, and a
  half-configured one shipped here would be trusted more than it deserves.
- **Automated backups**, alerting rules, or a log aggregator. All three depend
  on infrastructure this repository does not own.
- **Horizontal scaling of the bundled PostgreSQL.** Use a managed database.
