import logging
import logging.config
import os
import sys
import time
import uuid
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse, FileResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response


from src.routes import (
    postgres_routes,
    project_routes,
    message_routes,
    websocket,
    conversation_routes,
)
from src.routes.basemap_routes import basemap_router
from src.routes.layer_router import layer_router
from src.routes.attribute_table import attribute_table_router
from src.routes.lakehouse_routes import lakehouse_router
from src.routes.rwanda_routes import rwanda_router
from src.routes.worldcover_router import worldcover_router
from src.dependencies.db_pool import close_all_pools
from src.dependencies.rate_limiter import limiter, rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
# from fastapi_mcp import FastApiMCP


# ---------------------------------------------------------------------------
# OpenTelemetry initialization (must happen before any tracer usage)
# ---------------------------------------------------------------------------

def _init_opentelemetry():
    """Initialize OpenTelemetry SDK with OTLP exporter.

    Only activates when OTEL_EXPORTER_OTLP_ENDPOINT is set.
    Uses environment variables for configuration:
      - OTEL_EXPORTER_OTLP_ENDPOINT: e.g. https://otel.example.com:4318
      - OTEL_EXPORTER_OTLP_HEADERS: e.g. x-api-key=abc123
      - OTEL_SERVICE_NAME: defaults to "ingabe"
    """
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    if not endpoint:
        return

    try:
        from opentelemetry import trace
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        resource = Resource.create({
            "service.name": os.environ.get("OTEL_SERVICE_NAME", "ingabe"),
            "service.version": "0.0.1",
        })

        provider = TracerProvider(resource=resource)

        headers_str = os.environ.get("OTEL_EXPORTER_OTLP_HEADERS", "")
        headers = {}
        if headers_str:
            for pair in headers_str.split(","):
                if "=" in pair:
                    k, v = pair.split("=", 1)
                    headers[k.strip()] = v.strip()

        exporter = OTLPSpanExporter(
            endpoint=f"{endpoint.rstrip('/')}/v1/traces",
            headers=headers,
        )
        provider.add_span_processor(BatchSpanProcessor(exporter))
        trace.set_tracer_provider(provider)

        logging.getLogger("src").info(
            "OpenTelemetry initialized → %s (service=%s)",
            endpoint,
            os.environ.get("OTEL_SERVICE_NAME", "ingabe"),
        )
    except ImportError as e:
        logging.getLogger("src").warning("OpenTelemetry SDK packages not installed: %s", e)
    except Exception as e:
        logging.getLogger("src").warning("OpenTelemetry init failed: %s", e)


# Initialize before any module imports trace.get_tracer()
_init_opentelemetry()


