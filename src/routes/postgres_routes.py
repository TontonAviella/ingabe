import os
import json
from io import BytesIO
from pathlib import Path
from urllib.parse import urlparse
import aiohttp
import fiona
from fastapi import (
    APIRouter,
    HTTPException,
    status,
    Request,
    Depends,
)
from fastapi.responses import Response
from pydantic import BaseModel, Field
from src.dependencies.dag import forked_map_by_user, get_map, get_layer, edit_map
from src.dependencies.rate_limiter import heavy_limit
from src.database.models import MundiMap, MapLayer
from src.dependencies.session import (
    verify_session_required,
    verify_session_optional,
    UserContext,
)
from typing import List, Optional
import logging
from fastapi import File, UploadFile, Form
from src.dependencies.redis_client import get_redis_client
import tempfile
from starlette.responses import (
    JSONResponse as StarletteJSONResponse,
)
import asyncio
from src.utils import (
    get_bucket_name,
    get_async_s3_client,
)
from src.structures import get_async_db_connection, async_conn
from src.tile_cache import tile_cache
from src.fs_lru import layer_cache
from src.dependencies.base_map import BaseMapProvider, get_base_map_provider
from src.dependencies.postgis import get_postgis_provider
from src.dependencies.layer_describer import LayerDescriber, get_layer_describer
from src.dependencies.postgres_connection import (
    PostgresConnectionManager,
    get_postgres_connection_manager,
)
from typing import Callable
from opentelemetry import trace
from src.dag import DAGEditOperationResponse

# Import shared service functions
from src.services.map_service import (
    generate_id,
    validate_remote_url,
    internal_upload_layer,
    get_map_style_internal,
    render_map_internal,
)

fiona.drvsupport.supported_drivers["WFS"] = "r"  # type: ignore[attr-defined]
fiona.drvsupport.supported_drivers["PMTiles"] = "r"  # type: ignore[attr-defined]
fiona.drvsupport.supported_drivers["KML"] = "r"  # type: ignore[attr-defined]


logger = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)

redis = get_redis_client()


# Upload models — imported from src.upload.models (canonical location)
from src.upload.models import (  # noqa: E402
    VectorProcessingResult,
)


# Upload preprocessing — imported from src.upload.preprocessing (canonical location)
from src.upload.preprocessing import (  # noqa: E402
    get_layer_bounds_and_metadata,
)


# Create router
router = APIRouter()


class MapCreateRequest(BaseModel):
    title: str = Field(
        default="Untitled Map", description="Display name for the new map"
    )


class MapResponse(BaseModel):
    id: str = Field(description="Unique identifier for the map")
    project_id: str = Field(
        description="ID of the project containing this map. Projects can contain multiple related maps."
    )
    title: str = Field(description="Display name of the map")
    created_on: str = Field(description="ISO timestamp when the map was created")
    map_link: str = Field(description="URL to view the map project")


class UserMapsResponse(BaseModel):
    maps: List[MapResponse]


# mundi-public/frontendts/src/lib/types.tsx
class LayerMetadata(BaseModel):
    original_filename: Optional[str] = None
    original_format: Optional[str] = None
    converted_to: Optional[str] = None
    original_srid: Optional[int] = None
    feature_count: Optional[int] = None
    geometry_type: Optional[str] = None
    raster_value_stats_b1: Optional[dict] = None  # {min: float, max: float}
    pointcloud_anchor: Optional[dict] = None  # {lon: float, lat: float}
    pointcloud_z_range: Optional[List[float]] = None  # [min_z, max_z]


def _filter_layer_metadata(md: Optional[dict]) -> Optional[dict]:
    if not md or not isinstance(md, dict):
        return None

    allowed_keys = {
        "original_filename",
        "original_format",
        "converted_to",
        "original_srid",
        "feature_count",
        "geometry_type",
        "raster_value_stats_b1",
        "pointcloud_anchor",
        "pointcloud_z_range",
    }

    out: dict = {}
    for k in allowed_keys:
        if k in md:
            out[k] = md[k]

    return LayerMetadata(**out).model_dump(exclude_none=True)


class LayerResponse(BaseModel):
    id: str
    name: str
    type: str
    metadata: Optional[LayerMetadata] = None
    bounds: Optional[List[float]] = (
        None  # [xmin, ymin, xmax, ymax] in WGS84 coordinates
    )
    geometry_type: Optional[str] = None  # point, multipoint, line, polygon, etc.
    feature_count: Optional[int] = None  # number of features in the layer
    original_srid: Optional[int] = None  # original projection EPSG code


class LayersListResponse(BaseModel):
    map_id: str
    layers: List[LayerResponse]


class LayerUploadResponse(DAGEditOperationResponse):
    id: str = Field(description="Unique identifier for the newly uploaded layer")
    name: str = Field(description="Display name of the layer as it appears in the map")
    type: str = Field(description="Layer type (vector, raster, or point_cloud)")
    url: str = Field(
        description="Direct URL to access the layer data (PMTiles for vector, COG for raster)"
    )
    message: str = Field(
        default="Layer added successfully",
        description="Status message confirming successful upload",
    )


