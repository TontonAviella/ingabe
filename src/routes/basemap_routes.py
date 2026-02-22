import json
import datetime
import logging

from botocore.exceptions import ClientError
from fastapi import APIRouter, HTTPException, status, Depends, Query
from fastapi.responses import Response
from starlette.responses import JSONResponse as StarletteJSONResponse

from src.dependencies.base_map import BaseMapProvider, get_base_map_provider
from src.utils import get_async_s3_client, get_bucket_name
from src.services.map_service import render_map_internal

logger = logging.getLogger(__name__)

# Create router for basemap endpoints
basemap_router = APIRouter()


@basemap_router.get(
    "/available",
    operation_id="get_available_basemaps",
    response_class=StarletteJSONResponse,
)
async def get_available_basemaps(
    base_map: BaseMapProvider = Depends(get_base_map_provider),
):
    """Get list of available basemap styles."""
    return {
        "styles": base_map.get_available_styles(),
        "display_names": base_map.get_style_display_names(),
    }


@basemap_router.get(
    "/{name}/style.json",
    operation_id="get_basemap_style",
    response_class=StarletteJSONResponse,
)
async def get_basemap_style(
    name: str,
    base_map: BaseMapProvider = Depends(get_base_map_provider),
):
    """Return the MapLibre GL style JSON for a single basemap (sources + layers only).

    Used by the frontend to swap basemaps client-side without a full
    map.setStyle() call, which would destroy all overlay layers.
    """
    available = base_map.get_available_styles()
    if name not in available:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid basemap '{name}'. Available: {available}",
        )
    style = await base_map.get_base_style(name)
    return style


@basemap_router.get("/render.png", operation_id="render_basemap")
async def render_basemap(
    basemap: str = Query(...),
    base_map: BaseMapProvider = Depends(get_base_map_provider),
):
    available_basemaps = base_map.get_available_styles()
    if basemap not in available_basemaps:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid basemap '{basemap}'. Available options: {available_basemaps}",
        )

    s3_key = f"basemap-previews/{basemap}.png"
    s3 = await get_async_s3_client()
    bucket = get_bucket_name()

    try:
        head_response = await s3.head_object(Bucket=bucket, Key=s3_key)
        last_modified = head_response["LastModified"]

        # Check if cache is less than 24 hours old
        now = datetime.datetime.now(datetime.timezone.utc)
        age = now - last_modified

        if age.total_seconds() < 86400:  # 24 hours = 86400 seconds
            response = await s3.get_object(Bucket=bucket, Key=s3_key)
            cached_image = await response["Body"].read()
            return Response(content=cached_image, media_type="image/png")
    except ClientError as e:
        error_code = e.response.get("Error", {}).get("Code", "")
        if error_code in ("404", "NoSuchKey"):
            logger.debug("S3 cache miss for basemap thumbnail %s", basemap)
        else:
            logger.warning("S3 error fetching basemap thumbnail %s: %s", basemap, e)
    except Exception as e:
        logger.warning("Unexpected error fetching basemap thumbnail %s: %s", basemap, e)

    style_json = await base_map.get_base_style(basemap)

    response, _ = await render_map_internal(
        map_id=f"basemap_{basemap}",
        bbox="-10,29.75,30,70",
        width=256,
        height=256,
        renderer="mbgl",
        bgcolor="white",
        style_json=json.dumps(style_json),
    )

    try:
        await s3.put_object(Bucket=bucket, Key=s3_key, Body=response.body)
    except ClientError as e:
        logger.warning("S3 error caching basemap thumbnail: %s", e)
    except Exception as e:
        logger.warning("Unexpected error caching basemap thumbnail: %s", e)

    return response
