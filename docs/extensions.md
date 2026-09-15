# Extending the platform

Where to plug your own work in, and roughly what each extension costs.

The platform's value is not its five demo tools — those operate on a freight
dataset because the demo needed *a* domain. The value is everything around them:
the registry, the safety classes, the approval gate, the durable run records,
the tenancy and the observability. All of that is domain-agnostic and none of it
changes when you add your own.

**Nothing on this page is implemented for you.** Each section names the existing
extension point and the source file that defines it, and separates what the
framework already gives you from what you would have to write.

| | |
| --- | --- |
| **Extension point** | Exists today; your code plugs into it |
| **You write** | The work involved |
| **You get for free** | What the framework does around it, unchanged |

---

## 1. A read-only tool

The smallest useful change, and the one to do first.

**Extension point:** `Tool[InputT, OutputT]` in
[`backend/app/tools/base.py`](../backend/app/tools/base.py). The worked example
is `GetShipmentTool` in
[`backend/app/tools/business/shipment.py`](../backend/app/tools/business/shipment.py).

**You write:** a Pydantic input model, a Pydantic output model, a subclass with
`metadata`, `input_model`, `output_model` and an async `execute()`, and a line
registering it in `build_business_registry()`
([`app/tools/business/__init__.py`](../backend/app/tools/business/__init__.py)).

```python
class GetThingTool(BusinessTool[ThingInput, ThingOutput]):
    metadata = ToolMetadata(
        name="get_thing",
        description="What the model reads to decide whether to call this.",
        safety=ToolSafety.READ_ONLY,
        timeout_seconds=10.0,
    )
    input_model = ThingInput
    output_model = ThingOutput

    async def execute(self, arguments, context): ...
```

**You get for free:** the model being offered the tool, argument validation
against your schema, rejection of reserved argument names, the timeout, the
result-size bound, the durable `tool_executions` row, the metric, the span, and
the tool appearing in the console's activity list.

**Two things worth copying from the example.** Build repositories from
`context.organization_id`, never from anything in `arguments` — the context is
trusted, the arguments are not. And write the `description` for the model: it is
the only thing the model uses to decide whether this tool answers the question.

## 2. A destructive tool that requires approval

**Extension point:** the same one. The difference is one field.

**You write:** the same class with `safety=ToolSafety.DESTRUCTIVE`, plus an
`approval_summary` declaring which fields a human may be shown. `CancelShipmentTool`
in [`shipment.py`](../backend/app/tools/business/shipment.py) is the example.

**You get for free:** the run pausing at `awaiting_approval`, the approval row,
the inbox entry, the allow-listed summary, the admin-only decision, the
conditional-`UPDATE` claim that makes execution at-most-once, expiry, and the
resume that continues the same run whichever way the decision went.

**You cannot opt out.** A tool declared `destructive` without
`requires_approval` fails validation when it is defined — see the model
validator in [`app/tools/models.py`](../backend/app/tools/models.py). That is
deliberate: the check belongs at definition time, not at execution time.

**The summary is an allow-list, not a redaction.** You name the fields a person
may see; everything else is absent rather than masked. See
[`app/tools/summary.py`](../backend/app/tools/summary.py).

## 3. A new agent

**Extension point:** `AgentRegistry` in
[`backend/app/agents/registry.py`](../backend/app/agents/registry.py), which has
a `register()` method and a `from_settings()` that currently builds one agent.

**You write:** an `Agent` ([`app/agents/models.py`](../backend/app/agents/models.py))
with a name, a description, instructions and its limits.

**You get for free:** the tool-use loop, the step budget, run records, the
cancellation token, conversation persistence, and the agent appearing in the
console's picker.

**The honest limit.** Agents are built from settings at start-up and are
platform-level rather than tenant-owned — `organization_id` is `None` on the
built-in one. Per-tenant agent definitions stored in the database are **not
implemented**; `AgentRegistry.register()` exists and is used by tests, so the
seam is there, but the persistence and the API around it are not.

## 4. A workflow

Workflows are data, not code — you do not need to touch the repository to add
one.

**Extension point:** `POST /api/v1/ai/workflows` with a definition document,
validated by [`app/workflows/definition.py`](../backend/app/workflows/definition.py)
and executed by [`app/workflows/engine.py`](../backend/app/workflows/engine.py).

**You write:** a JSON definition with an entry step and a list of steps. Four
step types exist: `tool_call`, `agent_step`, `condition` and `approval`.

**You get for free:** validation at activation (acyclic, bounded, size-limited),
durable run and step-run state, data flowing between steps by reference,
approval gates at any step, resume after a decision, and the `/workflows`
screen.

