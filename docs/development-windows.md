# Native Windows development

How to run the platform directly on Windows, without Docker.

Docker remains fully supported and is still the reference environment — see
[the README](../README.md) for that path. Nothing in this guide replaces it;
the two setups read the same code and the same settings, just with different
hosts and a different `.env` file.

---

## 1. Prerequisites

| Tool | Version | Notes |
| --- | --- | --- |
| Python | 3.12 | Matches the container image. Tick "Add python.exe to PATH" in the installer. |
| Node.js | 20 LTS or newer | Ships with npm. |
| PostgreSQL | 16 or 17 | The Windows installer includes `psql` and pgAdmin. |
| Git | any recent | |
| Redis | — | **Not required.** See section 6. |

Check what you have:

```powershell
py --list          # installed Python versions
node --version
npm --version
psql --version
```

If `psql` is not found, add the PostgreSQL `bin` directory to your `PATH` — by
default `C:\Program Files\PostgreSQL\17\bin`.

---

## 2. One-time PostgreSQL setup

The backend expects a database `aiops` owned by a role `aiops`. Create them
once, using the superuser password you set during installation:

```powershell
psql -U postgres -c "CREATE ROLE aiops WITH LOGIN PASSWORD 'aiops_dev_password';"
psql -U postgres -c "CREATE DATABASE aiops OWNER aiops;"
```

Then enable the extensions the platform will use:

```powershell
psql -U postgres -d aiops -c "CREATE EXTENSION IF NOT EXISTS pgcrypto;"
psql -U postgres -d aiops -c "CREATE EXTENSION IF NOT EXISTS pg_trgm;"
```

`pgcrypto` and `pg_trgm` ship with the standard Windows installer.

> **`vector` is deliberately not created here.** pgvector is not bundled with
> the Windows installer and must be compiled separately. Nothing uses it until
> the document-retrieval phase, so skip it for now. The Docker stack creates it
> automatically via
> [infrastructure/postgres/init/01-extensions.sql](../infrastructure/postgres/init/01-extensions.sql).

Confirm the connection works:

```powershell
psql -U aiops -d aiops -c "SELECT version();"
```

---

## 3. Backend

From the repository root:

```powershell
cd backend

py -3.12 -m venv .venv          # creates backend\.venv
.venv\Scripts\activate          # PowerShell or cmd.exe

pip install -r requirements-dev.txt

copy .env.example .env          # optional - defaults already target localhost

alembic upgrade head            # create the schema

uvicorn app.main:app --reload
```

The API is then on <http://localhost:8000>, with interactive docs at
<http://localhost:8000/docs>.

Re-run `alembic upgrade head` whenever you pull changes that add a migration.

### About the virtual environment

- `backend\.venv` is git-ignored; it is yours alone and never committed.
- Activate it in **every new terminal** before running `pip`, `uvicorn`,
  `pytest` or `ruff`. Your prompt shows `(.venv)` when it is active.
- Deactivate with `deactivate`.
- To start over, delete the folder and recreate it:
  `rmdir /s /q .venv` (cmd) or `Remove-Item -Recurse -Force .venv` (PowerShell).
- Activation differs slightly by shell, though `.venv\Scripts\activate` works in
  both:

  | Shell | Command |
  | --- | --- |
  | PowerShell | `.venv\Scripts\Activate.ps1` |
  | cmd.exe | `.venv\Scripts\activate.bat` |
  | Git Bash | `source .venv/Scripts/activate` |

- If PowerShell refuses with *"running scripts is disabled on this system"*,
  allow it for the current terminal only:

  ```powershell
  Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
  ```

- Without a 3.12 interpreter, `python -m venv .venv` uses whatever `python`
  resolves to. Newer versions work, but 3.12 is what the container runs and
  what CI will target, so prefer installing it.

### Configuration

Settings come from environment variables, with a `.env` file in `backend\`
read automatically. Every setting already defaults to a localhost-friendly
value, so `.env` is only needed to change something — see
[backend/.env.example](../backend/.env.example) for the full list.

`DATABASE_URL` is the one to check if PostgreSQL is not on the default port or
you chose different credentials:

```
DATABASE_URL=postgresql+asyncpg://aiops:aiops_dev_password@localhost:5432/aiops
```

---

## 4. Frontend

In a second terminal, from the repository root:

```powershell
cd frontend

npm install

copy .env.example .env.local    # optional - defaults point at localhost:8000

