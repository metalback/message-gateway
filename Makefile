# ---------------------------------------------------------------------------
# Message Gateway – developer convenience commands.
# ---------------------------------------------------------------------------
# Targets are intentionally simple; the actual workflows live in
# docker-compose / CI. Use this for the 80% case.
# ---------------------------------------------------------------------------

.PHONY: help install ci backend-install backend-test backend-lint backend-typecheck frontend-install frontend-test frontend-build frontend-lint compose-up compose-down compose-logs compose-validate alembic-upgrade alembic-revision precommit-install precommit-run precommit-clean clean

help:
	@echo "Message Gateway – make targets:"
	@echo "  install             Install backend + frontend dependencies"
	@echo "  ci                  Run every check CI runs (lint + typecheck + tests + compose)"
	@echo "  backend-test        Run backend pytest suite"
	@echo "  backend-lint        Run ruff on the backend"
	@echo "  backend-typecheck   Run mypy on the backend"
	@echo "  frontend-test       Run frontend tests"
	@echo "  frontend-build      Build frontend for production"
	@echo "  frontend-lint       Run eslint on the frontend"
	@echo "  compose-up          Start the full docker-compose stack"
	@echo "  compose-down        Stop the stack and remove containers"
	@echo "  compose-logs        Tail logs from all services"
	@echo "  compose-validate    Validate docker-compose.yml without building"
	@echo "  alembic-upgrade     Apply pending DB migrations"
	@echo "  alembic-revision    Create a new migration (use msg=\"...\" arg)"
	@echo "  precommit-install   Install the pre-commit git hook (one-time)"
	@echo "  precommit-run       Run every pre-commit hook against the working tree"
	@echo "  precommit-clean     Remove the pre-commit hook and cached environments"
	@echo "  clean               Remove build artefacts and caches"

install: backend-install frontend-install

# ---------------------------------------------------------------------------
# `make ci` mirrors the GitHub Actions pipeline so contributors can
# reproduce a green/red build locally before pushing. Each step is a
# standalone target so it can be re-run in isolation.
# ---------------------------------------------------------------------------
ci: backend-lint backend-typecheck backend-test frontend-lint frontend-build compose-validate
	@echo ""
	@echo "CI checks passed."

backend-install:
	cd backend && pip install -r requirements.txt -r requirements-dev.txt

backend-test:
	cd backend && pytest

backend-lint:
	cd backend && ruff check .

backend-typecheck:
	cd backend && mypy

frontend-install:
	cd frontend && npm install

frontend-test:
	cd frontend && npm test -- --watch=false --browsers=ChromeHeadlessCI

frontend-build:
	cd frontend && npm run build

frontend-lint:
	cd frontend && npm run lint

compose-up:
	docker compose up --build

compose-down:
	docker compose down

compose-logs:
	docker compose logs -f

# Parse-only check that mirrors the `compose` CI job. Useful when you
# just want to know whether `docker-compose.yml` is well-formed without
# paying the cost of a full `compose up`.
compose-validate:
	docker compose -f docker-compose.yml config --quiet

alembic-upgrade:
	cd backend && alembic upgrade head

alembic-revision:
	cd backend && alembic revision --autogenerate -m "$(msg)"

# ---------------------------------------------------------------------------
# Pre-commit hooks. The framework is the local mirror of the CI
# pipeline: running `make precommit-run` executes the same Ruff,
# Mypy and ESLint checks the GitHub Actions workflow runs, so a
# contributor can catch drift before pushing. `precommit-install`
# wires the framework into `.git/hooks/pre-commit` (one-time per
# clone) and `precommit-clean` tears it down. The framework binary
# itself is expected to be on $PATH; `precommit-install` does not
# install it – that lives in `requirements-dev.txt` for the
# backend, mirroring the `pip install pre-commit` step CI uses.
# ---------------------------------------------------------------------------
precommit-install:
	@if ! command -v pre-commit >/dev/null 2>&1; then \
		echo "pre-commit is not installed. Install it with:"; \
		echo "  pip install pre-commit"; \
		exit 1; \
	fi
	pre-commit install

precommit-run:
	@if ! command -v pre-commit >/dev/null 2>&1; then \
		echo "pre-commit is not installed. Install it with:"; \
		echo "  pip install pre-commit"; \
		exit 1; \
	fi
	pre-commit run --all-files

precommit-clean:
	@if [ -f .git/hooks/pre-commit ]; then pre-commit uninstall; fi
	@rm -rf .pre-commit-cache
	@echo "pre-commit hook and cache removed."

clean:
	rm -rf backend/.pytest_cache backend/__pycache__ backend/**/__pycache__
	rm -rf frontend/dist frontend/node_modules frontend/.angular
	rm -rf backend/.mypy_cache backend/.ruff_cache
	rm -rf .venv-test
	rm -rf .pre-commit-cache
