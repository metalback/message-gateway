# Changelog

All notable changes to **Message Gateway** are documented in this file.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and the project adheres to [Semantic Versioning](https://semver.org/).

> Release notes are generated from this file on every tag. Do not
> edit history retroactively; instead, add a new entry under
> `Unreleased` and move it to a dated section on the next release.

---

## [Unreleased]

### Added

- `.git-blame-ignore-revs` at the repository root so `git blame`
  (and the GitHub blame view) skip the project-wide Ruff format
  commit and any future mechanical refactor. The file is
  documented in a new `CONTRIBUTING.md` §11 section that explains
  when to add a new entry, and three fixture tests in
  `backend/tests/test_infra.py` pin its presence, the
  non-empty-content rule and the 40-character SHA shape so a
  careless delete or typo is caught in CI.
- `precommit` job to `.github/workflows/ci.yml` that runs
  `pre-commit run --all-files` against the whole tree. The job
  installs the framework from `backend/requirements-dev.txt`,
  caches `~/.cache/pre-commit` (keyed on `.pre-commit-config.yaml`
  so a hook rev bump invalidates the cache automatically) and
  is the only CI surface that exercises the standard hygiene
  hooks (`trailing-whitespace`, `end-of-file-fixer`,
  `check-yaml`, `check-toml`, `check-added-large-files`,
  `check-merge-conflict`) the `backend` / `frontend` jobs do
  not duplicate. This was documented in
  `.pre-commit-config.yaml` as "for the future"; the TODO is
  resolved. Fixture tests in `backend/tests/test_infra.py`
  pin the job's presence, the `--all-files` flag, the Python
  3.12 toolchain and the cache key shape.
- Pre-commit configuration (`.pre-commit-config.yaml`) wiring the
  same checks the CI pipeline runs (Ruff, Mypy, ESLint, plus
  standard hygiene hooks from `pre-commit/pre-commit-hooks`) so a
  contributor catches drift at commit time instead of at the
  `git push` step. New `make precommit-install` /
  `make precommit-run` / `make precommit-clean` targets wrap
  the framework binary; `pre-commit` is added to
  `backend/requirements-dev.txt` so the binary ships with the
  dev install. Fixture tests in
  `backend/tests/test_infra.py` pin the file's presence, the
  required hook ids, the `mypy` / `eslint` hook entry points
  and the new Makefile targets.
- `CODING_STANDARDS.md` §8 now documents the pre-commit
  workflow (config location, install command, manual run) so
  the contributor-facing guide is no longer out of sync with
  the scaffold.
- Repository infrastructure: `LICENSE` (Apache 2.0), `.gitattributes`,
  `.github/CODEOWNERS`, `.github/dependabot.yml`, `SECURITY.md`,
  `.github/PULL_REQUEST_TEMPLATE.md`, and the
  `.github/ISSUE_TEMPLATE/` chooser.
- This `CHANGELOG.md` (Keep a Changelog format).
- Fixture tests in `backend/tests/test_infra.py` pinning the new
  infra files so a missing piece is caught in CI, not in code
  review.
- `CONTRIBUTING.md` now links to `SECURITY.md`, the issue picker
  under `.github/ISSUE_TEMPLATE/` (bug / feature / security
  templates) and the PR template under
  `.github/PULL_REQUEST_TEMPLATE.md`. The new
  `test_contributing_links_to_security_and_templates` fixture
  test fails if any of those references is dropped.
- PII redaction helpers in `app/observability/redact.py`:
  `hash_phone`, `hash_rut`, `mask_phone`, `mask_rut` plus the
  `normalise_*` and `RedactionResult` value object. Phone numbers
  are normalised to the canonical ``+56...`` form and either
  hashed (with a salt derived from `SECRET_KEY`) or masked for
  operator-facing dashboards; Chilean RUTs follow the same
  pattern with the check digit always preserved. The helpers
  ship with a full unit test suite in
  `backend/tests/observability/test_redact.py`.
- `app/redis_client.py`: a process-wide cached async Redis
  client (`get_redis_client`) that mirrors the lazy / singleton
  pattern of `app.db`. The client is built on first use from
  `Settings.redis_url` and reused for the life of the
  process, so the Arq worker, the health probe and the future
  rate-limiter / cache helpers share a single connection pool.
  Backed by `backend/tests/test_redis_client.py` (singleton,
  URL derivation, socket timeouts, cache reset).
- `app/observability/logging.py`: a centralised logging
  configuration entry point (`configure_logging` /
  `get_logger`) that installs a single project handler on the
  root logger and applies the level declared in
  `Settings.log_level`. The configuration is idempotent and
  preserves any handlers other libraries (e.g. uvicorn) had
  already attached. Backed by
  `backend/tests/observability/test_logging.py` (handler
  identity, level normalisation, idempotency, fallback).

### Changed

- `CONTRIBUTING.md` is wired to the rest of the contributor
  surface (templates + security policy) so the "first stop"
  guide no longer hides the disclosure path or the issue
  picker.
- `app/observability/__init__.py` now exports the redaction
  helpers and the logging helpers so route handlers, Arq
  workers and the future logging configuration module can
  ``from app.observability import hash_phone, mask_rut,
  configure_logging, get_logger`` without reaching into the
  subpackage.
- `app/main.py` calls `configure_logging` on startup so the
  very first log line is routed through the project formatter
  and the `LOG_LEVEL` env-var override takes effect without a
  code change. The `app/health.py` Redis probe now uses the
  shared cached client (`app.redis_client.get_redis_client`)
  instead of opening a fresh connection on every check, and
  no longer closes the client (the pool is shared).

---

## [0.1.0] – scaffold

Initial monorepo bootstrap: FastAPI backend + Angular 18 frontend,
local `docker-compose` stack (postgres, redis, backend, frontend),
GitHub Actions CI (backend / frontend / compose jobs), Ruff + Mypy
on the backend, ESLint + Karma on the frontend, Alembic migrations,
`PRD.md`, `CODING_STANDARDS.md`, `CONTRIBUTING.md`, `Makefile`,
`.editorconfig`, `.nvmrc` and `.python-version`.

See the commit history for the full diff; this entry exists so
contributors have a stable reference point for the first tag.

[Unreleased]: https://github.com/msg-gateway/message-gateway/compare/main...HEAD
[0.1.0]: https://github.com/msg-gateway/message-gateway/releases/tag/v0.1.0
