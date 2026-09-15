## What this changes

## Why

<!-- The diff says what. This says why it is worth doing. -->

## Verification

<!-- What you actually ran, and the result. Say what you could not check. -->

```
backend:   ruff check app tests scripts / ruff format --check / mypy / alembic check / pytest
frontend:  npm run lint / npm run typecheck / npm test / npm run build
```

- Integration tests: <!-- ran against PostgreSQL / skipped, no database -->
- Could not verify: <!-- e.g. Docker build, live provider call, browser -->

## Does this touch any of these?

- [ ] Tenant isolation or authorization
- [ ] Approval semantics, or when a destructive tool may run
- [ ] Agent, workflow or tool execution behaviour
- [ ] Database schema — migration included, `alembic check` clean
- [ ] What metrics, traces or logs contain
- [ ] An API response contract
- [ ] A new dependency — say what the standard library could not do

<!--
Any box ticked is fine. It just tells the reviewer where to look hardest.

Please do not delete a test, weaken an assertion, or change an expectation to
make something pass. If a test contradicts a documented contract, say so here.
-->
