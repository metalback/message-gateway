"""Scaffold / infra consistency tests.

These tests pin the shape of the monorepo plumbing so a careless
edit to one file can't silently break the rest of the stack.

Scope:
  - `.env.example` exposes every env var the backend `Settings`
    class actually reads.
  - `docker-compose.yml` is syntactically valid and contains the
    services the README promises.
  - `.github/workflows/ci.yml` exercises backend, frontend, the
    compose file and the pre-commit framework.
  - `frontend/nginx.conf` reverse-proxies `/api` to the backend
    and falls back to `index.html` for SPA routes.
  - Both Dockerfiles declare the expected EXPOSE / CMD contracts.
  - Both `.dockerignore` files keep dev / test artefacts, virtualenvs
    and editor noise out of the Docker build context.
  - The root `Makefile` exposes the developer-facing targets
    referenced in `README.md`.

The tests deliberately do **not** shell out to `docker` / `npm` /
`ng`; everything runs against the static files so it works in
CI on a vanilla Python image.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

# Repository layout – resolved from this file so the tests work
# regardless of where pytest is invoked from.
TESTS_DIR = Path(__file__).resolve().parent
BACKEND_DIR = TESTS_DIR.parent
REPO_ROOT = BACKEND_DIR.parent


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def env_example() -> str:
    return (REPO_ROOT / ".env.example").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def env_keys(env_example: str) -> set[str]:
    """Set of `KEY=...` declarations present in `.env.example`."""
    keys: set[str] = set()
    for line in env_example.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key = line.split("=", 1)[0].strip()
        if key:
            keys.add(key)
    return keys


@pytest.fixture(scope="module")
def compose_doc() -> dict:
    text = (REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    return yaml.safe_load(text)


@pytest.fixture(scope="module")
def ci_doc() -> dict:
    text = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    return yaml.safe_load(text)


@pytest.fixture(scope="module")
def nginx_conf() -> str:
    return (REPO_ROOT / "frontend" / "nginx.conf").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def backend_dockerfile() -> str:
    return (REPO_ROOT / "backend" / "Dockerfile").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def frontend_dockerfile() -> str:
    return (REPO_ROOT / "frontend" / "Dockerfile").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def frontend_package() -> dict:
    text = (REPO_ROOT / "frontend" / "package.json").read_text(encoding="utf-8")
    return json_loads(text)


@pytest.fixture(scope="module")
def backend_requirements() -> str:
    return (REPO_ROOT / "backend" / "requirements.txt").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def backend_requirements_dev() -> str:
    """Text of `backend/requirements-dev.txt` (the development /
    test / lint dependency list). The runtime `backend_requirements`
    fixture is separate because the two files are pinned
    independently: a CI build that installs dev deps does not
    imply a production image that does."""
    return (REPO_ROOT / "backend" / "requirements-dev.txt").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def backend_pytest_ini() -> str:
    return (BACKEND_DIR / "pytest.ini").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def makefile() -> str:
    return (REPO_ROOT / "Makefile").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def backend_dockerignore() -> str:
    return (BACKEND_DIR / ".dockerignore").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def frontend_dockerignore() -> str:
    return (REPO_ROOT / "frontend" / ".dockerignore").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def coding_standards() -> str:
    return (REPO_ROOT / "CODING_STANDARDS.md").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def contributing() -> str:
    return (REPO_ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def readme() -> str:
    return (REPO_ROOT / "README.md").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def editorconfig() -> str:
    return (REPO_ROOT / ".editorconfig").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def root_gitignore() -> str:
    return (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def backend_gitignore() -> str:
    return (BACKEND_DIR / ".gitignore").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def frontend_gitignore() -> str:
    return (REPO_ROOT / "frontend" / ".gitignore").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def backend_pyproject() -> str:
    return (BACKEND_DIR / "pyproject.toml").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def frontend_eslintrc() -> str:
    return (REPO_ROOT / "frontend" / ".eslintrc.json").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def nvmrc() -> str:
    return (REPO_ROOT / ".nvmrc").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def python_version_file() -> str:
    return (REPO_ROOT / ".python-version").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def precommit_config_text() -> str:
    """Raw text of `.pre-commit-config.yaml`. Used by the
    presence / well-formedness fixture tests below; the parsed
    dict is exposed separately via :func:`precommit_config`."""
    return (REPO_ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def precommit_config(precommit_config_text: str) -> dict:
    """Parsed `.pre-commit-config.yaml` document.

    Pre-commit reads the file as a YAML document with two top-level
    keys (``repos:``, ``fail_fast:``) and a per-hook ``language`` /
    ``entry`` block. We expose the parsed dict so the fixture tests
    can iterate over ``repos`` without repeating the YAML load.
    """
    return yaml.safe_load(precommit_config_text)


def json_loads(text: str) -> dict:
    import json

    return json.loads(text)


# ---------------------------------------------------------------------------
# .env.example
# ---------------------------------------------------------------------------


# Mirrors the `alias=` declarations in `app/config.py`. Keep in sync.
EXPECTED_ENV_KEYS: set[str] = {
    "BACKEND_ENV",
    "LOG_LEVEL",
    "SECRET_KEY",
    "CORS_ALLOW_ORIGINS",
    "POSTGRES_USER",
    "POSTGRES_PASSWORD",
    "POSTGRES_DB",
    "POSTGRES_HOST",
    "POSTGRES_PORT",
    "REDIS_HOST",
    "REDIS_PORT",
    "REDIS_DB",
    # Auth & registration settings (issue #3).
    "BCRYPT_ROUNDS",
    "JWT_SECRET",
    "JWT_ALGORITHM",
    "JWT_TTL_MINUTES",
    "API_KEY_PREFIX",
    # Provider integration settings (issue #4).
    "META_WHATSAPP_ACCESS_TOKEN",
    "META_WHATSAPP_PHONE_NUMBER_ID",
    "META_WHATSAPP_API_BASE",
    "META_WHATSAPP_API_VERSION",
    "SMS_AGGREGATOR_API_URL",
    "SMS_AGGREGATOR_API_KEY",
    "SMS_AGGREGATOR_SENDER_ID",
    "PROVIDER_TIMEOUT_SECONDS",
    # Provider failover chains (issue #11).
    "PROVIDER_FAILOVER_CHAINS",
    # Webhooks – delivery receipts (issue #5).
    "WEBHOOK_DELIVERY_TIMEOUT_SECONDS",
    "WEBHOOK_MAX_DELIVERY_ATTEMPTS",
    # Batch messaging (issue #9) – rate limit + completion webhook.
    "BATCH_RATE_LIMIT_PER_SECOND",
    "BATCH_WEBHOOK_TIMEOUT_SECONDS",
    "BATCH_WEBHOOK_MAX_DELIVERY_ATTEMPTS",
    # Billing & Flow (issue #7).
    "BILLING_CURRENCY",
    "BILLING_IVA_RATE",
    "BILLING_DUE_DAYS",
    "BILLING_DEFAULT_PLAN_CODE",
    "FLOW_API_KEY",
    "FLOW_SECRET_KEY",
    "FLOW_BASE_URL",
    "FLOW_ENVIRONMENT",
    "FLOW_CONFIRMATION_URL",
    "FLOW_RETURN_URL",
    "FLOW_WEBHOOK_URL",
    "DTE_EMISOR_RUT",
    "DTE_EMISOR_RAZON_SOCIAL",
    "DTE_EMISOR_GIRO",
    "DTE_EMISOR_DIRECCION",
    "DTE_EMISOR_COMUNA",
    "DTE_EMISOR_CIUDAD",
    "DTE_RESOLUTION_NUMBER",
    "DTE_RESOLUTION_DATE",
    "DTE_SII_OFFICE",
    # Compose project name is consumed by docker-compose itself.
    "COMPOSE_PROJECT_NAME",
    # API_BASE_URL is consumed by the Angular build at image build time.
    "API_BASE_URL",
}


def test_env_example_declares_every_settings_alias(env_keys: set[str]) -> None:
    missing = EXPECTED_ENV_KEYS - env_keys
    assert not missing, f".env.example is missing keys: {sorted(missing)}"


def test_env_example_has_no_blank_or_duplicate_values(env_example: str) -> None:
    """Lines that look like `KEY=` (no value) usually mean an unfinished
    scaffold entry. Catch them early so the file stays deploy-ready."""
    for raw in env_example.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, _, value = line.partition("=")
        assert key, f"Malformed line in .env.example: {raw!r}"
        assert value.strip(), f"Empty value for {key} in .env.example"


# ---------------------------------------------------------------------------
# docker-compose.yml
# ---------------------------------------------------------------------------


REQUIRED_SERVICES: set[str] = {"postgres", "redis", "backend", "frontend"}


def test_compose_file_parses_as_yaml(compose_doc: dict) -> None:
    assert isinstance(compose_doc, dict)
    assert "services" in compose_doc


def test_compose_declares_required_services(compose_doc: dict) -> None:
    services = set(compose_doc.get("services", {}).keys())
    missing = REQUIRED_SERVICES - services
    assert not missing, f"docker-compose is missing services: {sorted(missing)}"


def test_compose_postgres_has_healthcheck(compose_doc: dict) -> None:
    pg = compose_doc["services"]["postgres"]
    assert "healthcheck" in pg, "postgres service must have a healthcheck"


def test_compose_redis_has_healthcheck(compose_doc: dict) -> None:
    redis = compose_doc["services"]["redis"]
    assert "healthcheck" in redis, "redis service must have a healthcheck"


def test_compose_backend_has_healthcheck(compose_doc: dict) -> None:
    """The backend service must declare a ``healthcheck`` that calls
    the FastAPI ``/health`` endpoint. Without it, the compose
    orchestrator can only tell that the container is running – it
    cannot distinguish "uvicorn is up and the app imported" from
    "uvicorn crashed on startup and the entrypoint is still
    running". Hitting ``/health`` flips the check from a process
    probe to a behavioural one, which is what every other service
    in the stack does (see postgres / redis above)."""
    backend = compose_doc["services"]["backend"]
    assert "healthcheck" in backend, "backend service must have a healthcheck"
    healthcheck = backend["healthcheck"]
    # The healthcheck must define a ``test`` (otherwise Docker treats
    # the block as a no-op) plus the standard ``interval`` /
    # ``timeout`` / ``retries`` knobs. Missing any of them silently
    # degrades the contract.
    assert "test" in healthcheck, "backend healthcheck must declare a `test`"
    assert "interval" in healthcheck, "backend healthcheck must declare `interval`"
    assert "timeout" in healthcheck, "backend healthcheck must declare `timeout`"
    assert "retries" in healthcheck, "backend healthcheck must declare `retries`"
    # The test command should actually hit the ``/health`` endpoint –
    # that's the whole point of wiring the orchestrator to the
    # liveness route. A TCP-only probe would re-implement what
    # Docker already gives us via ``EXPOSE`` + ``depends_on``.
    rendered = " ".join(str(part) for part in healthcheck["test"])
    assert "/health" in rendered, (
        "backend healthcheck must hit the FastAPI `/health` endpoint, "
        f"got test={healthcheck['test']!r}"
    )


def test_compose_backend_depends_on_postgres_and_redis(compose_doc: dict) -> None:
    backend = compose_doc["services"]["backend"]
    deps = backend.get("depends_on", {})
    if isinstance(deps, list):  # short syntax
        dep_names = set(deps)
    else:  # long syntax with conditions
        dep_names = set(deps.keys())
    assert "postgres" in dep_names
    assert "redis" in dep_names


def test_compose_backend_publishes_8000(compose_doc: dict) -> None:
    ports = compose_doc["services"]["backend"].get("ports", [])
    assert any("8000" in str(p) for p in ports), ports


def test_compose_frontend_publishes_4200(compose_doc: dict) -> None:
    ports = compose_doc["services"]["frontend"].get("ports", [])
    assert any("4200" in str(p) for p in ports), ports


def test_compose_uses_a_named_network(compose_doc: dict) -> None:
    networks = compose_doc.get("networks", {})
    assert networks, "docker-compose should declare at least one named network"


def test_compose_declares_persistent_volumes(compose_doc: dict) -> None:
    volumes = compose_doc.get("volumes", {})
    assert "postgres_data" in volumes
    assert "redis_data" in volumes


# ---------------------------------------------------------------------------
# .github/workflows/ci.yml
# ---------------------------------------------------------------------------


def test_ci_workflow_parses_as_yaml(ci_doc: dict) -> None:
    assert isinstance(ci_doc, dict)


def test_ci_runs_on_pull_request_and_push(ci_doc: dict) -> None:
    triggers = ci_doc.get(True) or ci_doc.get("on") or {}
    assert "pull_request" in triggers
    assert "push" in triggers


def test_ci_defines_backend_frontend_and_compose_jobs(ci_doc: dict) -> None:
    jobs = ci_doc.get("jobs", {})
    assert "backend" in jobs, "CI must have a `backend` job"
    assert "frontend" in jobs, "CI must have a `frontend` job"
    assert "compose" in jobs, "CI must have a `compose` job (docker compose config)"
    # The pre-commit job is the canonical "is the whole tree clean?"
    # surface: it runs the framework, not the individual CLIs, so it
    # also exercises the standard hygiene hooks the backend/frontend
    # jobs don't. Missing it means contributors can land a commit
    # that fails `pre-commit run` locally without CI catching it.
    assert "precommit" in jobs, "CI must have a `precommit` job"


def test_ci_backend_job_uses_python_312(ci_doc: dict) -> None:
    backend_steps = ci_doc["jobs"]["backend"]["steps"]
    python_setup = _find_step(backend_steps, lambda s: "setup-python" in s.get("uses", ""))
    assert python_setup is not None, "backend job must set up Python"
    with_block = python_setup.get("with", {})
    assert str(with_block.get("python-version")) == "3.12"


def test_ci_backend_job_runs_pytest(ci_doc: dict) -> None:
    steps = ci_doc["jobs"]["backend"]["steps"]
    run_block = _find_step(steps, lambda s: "pytest" in (s.get("run") or ""))
    assert run_block is not None, "backend job must run pytest"


def test_ci_compose_job_validates_compose_file(ci_doc: dict) -> None:
    steps = ci_doc["jobs"]["compose"]["steps"]
    run_block = _find_step(steps, lambda s: "docker compose" in (s.get("run") or ""))
    assert run_block is not None, "compose job must invoke `docker compose`"
    assert "config" in run_block["run"]


def test_ci_precommit_job_runs_framework_against_tree(ci_doc: dict) -> None:
    """The pre-commit CI job must run ``pre-commit run --all-files`` so
    every contributor's working tree is exercised, not just the
    files touched by the most recent commit. The framework is the
    canonical "is this tree clean?" surface documented in
    CODING_STANDARDS.md §8, and a CI run is the only place a
    contributor who skipped the local hook still gets caught."""
    steps = ci_doc["jobs"]["precommit"]["steps"]
    run_block = _find_step(steps, lambda s: "pre-commit run" in (s.get("run") or ""))
    assert run_block is not None, "precommit job must invoke `pre-commit run`"
    assert (
        "--all-files" in run_block["run"]
    ), "precommit job must pass --all-files so the whole tree is checked"


def test_ci_precommit_job_uses_python_312(ci_doc: dict) -> None:
    """`pre-commit` is a Python tool shipped from
    ``backend/requirements-dev.txt``; the job must therefore use the
    same Python version the backend job uses. A drift here would
    mean the framework is installed by a different toolchain than
    the one the local ``make precommit-run`` target uses."""
    steps = ci_doc["jobs"]["precommit"]["steps"]
    python_setup = _find_step(steps, lambda s: "setup-python" in s.get("uses", ""))
    assert python_setup is not None, "precommit job must set up Python"
    with_block = python_setup.get("with", {})
    assert str(with_block.get("python-version")) == "3.12"


def test_ci_precommit_job_caches_hook_environments(ci_doc: dict) -> None:
    """Caching ``~/.cache/pre-commit`` keeps the cold-start cost of
    downloading + installing the Ruff and ESLint hook virtualenvs
    from blowing the CI budget. The cache key must hash the
    pre-commit config so a hook version bump invalidates the
    cache automatically."""
    steps = ci_doc["jobs"]["precommit"]["steps"]
    cache_step = _find_step(steps, lambda s: "actions/cache" in s.get("uses", ""))
    assert cache_step is not None, "precommit job must cache ~/.cache/pre-commit"
    with_block = cache_step.get("with", {})
    assert "~/.cache/pre-commit" in with_block.get(
        "path", ""
    ), "pre-commit job cache must target ~/.cache/pre-commit"
    # The key has to depend on the config so a hook rev bump
    # invalidates the cache instead of serving a stale virtualenv.
    assert ".pre-commit-config.yaml" in with_block.get(
        "key", ""
    ), "pre-commit cache key must hash .pre-commit-config.yaml"


# ---------------------------------------------------------------------------
# nginx.conf
# ---------------------------------------------------------------------------


def test_nginx_listens_on_port_80(nginx_conf: str) -> None:
    assert re.search(r"listen\s+80\s*;", nginx_conf), "nginx must listen on port 80"


def test_nginx_reverse_proxies_api(nginx_conf: str) -> None:
    assert re.search(r"location\s+/api/", nginx_conf), "nginx must define an /api location"
    assert "proxy_pass" in nginx_conf, "nginx must use proxy_pass for /api"
    # The upstream should target the docker-compose `backend` service.
    assert "backend" in nginx_conf, "nginx must proxy /api to the backend service"


def test_nginx_serves_spa_fallback(nginx_conf: str) -> None:
    # Either explicit `try_files ... /index.html` or `index index.html` will
    # cover SPA routes. We require the fallback path to be present.
    assert re.search(
        r"try_files\s+.*\s+/index\.html", nginx_conf
    ), "nginx must fall back to /index.html for SPA routes"


# ---------------------------------------------------------------------------
# Dockerfiles
# ---------------------------------------------------------------------------


def test_backend_dockerfile_exposes_8000(backend_dockerfile: str) -> None:
    assert re.search(r"^EXPOSE\s+8000\b", backend_dockerfile, re.MULTILINE)


def test_backend_dockerfile_uses_uvicorn(backend_dockerfile: str) -> None:
    assert "uvicorn" in backend_dockerfile


def test_backend_dockerfile_runs_as_non_root(backend_dockerfile: str) -> None:
    # Multi-stage build must drop privileges in the runtime stage.
    assert re.search(
        r"^USER\s+\w+", backend_dockerfile, re.MULTILINE
    ), "backend Dockerfile must switch to a non-root USER"


def test_frontend_dockerfile_uses_nginx(frontend_dockerfile: str) -> None:
    assert "nginx" in frontend_dockerfile.lower()


def test_frontend_dockerfile_exposes_80(frontend_dockerfile: str) -> None:
    assert re.search(r"^EXPOSE\s+80\b", frontend_dockerfile, re.MULTILINE)


def test_frontend_dockerfile_bakes_api_base_url(frontend_dockerfile: str) -> None:
    # The ARG / ENV wiring is what allows the same image to target
    # different backends across environments.
    assert "API_BASE_URL" in frontend_dockerfile


# ---------------------------------------------------------------------------
# .dockerignore
# ---------------------------------------------------------------------------


# Patterns every .dockerignore should exclude. `.dockerignore` is a
# glob syntax; we keep the patterns simple so the matcher above can
# reason about them.
COMMON_DOCKERIGNORE_PATTERNS: set[str] = {
    ".git",
    "Dockerfile",
    ".dockerignore",
    ".env",
    ".idea",
    ".vscode",
    ".DS_Store",
}


BACKEND_DOCKERIGNORE_PATTERNS: set[str] = {
    "__pycache__",
    "*.py[cod]",  # also covers *.pyc, *.pyo, *.pyd
    ".pytest_cache",
    ".coverage",
    "htmlcov",
    "tests",
    ".venv",
    "venv",
    "*.log",
}


FRONTEND_DOCKERIGNORE_PATTERNS: set[str] = {
    "node_modules",
    "dist",
    ".angular",
    "coverage",
    "*.tsbuildinfo",
    "*.log",
}


def _dockerignore_lines(text: str) -> list[str]:
    """Parse a `.dockerignore` body into its non-empty, non-comment lines.

    `.dockerignore` is a `gitignore`-style file: blank lines and `#`
    comments are ignored. We strip inline comments to keep the matcher
    simple and predictable.
    """
    lines: list[str] = []
    for raw in text.splitlines():
        # Drop inline comments first (handles `pattern # comment`).
        cleaned = raw.split("#", 1)[0].strip()
        if cleaned:
            lines.append(cleaned)
    return lines


def _dockerignore_matches(lines: list[str], pattern: str) -> bool:
    """Cheap gitignore-style matcher for the patterns we ship.

    Exact equality wins first. Otherwise each line is split on `*` and
    we check that the resulting segments appear in `pattern` in order
    (gitignore semantics: `*` matches any run of characters). Real
    `.dockerignore` supports negation, `**`, `[...]` char classes, etc.,
    but the patterns we generate here are simple wildcards.
    """
    if pattern in lines:
        return True
    for line in lines:
        if "*" not in line:
            continue
        segments = line.split("*")
        # All segments must appear in `pattern` in order.
        cursor = 0
        ok = True
        for idx, segment in enumerate(segments):
            if not segment:
                continue
            found = pattern.find(segment, cursor)
            if found < 0:
                ok = False
                break
            # First segment must be at the start; last segment must reach
            # the end of the pattern. Middle segments are unconstrained.
            if idx == 0 and found != 0:
                ok = False
                break
            if idx == len(segments) - 1 and found + len(segment) != len(pattern):
                ok = False
                break
            cursor = found + len(segment)
        if ok:
            return True
    return False


def test_backend_dockerignore_excludes_dev_artefacts(backend_dockerignore: str) -> None:
    lines = _dockerignore_lines(backend_dockerignore)
    assert lines, "backend/.dockerignore must not be empty"
    missing = {p for p in BACKEND_DOCKERIGNORE_PATTERNS if not _dockerignore_matches(lines, p)}
    assert (
        not missing
    ), f"backend/.dockerignore must exclude dev artefacts; missing: {sorted(missing)}"


def test_frontend_dockerignore_excludes_dev_artefacts(frontend_dockerignore: str) -> None:
    lines = _dockerignore_lines(frontend_dockerignore)
    assert lines, "frontend/.dockerignore must not be empty"
    missing = {p for p in FRONTEND_DOCKERIGNORE_PATTERNS if not _dockerignore_matches(lines, p)}
    assert (
        not missing
    ), f"frontend/.dockerignore must exclude dev artefacts; missing: {sorted(missing)}"


@pytest.mark.parametrize("pattern", sorted(COMMON_DOCKERIGNORE_PATTERNS))
def test_backend_dockerignore_excludes_common_patterns(
    backend_dockerignore: str, pattern: str
) -> None:
    lines = _dockerignore_lines(backend_dockerignore)
    assert _dockerignore_matches(lines, pattern), f"backend/.dockerignore must exclude {pattern!r}"


@pytest.mark.parametrize("pattern", sorted(COMMON_DOCKERIGNORE_PATTERNS))
def test_frontend_dockerignore_excludes_common_patterns(
    frontend_dockerignore: str, pattern: str
) -> None:
    lines = _dockerignore_lines(frontend_dockerignore)
    assert _dockerignore_matches(lines, pattern), f"frontend/.dockerignore must exclude {pattern!r}"


# ---------------------------------------------------------------------------
# frontend/package.json
# ---------------------------------------------------------------------------


REQUIRED_NPM_SCRIPTS: set[str] = {"start", "build", "test"}


def test_frontend_package_has_required_scripts(frontend_package: dict) -> None:
    scripts = set((frontend_package.get("scripts") or {}).keys())
    missing = REQUIRED_NPM_SCRIPTS - scripts
    assert not missing, f"frontend/package.json missing scripts: {sorted(missing)}"


def test_frontend_package_targets_angular_18(frontend_package: dict) -> None:
    # The PRD locks the frontend to Angular 18; guard against silent upgrades.
    deps = {
        **frontend_package.get("dependencies", {}),
        **frontend_package.get("devDependencies", {}),
    }
    for name in ("@angular/core", "@angular/cli"):
        assert name in deps, f"{name} must be declared in package.json"
        version = deps[name].lstrip("^~")
        assert version.startswith("18."), f"{name} must be on Angular 18.x"


def test_frontend_package_declares_tailwind(frontend_package: dict) -> None:
    deps = frontend_package.get("devDependencies", {})
    assert "tailwindcss" in deps, "tailwindcss must be a devDependency"


# ---------------------------------------------------------------------------
# backend/requirements.txt
# ---------------------------------------------------------------------------


REQUIRED_BACKEND_PACKAGES: set[str] = {
    "fastapi",
    "uvicorn",
    "pydantic",
    "pydantic-settings",
    "sqlalchemy",
    "asyncpg",
    "alembic",
    "redis",
    "arq",
    "httpx",
    # Auth & registration dependencies (issue #3). The
    # ``_normalise_requirements`` helper lower-cases the names
    # so we mirror the canonical pip spelling here.
    "bcrypt",
    "pyjwt",
    "email-validator",
}


def test_requirements_declares_runtime_packages(backend_requirements: str) -> None:
    declared = _normalise_requirements(backend_requirements)
    missing = REQUIRED_BACKEND_PACKAGES - declared
    assert not missing, f"requirements.txt missing packages: {sorted(missing)}"


# ---------------------------------------------------------------------------
# backend/pytest.ini
# ---------------------------------------------------------------------------
# The coverage gate is documented in CODING_STANDARDS.md (section 2.5)
# and CONTRIBUTING.md (section 7): `pytest` must fail when coverage on
# `app/` drops below 80%. That mechanism lives in `pytest.ini` and is
# exercised by CI – if the flag disappears, the gate disappears with it.
# ---------------------------------------------------------------------------


# `--cov-fail-under` is exposed in pytest's `addopts` line as a single
# string like `--cov=app --cov-report=term-missing --cov-fail-under=80`.
# Splitting on whitespace gives us a clean, order-independent list of
# option tokens we can assert against.
def _pytest_addopts(text: str) -> list[str]:
    match = re.search(r"^addopts\s*=\s*(.+)$", text, re.MULTILINE)
    assert match is not None, "pytest.ini must declare an `addopts` line"
    return match.group(1).split()


def test_pytest_ini_covers_app_module(backend_pytest_ini: str) -> None:
    """`--cov=app` is what makes the coverage report target the package
    that holds the production code. Without it, the gate would measure
    test files only and silently pass on empty coverage of `app/`."""
    tokens = _pytest_addopts(backend_pytest_ini)
    assert "--cov=app" in tokens, (
        "pytest.ini must include --cov=app in addopts " "(see CODING_STANDARDS.md §2.5)"
    )


def test_pytest_ini_emits_term_missing_report(backend_pytest_ini: str) -> None:
    """`--cov-report=term-missing` prints uncovered line ranges in the
    terminal so contributors can fix gaps locally. CODING_STANDARDS.md
    §2.5 pins this as the default report format."""
    tokens = _pytest_addopts(backend_pytest_ini)
    assert (
        "--cov-report=term-missing" in tokens
    ), "pytest.ini must include --cov-report=term-missing in addopts"


def test_pytest_ini_enforces_coverage_gate(backend_pytest_ini: str) -> None:
    """`--cov-fail-under=80` is the coverage gate. CONTRIBUTING.md §7
    states "Coverage <80% blocks PR merge" – if this flag is removed
    the contract is silently broken. We assert on both the flag and
    its value (==80) so accidental bumps (e.g. `=0` to silence CI)
    are caught by tests, not by reviewers."""
    tokens = _pytest_addopts(backend_pytest_ini)

    fail_under = [t for t in tokens if t.startswith("--cov-fail-under")]
    assert fail_under, (
        "pytest.ini must declare --cov-fail-under (see CODING_STANDARDS.md §2.5 "
        "and CONTRIBUTING.md §7)"
    )
    # Exactly one `--cov-fail-under` entry; multiple would mean the
    # later one wins, which is confusing.
    assert (
        len(fail_under) == 1
    ), f"pytest.ini must declare exactly one --cov-fail-under, got {fail_under!r}"

    # The value is `=N`; strip the prefix and compare as int so
    # `=80.0` or `=80 ` don't sneak through.
    raw_value = fail_under[0].split("=", 1)[1]
    assert raw_value == "80", f"--cov-fail-under must be 80 (CONTRIBUTING.md §7), got {raw_value!r}"


def test_pytest_ini_uses_asyncio_auto_mode(backend_pytest_ini: str) -> None:
    """`pytest-asyncio` is set to `auto` so every `async def test_*` is
    picked up without an explicit `@pytest.mark.asyncio` decoration.
    The coding standards (§2.5) rely on this default; losing it would
    mean every async test in the suite starts failing collection."""
    assert re.search(
        r"^asyncio_mode\s*=\s*auto\b", backend_pytest_ini, re.MULTILINE
    ), "pytest.ini must set `asyncio_mode = auto`"


# ---------------------------------------------------------------------------
# Makefile
# ---------------------------------------------------------------------------


REQUIRED_MAKE_TARGETS: set[str] = {
    "install",
    "ci",
    "backend-test",
    "backend-lint",
    "backend-typecheck",
    "frontend-test",
    "frontend-build",
    "frontend-lint",
    "compose-up",
    "compose-down",
    "compose-validate",
    "alembic-upgrade",
}


def test_makefile_exposes_documented_targets(makefile: str) -> None:
    pattern = r"^([a-zA-Z][\w-]*):"
    targets = {match.group(1) for match in re.finditer(pattern, makefile, re.MULTILINE)}
    missing = REQUIRED_MAKE_TARGETS - targets
    assert not missing, f"Makefile missing targets: {sorted(missing)}"


def test_makefile_targets_are_double_colon_phony(makefile: str) -> None:
    # We don't want the targets to silently no-op because a file with the
    # same name as the target exists in the repo.
    assert ".PHONY" in makefile, "Makefile should declare a .PHONY list"


# `make ci` is the local mirror of the GitHub Actions pipeline. It must
# chain the same set of checks the CI workflow runs so a green local
# build is a strong predictor of a green PR. The fixture below pins
# both the target's existence and the exact list of sub-targets it
# depends on; removing any of them would silently re-shape the gate
# and is the kind of regression the test was written to catch.
CI_REQUIRED_PREREQS: tuple[str, ...] = (
    "backend-lint",
    "backend-typecheck",
    "backend-test",
    "frontend-lint",
    "frontend-build",
    "compose-validate",
)


def _makefile_target_block(makefile: str, target: str) -> str | None:
    """Return the full block for ``target`` – the header line and
    every indented recipe line – or ``None`` if the target is
    missing. Recipe lines start with a tab, so we keep them
    verbatim; that lets us inspect dependencies (after the colon)
    and recipe commands alike."""
    lines = makefile.splitlines(keepends=True)
    header_re = re.compile(rf"^{re.escape(target)}\s*:")
    for idx, line in enumerate(lines):
        if header_re.match(line):
            block = [line]
            for following in lines[idx + 1 :]:
                # Recipe lines always start with a tab; a blank
                # line ends the block, as does a non-tab line (the
                # next target).
                if following.startswith("\t"):
                    block.append(following)
                elif following.strip() == "":
                    block.append(following)
                else:
                    break
            return "".join(block)
    return None


def test_makefile_ci_target_aggregates_pipeline(makefile: str) -> None:
    """The ``ci`` target must run every check the GitHub Actions
    workflow runs, in declaration order. CONTRIBUTING.md instructs
    contributors to use ``make ci`` as the pre-PR sanity check; if
    the dependency list silently drops a step the local gate is
    weaker than CI and the contract is broken."""
    block = _makefile_target_block(makefile, "ci")
    assert block is not None, "Makefile must define a `ci` target"
    for prereq in CI_REQUIRED_PREREQS:
        assert prereq in block, (
            f"`make ci` must depend on `{prereq}` so the local gate "
            f"matches CI; block was:\n{block}"
        )


def test_makefile_compose_validate_uses_docker_compose_config(
    makefile: str,
) -> None:
    """``compose-validate`` is the local mirror of the CI ``compose``
    job. It must invoke ``docker compose ... config --quiet`` so the
    fast path stays identical to what CI checks."""
    block = _makefile_target_block(makefile, "compose-validate")
    assert block is not None, "Makefile must define a `compose-validate` target"
    assert "docker compose" in block, "`compose-validate` must invoke `docker compose`"
    assert "config" in block, "`compose-validate` must run `docker compose ... config`"
    assert "--quiet" in block, (
        "`compose-validate` must pass `--quiet` so the command exits " "non-zero on parse errors"
    )


def test_makefile_ci_and_compose_validate_listed_in_phony(makefile: str) -> None:
    """`ci` and `compose-validate` are pure aggregator/parse targets
    – they have no on-disk artefact of the same name, but listing
    them in `.PHONY` keeps the intent explicit and prevents future
    files named `ci` or `compose-validate` from short-circuiting the
    recipe."""
    phony_match = re.search(r"^\.PHONY\s*:([^\n]*)", makefile, re.MULTILINE)
    assert phony_match is not None, "Makefile must declare a .PHONY list"
    phony_targets = phony_match.group(1).split()
    for target in ("ci", "compose-validate"):
        assert target in phony_targets, (
            f"`{target}` must be declared in .PHONY so it cannot be "
            "shadowed by a file with the same name"
        )


def test_makefile_help_documents_ci_and_compose_validate(
    makefile: str,
    contributing: str,
) -> None:
    """A target that isn't in ``make help`` is invisible to most
    contributors. Both ``ci`` and ``compose-validate`` must show up
    in the help block, and CONTRIBUTING.md must mention ``make ci``
    as the recommended pre-PR check."""
    for needle in ("ci", "compose-validate"):
        assert needle in makefile, f"Makefile must mention `{needle}`"
    assert "make ci" in contributing, "CONTRIBUTING.md must reference `make ci` as the pre-PR check"


# ---------------------------------------------------------------------------
# Documentation files (CODING_STANDARDS.md, CONTRIBUTING.md, README.md,
# .editorconfig) – these are the "infra" artefacts that complement the
# Docker / CI plumbing.
# ---------------------------------------------------------------------------


CODING_STANDARDS_SECTIONS: tuple[str, ...] = (
    "Repository layout",
    "Python",
    "TypeScript / Angular",
    "Docker",
    "CI / GitHub Actions",
    "Git workflow",
)


def test_coding_standards_covers_required_sections(coding_standards: str) -> None:
    """The standards doc must walk a contributor through the parts of
    the stack that have non-trivial conventions. Keep the section
    headings stable so internal links keep working."""
    for needle in CODING_STANDARDS_SECTIONS:
        assert needle in coding_standards, f"CODING_STANDARDS.md is missing the {needle!r} section"


def test_coding_standards_references_canonical_files(coding_standards: str) -> None:
    """A standards doc that doesn't anchor itself to real files is
    decorative. Make sure the obvious anchors are mentioned."""
    for anchor in (
        "`backend/`",
        "`frontend/`",
        "`docker-compose.yml`",
        ".env.example",
        "app/config.py",
    ):
        assert anchor in coding_standards, f"CODING_STANDARDS.md should mention {anchor}"


def test_contributing_documents_dev_loop(contributing: str) -> None:
    """CONTRIBUTING.md is the entry point for new contributors; it
    must call out the daily commands from the Makefile so a fresh
    checkout has a clear path to a green test run."""
    for needle in (
        "Prerequisites",
        "First-time setup",
        "Daily workflow",
        "make backend-test",
        "make frontend-test",
        "make compose-up",
        "Conventional Commits",
    ):
        assert needle in contributing, f"CONTRIBUTING.md is missing the {needle!r} section/command"


def test_contributing_links_to_security_and_templates(contributing: str) -> None:
    """The contributor guide is the natural entry point for
    someone who wants to report a bug, propose a feature or
    disclose a vulnerability. Each of those paths lives in a
    dedicated template / policy file, so `CONTRIBUTING.md` must
    point at them by name. A "follow the link" surface area
    with no links forces readers to spelunk through
    ``.github/`` themselves.

    The references are pinned as plain substrings (not full
    Markdown links) so the test stays robust against the exact
    link syntax – a missing reference is what we care about,
    not the angle brackets around it.
    """
    for needle in (
        # Security policy: the GH *Security* tab renders this file.
        "SECURITY.md",
        # Issue picker: the three templates live in this directory.
        ".github/ISSUE_TEMPLATE/",
        "bug_report.md",
        "feature_request.md",
        "security_advisory.md",
        # Pull request template.
        ".github/PULL_REQUEST_TEMPLATE.md",
    ):
        assert needle in contributing, (
            f"CONTRIBUTING.md must reference {needle!r} so contributors "
            f"can find the right template / policy"
        )


def test_readme_links_to_coding_standards_and_contributing(
    readme: str, coding_standards: str, contributing: str
) -> None:
    """The standards and contributing docs are only useful if
    contributors can find them from the README. Pin the link paths
    so a stray rename surfaces immediately."""
    # Both docs must point at each other and at PRD.md so navigation
    # works in either direction.
    assert "PRD.md" in coding_standards
    assert "PRD.md" in contributing
    assert "CODING_STANDARDS.md" in contributing
    assert "CONTRIBUTING.md" in coding_standards

    # README only needs to mention the existence of the docs; the
    # exact wording is allowed to evolve.
    readme_lower = readme.lower()
    assert (
        "coding_standards" in readme_lower or "coding standards" in readme_lower
    ), "README.md should reference CODING_STANDARDS.md"
    assert "contributing" in readme_lower, "README.md should reference CONTRIBUTING.md"


def test_editorconfig_declares_root_and_python_block(editorconfig: str) -> None:
    """`.editorconfig` is opt-in per editor; declaring `root = true`
    at the top is what makes the file apply repo-wide instead of
    inheriting from a parent directory."""
    assert re.search(
        r"^root\s*=\s*true", editorconfig, re.MULTILINE
    ), ".editorconfig must declare `root = true`"
    assert "[*.py]" in editorconfig, ".editorconfig must cover Python files"
    assert (
        "indent_size = 4" in editorconfig
    ), "Python should use 4-space indentation (matches the codebase)"


def test_editorconfig_covers_frontend_and_dockerfile(editorconfig: str) -> None:
    # The frontend ships TypeScript / HTML / SCSS / JSON; all of them
    # should use 2-space indentation per the Angular community default.
    assert (
        "[*.{ts,tsx,js,jsx,mjs,cjs,html,scss,css}]" in editorconfig
    ), ".editorconfig should cover frontend source files"
    assert "[Dockerfile*]" in editorconfig, ".editorconfig should cover Dockerfiles"
    # Makefiles require literal tabs; ensure we override the default
    # `space` indent_style for them.
    assert re.search(
        r"\[Makefile\][\s\S]+indent_style\s*=\s*tab", editorconfig
    ), "Makefile section in .editorconfig must use tab indentation"


# ---------------------------------------------------------------------------
# Tool version pinning: `.nvmrc` and `.python-version` are the standard
# hooks `nvm` / `pyenv` / `asdf` consult to auto-switch the local tool
# version. The CI workflow hardcodes the same versions, so the files act
# as a contributor-friendly mirror: anyone cloning the repo gets the
# same Python / Node versions CI uses without reading a doc.
# ---------------------------------------------------------------------------


def _stripped_version(raw: str) -> str:
    """Return the first non-empty, non-comment line of a version file.

    Both `.nvmrc` and `.python-version` use a single line with the
    version, optionally prefixed with `v` (e.g. `v20.10.0`) or with a
    trailing newline. We trim, drop a leading `v` and ignore blank /
    comment lines so the assertions below are robust to either format.
    """
    for line in raw.splitlines():
        candidate = line.strip()
        if not candidate or candidate.startswith("#"):
            continue
        return candidate.lstrip("v")
    return ""


def test_nvmrc_pins_node_20(nvmrc: str) -> None:
    """The CI workflow uses `node-version: "20"`; the file should
    mirror that so `nvm use` matches what the `frontend` CI job runs.
    A major version (`20`) is the convention – it keeps the file
    stable while still benefiting from patch updates."""
    version = _stripped_version(nvmrc)
    assert version, ".nvmrc must declare a Node version"
    assert version.split(".")[0] == "20", f".nvmrc must pin Node 20.x (matches CI), got {version!r}"


def test_python_version_file_pins_python_312(python_version_file: str) -> None:
    """The CI workflow uses `python-version: "3.12"`; the file should
    mirror it so `pyenv` / `asdf` auto-switch. Python is pinned to a
    full `major.minor` because the backend Dockerfile is built on the
    `python:3.12-slim` image and the runtime wheels are tied to that
    minor version."""
    version = _stripped_version(python_version_file)
    assert version, ".python-version must declare a Python version"
    parts = version.split(".")
    assert len(parts) >= 2, f".python-version must include a minor version, got {version!r}"
    assert (
        parts[0] == "3" and parts[1] == "12"
    ), f".python-version must pin Python 3.12 (matches CI), got {version!r}"


def test_nvmrc_and_python_version_agree_with_ci(
    nvmrc: str, python_version_file: str, ci_doc: dict
) -> None:
    """Cross-check: the tool versions the version files declare must
    match the versions the CI workflow installs. A silent drift
    between the two means a green local build but a red CI run (or
    vice-versa); the test exists to make that drift loud."""
    node_pin = _stripped_version(nvmrc).split(".")[0]
    python_pin = _stripped_version(python_version_file)
    python_pin_mm = ".".join(python_pin.split(".")[:2])

    # CI uses setup-node / setup-python; their `with.python-version`
    # / `with.node-version` keys hold the canonical pin.
    frontend_steps = ci_doc["jobs"]["frontend"]["steps"]
    node_setup = _find_step(frontend_steps, lambda s: "setup-node" in s.get("uses", ""))
    assert node_setup is not None, "frontend CI job must set up Node"
    ci_node = str(node_setup.get("with", {}).get("node-version", ""))
    assert (
        ci_node.split(".")[0] == node_pin
    ), f".nvmrc pins Node {node_pin} but CI installs {ci_node!r}"

    backend_steps = ci_doc["jobs"]["backend"]["steps"]
    python_setup = _find_step(backend_steps, lambda s: "setup-python" in s.get("uses", ""))
    assert python_setup is not None, "backend CI job must set up Python"
    ci_python = str(python_setup.get("with", {}).get("python-version", ""))
    assert (
        ci_python == python_pin_mm
    ), f".python-version pins Python {python_pin_mm} but CI installs {ci_python!r}"


# ---------------------------------------------------------------------------
# Backend pyproject.toml: pins Ruff + Mypy configuration. The PRD calls for
# both tools in CI; this file is the single source of truth for their
# settings, so any change to the config has to land here.
# ---------------------------------------------------------------------------


def test_backend_pyproject_declares_ruff_config(backend_pyproject: str) -> None:
    assert "[tool.ruff]" in backend_pyproject, "pyproject.toml must declare a [tool.ruff] section"
    assert "line-length" in backend_pyproject, "Ruff config must pin a line-length"


def test_backend_pyproject_declares_mypy_config(backend_pyproject: str) -> None:
    assert "[tool.mypy]" in backend_pyproject, "pyproject.toml must declare a [tool.mypy] section"
    assert "python_version" in backend_pyproject, "Mypy config must pin a python_version"


# ---------------------------------------------------------------------------
# Frontend ESLint configuration: Angular 18 ships with `ng lint` backed
# by `@angular-eslint`. The presence of `.eslintrc.json` and the angular
# builder wiring in `angular.json` is the contract the lint job depends on.
# ---------------------------------------------------------------------------


def test_frontend_eslintrc_exists(frontend_eslintrc: str) -> None:
    assert frontend_eslintrc, "frontend/.eslintrc.json must not be empty"
    assert "root" in frontend_eslintrc, ".eslintrc.json must declare `root`"
    assert (
        "@angular-eslint" in frontend_eslintrc
    ), ".eslintrc.json must extend the @angular-eslint recommended config"


# ---------------------------------------------------------------------------
# CI workflow: the `backend` job runs ruff + mypy + pytest and the
# `frontend` job runs eslint + build + test. The previous fixture tests
# pin the *shape* of the jobs; these pin the *commands* the issue calls
# for (lint, typecheck, test).
# ---------------------------------------------------------------------------


def test_ci_backend_job_runs_ruff(ci_doc: dict) -> None:
    steps = ci_doc["jobs"]["backend"]["steps"]
    run = _find_step(steps, lambda s: "ruff" in (s.get("run") or ""))
    assert run is not None, "backend job must run ruff"


def test_ci_backend_job_runs_mypy(ci_doc: dict) -> None:
    steps = ci_doc["jobs"]["backend"]["steps"]
    run = _find_step(steps, lambda s: "mypy" in (s.get("run") or ""))
    assert run is not None, "backend job must run mypy"


def test_ci_frontend_job_runs_eslint(ci_doc: dict) -> None:
    steps = ci_doc["jobs"]["frontend"]["steps"]
    run = _find_step(steps, lambda s: "lint" in (s.get("run") or ""))
    assert run is not None, "frontend job must run npm run lint"


# ---------------------------------------------------------------------------
# Frontend Karma configuration: the CI workflow runs
# `npm test -- --browsers=ChromeHeadlessCI`. The `CI` launcher is
# not part of the default karma-chrome-launcher distribution, so the
# project must ship a `karma.conf.js` that registers it as a custom
# launcher; otherwise the `frontend` CI job fails before any test
# runs.
# ---------------------------------------------------------------------------


KARMA_CONF_PATH = REPO_ROOT / "frontend" / "karma.conf.js"


@pytest.fixture(scope="module")
def karma_conf() -> str:
    return KARMA_CONF_PATH.read_text(encoding="utf-8")


def test_frontend_karma_conf_exists() -> None:
    assert KARMA_CONF_PATH.is_file(), (
        "frontend/karma.conf.js must exist so the CI can register "
        "the ChromeHeadlessCI custom launcher"
    )


def test_frontend_karma_conf_registers_chrome_headless_ci(karma_conf: str) -> None:
    """The CI workflow passes `--browsers=ChromeHeadlessCI`; the
    karma config must register that exact launcher name or the test
    job crashes with "browser not registered" before running."""
    assert "ChromeHeadlessCI" in karma_conf, (
        "frontend/karma.conf.js must register the ChromeHeadlessCI "
        "custom launcher used by the CI workflow"
    )
    assert (
        "customLaunchers" in karma_conf
    ), "frontend/karma.conf.js must define a customLaunchers map"


def test_frontend_karma_conf_uses_chrome_headless_base(karma_conf: str) -> None:
    """The CI launcher must extend `ChromeHeadless` (not `Chrome`)
    so it can run inside a container without a display server."""
    assert (
        "base: 'ChromeHeadless'" in karma_conf
    ), "ChromeHeadlessCI must extend ChromeHeadless (headless mode)"


# ---------------------------------------------------------------------------
# angular.json: the `test` architect target must load `karma.conf.js` so
# the ChromeHeadlessCI custom launcher (registered in that file) is
# available when CI runs `npm test -- --browsers=ChromeHeadlessCI`.
# Without the wiring the Angular CLI generates a default karma config
# on the fly, the `ChromeHeadlessCI` launcher is never registered and
# the `frontend` CI job crashes with "browser not registered" before
# any spec runs. The fixture below pins the link between the two
# files so a future edit to `angular.json` (e.g. dropping
# `karmaConfig`) cannot silently break CI.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def frontend_angular_json() -> dict:
    text = (REPO_ROOT / "frontend" / "angular.json").read_text(encoding="utf-8")
    return json_loads(text)


def _angular_test_target(angular: dict) -> dict:
    """Return the `test` architect target for the `msg-gateway` project.

    Helper kept narrow on purpose: every other architect target is
    irrelevant to the karma wiring, and the `test` target is the one
    `ng test` and the CI workflow actually invoke.
    """
    projects = angular.get("projects", {})
    targets = projects.get("msg-gateway", {}).get("architect", {})
    target = targets.get("test")
    assert target is not None, (
        "angular.json must define a `test` architect target under " "projects.msg-gateway.architect"
    )
    return target


def test_angular_test_target_wires_karma_config(frontend_angular_json: dict) -> None:
    """The `test` target must declare `karmaConfig: "karma.conf.js"`
    so the project's karma configuration (and its
    `ChromeHeadlessCI` custom launcher) is loaded by `ng test`."""
    options = _angular_test_target(frontend_angular_json).get("options", {})
    karma_config = options.get("karmaConfig")
    assert karma_config == "karma.conf.js", (
        "angular.json projects.msg-gateway.architect.test.options "
        f'must set `karmaConfig: "karma.conf.js"`, got {karma_config!r}'
    )


def test_angular_test_target_karma_config_points_at_existing_file(
    frontend_angular_json: dict,
) -> None:
    """The path declared in `karmaConfig` must resolve to a real
    file on disk; a stale relative path would make `ng test` fail
    with a confusing "config not found" error before the launcher
    registration check ever runs."""
    options = _angular_test_target(frontend_angular_json).get("options", {})
    karma_config = options.get("karmaConfig")
    assert karma_config, (
        "angular.json test target must declare a `karmaConfig` "
        "option (see CODING_STANDARDS.md §3.5 and the `frontend` CI job)"
    )
    # `ng test` resolves the path relative to the `frontend/` working
    # directory, not the repository root.
    resolved = REPO_ROOT / "frontend" / karma_config
    assert resolved.is_file(), (
        f"angular.json `karmaConfig` points at {karma_config!r}, "
        f"but {resolved} does not exist on disk"
    )


# ---------------------------------------------------------------------------
# Alembic: the migrations directory must ship with the backend image.
# ---------------------------------------------------------------------------


ALEMBIC_INI_PATH = BACKEND_DIR / "alembic.ini"
ALEMBIC_DIR_PATH = BACKEND_DIR / "alembic"


def test_alembic_ini_is_present() -> None:
    assert ALEMBIC_INI_PATH.is_file(), "alembic.ini must ship in backend/"


def test_alembic_directory_is_present() -> None:
    assert (ALEMBIC_DIR_PATH / "env.py").is_file(), "alembic/env.py must ship"
    assert (
        ALEMBIC_DIR_PATH / "versions"
    ).is_dir(), "alembic/versions/ must exist so autogenerate has a place to write"


# ---------------------------------------------------------------------------
# Repository infrastructure – the GitHub-level files that round out the
# scaffold. Each piece is small but the contract is non-trivial (e.g.
# CODEOWNERS must mention both backend and frontend, dependabot must
# track both ecosystems); the fixture tests below pin the contract.
# ---------------------------------------------------------------------------


# `Path` constants first; fixtures in the next block reuse them. Keeping
# the paths at module scope makes the test names self-explanatory: a
# failure message says "SECURITY.md is missing" rather than "the
# security file is missing".
LICENSE_PATH = REPO_ROOT / "LICENSE"
GITATTRIBUTES_PATH = REPO_ROOT / ".gitattributes"
CODEOWNERS_PATH = REPO_ROOT / ".github" / "CODEOWNERS"
DEPENDABOT_PATH = REPO_ROOT / ".github" / "dependabot.yml"
SECURITY_MD_PATH = REPO_ROOT / "SECURITY.md"
PR_TEMPLATE_PATH = REPO_ROOT / ".github" / "PULL_REQUEST_TEMPLATE.md"
ISSUE_TEMPLATE_DIR = REPO_ROOT / ".github" / "ISSUE_TEMPLATE"
ISSUE_CONFIG_PATH = ISSUE_TEMPLATE_DIR / "config.yml"
CHANGELOG_PATH = REPO_ROOT / "CHANGELOG.md"


@pytest.fixture(scope="module")
def codeowners_text() -> str:
    return CODEOWNERS_PATH.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def dependabot_doc() -> dict:
    return yaml.safe_load(DEPENDABOT_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def issue_templates() -> list[Path]:
    return sorted(ISSUE_TEMPLATE_DIR.glob("*.md"))


# --- LICENSE ---------------------------------------------------------------


def test_license_file_is_present() -> None:
    """A project that links to a LICENSE in `pyproject.toml` /
    `package.json` or imports third-party code with copyleft clauses
    cannot ship without one. The file is what every package manager
    reads when generating attribution pages."""
    assert LICENSE_PATH.is_file(), "LICENSE must ship at the repository root"


def test_license_is_apache_2() -> None:
    """The file is named `LICENSE` (not `LICENSE.md`) and must be a
    full Apache 2.0 license text so downstream consumers can include
    the notice verbatim. We assert on the canonical header line
    instead of the full text to stay robust to whitespace changes."""
    text = LICENSE_PATH.read_text(encoding="utf-8")
    assert "Apache License" in text, "LICENSE must be the Apache License text"
    assert "Version 2.0" in text, "LICENSE must declare Apache 2.0"
    assert (
        "Apache License, Version 2.0" in text
    ), "LICENSE must include the canonical 'Version 2.0' line"


# --- .gitattributes --------------------------------------------------------


def test_gitattributes_is_present() -> None:
    """`gitattributes` is what stops CRLF/LF mismatches from sneaking
    into the repo on Windows. Without it, every `git diff` of an
    otherwise untouched file shows the whole file as changed."""
    assert GITATTRIBUTES_PATH.is_file(), ".gitattributes must ship at the repository root"


def test_gitattributes_declares_lf_default() -> None:
    """The default rule must force LF for every file. A weaker
    `text=auto` is fine for new files but does not normalise an
    already-checked-in CRLF; `eol=lf` is the only way to make
    `git checkout` re-write the working tree to LF."""
    text = GITATTRIBUTES_PATH.read_text(encoding="utf-8")
    assert re.search(
        r"^\*\s+text=auto\s+eol=lf\b", text, re.MULTILINE
    ), ".gitattributes must declare `*  text=auto eol=lf` as the default"


def test_gitattributes_marks_lockfiles_and_coverage_binary() -> None:
    """Lockfiles and coverage outputs should be marked so `git diff`
    does not try to display them and `git grep` does not try to
    search them."""
    text = GITATTRIBUTES_PATH.read_text(encoding="utf-8")
    assert "package-lock.json" in text, ".gitattributes must declare an entry for package-lock.json"
    assert "backend/.coverage" in text, ".gitattributes must declare backend/.coverage as binary"
    # PNG is the canonical binary marker; if we mark PNG binary we
    # cover the more exotic formats that follow.
    assert re.search(
        r"^\*\.png\s+binary\b", text, re.MULTILINE
    ), ".gitattributes must mark *.png as binary"


def test_gitattributes_overrides_bat_to_crlf() -> None:
    """The platform-neutral default is LF, but `.bat` / `.cmd` /
    `.ps1` rely on a trailing CR; the file must override the
    default for those extensions only."""
    text = GITATTRIBUTES_PATH.read_text(encoding="utf-8")
    assert re.search(
        r"^\*\.bat\s+text\s+eol=crlf\b", text, re.MULTILINE
    ), ".gitattributes must override .bat to CRLF"


# --- .git-blame-ignore-revs -------------------------------------------------
# A project-wide formatter / license header / import-sort pass touches
# almost every line in the tree and drowns the actual authorship signal
# in `git blame`. The standard remedy is to list those revisions in
# `.git-blame-ignore-revs` and let `git` (>= 2.23) skip them
# automatically. The fixture tests below pin the file's presence and
# shape so a future careless delete is caught here, not as a confusing
# "why is `git blame` showing a 2024 format commit for code I wrote in
# 2026?" question on the PR.


GIT_BLAME_IGNORE_REVS_PATH = REPO_ROOT / ".git-blame-ignore-revs"


def test_git_blame_ignore_revs_is_present() -> None:
    """A missing `.git-blame-ignore-revs` means every project-wide
    refactor commit will pollute `git blame` until a contributor
    notices and re-adds the file. Pin the presence so a careless
    delete is caught by tests."""
    assert (
        GIT_BLAME_IGNORE_REVS_PATH.is_file()
    ), ".git-blame-ignore-revs must ship at the repository root"


def test_git_blame_ignore_revs_has_at_least_one_entry() -> None:
    """An empty `.git-blame-ignore-revs` is equivalent to no file at
    all – `git` silently treats it as a no-op. The file must
    declare at least one full SHA so the contract is real."""
    text = GIT_BLAME_IGNORE_REVS_PATH.read_text(encoding="utf-8")
    shas = [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    assert shas, ".git-blame-ignore-revs must list at least one revision"


def test_git_blame_ignore_revs_entries_are_full_shas() -> None:
    """`git blame --ignore-rev` requires a full 40-character SHA
    (or at least an unambiguous abbreviated one). A truncated or
    typo'd SHA would either be silently ignored or fail with a
    non-obvious error. We assert on the canonical 40-character
    hex shape so contributors catch the mistake at the lint
    step instead of at the next `git blame` run."""
    text = GIT_BLAME_IGNORE_REVS_PATH.read_text(encoding="utf-8")
    sha_re = re.compile(r"\b[0-9a-f]{40}\b")
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        assert sha_re.fullmatch(line), (
            f".git-blame-ignore-revs entries must be full 40-char SHAs; "
            f"got {line!r}"
        )


# --- .gitignore -------------------------------------------------------------
# `.gitignore` is the contract that decides which on-disk artefacts ever
# leave the contributor's working tree. A missing pattern shows up as
# noise in `git status`; a *missing* file means every byte the developer
# creates locally is potentially eligible for an accidental commit.
# The fixture tests below pin the patterns the scaffold relies on so a
# careless edit (e.g. removing `.venv/` while cleaning up) surfaces here
# instead of in a "why is my virtualenv tracked?" review thread.


def _gitignore_lines(text: str) -> list[str]:
    """Return the non-empty, non-comment lines of a `.gitignore`.

    Mirrors the parsing rules of ``git`` itself: blank lines and lines
    starting with ``#`` are ignored. We deliberately do *not* try to
    apply the full gitignore glob semantics; the patterns we ship are
    simple directory or extension patterns that match by equality or
    prefix.
    """
    lines: list[str] = []
    for raw in text.splitlines():
        cleaned = raw.split("#", 1)[0].strip()
        if cleaned:
            lines.append(cleaned)
    return lines


def _gitignore_has_pattern(lines: list[str], pattern: str) -> bool:
    """Return ``True`` when ``lines`` declares ``pattern`` directly.

    ``.gitignore`` accepts two equivalent spellings for a directory
    pattern: ``foo`` and ``foo/`` (the trailing slash pins the entry
    to directories only, which is what contributors want for the
    patterns this scaffold ships). We accept both so a contributor
    can normalise the file either way without breaking the test.
    """
    if pattern in lines:
        return True
    # `foo` is also satisfied by `foo/`, and vice versa.
    if pattern.endswith("/") and pattern[:-1] in lines:
        return True
    if (pattern + "/") in lines:
        return True
    return False


# Patterns the root `.gitignore` must declare so a fresh clone does not
# leave dev / build / test artefacts in `git status`. Keep this set in
# sync with the comments at the top of `.gitignore`.
REQUIRED_ROOT_GITIGNORE_PATTERNS: tuple[str, ...] = (
    # --- Python build / test / dev artefacts ---
    "__pycache__",
    "*.pyc",
    ".pytest_cache",
    ".coverage",
    "htmlcov",
    ".venv",
    ".venv-test",  # test virtualenv created by sandboxed test runs
    "venv",
    "*.egg-info",
    # --- Node / Angular artefacts ---
    "node_modules",
    "dist",
    ".angular",
    # --- Local config / secrets / logs ---
    ".env",
    "*.log",
    # --- Editor / OS noise ---
    ".DS_Store",
    ".idea",
    ".vscode",
    # --- Tool caches ---
    ".mypy_cache",
    ".ruff_cache",
)


# Patterns the backend `.gitignore` must declare. Defence in depth:
# contributors running `git` from inside `backend/` see the same
# exclusions the root file lists, so a missed pattern in the root
# file does not silently start tracking backend-only artefacts.
REQUIRED_BACKEND_GITIGNORE_PATTERNS: tuple[str, ...] = (
    "__pycache__",
    "*.py[cod]",
    "*.egg-info",
    ".pytest_cache",
    ".coverage",
    "htmlcov",
    ".venv",
    ".venv-test",
    "venv",
    ".mypy_cache",
    ".ruff_cache",
    ".idea",
    ".vscode",
    ".DS_Store",
)


# Patterns the frontend `.gitignore` must declare. Mirrors the
# backend / root contracts but with the Node / Angular specific set.
REQUIRED_FRONTEND_GITIGNORE_PATTERNS: tuple[str, ...] = (
    "node_modules",
    "dist",
    ".angular",
    "coverage",
    ".env",
    ".env.local",
    ".idea",
    ".vscode",
    ".DS_Store",
)


@pytest.mark.parametrize("pattern", REQUIRED_ROOT_GITIGNORE_PATTERNS)
def test_root_gitignore_declares_pattern(root_gitignore: str, pattern: str) -> None:
    """Every standard scaffold artefact must be listed in the root
    ``.gitignore`` so a fresh clone ships with a clean ``git status``.
    A single missing pattern is enough to make contributors wonder
    whether ``node_modules/`` is *meant* to be tracked."""
    lines = _gitignore_lines(root_gitignore)
    assert _gitignore_has_pattern(lines, pattern), (
        f"root .gitignore must declare {pattern!r} " f"(see the comments at the top of .gitignore)"
    )


@pytest.mark.parametrize("pattern", REQUIRED_BACKEND_GITIGNORE_PATTERNS)
def test_backend_gitignore_declares_pattern(backend_gitignore: str, pattern: str) -> None:
    """The backend ``.gitignore`` must list the same Python-specific
    patterns as the root file. ``git`` consults whichever ``.gitignore``
    is closest to the path, so contributors working inside
    ``backend/`` rely on this file alone when running ``git add .``."""
    lines = _gitignore_lines(backend_gitignore)
    assert _gitignore_has_pattern(lines, pattern), f"backend/.gitignore must declare {pattern!r}"


@pytest.mark.parametrize("pattern", REQUIRED_FRONTEND_GITIGNORE_PATTERNS)
def test_frontend_gitignore_declares_pattern(frontend_gitignore: str, pattern: str) -> None:
    """The frontend ``.gitignore`` mirrors the backend's contract for
    Node / Angular artefacts. Same reasoning as the backend test:
    ``git`` resolves ignores from the path upwards, so this file is
    the last line of defence for the Angular workspace."""
    lines = _gitignore_lines(frontend_gitignore)
    assert _gitignore_has_pattern(lines, pattern), f"frontend/.gitignore must declare {pattern!r}"


def test_root_gitignore_excludes_venv_wildcard(root_gitignore: str) -> None:
    """``git`` treats ``.venv-*`` as a glob that matches ``.venv-test``,
    ``.venv-dev`` and similar; the contract is to keep the wildcard
    so future test / dev virtualenvs (e.g. ``.venv-lint``) are ignored
    without touching the file again."""
    lines = _gitignore_lines(root_gitignore)
    assert _gitignore_has_pattern(lines, ".venv-*"), (
        "root .gitignore must declare the `.venv-*` wildcard so future "
        "test virtualenvs are ignored without further edits"
    )


def test_backend_gitignore_excludes_venv_wildcard(
    backend_gitignore: str,
) -> None:
    """Same wildcard contract as the root file, replicated in
    ``backend/.gitignore`` for the in-tree workflow."""
    lines = _gitignore_lines(backend_gitignore)
    assert _gitignore_has_pattern(
        lines, ".venv-*"
    ), "backend/.gitignore must declare the `.venv-*` wildcard"


def test_root_gitignore_is_present() -> None:
    """A missing ``.gitignore`` is the single highest-impact
    infrastructure regression: every ``git add .`` would then
    stage the contributor's virtualenv, build cache and IDE
    metadata. Pin the file's presence so a careless delete is
    caught by tests, not by a code-review noise complaint."""
    assert (REPO_ROOT / ".gitignore").is_file(), ".gitignore must ship at the repository root"


def test_backend_gitignore_is_present() -> None:
    """Same reasoning as the root file: ``backend/.gitignore`` is the
    last-line filter for contributors working inside the backend
    directory."""
    assert (BACKEND_DIR / ".gitignore").is_file(), "backend/.gitignore must ship inside backend/"


def test_frontend_gitignore_is_present() -> None:
    """Same reasoning as the root file: ``frontend/.gitignore`` is
    the last-line filter for contributors working inside the
    Angular workspace."""
    assert (
        REPO_ROOT / "frontend" / ".gitignore"
    ).is_file(), "frontend/.gitignore must ship inside frontend/"


def test_makefile_clean_target_removes_test_venv(makefile: str) -> None:
    """``make clean`` is the local mirror of "drop every build
    artefact the scaffold generated". ``.venv-test`` is the test
    virtualenv a sandboxed test run creates; without an explicit
    ``rm -rf`` the directory lingers across clean / test cycles
    and consumes ~200 MB per worktree. Pin the entry so a future
    rename of the directory (e.g. to ``.venv-sandbox``) is
    accompanied by an update to the clean target."""
    block = _makefile_target_block(makefile, "clean")
    assert block is not None, "Makefile must define a `clean` target"
    assert ".venv-test" in block, (
        "`make clean` must remove the `.venv-test` directory so a "
        "fresh `make backend-test` does not see a stale virtualenv"
    )


# --- CODEOWNERS ------------------------------------------------------------


def test_codeowners_file_is_present() -> None:
    """A missing CODEOWNERS means GitHub falls back to "no
    reviewers", and PR review requests land in nobody's queue.
    The file must sit at `.github/CODEOWNERS` (the first path
    GitHub checks)."""
    assert (
        CODEOWNERS_PATH.is_file()
    ), "CODEOWNERS must live at .github/CODEOWNERS so GitHub picks it up"


def test_codeowners_has_a_default_owner(codeowners_text: str) -> None:
    """A CODEOWNERS file without a default (`*`) rule leaves any
    file not explicitly listed without an owner. The platform team
    must be the catch-all."""
    assert re.search(
        r"^\*\s+\S+", codeowners_text, re.MULTILINE
    ), "CODEOWNERS must declare a default owner (`* @team`)"


def test_codeowners_lists_backend_and_frontend(codeowners_text: str) -> None:
    """The monorepo has two app surfaces; CODEOWNERS must route
    PRs to the matching team. A missing entry means GitHub picks
    the default reviewer for backend / frontend code, which is the
    exact failure mode CODEOWNERS exists to prevent."""
    for path in ("/backend/", "/frontend/"):
        assert re.search(
            rf"^{re.escape(path)}\s+\S+",
            codeowners_text,
            re.MULTILINE,
        ), f"CODEOWNERS must own {path}"


def test_codeowners_assigns_security_to_security_md(codeowners_text: str) -> None:
    """`SECURITY.md` is the disclosure policy; it must be owned by
    the security team so a typo in the policy is reviewed by the
    right people."""
    assert re.search(
        r"^SECURITY\.md\s+\S*security\S*",
        codeowners_text,
        re.MULTILINE,
    ), "CODEOWNERS must route SECURITY.md to the security team"


def test_codeowners_lines_are_well_formed(codeowners_text: str) -> None:
    """Each non-comment line must be `<pattern> <owner>`, with at
    least one owner. Empty owners silently disable the rule
    (GitHub treats `  /backend/` as 'no owner') so we catch the
    shape before it ships."""
    for raw in codeowners_text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        assert len(parts) >= 2, f"CODEOWNERS line must be `<pattern> <owner>`; got {raw!r}"
        # The owner must look like `@org/team` or `@user`.
        assert all(
            p.startswith("@") for p in parts[1:]
        ), f"CODEOWNERS owners must start with '@'; got {raw!r}"


# --- dependabot.yml --------------------------------------------------------


def test_dependabot_file_is_present() -> None:
    """Dependabot is what keeps the dependency graph current; a
    missing config means CVEs sit unpatched. GitHub looks at
    `.github/dependabot.yml` (not `.yaml`)."""
    assert (
        DEPENDABOT_PATH.is_file()
    ), "dependabot.yml must live at .github/dependabot.yml (not .yaml)"


def test_dependabot_tracks_pip_and_npm(dependabot_doc: dict) -> None:
    """The monorepo has two package ecosystems; dependabot must
    track both. Pinning only one would leave the other stale."""
    ecosystems = {update["package-ecosystem"] for update in dependabot_doc.get("updates", [])}
    assert "pip" in ecosystems, "dependabot must track the pip ecosystem"
    assert "npm" in ecosystems, "dependabot must track the npm ecosystem"


def test_dependabot_tracks_docker_and_actions(dependabot_doc: dict) -> None:
    """Base image bumps and GitHub Actions releases are also CVEs;
    dependabot must watch both. Skipping docker means
    `python:3.12-slim` patches don't get auto-PRs."""
    ecosystems = {update["package-ecosystem"] for update in dependabot_doc.get("updates", [])}
    assert "docker" in ecosystems, "dependabot must track Docker base images"
    assert "github-actions" in ecosystems, "dependabot must track GitHub Actions versions"


