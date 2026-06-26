# Coding Standards — Message Gateway

> Conventions every contributor to this monorepo must follow. The
> standards below are **derived from the code that already ships in
> the scaffold**: if you ever find a conflict between this document
> and reality, treat the code as a bug and align both sides.

---

## 1. Repository layout

This is a **monorepo** with two top-level applications and shared
infrastructure at the root.

| Path                       | Purpose                                              |
| -------------------------- | ---------------------------------------------------- |
| `backend/`                 | Python 3.12 · FastAPI REST API                       |
| `backend/alembic/`         | Alembic migration scripts + async env                |
| `frontend/`                | Node 20 · Angular 18 dashboard (NgModules + Tailwind)|
| `docker-compose.yml`       | Local stack (postgres, redis, backend, frontend)     |
| `.github/workflows/ci.yml` | CI pipeline                                          |
| `Makefile`                 | Developer convenience targets                        |
| `backend/pyproject.toml`   | Ruff + Mypy configuration                            |
| `frontend/.eslintrc.json`  | ESLint configuration (Angular preset)                |
| `.env.example`             | Canonical list of environment variables              |
| `PRD.md`                   | Product requirements / architectural decisions       |
| `CODING_STANDARDS.md`      | This file                                            |
| `CONTRIBUTING.md`          | How to propose changes                               |
| `.editorconfig`            | Cross-editor formatting defaults                     |

Do not introduce new top-level directories without updating this
file and the `test_infra.py` fixture tests.

---

## 2. Python (backend)

### 2.1 Version and tooling

- **Python 3.12** in production (the Docker image uses
  `python:3.12-slim`). Local development on 3.11 is tolerated but
  keep code 3.12-compatible (no PEP 695 syntax in shared modules).
- Use `pydantic~=2.9` and `pydantic-settings~=2.6` for any data
  model or runtime setting.
- HTTP framework: **FastAPI** only. No Flask, no Starlette-only
  handlers.
- Linter / formatter: **Ruff** (`ruff check .`). Configuration
  lives in `backend/pyproject.toml` under `[tool.ruff]`.
- Static type checker: **Mypy** (`mypy`). Configuration lives in
  `backend/pyproject.toml` under `[tool.mypy]`. The `pydantic.mypy`
  plugin is enabled so model fields are inferred correctly.

### 2.2 Module conventions

- `app/` is an importable package; keep `__init__.py` short and
  docstring-only.
- Subpackages follow a fixed layout and are not optional:
  `app/routes/` (HTTP handlers), `app/models/` (ORM),
  `app/services/` (domain logic), `app/adapters/` (provider
  integrations), `app/observability/` (logging / metrics / PII
  redaction), `app/api/` (versioned routers that aggregate the
  subpackages). Cross-cutting singletons live at the top
  level: `app.db` (PostgreSQL engine), `app.redis_client`
  (cached async Redis client), and the
  `app.observability.logging` module (root-logger
  configuration). Every caller must go through these accessors
  – never instantiate a `Redis` / `Engine` directly inside a
  route handler.
- One concern per file. Routes, models, services, repositories
  belong in separate modules (e.g. `app/routes/messages.py`,
  `app/models/message.py`, `app/services/messaging.py`).
- Type hints on **every** public function and method. Prefer
  `from __future__ import annotations` at the top of new modules
  so forward references work without quotes.
- Use Pydantic models for request/response bodies and for any
  value that crosses a process boundary. Plain dicts are only
  acceptable inside a single private function.
- Async-first: route handlers, DB calls and external HTTP use
  `async def`. CPU-bound work must be offloaded to a worker pool
  (`run_in_executor`, Arq job, …).

### 2.3 Settings

- All environment variables are declared in `app/config.py` as
  `Field(alias="UPPER_SNAKE")` on the `Settings` class.
- Add new env-driven values to `Settings` **and** to
  `.env.example` in the same commit. The
  `test_env_example_declares_every_settings_alias` test will
  fail otherwise.
- Never read `os.environ` directly outside `app/config.py`.

### 2.4 Error handling