def _configure_app_logging():
    """Configure application logging AFTER uvicorn has set up its own logging.

    Uvicorn's configure_logging() clears root logger handlers during startup,
    so any logging.basicConfig() or root handler setup done at module scope
    gets wiped. This function must be called from the lifespan event which
    runs after uvicorn is fully initialised.

    We use logging.config.dictConfig to forcefully set up our loggers,
    overriding whatever uvicorn left behind.
    """
    log_level_name = os.environ.get("LOG_LEVEL", "INFO").upper()
    log_format = os.environ.get("LOG_FORMAT", "json")  # "json" or "text"

    if log_format == "json":
        formatter_config = {
            "()": "src.logging_json.JsonFormatter",
        }
    else:
        formatter_config = {
            "format": "%(asctime)s %(levelname)s %(name)s: %(message)s",
        }

    logging.config.dictConfig({
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "standard": formatter_config,
        },
        "handlers": {
            "stderr": {
                "class": "logging.StreamHandler",
                "stream": "ext://sys.stderr",
                "formatter": "standard",
            },
        },
        "loggers": {
            "src": {
                "handlers": ["stderr"],
                "level": log_level_name,
                "propagate": False,
            },
            "uvicorn.access": {
                "handlers": ["stderr"],
                "level": "INFO",
                "propagate": False,
            },
        },
        "root": {
            "handlers": ["stderr"],
            "level": log_level_name,
        },
    })

    # Force stderr to be unbuffered so Docker sees log lines immediately
    sys.stderr = os.fdopen(sys.stderr.fileno(), "w", buffering=1, closefd=False)

    # dictConfig can disable pre-existing loggers even with
    # disable_existing_loggers=False.  Force-enable all src.* loggers
    # that were created during module import (before lifespan ran).
    for name, lgr in logging.Logger.manager.loggerDict.items():
        if isinstance(lgr, logging.Logger) and name.startswith("src"):
            lgr.disabled = False

    src_logger = logging.getLogger("src")
    src_logger.info("Application logging configured (level=%s)", log_level_name)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan: configure logging on startup, close pools on shutdown.

    Migrations are run externally via ``alembic upgrade head`` before uvicorn
    starts (see docker-compose.yml command) to avoid deadlocks between Alembic's
    synchronous engine, logging reconfiguration, and uvicorn's async lifespan.
    """
    _configure_app_logging()

    yield
    # Cleanup: close all connection pools and shared clients
    await close_all_pools()
    from src.dependencies.redis_client import close_async_redis
    await close_async_redis()
    from src.utils import close_s3_clients
    await close_s3_clients()


app = FastAPI(
    title="Ingabe",
    description="Open source, AI-native web GIS for Rwanda agriculture",
    version="0.0.1",
    # Don't show OpenAPI spec, docs, redoc
    openapi_url=None,
    lifespan=lifespan,
)

# ---------------------------------------------------------------------------
# Rate limiting (slowapi)
# ---------------------------------------------------------------------------

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, rate_limit_exceeded_handler)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Security middleware
# ---------------------------------------------------------------------------

# CORS — restrict to known origin (falls back to permissive for local dev)
_allowed_origin = os.environ.get("WEBSITE_DOMAIN", "http://localhost:5173")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[_allowed_origin],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Add HSTS, CSP, and other security headers to every response."""

    async def dispatch(self, request: Request, call_next) -> Response:  # type: ignore[override]
        response: Response = await call_next(request)
        # HSTS — enforce HTTPS for 1 year (ignored on plain HTTP / localhost)
        response.headers["Strict-Transport-Security"] = (
            "max-age=31536000; includeSubDomains"
        )
        # Prevent MIME-type sniffing
        response.headers["X-Content-Type-Options"] = "nosniff"
        # Clickjacking protection (skip if endpoint already set its own)
        if "X-Frame-Options" not in response.headers:
            response.headers["X-Frame-Options"] = "DENY"
        # Basic CSP — allow self and known external tile/API sources
        # Skip if endpoint already set a custom CSP (e.g. embed route)
        if "Content-Security-Policy" in response.headers:
            return response
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline' 'unsafe-eval' blob: https://*.posthog.com https://*.i.posthog.com; "
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data: blob: https://*.arcgisonline.com https://tile.openstreetmap.org https://*.basemaps.cartocdn.com https://tiles.openfreemap.org; "
            "connect-src 'self' https://*.arcgisonline.com https://tile.openstreetmap.org https://*.basemaps.cartocdn.com https://tiles.openfreemap.org https://demotiles.maplibre.org https://isdasoil.s3.amazonaws.com https://*.r2.cloudflarestorage.com https://*.posthog.com https://*.i.posthog.com ws: wss:; "
            "font-src 'self' https://demotiles.maplibre.org https://tiles.openfreemap.org; "
            "worker-src 'self' blob:; "
            "frame-ancestors 'none'"
        )
        return response


app.add_middleware(SecurityHeadersMiddleware)


# ---------------------------------------------------------------------------
# Request ID + Metrics middleware
# ---------------------------------------------------------------------------

# In-memory request metrics (per-worker, aggregated at /metrics)
_request_count = 0
_request_errors = 0
_request_latency_sum = 0.0
_request_latency_count = 0
_active_requests = 0


class RequestIdMetricsMiddleware(BaseHTTPMiddleware):
    """Attach X-Request-ID for distributed tracing and collect request metrics."""

    async def dispatch(self, request: Request, call_next) -> Response:
        global _request_count, _request_errors, _request_latency_sum, _request_latency_count, _active_requests

        # Generate or propagate request ID
        request_id = request.headers.get("X-Request-ID", str(uuid.uuid4())[:8])
        request.state.request_id = request_id

        _active_requests += 1
        _request_count += 1
        start = time.monotonic()
        try:
            response: Response = await call_next(request)
            if response.status_code >= 500:
                _request_errors += 1
            response.headers["X-Request-ID"] = request_id
            return response
        except Exception:
            _request_errors += 1
            raise
        finally:
            elapsed = time.monotonic() - start
            _request_latency_sum += elapsed
            _request_latency_count += 1
            _active_requests -= 1


app.add_middleware(RequestIdMetricsMiddleware)


# ---------------------------------------------------------------------------
# Cache-Control middleware (CDN & browser caching)
# ---------------------------------------------------------------------------