**Start from the tests.** [`backend/tests/integration/workflow_helpers.py`](../backend/tests/integration/workflow_helpers.py)
builds definitions programmatically and is the clearest reference for the
document shape.

**Not implemented:** scheduling. A workflow run is started by a request; there
is deliberately no queue and no scheduler, so something outside the platform
starts it on a timetable.

## 5. Another model provider

**Extension point:** `LLMProvider` in
[`backend/app/ai/providers/base.py`](../backend/app/ai/providers/base.py) and
the `PROVIDERS` dict in
[`registry.py`](../backend/app/ai/providers/registry.py).

**You write:** a subclass with a `name`, `generate()` and
`generate_structured()`, translating every SDK exception into the existing
`app.ai.exceptions` vocabulary; a `PROVIDERS` entry; and the settings for its
credential and model.

**You get for free:** the retry policy, the timeout, token accounting, cost via
the price book, metrics, spans, and the error contract clients already parse.

**Two rules the existing adapters follow.** Only modules under
`app/ai/providers/` may import a vendor SDK — a test runs a fresh interpreter to
prove `import app.ai` pulls in neither. And no SDK exception may escape: the
`_translate` method has a catch-all branch so an unrecognised failure still
becomes an `LLMProviderError`.

This is also the seam for a **self-hosted model**. The gateway does not care
what is behind the adapter.

## 6. The console

**Extension point:** [`frontend/src/app/(app)/`](../frontend/src/app) for routes,
[`frontend/src/components/`](../frontend/src/components) for UI, and the single
API client in [`frontend/src/lib/api/`](../frontend/src/lib/api).

**You write:** a route, components, and typed calls through the existing client.

**You get for free:** the session, the guard on `(app)` routes, the tenant
header, the error envelope and its presentation mapping, and the design
primitives in `components/ui/`.

**Rules that are load-bearing.** One API client — no ad-hoc `fetch` in a
component. No token in `localStorage`, `sessionStorage`, cookies or IndexedDB.
Model output renders as plain text, never as markup. The backend decides what a
user may do; anything the UI decides is presentation.

**Testing.** The frontend suite is Node's built-in runner with no framework,
which cannot compile JSX — so testable logic lives in `.ts` modules and React
bindings in separate `.tsx` files. Follow that split or your logic is untestable
in this repository.

## 7. Domain integrations

**Extension point:** a tool. There is no separate integration layer, and that is
deliberate — an integration that is not a tool is an integration the approval
gate and the run record do not see.

**You write:** tools that call your systems, with the safety class that matches
what they do to the world.

**Worth deciding early:** `mutating` versus `destructive`. `mutating` executes
without asking; `destructive` cannot. "Hard or impossible to undo" is the line
the existing tools draw.

**Not implemented and relevant here:** rate limiting and per-tenant quotas.
Nothing bounds how often a tool is called or how much a tenant spends, so an
integration against a metered third-party API needs its own protection until
that exists.

## 8. Retrieval (RAG), later

The largest genuinely absent capability, listed because the placeholders are in
the repository and can mislead.

**What exists:** a `documents` table holding metadata
([`app/models/document.py`](../backend/app/models/document.py)), an empty
`app/knowledge/` package whose docstring says so, and PostgreSQL running the
`pgvector` image with the extension created at database init.

**What does not:** upload, storage, text extraction, chunking, embedding, vector
search — all of it.

**How it would attach:** as a tool. A `search_documents` tool declared
`read_only` needs no change to the runtime, the gate or the records. The work is
ingestion, not integration — and ingestion wants a background job, which the
platform deliberately does not have.

Design sketch: [architecture.md](architecture.md#8-retrieval-rag---not-built).

---

## Before you start

A few things that will otherwise surprise you:

- **Every function is typed.** `mypy` runs strict.
- **The tenant filter is written in exactly one place** — `TenantScopedRepository`
  in [`app/repositories/tenant.py`](../backend/app/repositories/tenant.py). Do
  not add an `organization_id` comparison anywhere else.
- **Prefer no new dependency.** The metrics registry, the tracing and the whole
  frontend test suite are dependency-free on purpose.
- **A schema change needs a migration**, and `alembic check` must come back
  clean. Tenant-owned tables carry `organization_id` and reference each other
  through composite `(id, organization_id)` foreign keys.

[CONTRIBUTING.md](../CONTRIBUTING.md) has the full list and the verification
commands. [evaluation.md](evaluation.md) explains the boundaries these rules
protect, which is worth reading before working around one.