npm run dev
```

The homepage is then on <http://localhost:3000>. It shows a live indicator for
backend reachability, so with both terminals running you should see the API
reported as reachable.

Only `NEXT_PUBLIC_*` variables reach the browser. Never put a secret in
[frontend/.env.example](../frontend/.env.example) or `.env.local`.

---

## 5. Verifying the setup

```powershell
curl http://localhost:8000/health
```

```json
{"status":"ok","service":"AI Operations Agent Platform","version":"0.1.0","environment":"development"}
```

```powershell
curl http://localhost:8000/health/ready
```

With PostgreSQL running and no Redis installed, this returns **200**:

```json
{
  "status": "ready",
  "dependencies": {
    "postgres": {"status": "ok", "required": true},
    "redis": {"status": "unavailable", "required": false}
  }
}
```

A `503` means a **required** dependency is down — in practice PostgreSQL. Check
that the service is running (`Get-Service postgresql*`) and that `DATABASE_URL`
matches your installation.

---

## 6. Redis is optional

No implemented feature uses Redis yet, so it does not need to be installed and
there is no good native Windows build of it. The backend:

- never connects at startup — connections are lazy, so it boots with Redis absent;
- reports Redis as `unavailable` on `/health/ready` without failing the check;
- logs the absence at INFO rather than WARNING.

This is controlled by `REDIS_REQUIRED`, which is `false` by default and `true`
in the Docker stack, where Redis is genuinely part of the environment. When a
feature starts depending on Redis — caching, rate limiting, the background job
queue — flip it to `true` and run Redis under WSL2 or use a managed instance.

---

## 7. Day-to-day commands

Backend, with the virtual environment active:

```powershell
uvicorn app.main:app --reload        # run the API
alembic upgrade head                 # apply migrations
alembic revision --autogenerate -m "add tenants"
pytest                               # full test suite
pytest tests\unit                    # unit tests only, no database needed
pytest -m integration                # only the tests needing PostgreSQL
ruff check app tests                 # lint
ruff format app tests                # format
mypy                                 # type check
```

Frontend:

```powershell
npm run dev                          # dev server with hot reload
npm run build                        # production build
npm run typecheck                    # tsc --noEmit
```

Integration tests skip themselves when PostgreSQL is unreachable, so the suite
passes on a machine that has not finished setup yet. With PostgreSQL running
they execute for real.

---

## 8. Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| `'psql' is not recognized` | PostgreSQL `bin` not on `PATH`. Add `C:\Program Files\PostgreSQL\17\bin`. |
| `running scripts is disabled on this system` | PowerShell execution policy. Run `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass`, or use `.venv\Scripts\activate.bat`. |
| `/health/ready` returns 503 with `postgres: unavailable` | PostgreSQL is not running, or `DATABASE_URL` is wrong. Test with `psql -U aiops -d aiops -c "SELECT 1;"`. |
| `password authentication failed for user "aiops"` | The role password does not match `DATABASE_URL`. Reset it: `psql -U postgres -c "ALTER ROLE aiops WITH PASSWORD 'aiops_dev_password';"` |
| `database "aiops" does not exist` | Section 2 was not completed. |
| `ModuleNotFoundError: No module named 'app'` | Run `uvicorn` and `pytest` from the `backend` directory, not the repository root. |
| `pip` installs into the wrong Python | The virtual environment is not active. Check for `(.venv)` in the prompt; confirm with `where python`. |
| Port 8000 or 3000 already in use | Find the owner with `netstat -ano \| findstr :8000`, then stop it or run on another port (`uvicorn ... --port 8001`, `npm run dev -- -p 3001`). |
| Frontend shows the backend as unreachable | The backend is not running, or `NEXT_PUBLIC_API_URL` is wrong. `NEXT_PUBLIC_*` values are baked in at build time — restart `npm run dev` after changing them. |
| `CREATE EXTENSION "vector"` fails | Expected on Windows. pgvector is not needed yet; skip it. |
| `alembic: command not found` | The virtual environment is not active. |
| Alembic fails with `Can't locate timezone: UTC` | `tzdata` is missing; `pip install -r requirements-dev.txt` installs it. |
| Alembic cannot connect | It reads `DATABASE_URL` from the same settings as the app, so fix that and both follow. |

---

## 9. Which environment file is which

Three templates exist, for three different situations. Copy the one that
matches how you are running the stack.

| File | For | Hosts | Copy to |
| --- | --- | --- | --- |
| [.env.example](../.env.example) | Docker Compose | `postgres`, `redis` | `.env` (repository root) |
| [backend/.env.example](../backend/.env.example) | Native backend | `localhost` | `backend\.env` |
| [frontend/.env.example](../frontend/.env.example) | Native frontend | `localhost` | `frontend\.env.local` |

All three are optional — every value is already a working default. Real `.env`
files are git-ignored and must never be committed.