class RemoteLayerRequest(BaseModel):
    url: str = Field(description="Remote URL to the spatial data file")
    name: str = Field(description="Display name for the layer")
    source_type: str = Field(
        description="Type of remote source: 'vector', 'raster', 'sheets'"
    )
    add_layer_to_map: bool = Field(
        default=True, description="Whether to add layer to the map"
    )


# InternalLayerUploadResponse imported from src.upload.models above


class LayerRemovalResponse(DAGEditOperationResponse):
    layer_id: str
    layer_name: str
    message: str = "Layer successfully removed from map"


class PresignedUrlResponse(BaseModel):
    url: str
    expires_in_seconds: int = 3600 * 24  # Default 24 hours
    format: str


class MapUpdateRequest(BaseModel):
    basemap: Optional[str] = Field(None, description="Basemap style name")


@router.post(
    "/create",
    response_model=MapResponse,
    operation_id="create_map",
    summary="Create a new map",
)
async def create_map(
    map_request: MapCreateRequest,
    session: UserContext = Depends(verify_session_required),
):
    """Creates a new map project. Projects contain multiple map versions ("maps"),
    unattached layer data, and a history of changes to the project. Each edit will
    create a new map version.

    Accepts `title` in the request body. Returns overarching project id
    `project_id` and initial map version id `id`.

    ```py
    result = httpx.post(
        "https://app.mundi.ai/api/maps/create",
        json={"title": "Brazilian catchment areas"},
        headers={"Authorization": f"Bearer {os.environ['MUNDI_API_KEY']}"}
    ).json()

    assert result == {
        "title": "Brazilian catchment areas",
        "created_on": "2025-08-29T12:34:56.789Z",
        "map_link": "https://app.mundi.ai/project/PGJSkB1zj7fT",
        "id": "MWfqcRak59bo",
        "project_id": "PGJSkB1zj7fT"
    }
    ```
    """
    owner_id = session.get_user_id()

    # Generate unique IDs for project and map
    project_id = generate_id(prefix="P")
    map_id = generate_id(prefix="M")

    # Connect to database
    async with get_async_db_connection() as conn:
        async with conn.transaction():
            # First create a project
            await conn.execute(
                """
                INSERT INTO user_mundiai_projects
                (id, owner_uuid, maps, title)
                VALUES ($1, $2, ARRAY[$3], $4)
                """,
                project_id,
                owner_id,
                map_id,
                map_request.title,
            )

            # Then insert map with data including project_id and layer_ids
            result = await conn.fetchrow(
                """
                INSERT INTO user_mundiai_maps
                (id, project_id, owner_uuid, title)
                VALUES ($1, $2, $3, $4)
                RETURNING id, title, created_on
                """,
                map_id,
                project_id,
                owner_id,
                map_request.title,
            )

        # Validate the result
        if not result:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Database operation returned no result",
            )

        # Return the created map data
        website_domain = os.environ.get("WEBSITE_DOMAIN", "https://app.mundi.ai")
        return MapResponse(
            id=map_id,
            project_id=project_id,
            title=result["title"],
            created_on=result["created_on"].isoformat(),
            map_link=f"{website_domain}/project/{project_id}",
        )


@router.get(
    "/{map_id}",
    operation_id="get_map",
)
async def get_map_route(
    request: Request,
    map: MundiMap = Depends(get_map),
    session: UserContext = Depends(verify_session_optional),
):
    # Ensure map is part of a project
    if not map.project_id:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Map is not part of a project",
        )

    async with get_async_db_connection() as conn:
        # Load project and its changelog
        project = await conn.fetchrow(
            """
            SELECT maps, map_diff_messages
            FROM user_mundiai_projects
            WHERE id = $1 AND soft_deleted_at IS NULL
            """,
            map.project_id,
        )
        if not project:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Project not found",
            )

        # Get last_edited times for maps in the project
        map_ids = project["maps"] or []
        if map_ids:
            map_edit_rows = await conn.fetch(
                """
                SELECT id, last_edited
                FROM user_mundiai_maps
                WHERE id = ANY($1)
                """,
                map_ids,
            )
            map_edit_times = {row["id"]: row["last_edited"] for row in map_edit_rows}
        else:
            map_edit_times = {}

        proj_maps = project["maps"] or []
        diff_msgs = project["map_diff_messages"] or []
        diff_msgs = diff_msgs + ["current edit"]
        changelog = []
        # Pair each diff message with its resulting map state up to current
        for msg, state in zip(diff_msgs, proj_maps):
            changelog.append(
                {
                    "message": msg,
                    "map_state": state,
                    "last_edited": map_edit_times.get(state).isoformat()
                    if state in map_edit_times
                    else None,
                }
            )

        # Get layer IDs from the map
        layer_ids = map.layers if map.layers else []

        # Load layers using the layer IDs
        layers = await conn.fetch(
            """
            SELECT layer_id AS id,
                    name,
                    type,
                    metadata,
                    bounds,
                    geometry_type,
                    feature_count
            FROM map_layers
            WHERE layer_id = ANY($1)
            ORDER BY id
            """,
            layer_ids,
        )
        # Convert Record objects to mutable dictionaries
        layers = [dict(layer) for layer in layers]
        for layer in layers:
            if layer.get("metadata") and isinstance(layer["metadata"], str):
                layer["metadata"] = json.loads(layer["metadata"])
            layer["metadata"] = _filter_layer_metadata(layer.get("metadata"))

        # Return JSON payload
        response = {
            "map_id": map.id,
            "project_id": map.project_id,
            "layers": layers,
            "changelog": changelog,
        }

        return response


