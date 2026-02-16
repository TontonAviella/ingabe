from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse, FileResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

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
# from fastapi_mcp import FastApiMCP


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Run database migrations on startup"""
    from src.database.migrate import run_migrations

    await run_migrations()
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