def test_dependabot_targets_backend_and_frontend_dirs(
    dependabot_doc: dict,
) -> None:
    """Each ecosystem update must point at the right manifest
    directory. A pip update with `directory: /frontend` would scan
    the wrong manifest and open noisy / broken PRs."""
    pip_dirs = {
        u["directory"] for u in dependabot_doc["updates"] if u["package-ecosystem"] == "pip"
    }
    npm_dirs = {
        u["directory"] for u in dependabot_doc["updates"] if u["package-ecosystem"] == "npm"
    }
    assert "/backend" in pip_dirs, (
        "dependabot pip updates must target /backend " "(where requirements.txt lives)"
    )
    assert "/frontend" in npm_dirs, (
        "dependabot npm updates must target /frontend " "(where package.json lives)"
    )


# --- SECURITY.md -----------------------------------------------------------


def test_security_md_is_present() -> None:
    """A missing security policy means reporters have nowhere to
    go and the team has no disclosure window to point to. The
    file lives at the repository root so GitHub can render the
    *Security* tab automatically."""
    assert SECURITY_MD_PATH.is_file(), "SECURITY.md must ship at the repository root"


def test_security_md_contains_disclosure_instructions() -> None:
    """The doc must tell a reporter *how* to disclose and *what*
    to expect. A wall of text without an email address is
    equivalent to no policy at all."""
    text = SECURITY_MD_PATH.read_text(encoding="utf-8")
    # At least one of the canonical channels must be mentioned.
    for channel in ("security@", "Security Advisories", "private"):
        assert channel in text, f"SECURITY.md must mention a disclosure channel ({channel!r})"
    # The 90-day window is the project's commitment; it must be
    # written down so a future maintainer can change it on purpose.
    assert (
        "90" in text and "day" in text.lower()
    ), "SECURITY.md must declare a disclosure timeline (90 days)"


