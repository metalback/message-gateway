# Message Gateway

API unificada de mensajería (SMS, WhatsApp) para el mercado chileno con facturación local SII.

Este repositorio es un **monorepo** con dos servicios:

| Carpeta      | Stack                       | Descripción                                      |
| ------------ | --------------------------- | ------------------------------------------------ |
| `backend/`   | Python 3.12 · FastAPI       | API REST versionada (`/v1/...`)                  |
| `frontend/`  | Node 20 · Angular 18        | Dashboard web (NgModules + Tailwind)             |
| `docker-compose.yml` | –                  | Stack local: postgres, redis, backend, frontend  |
| `.github/workflows/ci.yml` | –             | CI: backend tests, frontend build, compose check |

## Quick start (Docker)

```bash
cp .env.example .env
docker compose up --build
```

Servicios disponibles:

- API    → http://localhost:8000 (`/docs` para OpenAPI)
- App    → http://localhost:4200
- PG     → `localhost:5432`  (user/pass: `msg_gateway`/`msg_gateway`)
- Redis  → `localhost:6379`

## Quick start (sin Docker)

```bash
make install
make backend-test
make frontend-test
```

## Documentación adicional

- [PRD.md](./PRD.md) — definición del producto y decisiones de arquitectura.
- [CODING_STANDARDS.md](./CODING_STANDARDS.md) — convenciones de código (Python, Angular, Docker, CI, Git).
- [CONTRIBUTING.md](./CONTRIBUTING.md) — flujo de trabajo diario para nuevos contribuidores (branching, commits, tests, PRs).
