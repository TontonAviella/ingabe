import logging
import logging.config
import os
import sys
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

    logging.config.dictConfig({
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "standard": {
                "format": "%(asctime)s %(levelname)s %(name)s: %(message)s",
            },
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
    # Cleanup: close all PostGIS connection pools
    await close_all_pools()


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
        # Clickjacking protection
        response.headers["X-Frame-Options"] = "DENY"
        # Basic CSP — allow self and known external tile/API sources
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline' 'unsafe-eval' blob:; "
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data: blob: https://*.arcgisonline.com https://*.tile.openstreetmap.org https://*.basemaps.cartocdn.com; "
            "connect-src 'self' https://*.arcgisonline.com https://*.tile.openstreetmap.org https://*.basemaps.cartocdn.com https://isdasoil.s3.amazonaws.com ws: wss:; "
            "worker-src 'self' blob:; "
            "frame-ancestors 'none'"
        )
        return response


app.add_middleware(SecurityHeadersMiddleware)


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


@app.get("/health")
async def health_check():
    """Detailed health check (PostgreSQL, Redis, QGIS).

    Always returns 200 so monitoring tools can read the body.
    The "status" field is "healthy" or "degraded".
    """
    import httpx
    from redis.asyncio import Redis as AsyncRedis

    checks = {}

    # 1. PostgreSQL
    try:
        from src.structures import async_read_conn
        async with async_read_conn("health_check") as conn:
            await conn.fetchval("SELECT 1")
        checks["postgres"] = "ok"
    except Exception as e:
        checks["postgres"] = f"error: {e}"

    # 2. Redis
    try:
        redis_client = AsyncRedis(
            host=os.environ.get("REDIS_HOST", "localhost"),
            port=int(os.environ.get("REDIS_PORT", 6379)),
            decode_responses=True,
        )
        await redis_client.ping()
        await redis_client.aclose()
        checks["redis"] = "ok"
    except Exception as e:
        checks["redis"] = f"error: {e}"

    # 3. QGIS Processing
    qgis_url = os.environ.get("QGIS_PROCESSING_URL", "http://qgis-processing:8817")
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{qgis_url}/health")
            checks["qgis"] = "ok" if resp.status_code == 200 else f"status {resp.status_code}"
    except Exception as e:
        checks["qgis"] = f"error: {e}"

    all_ok = all(v == "ok" for v in checks.values())
    return JSONResponse(
        status_code=200,
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