# --- Pull request / issue templates ----------------------------------------


def test_pull_request_template_is_present() -> None:
    """PR templates populate the description box; without one,
    contributors paste a one-liner and reviewers have to chase
    context."""
    assert (
        PR_TEMPLATE_PATH.is_file()
    ), "PULL_REQUEST_TEMPLATE.md must live at .github/PULL_REQUEST_TEMPLATE.md"


def test_pull_request_template_sections() -> None:
    """The template must prompt for the four sections a reviewer
    cannot derive from the diff: summary, what changed, how it
    was tested, and the contributor checklist."""
    text = PR_TEMPLATE_PATH.read_text(encoding="utf-8")
    for section in ("Summary", "What changed", "How it was tested", "Checklist"):
        assert section in text, f"PULL_REQUEST_TEMPLATE.md must include a {section!r} section"


def test_issue_template_dir_exists() -> None:
    """GitHub renders the directory as the *New issue* picker; a
    missing directory means every issue starts blank."""
    assert (
        ISSUE_TEMPLATE_DIR.is_dir()
    ), ".github/ISSUE_TEMPLATE/ must exist so GitHub can pick it up"


def test_issue_template_config_yml_is_present() -> None:
    """`config.yml` is what powers the picker; a missing file
    means the templates never show up even if the directory
    exists."""
    assert (
        ISSUE_CONFIG_PATH.is_file()
    ), ".github/ISSUE_TEMPLATE/config.yml must exist (picker definition)"