class CacheControlMiddleware(BaseHTTPMiddleware):
    """Set Cache-Control headers for static assets, tiles, and API responses."""

    # Immutable hashed assets (Vite adds content hash to filenames)
    _IMMUTABLE_PREFIXES = ("/assets/",)
    # Tile responses — cache at CDN, short browser cache
    _TILE_SUFFIXES = (".mvt", ".pmtiles", ".pbf", ".png", ".webp")
    # Favicons / static images — moderate cache
    _STATIC_FILES = ("/favicon-light.svg", "/favicon-dark.svg")

    async def dispatch(self, request: Request, call_next) -> Response:
        response: Response = await call_next(request)
        path = request.url.path

        # Don't cache error responses
        if response.status_code >= 400:
            return response

        # Vite hashed assets — immutable, long cache
        if any(path.startswith(p) for p in self._IMMUTABLE_PREFIXES):
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
            return response

        # Tile responses — CDN caches 1h, browser caches 5min
        if any(path.endswith(s) for s in self._TILE_SUFFIXES):
            response.headers["Cache-Control"] = "public, max-age=300, s-maxage=3600"
            return response

        # Static files — CDN caches 1d, browser caches 1h
        if path in self._STATIC_FILES:
            response.headers["Cache-Control"] = "public, max-age=3600, s-maxage=86400"
            return response

        # API responses — no cache by default
        if path.startswith("/api/"):
            response.headers.setdefault("Cache-Control", "no-store")

        return response


app.add_middleware(CacheControlMiddleware)


# ---------------------------------------------------------------------------
# Health endpoints
# ---------------------------------------------------------------------------

@app.get("/healthz")
async def healthz():
    """Simple liveness probe — always returns 200.

    Used by Render's healthCheckPath so deploys aren't blocked by
    dependency failures during startup.  Use /health for detailed status.
    """
    return JSONResponse(status_code=200, content={"status": "ok"})


@app.get("/ready")
async def readiness():
    """Readiness probe — returns 200 only when critical dependencies are up."""
    try:
        from src.structures import async_read_conn
        async with async_read_conn("readiness") as conn:
            await conn.fetchval("SELECT 1")
        return JSONResponse(status_code=200, content={"ready": True})
    except Exception as e:
        return JSONResponse(status_code=503, content={"ready": False, "error": str(e)})


@app.get("/metrics")
async def metrics():
    """Lightweight metrics endpoint for monitoring (Prometheus-compatible text format)."""
    import asyncio
    from src.database.pool import _async_connection_pool, _async_read_pool

    lines = []
    # Request metrics
    lines.append(f"# HELP http_requests_total Total HTTP requests")
    lines.append(f"# TYPE http_requests_total counter")
    lines.append(f"http_requests_total {_request_count}")
    lines.append(f"# HELP http_request_errors_total Total HTTP 5xx errors")
    lines.append(f"# TYPE http_request_errors_total counter")
    lines.append(f"http_request_errors_total {_request_errors}")
    lines.append(f"# HELP http_requests_active Currently active requests")
    lines.append(f"# TYPE http_requests_active gauge")
    lines.append(f"http_requests_active {_active_requests}")

    if _request_latency_count > 0:
        avg_latency = _request_latency_sum / _request_latency_count
        lines.append(f"# HELP http_request_duration_seconds_avg Average request duration")
        lines.append(f"# TYPE http_request_duration_seconds_avg gauge")
        lines.append(f"http_request_duration_seconds_avg {avg_latency:.4f}")

    # Database pool metrics
    if _async_connection_pool:
        lines.append(f"# HELP db_pool_size Current write pool size")
        lines.append(f"# TYPE db_pool_size gauge")
        lines.append(f"db_pool_size {_async_connection_pool.get_size()}")
        lines.append(f"db_pool_free {_async_connection_pool.get_idle_size()}")
        lines.append(f"db_pool_max {_async_connection_pool.get_max_size()}")
    if _async_read_pool:
        lines.append(f"db_read_pool_size {_async_read_pool.get_size()}")
        lines.append(f"db_read_pool_free {_async_read_pool.get_idle_size()}")

    body = "\n".join(lines) + "\n"
    return Response(content=body, media_type="text/plain; charset=utf-8")


