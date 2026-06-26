# Contributing to Message Gateway

Thanks for your interest in the project. This guide walks you
through the day-to-day workflow: how to set up your environment,
how to run the test suite, and how to propose a change.

For project-wide conventions (Python style, Angular style, Docker
rules, Git conventions) read [`CODING_STANDARDS.md`](./CODING_STANDARDS.md).
For the *what* and *why* of the product, read [`PRD.md`](./PRD.md).
For vulnerability disclosure, read [`SECURITY.md`](./SECURITY.md)
before opening a public issue.

---

## 1. Prerequisites

| Tool       | Version | Notes                                          |
| ---------- | ------- | ---------------------------------------------- |
| Python     | 3.12    | 3.11 is tolerated for local dev only.         |
| Node       | 20.x    | Matches the Docker build image.                |
| Docker     | 24+     | With the Compose v2 plugin.                    |
| Make       | any     | Optional, but recommended for the dev loop.    |
| Git        | 2.30+   | Conventional Commits are enforced by review.   |

> The exact Node and Python versions are pinned at the repo root via
> [`.nvmrc`](./.nvmrc) and [`.python-version`](./.python-version).
> If you use [`nvm`](https://github.com/nvm-sh/nvm),
> [`fnm`](https://github.com/Schniz/fnm) or
> [`pyenv`](https://github.com/pyenv/pyenv), `cd`-ing into the
> repository will auto-switch the local toolchain to the same
> versions the CI workflow installs (`node-version: "20"` /
> `python-version: "3.12"`).

---

## 2. First-time setup

```bash
git clone <repo-url> message-gateway
cd message-gateway

# Environment file used by docker compose and the backend.
cp .env.example .env

# Native install (backend + frontend).
make install
```

If you prefer to run the full stack in containers, skip `make
install` and use `make compose-up` instead.

---

## 3. Daily workflow

The 80% development loop is two commands:

```bash
make backend-test     # Backend tests + coverage report
make frontend-test    # Frontend unit tests (Karma)
```

Other useful targets:

```bash
make ci               # Run every check CI runs (lint + typecheck + tests + compose)
make backend-lint      # Ruff on the backend
make backend-typecheck # Mypy on the backend
make frontend-lint     # ESLint on the Angular sources
make frontend-build    # Production Angular build
make compose-up        # Bring up postgres, redis, backend, frontend
make compose-down      # Stop the stack
make compose-logs      # Tail logs from every service
make compose-validate  # Parse docker-compose.yml without building images
make alembic-upgrade   # Apply pending DB migrations
make alembic-revision  # Create a new migration (msg="...")
make precommit-install # Wire the pre-commit git hook (one-time)
make precommit-run     # Run every pre-commit hook against the tree
make clean             # Remove build artefacts and caches
```

Before opening a PR, run `make ci` to reproduce the GitHub Actions
pipeline locally. It chains the backend lint, typecheck and test jobs
with the frontend lint and build, plus a `docker compose config` parse
check, so any green-then-red surprise at the CI level is caught
before you push.

The pre-commit framework (`.pre-commit-config.yaml`) is the local
mirror of those checks: install the hook once with
`make precommit-install` and every commit will run Ruff, Mypy and
ESLint before it lands. See
[`CODING_STANDARDS.md` §8](./CODING_STANDARDS.md) for the full
workflow.

---

## 4. Working with the docker-compose stack

```bash
# First boot.
make compose-up

# Run a one-off command inside a service.
docker compose exec backend pytest
docker compose exec frontend npm run build

# Reset state (deletes volumes, i.e. drops the DB).
docker compose down -v
```

The compose project name is taken from `COMPOSE_PROJECT_NAME` in
`.env`. Service hostnames (`postgres`, `redis`, `backend`,
`frontend`) are stable across the bridge network — reference them
by name, not by IP.

---

## 5. Branching and commits

1. Branch from `main` using a descriptive name:
   - `feat/messages-batch-endpoint`
   - `fix/fee-engine-rounded-down`
   - `docs/clarify-cors-config`
   - `chore/bump-fastapi-0.116`
2. Keep branches focused. One logical change per branch.
3. Write commit messages in the
   [Conventional Commits](https://www.conventionalcommits.org/)
   format:
   ```
   feat(messages): accept template id in batch payload
   fix(providers): surface Meta 429s as rate-limit errors
   docs: clarify the CORS env var
   test(infra): pin that the compose file declares redis
   ```
4. Run `make backend-test backend-lint backend-typecheck` and
   `make frontend-test frontend-lint` before pushing. CI will run
   the same gates plus a compose-validate job.

---

## 6. Pull requests

- Target `main`. The PR title becomes the squash commit subject,
  so phrase it as a Conventional Commit (`feat: ...`, etc.).
- The description is populated from
  [`.github/PULL_REQUEST_TEMPLATE.md`](.github/PULL_REQUEST_TEMPLATE.md);
  fill in the `Summary`, `What changed`, `How it was tested` and
  `Checklist` sections so a reviewer can land the change without
  having to chase context. Screenshots are required for any UI
  change.
- Keep PRs reviewable: aim for <400 lines of diff excluding
  generated files.
- Address every review comment. Mark a comment as **Resolved**
  once the next push addresses it; do not dismiss without a fix.
- The CI pipeline (`.github/workflows/ci.yml`) must be green
  before merge:
  - `backend` job: `pytest` with coverage.
  - `frontend` job: production build succeeds.
  - `compose` job: `docker compose config --quiet` passes.

---

## 7. Testing

- **Backend** (`make backend-test`):
  - `pytest` runs with `pytest-asyncio` in `auto` mode and
    `--cov=app --cov-report=term-missing`.
  - Target: 80% coverage on `app/`. Coverage <80% blocks PR
    merge.
  - New behaviour must come with tests in the matching
    `tests/<area>/` package.
- **Frontend** (`make frontend-test`):
  - Karma + Jasmine. The CI build only compiles; run tests
    locally before pushing.
- **Infra** (covered by `backend/tests/test_infra.py`):
  fixture tests pin the shape of `.env.example`,
  `docker-compose.yml`, `ci.yml`, both Dockerfiles, both
  `.dockerignore` files, the `Makefile` and the new
  documentation files. Touch one of these files and the
  fixture tests will tell you exactly which contract needs to
  stay in sync.

---

## 8. Reporting issues

- Use the issue picker under
  [`.github/ISSUE_TEMPLATE/`](.github/ISSUE_TEMPLATE/):
  - **Bugs** → `bug_report.md` (reproduction steps, expected
    vs. actual behaviour, the commit SHA, the environment —
    `docker compose ps` output is helpful).
  - **Feature requests** → `feature_request.md` (link the user
    story in `PRD.md` if it exists; otherwise describe the use
    case, the proposed API/UX and the impact on existing plans).
  - **Security advisories** → `security_advisory.md` (private
    disclosure path; do not open a public issue).
- For anything that does not fit a template, email the
  maintainers directly (see `PRD.md` for the contact
  information).
- Sensitive reports (data exposure, account takeover, …) must
  follow the disclosure process documented in
  [`SECURITY.md`](./SECURITY.md); the GitHub *Security* tab
  renders that file automatically.

---

## 9. Code review checklist

Before approving a PR, confirm:

- [ ] Conventional Commit title and a meaningful description.
- [ ] Tests cover the new behaviour, including failure paths.
- [ ] No secrets in code, logs, fixtures or screenshots.
- [ ] No PII (real phone numbers, RUTs, etc.) in fixtures.
- [ ] Backwards-compatible changes to public API; breaking
      changes are clearly flagged in the description and
      `PRD.md` is updated if needed.
- [ ] Fixture tests in `test_infra.py` pass — these guard the
      contract between repo layout, Docker, CI and docs.

---

## 10. License

By contributing, you agree that your contributions will be
licensed under the same terms as the rest of the project (see
the repository's `LICENSE` file once it lands; for now, internal
ownership is described in `PRD.md`).

---

## 11. `git blame` and `.git-blame-ignore-revs`

Project-wide refactors (a Ruff / Black re-format, a license
header pass, an import-sort migration) touch almost every line
in the tree. Without help, those commits dominate `git blame`
output and bury the actual authorship signal.

The repository ships a `.git-blame-ignore-revs` file at the
root that lists the SHAs of those mechanical commits. `git`
(>= 2.23) consults the file automatically, so `git blame` and
the GitHub blame view skip the listed revisions without any
per-developer configuration.

**When to add a new entry:**

- A commit message starts with `chore:`, `style:` or `refactor:`
  *and* the diff is purely cosmetic (whitespace, import
  ordering, quotes, trailing commas, license headers).
- The commit rewrites a non-trivial number of lines across the
  whole tree (single-file tweaks are not worth the bookkeeping).
- The author of the change agrees the line-level authorship is
  no longer meaningful.

Add the full 40-character SHA on its own line in
`.git-blame-ignore-revs`, with a short comment above it
explaining *what* the commit was. Keep the file sorted
chronologically (oldest at the top) so a code review can scan
it like a changelog.