def test_issue_template_picker_is_valid_yaml() -> None:
    """The picker must parse as YAML and have a top-level
    `contact_links` list (or blank_issues_enabled flag). A typo
    would silently break the picker without an error to a
    contributor."""
    config = yaml.safe_load(ISSUE_CONFIG_PATH.read_text(encoding="utf-8"))
    assert isinstance(config, dict)
    assert "contact_links" in config, "issue template config.yml must define `contact_links`"


def test_issue_templates_cover_bug_and_feature(issue_templates: list[Path]) -> None:
    """We need at least a bug-report and a feature-request template;
    a security template is the third pillar of the picker."""
    names = {p.name for p in issue_templates}
    assert "bug_report.md" in names, ".github/ISSUE_TEMPLATE/ must include bug_report.md"
    assert "feature_request.md" in names, ".github/ISSUE_TEMPLATE/ must include feature_request.md"
    assert "security_advisory.md" in names, (
        ".github/ISSUE_TEMPLATE/ must include security_advisory.md " "(private disclosure path)"
    )


def test_issue_templates_have_yaml_front_matter(issue_templates: list[Path]) -> None:
    """Each template must open with a YAML front-matter block
    declaring its `name` and `description`. GitHub silently ignores
    templates that don't, so we pin the format here."""
    for path in issue_templates:
        text = path.read_text(encoding="utf-8")
        assert text.startswith("---"), f"{path.name} must start with YAML front-matter (`---`)"
        # The front-matter must declare a name; that's the label
        # GitHub shows on the picker card.
        assert re.search(
            r"^name:\s*\S+", text, re.MULTILINE
        ), f"{path.name} front-matter must declare a `name`"