@router.get(
    "/{map_id}/layers",
    operation_id="list_map_layers",
    response_model=LayersListResponse,
)
async def get_map_layers(
    map: MundiMap = Depends(get_map),
):
    async with get_async_db_connection() as conn:
        # Get all layers by their IDs using ANY() instead of f-string
        layers = await conn.fetch(
            """
            SELECT layer_id as id, name, type, metadata, bounds, geometry_type, feature_count
            FROM map_layers
            WHERE layer_id = ANY($1)
            ORDER BY id
            """,
            map.layers,
        )

        # Process metadata JSON and add feature_count for vector layers if possible
        # Convert Record objects to mutable dictionaries
        layers = [dict(layer) for layer in layers]
        for layer in layers:
            if layer["metadata"] is not None:
                # Convert metadata from JSON string to Python dict if needed
                if isinstance(layer["metadata"], str):
                    layer["metadata"] = json.loads(layer["metadata"])
            layer["metadata"] = _filter_layer_metadata(layer.get("metadata"))

            # Set feature_count from metadata if it exists
            if (
                "metadata" in layer
                and layer["metadata"]
                and "feature_count" in layer["metadata"]
            ):
                layer["feature_count"] = layer["metadata"]["feature_count"]

            # Set original_srid from metadata if it exists
            if (
                "metadata" in layer
                and layer["metadata"]
                and "original_srid" in layer["metadata"]
            ):
                layer["original_srid"] = layer["metadata"]["original_srid"]

        # Return the layers
        return LayersListResponse(map_id=map.id, layers=layers)


