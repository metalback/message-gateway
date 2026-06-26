# ---------------------------------------------------------------------------
# Message Gateway – frontend (Angular) image.
#
# Multi-stage build:
#   1. `builder` installs npm deps and runs a production build.
#   2. `runtime` serves the compiled bundle with nginx.
#
# Build:    docker build -t msg-gateway-frontend ./frontend
# ---------------------------------------------------------------------------

# --- Stage 1: build -------------------------------------------------------
FROM node:20-alpine AS builder

WORKDIR /app

# Copy manifests first to maximise layer caching.
COPY package.json package-lock.json* ./
RUN npm install --no-audit --no-fund

# Copy the rest of the source.
COPY . .

# `API_BASE_URL` is consumed at build time by Angular's fileReplacements
# (see angular.json) to bake the backend origin into the bundle.
ARG API_BASE_URL=/api
ENV API_BASE_URL=${API_BASE_URL}

RUN npm run build -- --configuration production

# --- Stage 2: runtime -----------------------------------------------------
FROM nginx:1.27-alpine AS runtime

# Drop privileges: nginx ships as root, run as `nginx` user.
COPY --from=builder /app/dist/msg-gateway/browser /usr/share/nginx/html

# Minimal nginx config: serve SPA + reverse-proxy `/api` to the backend
# service defined in docker-compose.
COPY nginx.conf /etc/nginx/conf.d/default.conf

EXPOSE 80

CMD ["nginx", "-g", "daemon off;"]