# --- CHANGELOG.md ----------------------------------------------------------


def test_changelog_is_present() -> None:
    """The PR template (`PULL_REQUEST_TEMPLATE.md`) instructs
    contributors to update the changelog. A missing file means
    that instruction is a lie. We pin the file's existence so
    the contract stays honest."""
    assert CHANGELOG_PATH.is_file(), "CHANGELOG.md must ship at the repository root"


def test_changelog_uses_keep_a_changelog_sections() -> None:
    """The doc must follow [Keep a Changelog] conventions: a
    top-level `## [Unreleased]` heading and the three standard
    subsections (`Added`, `Changed`, `Fixed`). Tools that render
    release notes rely on this structure."""
    text = CHANGELOG_PATH.read_text(encoding="utf-8")
    assert "## [Unreleased]" in text, "CHANGELOG.md must declare a `## [Unreleased]` section"
    for section in ("### Added", "### Changed"):
        assert section in text, f"CHANGELOG.md must include the {section!r} subsection"


# ---------------------------------------------------------------------------
# .pre-commit-config.yaml
#
# The pre-commit framework is the local mirror of the CI pipeline
# (see CODING_STANDARDS.md §8). A missing or malformed config would
# silently disable the fast-feedback loop the standards doc relies on,
# so the fixture tests below pin both the file's presence and the
# specific hooks it must declare.
# ---------------------------------------------------------------------------