@router.get(
    "/{map_id}/describe",
    operation_id="get_map_description",
)
async def get_map_description(
    request: Request,
    map_id: str,
    session: UserContext = Depends(verify_session_required),
    postgis_provider: Callable = Depends(get_postgis_provider),
    layer_describer: LayerDescriber = Depends(get_layer_describer),
    connection_manager: PostgresConnectionManager = Depends(
        get_postgres_connection_manager
    ),
):
    async with get_async_db_connection() as conn:
        # First check if the map exists and is accessible
        map_result = await conn.fetchrow(
            """
            SELECT id, title, description, owner_uuid, project_id
            FROM user_mundiai_maps
            WHERE id = $1 AND soft_deleted_at IS NULL
            """,
            map_id,
        )
        if not map_result:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Map not found"
            )

        # User must own the map to access this endpoint
        if session.get_user_id() != str(map_result["owner_uuid"]):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You must own this map to access map description",
            )
        # Auto-provision the internal Rwanda PostGIS connection so Sage
        # always sees it and can create layers from admin boundary tables.
        from src.routes.message_routes import _ensure_rwanda_postgis_connection
        await _ensure_rwanda_postgis_connection(
            conn, map_result["project_id"], str(map_result["owner_uuid"]),
        )

        content = []
        # Get PostgreSQL connections for this map's project with documentation
        postgres_connections = await conn.fetch(
            """
            SELECT
                ppc.id,
                ppc.connection_uri,
                ppc.connection_name,
                pps.friendly_name,
                pps.summary_md,
                pps.generated_at
            FROM project_postgres_connections ppc
            JOIN user_mundiai_maps m ON ppc.project_id = m.project_id
            LEFT JOIN project_postgres_summary pps ON ppc.id = pps.connection_id
            WHERE m.id = $1 AND ppc.soft_deleted_at IS NULL
            ORDER BY ppc.connection_name, pps.generated_at DESC
            """,
            map_id,
        )

        # Add PostgreSQL connection documentation and tables to content
        seen_connections = set()
        for connection in postgres_connections:
            # Only show the most recent documentation for each connection
            if connection["id"] in seen_connections:
                continue

            content.append(f"<PostGISConnection id={connection['id']}>")
            seen_connections.add(connection["id"])

            connection_name = (
                connection["friendly_name"]
                or connection["connection_name"]
                or "Loading..."
            )
            content.append(
                f'\n## PostGIS "{connection_name}" (ID {connection["id"]})\n'
            )

            # Add documentation if available
            if connection["summary_md"]:
                content.append("<SchemaSummary>")
                content.append(connection["summary_md"])
                content.append("</SchemaSummary>")
            else:
                content.append(
                    "No documentation available for this database connection."
                )

            # Also add live table information
            try:
                tables = await postgis_provider.get_tables_by_connection_id(
                    connection["id"], connection_manager
                )
                content.append("\n**Available Tables:** " + tables)
            except Exception:
                content.append("\nException while connecting to database.")
            content.append(f"</PostGISConnection id={connection['id']}>")

        # Batch-fetch all layers with their active styles in ONE query
        # (avoids N+1: previously each layer triggered 3 separate queries)
        layers_with_styles = await conn.fetch(
            """
            SELECT ml.layer_id, ml.name, ml.type, ml.metadata, ml.bounds,
                   ml.geometry_type, ml.created_on, ml.last_edited,
                   ml.feature_count, ml.s3_key, ml.remote_url,
                   ml.postgis_query, ml.postgis_connection_id,
                   ls.style_json, ls.style_id
            FROM map_layers ml
            JOIN user_mundiai_maps m ON ml.layer_id = ANY(m.layers)
            LEFT JOIN map_layer_styles mls
                ON mls.layer_id = ml.layer_id AND mls.map_id = m.id
            LEFT JOIN layer_styles ls ON mls.style_id = ls.style_id
            WHERE m.id = $1
            ORDER BY ml.name
            """,
            map_id,
        )

        # Generate comprehensive description
        content.append(f"# Map: {map_result['title']}\n")

        if map_result["description"]:
            content.append(f"{map_result['description']}\n")

        # Build layer descriptions in parallel using pre-loaded data
        # (no per-layer DB calls — ownership already verified at map level)
        layer_descriptions = await asyncio.gather(*[
            layer_describer.describe_layer(row["layer_id"], dict(row))
            for row in layers_with_styles
        ])

        for row, layer_description in zip(layers_with_styles, layer_descriptions):
            content.append(f"<{row['layer_id']}>")
            content.append(layer_description)
            # Append style info from pre-loaded JOIN data
            if row["style_id"]:
                style_section = f"\n## Style ID ({row['style_id']})\n"
                style_section += "```json\n"
                style_json = row["style_json"]
                if isinstance(style_json, str):
                    style_section += style_json
                else:
                    style_section += json.dumps(style_json)
                style_section += "\n```"
                content.append(style_section)
            content.append(f"</{row['layer_id']}>")

        # Join all content and return as plain text response
        response_content = "\n".join(content)

        return Response(
            content=response_content,
            media_type="text/plain",
            headers={
                "Content-Disposition": f'attachment; filename="{map_result["title"]}_description.txt"',
            },
        )


@router.get(
    "/{map_id}/style.json",
    operation_id="get_map_stylejson",
    response_class=StarletteJSONResponse,
)
async def get_map_style(
    request: Request,
    map: MundiMap = Depends(get_map),
    only_show_inline_sources: bool = False,
    override_layers: Optional[str] = None,
    basemap: Optional[str] = None,
    base_map: BaseMapProvider = Depends(get_base_map_provider),
):
    return await get_map_style_internal(
        str(map.id), base_map, only_show_inline_sources, override_layers, basemap
    )


@router.post(
    "/{original_map_id}/layers",
    response_model=LayerUploadResponse,
    operation_id="upload_layer_to_map",
    summary="Upload file as layer",
)
@heavy_limit
async def upload_layer(
    request: Request,
    original_map_id: str,
    forked_map: MundiMap = Depends(forked_map_by_user),
    file: UploadFile = File(...),
    layer_name: str = Form(None),
    add_layer_to_map: bool = Form(True),
    session: UserContext = Depends(verify_session_required),
):
    """Uploads spatial data, processes it, and adds it as a layer to the specified map.

    Supported formats:
    - Vector: Shapefile (as .zip), GeoJSON, GeoPackage, FlatGeobuf
    - Raster: GeoTIFF, DEM
    - [Point cloud](/guides/visualizing-point-clouds-las-laz/): LAZ, LAS

    Once uploaded, Mundi transforms, reprojects, styles, and creates optimized formats for display in the browser.
    Vector data is converted to [PMTiles](https://docs.protomaps.com/pmtiles/) while raster data is converted to
    [cloud-optimized GeoTIFFs](https://cogeo.org/). Point cloud data is compressed to LAZ 1.3.

    Returns the new layer details including its unique layer ID. The layer can optionally not be added to the map,
    but will be faster to add to an existing map later.

    ```py
    with open("brazil_watersheds.gpkg", "rb") as f:
        # project ID is PGJSkB1zj7fT, previous map ID is M4NzE8rk4FZS
        result = httpx.post(
            f"https://app.mundi.ai/api/maps/M4NzE8rk4FZS/layers",
            files={"file": ("brazil_watersheds.gpkg", f, "application/octet-stream")},
            data={"layer_name": "Amazon Basin Watersheds", "add_layer_to_map": True},
            headers={"Authorization": f"Bearer {os.environ['MUNDI_API_KEY']}"}
        ).json()

    assert result["name"] == "Amazon Basin Watersheds"
    assert result["dag_child_map_id"] == "M4NzE8rk4FZS"
    # use result["dag_child_map_id"] as the new map id, and view this new uploaded layer
    # by navigating to https://app.mundi.ai/project/PGJSkB1zj7fT/M4NzE8rk4FZS
    subprocess.run(["open", "https://app.mundi.ai/project/PGJSkB1zj7fT/M4NzE8rk4FZS"])
    ```
    """
    layer_result = await internal_upload_layer(
        map_id=forked_map.id,
        file=file,
        layer_name=layer_name,
        add_layer_to_map=add_layer_to_map,
        user_id=session.get_user_id(),
        project_id=forked_map.project_id,
    )
    assert layer_result is not None

    return LayerUploadResponse(
        dag_child_map_id=forked_map.id,
        dag_parent_map_id=original_map_id,
        id=layer_result.id,
        name=layer_result.name,
        type=layer_result.type,
        url=layer_result.url,
        message=layer_result.message,
    )


