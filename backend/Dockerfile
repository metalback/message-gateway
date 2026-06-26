# ---------------------------------------------------------------------------
# Message Gateway – backend (FastAPI) image.
#
# Multi-stage build:
#   1. `builder` installs dependencies into a virtualenv so the runtime
#      image only carries the venv and application code.
#   2. `runtime` is a slim Debian image with the venv copied in.
#
# Build:    docker build -t msg-gateway-backend ./backend
# Run:      docker run --rm -p 8000:8000 msg-gateway-backend
# ---------------------------------------------------------------------------

# --- Stage 1: dependencies ------------------------------------------------
FROM python:3.12-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Install build deps in a single layer to keep the image small.
COPY requirements.txt ./
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --upgrade pip \
    && /opt/venv/bin/pip install -r requirements.txt

# --- Stage 2: runtime -----------------------------------------------------
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:${PATH}" \
    PYTHONPATH=/app

# Create an unprivileged user; the official python image ships as root.
RUN groupadd --system app && useradd --system --gid app --create-home --home-dir /home/app app

WORKDIR /app

COPY --from=builder /opt/venv /opt/venv
COPY --chown=app:app . /app

USER app

EXPOSE 8000

# `uvicorn` is the production entry point. `--proxy-headers` makes it play
# nicely with the typical reverse-proxy / load-balancer setup described in
# the PRD (AWS sa-east-1).
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