PRECOMMIT_CONFIG_PATH = REPO_ROOT / ".pre-commit-config.yaml"


# Hook ids the project's `.pre-commit-config.yaml` must declare. Each
# id matches a `pre-commit-hooks` entry (e.g. `trailing-whitespace`)
# or a `ruff-pre-commit` / local hook block; the fixture tests below
# iterate over this set so adding a new required hook is a one-line
# change.
REQUIRED_PRECOMMIT_HOOK_IDS: tuple[str, ...] = (
    # Hygiene hooks from the upstream `pre-commit-hooks` repo.
    "trailing-whitespace",
    "end-of-file-fixer",
    "check-yaml",
    "check-toml",
    "check-added-large-files",
    "check-merge-conflict",
    # Ruff covers the Python lint + format pipeline.
    "ruff",
    "ruff-format",
    # Mypy and ESLint are declared as local hooks (see below);
    # their ids must still be present so the framework indexes them.
    "mypy",
    "eslint",
)


def _precommit_hook_ids(config: dict) -> set[str]:
    """Flatten the `repos:` list into a set of hook ids.

    Pre-commit groups hooks under each `repos:` entry; the `hooks:`
    sub-list inside each entry is what carries the `id` we want to
    assert on. A single repo can contribute multiple hooks, so we
    iterate every entry and every hook list to collect all ids.
    """
    ids: set[str] = set()
    for repo in config.get("repos", []):
        for hook in repo.get("hooks", []):
            hook_id = hook.get("id")
            if hook_id:
                ids.add(hook_id)
    return ids