CLOUD_NATIVE_EXTS = {".pmtiles", ".tif"}
RASTER_EXTS = {".tif", ".jpg", ".jpeg", ".png", ".dem"}
VECTOR_EXTS = {".pmtiles", ".geojson", ".fgb", ".gpkg", ".shp", ".csv"}

ESRI_PREFIX = "ESRIJSON:"
WFS_PREFIX = "WFS:"
CSV_PREFIX = "CSV:"  # expected "CSV:/vsicurl/<URL>"


@router.post(
    "/{original_map_id}/layers/remote",
    response_model=LayerUploadResponse,
    operation_id="add_remote_layer_to_map",
    summary="Add remote layer to map",
)
async def add_remote_layer(
    original_map_id: str,
    request: RemoteLayerRequest,
    forked_map: MundiMap = Depends(forked_map_by_user),
    session: UserContext = Depends(verify_session_required),
):
    """Add a remote data source as a layer to the specified map.

    Supported remote sources:
    - Cloud Optimized GeoTIFFs (COG)
    - Remote vector files (GeoJSON, Shapefile, etc.)
    - Google Sheets (CSV export format)
    - WFS services (Web Feature Service)
    - ESRI Feature Services and Map Services
    - Any OGR/GDAL compatible URL

    The remote data is accessed via OGR's vsicurl virtual file system or appropriate drivers,
    allowing efficient access to cloud-optimized formats without downloading the entire file.
    """

    validate_remote_url(request.url, request.source_type)

    url = request.url
    declared = request.source_type
    if declared not in {"sheets", "vector", "raster"}:
        raise HTTPException(
            status_code=400, detail=f"Unsupported source type: {declared}"
        )
    if declared == "sheets" and not url.startswith(CSV_PREFIX):
        raise HTTPException(
            status_code=400, detail=f"Google Sheets must use CSV: prefix, got: {url}"
        )

    parsed = urlparse(url)
    ext = Path(parsed.path).suffix.lower()

    if url.startswith(CSV_PREFIX):
        kind = "csv"
        ext = ".csv"
    elif url.startswith(WFS_PREFIX):
        kind = "wfs"
        ext = ".gml"
    elif url.startswith(ESRI_PREFIX):
        kind = "esri"
        ext = ".geojson"
    else:
        if not url.startswith(("http://", "https://")):
            raise HTTPException(
                status_code=400,
                detail="Remote sources must be HTTP(S) URLs or supported service prefixes.",
            )
        kind = "cloud" if ext in CLOUD_NATIVE_EXTS else "http"

    # infer layer type
    if declared == "raster":
        layer_type = "raster"
    elif declared in {"vector", "sheets"}:
        layer_type = "vector"
    else:
        layer_type = "raster" if ext in RASTER_EXTS else "vector"

    is_cloud_native = kind == "cloud"
    if declared == "raster" and not url.startswith(("http://", "https://")):
        raise HTTPException(
            status_code=400, detail=f"Raster sources must be HTTP URLs, got: {url}"
        )

    temp_paths_to_cleanup: list[str] = []

    if kind == "csv":
        # "CSV:/vsicurl/<URL>"
        ogr_source = url
    elif kind in {"wfs", "esri"}:
        ogr_source = url
    elif is_cloud_native:
        ogr_source = f"/vsicurl/{url}"
    else:
        # regular HTTP file: download and use internal upload
        async with aiohttp.ClientSession() as http_session:
            async with http_session.get(url) as resp:
                if resp.status != 200:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail=f"Unable to download remote file: HTTP {resp.status}",
                    )
                content = await resp.read()

        filename = Path(parsed.path).name or f"remote_file{ext}"
        file_obj = UploadFile(
            file=BytesIO(content),
            filename=filename,
            headers={"content-type": "application/octet-stream"},
        )

        internal_response = await internal_upload_layer(
            forked_map.id,
            file_obj,
            request.name,
            request.add_layer_to_map,
            session.get_user_id(),
            forked_map.project_id,
        )
        assert internal_response is not None

        return LayerUploadResponse(
            dag_child_map_id=forked_map.id,
            dag_parent_map_id=original_map_id,
            id=internal_response.id,
            name=internal_response.name,
            type=internal_response.type,
            url=internal_response.url,
            message="Remote layer processed and added successfully",
        )

    # external vector sources are converted to local files
    if layer_type == "vector" and not is_cloud_native:
        with tempfile.NamedTemporaryFile(suffix=".fgb", delete=False) as t:
            out_path = t.name
        os.remove(out_path)

        ogr_cmd = ["ogr2ogr", "-overwrite", "-f", "FlatGeobuf", out_path, ogr_source]
        if ext == ".csv" or url.startswith(CSV_PREFIX):
            ogr_cmd += [
                "-oo",
                "X_POSSIBLE_NAMES=lon,long,longitude,lng,x",
                "-oo",
                "Y_POSSIBLE_NAMES=lat,latitude,y",
                "-oo",
                "KEEP_GEOM_COLUMNS=NO",
                "-a_srs",
                "EPSG:4326",
                "-lco",
                "SPATIAL_INDEX=YES",
            ]

        proc = await asyncio.create_subprocess_exec(
            *ogr_cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        _stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Failed to convert remote file to optimized format. Check URL accessibility and validity.",
            )

        temp_paths_to_cleanup.append(out_path)
        ogr_source = out_path
        ext = ".fgb"

    layer_id = generate_id(prefix="L")
    metadata = {"original_url": url, "source": "remote"}
    if url.startswith(CSV_PREFIX):
        metadata.update(
            {
                "original_filename": "Google Sheets CSV Export",
                "google_sheets_url": url.replace("CSV:/vsicurl/", ""),
            }
        )
    else:
        metadata["original_filename"] = Path(parsed.path).name or f"remote_file{ext}"

    bounds = None
    geometry_type = "unknown"
    feature_count = None

    try:
        layer_result: Optional[VectorProcessingResult] = None
        processing_source = ogr_source

        if is_cloud_native:
            li = await get_layer_bounds_and_metadata(processing_source, layer_type, url)
            bounds = li.bounds
            geometry_type = li.geometry_type if layer_type == "vector" else "raster"
            feature_count = li.feature_count
            metadata.update(li.metadata_updates.model_dump(exclude_none=True))
            layer_result = None
        elif layer_type == "vector":
            layer_result = await process_vector_layer_common(
                layer_id,
                processing_source,
                request.name,
                session.get_user_id(),
                forked_map.project_id,
            )
            bounds = layer_result.bounds
            geometry_type = layer_result.geometry_type
            feature_count = layer_result.feature_count
            metadata = {
                **metadata,
                **layer_result.metadata.model_dump(exclude_none=True),
            }
        else:
            li = await get_layer_bounds_and_metadata(processing_source, layer_type, url)
            bounds = li.bounds
            geometry_type = "raster"
            feature_count = None
            metadata.update(li.metadata_updates.model_dump(exclude_none=True))

        async with get_async_db_connection() as conn:
          async with conn.transaction():
            await conn.fetchrow(
                """
                INSERT INTO map_layers
                (layer_id, owner_uuid, name, type, metadata, bounds, geometry_type, feature_count, source_map_id, remote_url)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
                RETURNING layer_id
                """,
                layer_id,
                session.get_user_id(),
                request.name,
                layer_type,
                json.dumps(metadata),
                bounds,
                geometry_type if layer_type == "vector" else None,
                feature_count,
                forked_map.id,
                url,
            )

            if (
                layer_type == "vector"
                and geometry_type != "unknown"
                and not is_cloud_native
            ):
                assert layer_result is not None
                maplibre_layers = layer_result.maplibre_style
                if maplibre_layers:
                    style_id = generate_id(prefix="S")
                    await conn.execute(
                        "INSERT INTO layer_styles (style_id, layer_id, style_json, created_by) VALUES ($1,$2,$3,$4)",
                        style_id,
                        layer_id,
                        json.dumps(maplibre_layers),
                        session.get_user_id(),
                    )
                    await conn.execute(
                        "INSERT INTO map_layer_styles (map_id, layer_id, style_id) VALUES ($1,$2,$3)",
                        forked_map.id,
                        layer_id,
                        style_id,
                    )

            if request.add_layer_to_map:
                map_data = await conn.fetchrow(
                    "SELECT layers FROM user_mundiai_maps WHERE id=$1", forked_map.id
                )
                current_layers = (
                    map_data["layers"] if map_data and map_data["layers"] else []
                )
                await conn.execute(
                    "UPDATE user_mundiai_maps SET layers=$1, last_edited=CURRENT_TIMESTAMP WHERE id=$2",
                    current_layers + [layer_id],
                    forked_map.id,
                )

    finally:
        for p in temp_paths_to_cleanup:
            try:
                if p and os.path.exists(p):
                    os.unlink(p)
            except Exception:
                logger.debug("Failed to clean up temp file: %s", p)

    layer_url = (
        f"/api/layer/{layer_id}.pmtiles"
        if layer_type == "vector"
        else f"/api/layer/{layer_id}.cog.tif"
    )

    return LayerUploadResponse(
        dag_child_map_id=forked_map.id,
        dag_parent_map_id=original_map_id,
        id=layer_id,
        name=request.name,
        type=layer_type,
        url=layer_url,
        message="Remote layer processed and added successfully",
    )