- Surface domain errors as HTTP exceptions (`HTTPException` /
  FastAPI's `APIRouter` exception handlers). Do not `raise
  Exception(...)` from a route handler.
- Catch `httpx.HTTPError` (and friends) at the provider adapter
  boundary; convert to a domain exception
  (`ProviderUnavailableError`, …) so route handlers can render
  it consistently.

### 2.5 Tests

- Test framework: `pytest` with `pytest-asyncio` in `auto` mode
  (see `pytest.ini`).
- Mirror the source tree: `app/foo/bar.py` →
  `tests/foo/test_bar.py`. Shared fixtures live in
  `tests/conftest.py`.
- Tests assert **observable behaviour** (HTTP responses, returned
  objects, log lines) — never private attributes or call counts
  on internal collaborators.
- Target: 80% coverage on `app/` for every PR. The
  `--cov=app --cov-report=term-missing` flag is enabled by
  default in `pytest.ini`, so coverage gaps show up in red
  locally.
- Use `monkeypatch` for env-var manipulation. Do not depend on
  the host's environment in tests.

### 2.6 Database migrations (Alembic)

- Migrations live in `backend/alembic/`. The `env.py` is wired
  to :class:`app.config.Settings` and
  :data:`app.models.base.Base.metadata` so ``alembic revision
  --autogenerate`` works out of the box.
- `alembic.ini` ships with a placeholder `sqlalchemy.url`; the
  real URL is injected by `env.py` from `Settings.database_url`.
  Do **not** check production credentials into `alembic.ini`.
- Use the `make alembic-upgrade` / `make alembic-revision`
  targets in the Makefile instead of calling `alembic` directly.

---

## 3. TypeScript / Angular (frontend)

### 3.1 Version and tooling

- **Angular 18.x** (NgModules architecture, see `frontend/`
  scaffold). Standalone components are acceptable inside
  NgModules but the root bootstrap is NgModule-based.
- **Node 20** in production (the Docker image uses
  `node:20-alpine`).
- **Tailwind CSS 3.x** for styling. Do not introduce another
  CSS framework.
- Linter: **ESLint** with the `@angular-eslint` preset. Rules
  live in `frontend/.eslintrc.json`; the `lint` architect target
  is wired into the `npm run lint` script and into CI.
- Type checker: the TypeScript compiler (`tsc`) runs as part of
  `ng build --configuration=production`. A green build is the
  canonical "does it typecheck" gate in CI.

### 3.2 Module / component conventions

- Group files by feature: `src/app/<feature>/<feature>.module.ts`,
  `<feature>-routing.module.ts`, `pages/<page>/<page>.component.ts`,
  `services/<feature>.service.ts`.
- One component per file. Filename matches the class name
  (`dashboard.component.ts` → `DashboardComponent`).
- Templates inline if ≤30 lines, separate `*.component.html`
  file otherwise. Same rule for styles.
- Always declare `OnPush` change detection on new components.

### 3.3 Type safety

- `strict: true` in `tsconfig.json`. Do not silence the compiler
  with `// @ts-ignore` or `any`; introduce a real type or
  `unknown` + a narrowing guard.
- Public methods of services expose typed `Observable<T>` /
  `Promise<T>` signatures, never `any`.

### 3.4 Environment / configuration

- Per-environment config lives in `src/environments/`. The
  Angular CLI swaps these files at build time; do not read
  `process.env` at runtime.
- `API_BASE_URL` is baked into the bundle at build time
  (`ARG API_BASE_URL` in the Dockerfile). The default for
  production-style builds is `/api` so the nginx proxy can
  forward to the backend.

### 3.5 Tests

- Unit tests live next to the code as `*.spec.ts` files and are
  run with `ng test` (Karma). The CI build only compiles
  (`npm run build`); PR authors are still expected to run
  `make frontend-test` locally before requesting review.

---

## 4. Docker

### 4.1 Image hygiene

- Both Dockerfiles are **multi-stage** with a separate runtime
  stage.
- The runtime stage runs as a non-root user. The backend creates
  `app:app`; the frontend image uses the `nginx` user from the
  base image.
- Pin base images by major + minor (`python:3.12-slim`,
  `node:20-alpine`, `nginx:1.27-alpine`, `postgres:16-alpine`,
  `redis:7-alpine`). Floating tags (`latest`) are not allowed.

### 4.2 docker-compose

- One service per container. Compose file is the source of
  truth for the local stack.
- Named volumes for any persistent data; bind-mounts for source
  code only on the `backend` service (live reload).
- Healthchecks are mandatory for every backing service
  (postgres, redis). The `backend` service must use the
  long-form `depends_on: { condition: service_healthy }` syntax.
- All services share a single named network (`msg-net`).

### 4.3 .dockerignore

- Both `.dockerignore` files exclude `.git`, editor noise,
  Docker meta (`Dockerfile`, `.dockerignore`), local `.env`
  files and the build artefacts (`node_modules`, `dist`,
  `.angular`, `.venv`, `__pycache__`, `tests`, …). The
  `test_*_dockerignore_*` tests pin the patterns and will fail
  if any are removed.

---

## 5. CI / GitHub Actions

- `.github/workflows/ci.yml` is the only workflow. New jobs go
  in this file unless they belong to a clearly distinct
  workflow (release, security, etc.).
- Three jobs in parallel:
  1. `backend` – install deps, run `ruff check .`, run
     `mypy`, run `pytest`, upload the coverage artifact.
  2. `frontend` – install deps, run `npm run lint`, run
     `npm run build -- --configuration=production`, run
     `npm test`.
  3. `compose` – `docker compose -f docker-compose.yml config
     --quiet`.
- Concurrency cancellation is configured; preserve it when
  adding new workflows.
- Cache `pip` and `npm` directories with the existing
  `cache-dependency-path` entries. If you add a new
  language, mirror the pattern.

---

## 6. Git workflow

- One logical change per commit. Commit messages follow
  [Conventional Commits](https://www.conventionalcommits.org/):
  `feat:`, `fix:`, `chore:`, `docs:`, `test:`, `refactor:`,
  `ci:`, `build:`, `perf:`.
- Branches: `sandcastle/<kebab-case-description>` for Sandcastle
  work, `<type>/<scope>-<summary>` for normal feature work.
- PRs target `main`. The CI workflow must be green before
  merge; coverage gate is enforced by the same workflow's
  `--cov-fail-under` flag (configured in `pytest.ini`).
- Force-pushing to `main` is forbidden. Rebase feature branches
  freely; squash only if the reviewer asks.

---

## 7. Documentation

- **README.md** at the root is the entry point. Keep the quick
  start in sync with the actual `Makefile` / `docker compose`
  commands.
- **PRD.md** captures *what* we build and *why*. Architectural
  decisions go there, not in code comments.
- **CODING_STANDARDS.md** (this file) captures *how* we write
  code. Update it in the same PR that introduces a new
  convention.
- **CONTRIBUTING.md** captures the day-to-day workflow. If a
  step here disagrees with `Makefile` or CI, the tooling wins.

---

## 8. Linting and pre-commit

- Backend: `make backend-lint` (Ruff) and
  `make backend-typecheck` (Mypy). Both run in CI on the
  `backend` job.
- Frontend: `make frontend-lint` (ESLint) and `make
  frontend-test` / `make frontend-build` (the build is the
  TypeScript typecheck gate).
- Pre-commit hooks (`.pre-commit-config.yaml`) run the same
  checks locally before each commit. The config wires Ruff,
  Mypy and ESLint to mirror the CI pipeline so drift is caught
  at commit time rather than at the `git push` step. One-time
  install: `make precommit-install` (requires the
  `pre-commit` binary from `backend/requirements-dev.txt`).
  Manual run: `make precommit-run`.

---

## 9. Security

- **Never** commit secrets. Use `.env` (git-ignored) for local
  development; the canonical list of variables lives in
  `.env.example` with safe placeholders.
- API keys, provider tokens and DB passwords are read from
  `Settings` (env) only. No default values for production-bound
  secrets.
- Phone numbers and other PII are never logged in plain text.
  Use the helpers in `app/observability/redact` (`hash_phone`,
  `mask_rut`, …) – the hash/mask helpers ship with a full
  unit test suite and are re-exported from
  `app.observability`. Configure log levels with
  `app.observability.configure_logging` (called once from
  `app.main.create_app`); do not install ad-hoc handlers from
  route handlers.

---

## 10. Versioning and releases

- The backend version lives in `app/main.py` (`APP_VERSION`).
  Bump it on every breaking change. The frontend version is in
  `frontend/package.json`; keep them in lockstep for the public
  API surface.
- Tag releases from `main` with `vX.Y.Z`. The CI workflow does
  not yet publish images; that lands in a follow-up task.