def test_precommit_config_is_present() -> None:
    """A missing `.pre-commit-config.yaml` means contributors have
    no local mirror of the CI pipeline. The file must live at the
    repository root so `pre-commit install` finds it without
    configuration."""
    assert (
        PRECOMMIT_CONFIG_PATH.is_file()
    ), ".pre-commit-config.yaml must ship at the repository root"


def test_precommit_config_parses_as_yaml(precommit_config: dict) -> None:
    """Pre-commit reads the file as YAML; a syntax error would
    crash the framework before the first hook runs. The parsed
    document must be a dict with the standard `repos:` list."""
    assert isinstance(precommit_config, dict)
    assert (
        "repos" in precommit_config
    ), ".pre-commit-config.yaml must declare a top-level `repos:` list"
    assert isinstance(precommit_config["repos"], list)
    assert precommit_config["repos"], ".pre-commit-config.yaml must declare at least one repo entry"


@pytest.mark.parametrize("hook_id", REQUIRED_PRECOMMIT_HOOK_IDS)
def test_precommit_config_declares_required_hook(precommit_config: dict, hook_id: str) -> None:
    """Each hook id in :data:`REQUIRED_PRECOMMIT_HOOK_IDS` must
    be declared by *some* `repos:` entry. A missing id means the
    local gate is weaker than CI: Ruff, Mypy or ESLint regressions
    would not be caught before a push."""
    ids = _precommit_hook_ids(precommit_config)
    assert hook_id in ids, (
        f".pre-commit-config.yaml must declare the {hook_id!r} hook " f"(got ids={sorted(ids)})"
    )


def test_precommit_config_declares_ruff_version_pin(precommit_config: dict) -> None:
    """The Ruff hook must pin a `rev:` so every contributor runs
    the same revision. A missing pin (or a floating `latest` /
    `main`) would silently change the rule set between dev
    machines and CI."""
    repos = precommit_config.get("repos", [])
    ruff_repos = [
        repo for repo in repos if "astral-sh/ruff-pre-commit" in str(repo.get("repo", ""))
    ]
    assert ruff_repos, ".pre-commit-config.yaml must declare a `astral-sh/ruff-pre-commit` repo"
    for repo in ruff_repos:
        rev = repo.get("rev", "")
        assert rev and not rev.startswith("latest"), (
            "ruff-pre-commit repo must pin a `rev:` (not `latest`); " f"got rev={rev!r}"
        )