# PMTiles generation and vector processing — imported from src.upload.pmtiles (canonical location)
from src.upload.pmtiles import (  # noqa: E402
    process_vector_layer_common,
)


@router.put("/{map_id}/layer/{layer_id}", operation_id="add_layer_to_map")
async def add_layer_to_map(
    map: MundiMap = Depends(edit_map),
    layer: MapLayer = Depends(get_layer),
):
    if map.layers is not None and layer.id in map.layers:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Layer is already associated with this map",
        )

    async with get_async_db_connection() as conn:
        # Update the map to include the layer_id in its layers array
        updated_map = await conn.fetchrow(
            """
            UPDATE user_mundiai_maps
            SET layers = array_append(layers, $1),
                last_edited = CURRENT_TIMESTAMP
            WHERE id = $2
            RETURNING id
            """,
            layer.id,
            map.id,
        )

        if not updated_map:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to associate layer with map",
            )

        return {
            "message": "Layer successfully associated with map",
            "layer_id": layer.id,
            "layer_name": layer.name,
            "map_id": map.id,
        }


@router.get(
    "/{map_id}/render.png",
    operation_id="render_map_to_png",
    summary="Render a map as PNG",
)
async def render_map(
    request: Request,
    map: MundiMap = Depends(get_map),
    bbox: Optional[str] = None,
    width: int = 1024,
    height: int = 600,
    bgcolor: str = "#ffffff",
    base_map: BaseMapProvider = Depends(get_base_map_provider),
    session: Optional[UserContext] = Depends(verify_session_optional),
):
    """Renders a map as a static PNG image, including layers and their symbology.

    If no `bbox` is provided, the extent defaults to the smallest extent that contains
    all layers with well-defined bounding boxes. `bbox` must be in the format `xmin,ymin,xmax,ymax` (EPSG:4326).

    Width and height are in pixels.
    """
    style_json = await get_map_style_internal(
        str(map.id), base_map, only_show_inline_sources=True
    )

    return (
        await render_map_internal(
            map.id, bbox, width, height, "mbgl", bgcolor, style_json
        )
    )[0]


