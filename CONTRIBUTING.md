# Contributing

Thanks for looking. This is a solo project developed in public; issues,
questions and pull requests are all welcome, and none of them need to be large.

If you are unsure whether something is wanted, open an issue first. A short
description of the problem is more useful than a finished patch that solves a
different one.

## Getting set up

See the quick start in the [README](README.md#quick-start). The short version:

```bash
# Backend
cd backend
py -3.12 -m venv .venv          # python3 -m venv .venv on macOS/Linux
.venv\Scripts\activate          # source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env            # then set DATABASE_URL
alembic upgrade head

# Frontend
cd frontend
npm install
```

Integration tests need a reachable PostgreSQL. Without one they skip themselves
rather than fail, so a passing run on a machine with no database has proved
less than it looks — check the skip count.

### Something to work against

The repository ships a demo dataset — two organizations with customers,
shipments, charges and invoices — so there is data for an agent to answer
questions about:

```bash
cd backend
python -m scripts.seed_demo_data --attach-user you@example.com
```

It is idempotent (every row's id derives from its business key) and refuses to
run unless `APP_ENV=development`. Register the account first; the seeder
attaches an existing one, it does not create users.

The walkthrough that exercises the interesting paths — a two-tool answer, then
a destructive request that stops for approval — is in
[docs/demo.md](docs/demo.md), with a condensed version in the
[README](README.md#see-it-working). Running an agent needs a model provider
key; everything else does not.

Two maintenance scripts exist alongside it, both safe to run repeatedly and both
supporting `--dry-run`:

```bash
python -m scripts.expire_approvals --dry-run
python -m scripts.sweep_abandoned_runs --dry-run
```

## Before you open a pull request

Run what CI runs. It is the same set of commands, not a CI-only variant:

```bash
cd backend
ruff check app tests scripts
ruff format --check app tests scripts
mypy
alembic check
pytest

cd ../frontend
npm run lint
npm run typecheck
npm test
npm run build
```

Two findings predate the current CI configuration — an import-order rule and an
unused `noqa` in `backend/alembic/env.py`, and a code block in
`backend/README.md` that a newer `ruff` would reformat. The lint steps are
scoped to `app tests scripts` because of them. If you fix those files, widen
the scope in the same pull request.

## What the codebase expects

These are not style preferences; they are the properties the tests exist to
protect. A change that breaks one of them will be asked about.

- **Every function is typed.** `mypy` runs strict, with no untyped definitions.
- **The tenant filter is written in exactly one place** — the scoped repository
  base class. Do not add an `organization_id` comparison anywhere else.
- **Model output is untrusted input.** It names a tool and supplies arguments;
  both are validated before anything executes.
- **No transaction is held across an LLM call, a tool call, or a human wait.**
- **Concurrency is settled in the database**, by conditional UPDATE and
  `rowcount`, not by reading state and then acting on it.
- **Telemetry carries no identity.** No tenant id, no execution id, no prompt,
  no tool argument, no result — in a log, a metric or a span. There is an
  allow-list; adding to it is a deliberate change with its own review.
- **Money is `Decimal`,** serialised as a string on the wire. Never a float.
- **The browser stores nothing.** No token in `localStorage`, `sessionStorage`,
  cookies or IndexedDB.

## Tests

Tests here assert behaviour at a boundary, not line coverage. The useful
question for a new test is "what would break in production if this were wrong?"

- Backend: `pytest`. Unit tests need nothing; integration tests need
  PostgreSQL and are marked accordingly.
- Frontend: Node's built-in test runner, no test framework dependency. Only
  `.ts` modules are testable this way — which is why logic lives in `.ts` files
  and the React bindings in separate `.tsx` files.

Please do not delete a test, weaken an assertion, or change expected behaviour
to make something pass. If a test contradicts a documented contract, say so in
the issue or pull request — that is worth knowing about.

## Database changes

Migrations are Alembic, and the history is linear:

```bash
alembic revision --autogenerate -m "what changed"
alembic upgrade head
alembic check        # must report no new upgrade operations
```

- Every tenant-owned table carries `organization_id`, and a reference to
  another tenant-owned row uses the composite `(id, organization_id)` foreign
  key so a cross-tenant reference cannot be represented.
- Do not edit a migration that has already been committed.
- Say in the pull request whether the change is safe to apply while an older
  version of the application is still running.

## Dependencies

Prefer none. The observability stack, the frontend test suite and the metrics
endpoint are all written without one deliberately. A new dependency needs a
reason in the pull request: what it does that the standard library or the
existing framework cannot.

## Commits and pull requests

- One coherent change per pull request. Unrelated cleanup is easier to review
  separately.
- Explain *why* in the description, not just what — the diff already says what.
- Say what you ran and what the result was, including anything you could not
  verify. "I could not test the Docker build, no daemon here" is a useful
  sentence.

## Where to read first

If you are trying to understand the shape of the thing before changing it:

- [docs/evaluation.md](docs/evaluation.md) — where the trust boundary is, how
  isolation and approvals work, and what to test first.
- [docs/architecture.md](docs/architecture.md) — the design and the reasoning.
- `backend/app/tools/executor.py` — the trust boundary in one file.
- `backend/app/repositories/tenant.py` — the one place the tenant filter lives.

## Reporting bugs

Open an issue with what you expected, what happened, and enough to reproduce
it: the setup you used (Docker or native), the commands you ran, and the
response you got. If the API returned an error, the `X-Request-ID` header ties
it to the server-side log line.

**Do not report a security vulnerability in a public issue.** See
[SECURITY.md](SECURITY.md).

## License

Contributions are accepted under the MIT license that covers the project.
