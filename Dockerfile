# Dockerfile — Mundi.ai application image
# Uses pre-built base image with GDAL, tippecanoe, LAStools, MapLibre.
#
# To rebuild the base image (only needed when native deps change):
#   docker build -f Dockerfile.base -t ghcr.io/tontonaviella/mundi-base:latest .
#   docker push ghcr.io/tontonaviella/mundi-base:latest

# ── ARG for base image (override in render.yaml or CI) ──
ARG BASE_IMAGE=ghcr.io/tontonaviella/mundi-base:latest

# ── Python dependencies ──
FROM ${BASE_IMAGE} AS python-builder
COPY --from=ghcr.io/astral-sh/uv:0.4.9 /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy

# Install development headers for building Python packages + gfortran for DSSAT
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3-dev build-essential gfortran \
        libdbus-1-dev libdbus-glib-1-dev pkg-config \
        libgirepository1.0-dev libcairo2-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt /app/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv venv && \
    uv pip install -r requirements.txt && \
    uv pip install hyperdx-opentelemetry

# ── Frontend build ──
FROM node:20-bookworm-slim AS frontend-builder
WORKDIR /app/frontendts
COPY frontendts/package*.json ./
RUN --mount=type=cache,target=/root/.npm npm ci --legacy-peer-deps
ARG VITE_WEBSITE_DOMAIN
ARG VITE_CLERK_PUBLISHABLE_KEY
ARG VITE_CLERK_SIGN_IN_URL
ARG VITE_CLERK_SIGN_UP_URL
ARG VITE_POSTHOG_KEY=phc_W7tOZXWc5oPtpiYyyINii49GodL1NgyS6z30pNW8aTP
ARG VITE_POSTHOG_HOST=https://us.i.posthog.com
COPY frontendts/ ./
ENV VITE_WEBSITE_DOMAIN=$VITE_WEBSITE_DOMAIN \
    VITE_CLERK_PUBLISHABLE_KEY=$VITE_CLERK_PUBLISHABLE_KEY \
    VITE_CLERK_SIGN_IN_URL=$VITE_CLERK_SIGN_IN_URL \
    VITE_CLERK_SIGN_UP_URL=$VITE_CLERK_SIGN_UP_URL \
    VITE_POSTHOG_KEY=$VITE_POSTHOG_KEY \
    VITE_POSTHOG_HOST=$VITE_POSTHOG_HOST \
    NODE_OPTIONS=--max-old-space-size=4096
RUN npm run build

# ── Final stage ──
FROM ${BASE_IMAGE} AS final
WORKDIR /app

# Copy Python virtual environment from builder
COPY --from=python-builder /app/.venv /app/.venv
COPY --from=ghcr.io/astral-sh/uv:0.4.9 /uv /bin/uv
ENV PATH="/app/.venv/bin:$PATH"

# Pre-install DuckDB extensions so they don't need network access at runtime
RUN python3 -c "import duckdb; con = duckdb.connect(':memory:'); con.install_extension('spatial'); con.install_extension('iceberg'); con.close()"

# Copy application files
COPY . /app/
COPY --from=frontend-builder /app/frontendts/dist /app/frontendts/dist

# Setup environment
ENV DISPLAY=:99 \
    LANG=en_US.UTF-8 \
    PYTHONPATH="/app:/usr/local/lib/python3.11/dist-packages:/usr/lib/python3/dist-packages" \
    LD_LIBRARY_PATH="/usr/local/lib:/usr/lib" \
    GDAL_DATA="/usr/local/share/gdal" \
    GDAL_DRIVER_PATH="/usr/local/lib/gdalplugins"

COPY scripts/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

RUN useradd -r -s /bin/false appuser \
    && chown -R appuser:appuser /app \
    && chmod -R u+rwX,go+rX /app/src \
    && mkdir -p /cache \
    && chown appuser:appuser /cache
USER appuser

CMD ["/entrypoint.sh"]