@router.delete(
    "/{original_map_id}/layer/{layer_id}",
    operation_id="remove_layer_from_map",
    response_model=LayerRemovalResponse,
)
async def remove_layer_from_map(
    original_map_id: str,
    layer_id: str,
    forked_map: MundiMap = Depends(forked_map_by_user),
):
    # Check if the layer exists and is in the map's layers array
    if layer_id not in forked_map.layers:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Layer not found or not associated with this map",
        )

    async with get_async_db_connection() as conn:
        async with conn.transaction():
            # Get layer name and metadata for response and S3 cleanup
            layer_result = await conn.fetchrow(
                """
                SELECT name, metadata FROM map_layers WHERE layer_id = $1
                """,
                layer_id,
            )
            layer_name = layer_result["name"] if layer_result else "Unknown"
            layer_metadata = layer_result["metadata"] if layer_result else None

            # Parse metadata if it's a JSON string
            if layer_metadata and isinstance(layer_metadata, str):
                try:
                    layer_metadata = json.loads(layer_metadata)
                except Exception:
                    layer_metadata = None

            # Clean up all S3 objects associated with this layer
            s3_keys_to_delete = []
            if layer_metadata and isinstance(layer_metadata, dict):
                for key_name in ("pmtiles_key", "s3_key", "cog_key"):
                    key_val = layer_metadata.get(key_name)
                    if key_val:
                        s3_keys_to_delete.append(key_val)

            if s3_keys_to_delete:
                try:
                    s3 = await get_async_s3_client()
                    bucket = get_bucket_name()
                    for s3_key in s3_keys_to_delete:
                        await s3.delete_object(Bucket=bucket, Key=s3_key)
                        logger.info("Cleaned up S3 object: %s", s3_key)
                except Exception as e:
                    logger.warning("Failed to clean up S3 objects for layer %s: %s", layer_id, e)

            # Invalidate Redis tile cache (raster + MVT)
            try:
                deleted_tiles = await tile_cache.invalidate_layer(layer_id)
                if deleted_tiles:
                    logger.info("Invalidated %d cached tiles for layer %s", deleted_tiles, layer_id)
            except Exception as e:
                logger.warning("Failed to invalidate tile cache for layer %s: %s", layer_id, e)

            # Invalidate fs_lru GPKG cache
            try:
                lc = layer_cache()
                if lc.invalidate_layer(layer_id):
                    logger.info("Invalidated fs_lru GPKG cache for layer %s", layer_id)
            except Exception as e:
                logger.warning("Failed to invalidate fs_lru cache for layer %s: %s", layer_id, e)

            # Delete the map_layers row if no other map references this layer
            orphan_check = await conn.fetchval(
                """
                SELECT COUNT(*) FROM user_mundiai_maps
                WHERE $1 = ANY(layers) AND id != $2
                """,
                layer_id,
                forked_map.id,
            )
            if orphan_check == 0:
                await conn.execute(
                    "DELETE FROM map_layers WHERE layer_id = $1",
                    layer_id,
                )
                logger.info("Deleted orphaned map_layers row for layer %s", layer_id)

            # Remove the layer from the child map's layers array
            updated_layers = [lid for lid in forked_map.layers if lid != layer_id]
            await conn.execute(
                """
                UPDATE user_mundiai_maps
                SET layers = $1,
                    last_edited = CURRENT_TIMESTAMP
                WHERE id = $2
                """,
                updated_layers,
                forked_map.id,
            )

    return LayerRemovalResponse(
        dag_child_map_id=forked_map.id,
        dag_parent_map_id=original_map_id,
        layer_id=layer_id,
        layer_name=layer_name,
        message="Layer successfully removed from map",
    )