@app.get("/health")
async def health_check():
    """Detailed health check (PostgreSQL, Redis, QGIS).

    Always returns 200 so monitoring tools can read the body.
    The "status" field is "healthy" or "degraded".
    """
    import asyncio
    import httpx

    async def _check_postgres() -> str:
        try:
            from src.structures import async_read_conn
            async with async_read_conn("health_check") as conn:
                await conn.fetchval("SELECT 1")
            return "ok"
        except Exception as e:
            return f"error: {e}"

    async def _check_redis() -> str:
        try:
            from src.dependencies.redis_client import get_async_redis_for_ping
            redis_client = await get_async_redis_for_ping()
            await redis_client.ping()
            await redis_client.aclose()
            return "ok"
        except Exception as e:
            return f"error: {e}"

    async def _check_qgis() -> str:
        qgis_url = os.environ.get("QGIS_PROCESSING_URL", "http://qgis-processing:8817")
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(f"{qgis_url}/health")
                return "ok" if resp.status_code == 200 else f"status {resp.status_code}"
        except Exception as e:
            return f"error: {e}"

    pg, redis_r, qgis = await asyncio.gather(
        _check_postgres(), _check_redis(), _check_qgis(),
    )
    checks = {"postgres": pg, "redis": redis_r, "qgis": qgis}

    pg_ok = pg == "ok"
    all_ok = all(v == "ok" for v in checks.values())
    # If Postgres is down, the app can't serve requests — return 503 so
    # load balancers route traffic to healthy instances
    status_code = 200 if pg_ok else 503
    return JSONResponse(
        status_code=status_code,
        content={"status": "healthy" if all_ok else "degraded", "checks": checks},
    )


# ---------------------------------------------------------------------------
# Route registration
# ---------------------------------------------------------------------------

app.include_router(
    postgres_routes.router,
    prefix="/api/maps",
    tags=["Maps"],
)
app.include_router(
    message_routes.router,
    prefix="/api/maps",
    tags=["Messages"],
)
app.include_router(
    websocket.router,
    prefix="/api/maps",
    tags=["WebSocket"],
)
app.include_router(
    layer_router,
    prefix="/api",
    tags=["Layers"],
)
app.include_router(
    attribute_table_router,
    prefix="/api",
    tags=["Attribute Tables"],
)
app.include_router(
    project_routes.project_router,
    prefix="/api/projects",
    tags=["Maps"],
)
app.include_router(
    basemap_router,
    prefix="/api/basemaps",
    tags=["Basemaps"],
)
app.include_router(
    conversation_routes.router,
    prefix="/api",
    tags=["Conversations"],
)
app.include_router(
    lakehouse_router,
    prefix="/api",
    tags=["Lakehouse"],
)
app.include_router(
    rwanda_router,
    prefix="/api",
    tags=["Rwanda"],
)
app.include_router(
    worldcover_router,
    tags=["WorldCover"],
)


# TODO: this isn't useful right now. But we should work on it in the future
# mcp = FastApiMCP(
#     app,
#     name="Ingabe MCP",
#     description="GIS as an MCP",
#     exclude_operations=[
#         "upload_layer_to_map",
#         "view_layer_as_geojson",
#         "view_layer_as_pmtiles",
#         "view_layer_as_cog_tif",
#         "remove_layer_from_map",
#         "view_map_html",
#         "get_map_stylejson",
#         "describe_layer",
#     ],
# )
# mcp.mount()


app.mount("/assets", StaticFiles(directory="frontendts/dist/assets"), name="spa-assets")


@app.get("/favicon-light.svg")
async def get_favicon_light_svg():
    return FileResponse("frontendts/dist/favicon-light.svg")


@app.get("/favicon-dark.svg")
async def get_favicon_dark_svg():
    return FileResponse("frontendts/dist/favicon-dark.svg")


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    """Catch-all for unhandled exceptions — log traceback, return JSON for /api/."""
    import traceback
    request_id = getattr(request.state, "request_id", "unknown")
    logging.getLogger("src").error(
        "Unhandled %s on %s %s [req=%s]: %s\n%s",
        type(exc).__name__, request.method, request.url.path, request_id,
        exc, traceback.format_exc(),
    )
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error", "request_id": request_id},
    )


@app.exception_handler(StarletteHTTPException)
async def spa_server(request: Request, exc: StarletteHTTPException):
    # Don't handle API 404s - let them bubble up as real 404s
    if (
        request.url.path.startswith("/api/")
        or request.url.path.startswith("/supertokens/")
        or request.url.path.startswith("/mcp")
    ):
        # Return standard 404 response for API routes and MCP routes
        # Preserve structured detail (dict/list) instead of stringifying
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

    # For all other routes, return the SPA's index.html
    return FileResponse("frontendts/dist/index.html")