def test_precommit_config_declares_min_pre_commit_version(
    precommit_config: dict,
) -> None:
    """A pinned `minimum_pre_commit_version` is what makes a
    contributor on an old pre-commit see a clean error message
    instead of a cryptic crash on the first hook. The standards
    doc (CODING_STANDARDS.md §8) assumes the field is present."""
    minimum = precommit_config.get("minimum_pre_commit_version")
    assert minimum, ".pre-commit-config.yaml must declare `minimum_pre_commit_version`"


def test_precommit_mypy_hook_targets_backend(precommit_config: dict) -> None:
    """The Mypy hook must run from the `backend/` directory so the
    `mypy_path = "."` setting in `backend/pyproject.toml` resolves
    to the right sources. A hook that runs at the repo root would
    miss the type stubs and fail to find `app.*` imports."""
    mypy_hooks = [
        hook
        for repo in precommit_config.get("repos", [])
        for hook in repo.get("hooks", [])
        if hook.get("id") == "mypy"
    ]
    assert mypy_hooks, "Mypy hook must be declared"
    for hook in mypy_hooks:
        entry = hook.get("entry", "")
        assert "backend" in entry, (
            "Mypy hook must `cd backend` before invoking mypy so the "
            f"backend's mypy_path config is honoured; got entry={entry!r}"
        )


def test_precommit_eslint_hook_targets_frontend(precommit_config: dict) -> None:
    """The ESLint hook must run from the `frontend/` directory so
    `npm run lint` finds `node_modules/.bin/eslint` and the
    Angular ESLint config. A hook that runs at the repo root
    would crash with "command not found" before linting a single
    file."""
    eslint_hooks = [
        hook
        for repo in precommit_config.get("repos", [])
        for hook in repo.get("hooks", [])
        if hook.get("id") == "eslint"
    ]
    assert eslint_hooks, "ESLint hook must be declared"
    for hook in eslint_hooks:
        entry = hook.get("entry", "")
        assert "frontend" in entry, (
            "ESLint hook must `cd frontend` before invoking npm; " f"got entry={entry!r}"
        )


def test_precommit_eslint_hook_matches_frontend_sources(
    precommit_config: dict,
) -> None:
    """The ESLint hook must declare a `files:` pattern that
    actually matches the frontend sources; relying on
    `types: [ts, tsx, javascript, jsx]` is a known anti-pattern
    in pre-commit because ``ts`` and ``tsx`` are not built-in
    file types – the framework silently treats the filter as
    "matches nothing" and reports ``no files to check`` on every
    commit, hiding any future ESLint regression.

    The test enforces three properties that together prove the
    hook is wired against the real Angular source tree:

    1. The hook does **not** declare ``types:`` (the broken
       shape we just escaped from).
    2. The hook declares a ``files:`` regex.
    3. The regex matches at least one ``.ts`` file under
       ``frontend/src/`` – i.e. the regex actually does what
       it claims, instead of being a typo of ``\\.js$`` or a
       string that pre-commit would otherwise reject.
    """
    eslint_hooks = [
        hook
        for repo in precommit_config.get("repos", [])
        for hook in repo.get("hooks", [])
        if hook.get("id") == "eslint"
    ]
    assert eslint_hooks, "ESLint hook must be declared"
    for hook in eslint_hooks:
        assert "types" not in hook, (
            "ESLint hook must not use `types:` to filter files – "
            "pre-commit does not recognise `ts` / `tsx` as built-in "
            "file types and would silently skip every frontend file. "
            "Use `files:` (regex) instead; got hook="
            f"{hook!r}"
        )
        assert "files" in hook, (
            "ESLint hook must declare a `files:` regex so the hook "
            "actually triggers on frontend sources; got hook="
            f"{hook!r}"
        )
        pattern = str(hook["files"])
        # `re.search` with the (anchored) pattern against any TS
        # file under `frontend/src/` proves the regex matches
        # something the Angular project actually ships.
        ts_files = sorted((REPO_ROOT / "frontend" / "src").rglob("*.ts"))
        assert ts_files, (
            "frontend/src must contain at least one .ts file for the "
            "ESLint hook filter test to be meaningful"
        )
        matched = any(re.search(pattern, str(path)) for path in ts_files)
        assert matched, (
            "ESLint hook `files:` regex must match at least one "
            f"frontend .ts file; pattern={pattern!r}, "
            f"sample files={[str(p) for p in ts_files[:3]]!r}"
        )


def test_precommit_requirements_lists_precommit(
    backend_requirements_dev: str,
) -> None:
    """The `pre-commit` framework must be a development dependency
    so the local install (`make backend-install`) puts the
    `pre-commit` binary on $PATH. Without it, `make
    precommit-install` cannot wire the git hook and the standards
    doc's promise of "pre-commit runs the same checks as CI"
    becomes a lie."""
    declared = _normalise_requirements(backend_requirements_dev)
    assert "pre-commit" in declared, (
        "backend/requirements-dev.txt must declare `pre-commit` "
        "so the framework binary is installed alongside ruff/mypy"
    )


def test_makefile_exposes_precommit_targets(makefile: str) -> None:
    """`make precommit-install` and `make precommit-run` are the
    entry points every contributor uses; a missing target means
    the standards doc's promise of "pre-commit on every commit"
    is unwired. The tests pin both targets so a future rename
    surfaces here instead of in a contributor's first commit."""
    for target in ("precommit-install", "precommit-run"):
        block = _makefile_target_block(makefile, target)
        assert block is not None, f"Makefile must define a `{target}` target"


def test_makefile_precommit_install_uses_precommit_binary(
    makefile: str,
) -> None:
    """The `precommit-install` target must actually call
    `pre-commit install`; a target that just echoes "done" would
    leave the git hook unwired and the whole infra silent."""
    block = _makefile_target_block(makefile, "precommit-install")
    assert block is not None
    assert "pre-commit install" in block, (
        "`precommit-install` must invoke `pre-commit install` to "
        "wire the git hook; got:\n" + block
    )


def test_makefile_precommit_targets_listed_in_phony(makefile: str) -> None:
    """The pre-commit targets are pure recipes (no on-disk
    artefact of the same name) – listing them in `.PHONY` keeps
    them runnable even if a file named `precommit-install` ever
    lands in the working tree."""
    phony_match = re.search(r"^\.PHONY\s*:([^\n]*)", makefile, re.MULTILINE)
    assert phony_match is not None, "Makefile must declare a .PHONY list"
    phony_targets = phony_match.group(1).split()
    for target in ("precommit-install", "precommit-run", "precommit-clean"):
        assert target in phony_targets, (
            f"`{target}` must be declared in .PHONY so it cannot be "
            "shadowed by a file with the same name"
        )


def test_coding_standards_documents_precommit(coding_standards: str) -> None:
    """CODING_STANDARDS.md §8 is the contributor-facing doc for
    the pre-commit workflow. The section must mention the file
    and the framework so a contributor reading the standards
    knows which target to run."""
    assert (
        ".pre-commit-config.yaml" in coding_standards
    ), "CODING_STANDARDS.md must reference .pre-commit-config.yaml"
    assert (
        "pre-commit" in coding_standards
    ), "CODING_STANDARDS.md must mention the `pre-commit` framework"


# ---------------------------------------------------------------------------
# Backend infra singletons
#
# The scaffold splits cross-cutting concerns into top-level
# ``app/<name>.py`` modules (not subpackages): ``app.db`` for
# the SQLAlchemy engine, ``app.redis_client`` for the cached
# async Redis client, and ``app.observability.logging`` for
# the project-wide logging configuration. Each module is a
# singleton in the same sense: a process-wide object that
# request handlers, the Arq worker and the CLI scripts share.
# The fixture tests below pin the file presence so a refactor
# that moves the helpers does not silently break the wiring
# ``app/main.py`` relies on.
# ---------------------------------------------------------------------------


REDIS_CLIENT_PATH = BACKEND_DIR / "app" / "redis_client.py"
OBSERVABILITY_LOGGING_PATH = BACKEND_DIR / "app" / "observability" / "logging.py"


def test_backend_redis_client_module_is_present() -> None:
    """``app/redis_client.py`` is the single source of truth
    for the cached async Redis client. A missing file means
    every caller (health probe, Arq worker, future services)
    would have to construct its own client, defeating the
    point of a shared connection pool. The presence test
    exists so a careless rename surfaces here instead of as
    an ``ImportError`` at request time."""
    assert REDIS_CLIENT_PATH.is_file(), (
        "backend/app/redis_client.py must exist so the cached "
        "Redis client is shared across the app"
    )


def test_redis_client_module_uses_settings() -> None:
    """The Redis client module must read its configuration
    from :class:`app.config.Settings` (not from a hard-coded
    URL) so ``REDIS_HOST`` / ``REDIS_PORT`` env-var overrides
    take effect in every environment. We assert on the import
    symbol because reading the file in a test doubles as
    documentation of the contract."""
    text = REDIS_CLIENT_PATH.read_text(encoding="utf-8")
    assert (
        "from app.config import" in text or "import app.config" in text
    ), "app/redis_client.py must read its config from app.config.Settings"
    assert (
        "redis_url" in text
    ), "app/redis_client.py must derive the Redis URL from Settings.redis_url"


def test_backend_observability_logging_module_is_present() -> None:
    """``app/observability/logging.py`` owns the project-wide
    logging configuration. ``app/main.create_app`` calls
    :func:`app.observability.configure_logging` on startup;
    a missing file would surface as a circular import or an
    ``AttributeError`` on the first request. Pin the file so
    a deletion is caught here, not in production."""
    assert OBSERVABILITY_LOGGING_PATH.is_file(), (
        "backend/app/observability/logging.py must exist so "
        "configure_logging has a place to live"
    )


def test_observability_logging_module_exposes_required_helpers() -> None:
    """The logging module must export both
    :func:`configure_logging` (called once from
    ``create_app``) and :func:`get_logger` (used by every
    module that wants a logger). A missing export means
    either the FastAPI startup crashes or call sites have
    to import from a different path, breaking the
    ``from app.observability import get_logger`` convention
    documented in CODING_STANDARDS.md §9."""
    text = OBSERVABILITY_LOGGING_PATH.read_text(encoding="utf-8")
    assert (
        "def configure_logging" in text
    ), "app/observability/logging.py must define `configure_logging`"
    assert "def get_logger" in text, "app/observability/logging.py must define `get_logger`"


def test_app_main_calls_configure_logging() -> None:
    """``app/main.py`` must invoke
    :func:`app.observability.configure_logging` on startup
    so the very first log line is routed through the
    project formatter. A missing call means uvicorn's
    default formatter wins and PII redaction / log-level
    overrides stop working."""
    main_text = (BACKEND_DIR / "app" / "main.py").read_text(encoding="utf-8")
    assert "configure_logging" in main_text, (
        "app/main.py must call configure_logging so the "
        "project log handler is installed on startup"
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _find_step(steps: list[dict], predicate) -> dict | None:
    for step in steps:
        if predicate(step):
            return step
    return None


def _normalise_requirements(text: str) -> set[str]:
    """Extract package names from a pip requirements file.

    Strips comments, blank lines, hashes, version specifiers and extras
    so we can compare against the plain package name set.
    """
    names: set[str] = set()
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        # Drop inline comments and hashes (PEP 508 markers are ignored on
        # purpose – we only care about top-level package names).
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        # `package[extra]==1.2.3` -> `package`
        name = re.split(r"[\[<>=;~! ]", line, maxsplit=1)[0].strip()
        if name:
            names.add(name.lower())
    return names