@router.patch("/{map_id}", operation_id="update_map", summary="Update map")
async def update_map(
    update_data: MapUpdateRequest,
    map: MundiMap = Depends(get_map),
):
    """Updates an existing map's properties. Currently supports updating
    the map's basemap style.

    The basemap determines the background map tiles displayed beneath your
    data layers. Available basemap options for Mundi cloud are from MapTiler:
    - `hybrid` - Satellite imagery
    - `basic-v2` - Basic street map (default)
    - `dataviz` - Light basemap for data visualization
    - `dataviz-dark` - Dark basemap for data visualization
    - `outdoor-v2` - Outdoor/terrain map

    ```py
    result = httpx.patch(
        "https://app.mundi.ai/api/maps/MWfqcRak59bo",
        json={"basemap": "hybrid"},
        headers={"Authorization": f"Bearer {os.environ['MUNDI_API_KEY']}"}
    ).json()

    assert result == {
        "id": "MWfqcRak59bo",
        "basemap": "hybrid",
        "message": "Map updated successfully"
    }
    ```"""
    if update_data.basemap is None:
        return {"message": "No basemap update provided"}

    async with async_conn("update_map") as conn:
        updated_map = await conn.fetchrow(
            """
            UPDATE user_mundiai_maps
            SET basemap = $1, last_edited = CURRENT_TIMESTAMP
            WHERE id = $2
            RETURNING id, basemap
        """,
            update_data.basemap,
            map.id,
        )

        if not updated_map:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to update map",
            )

        return {
            "message": "Map updated successfully",
            "map_id": updated_map["id"],
            "basemap": updated_map["basemap"],
        }


@router.get("/", operation_id="list_user_maps", response_model=UserMapsResponse)
async def get_user_maps(
    request: Request, session: UserContext = Depends(verify_session_required)
):
    """
    Get all maps owned by the authenticated user.

    Returns a list of all maps that belong to the currently authenticated user.
    Authentication is required via SuperTokens session or API key.
    """
    # Get the user ID from authentication
    user_id = session.get_user_id()

    # Connect to database
    async with get_async_db_connection() as conn:
        # Get all maps owned by this user that are not soft-deleted
        maps_data = await conn.fetch(
            """
            SELECT m.id, m.title, m.description, m.created_on, m.last_edited, m.project_id
            FROM user_mundiai_maps m
            WHERE m.owner_uuid = $1 AND m.soft_deleted_at IS NULL
            ORDER BY m.last_edited DESC
            """,
            user_id,
        )

        # Convert datetime objects to ISO format strings for JSON serialization
        maps_response = []
        for map_data in maps_data:
            maps_response.append(
                MapResponse(
                    id=map_data["id"],
                    project_id=map_data["project_id"],
                    title=map_data["title"] or "Untitled Map",
                    created_on=map_data["created_on"].isoformat(),
                    map_link=f"{os.environ['WEBSITE_DOMAIN']}/project/{map_data['project_id']}",
                )
            )

        # Return the list of maps
        return UserMapsResponse(maps=maps_response)


# Export both routers
__all__ = ["router"]
