"""Brain hook processor: consumes pending hooks and creates brain pages.

Processes two hook types:
  - vector_upload: reads vector layer features from S3, creates one brain page
    per feature with geometry + attributes.
  - raster_upload: creates a single brain page for the raster layer with bounds
    as geometry.

Designed to run as a background task on app startup or via a periodic scheduler.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import date
from typing import Optional

import asyncpg

from src.services.brain_service import BrainService, PageInput, TimelineInput, _validate_slug

logger = logging.getLogger(__name__)

MAX_FEATURES_PER_LAYER = 500  # Cap to avoid creating thousands of pages


async def process_pending_hooks(
    conn: asyncpg.Connection,
    brain: BrainService,
    limit: int = 10,
) -> dict:
    """Fetch and process pending hooks. Returns {processed, failed, skipped}."""
    hooks = await brain.get_pending_hooks(conn, limit=limit)
    processed = 0
    failed = 0
    skipped = 0

    for hook in hooks:
        hook_id = hook["id"]
        hook_type = hook["hook_type"]
        payload = hook["payload"] if isinstance(hook["payload"], dict) else json.loads(hook["payload"])

        try:
            if hook_type == "vector_upload":
                n = await _process_vector_hook(conn, brain, payload)
            elif hook_type == "raster_upload":
                n = await _process_raster_hook(conn, brain, payload)
            elif hook_type == "partner_url_fetch":
                n = await _process_partner_url_hook(conn, brain, payload)
            else:
                logger.warning("Unknown hook type: %s", hook_type)
                skipped += 1
                await brain.complete_hook(conn, hook_id)
                continue

            await brain.complete_hook(conn, hook_id)
            processed += 1
            logger.info("Hook %d (%s) processed: %d pages created", hook_id, hook_type, n)

        except Exception as e:
            logger.exception("Hook %d (%s) failed", hook_id, hook_type)
            await brain.fail_hook(conn, hook_id, str(e)[:500])
            failed += 1

    return {"processed": processed, "failed": failed, "skipped": skipped}


async def _process_vector_hook(
    conn: asyncpg.Connection,
    brain: BrainService,
    payload: dict,
) -> int:
    """Create brain pages from vector layer features.

    For each feature, creates a brain page with:
    - slug: layer-{layer_id}-f{feature_index}
    - type: "field" (default, can be overridden by attributes)
    - title: from feature name/id attribute or auto-generated
    - compiled_truth: summary of feature attributes
    - geom: feature geometry as GeoJSON
    - frontmatter: all feature attributes
    """
    layer_ids = payload.get("layer_ids", [])
    layer_name = payload.get("layer_name", "Unknown Layer")
    user_id = payload.get("user_id", "")

    if not layer_ids or not user_id:
        logger.warning("vector_upload hook missing layer_ids or user_id")
        return 0

    total_pages = 0

    for layer_id in layer_ids:
        # Get layer info from DB
        layer = await conn.fetchrow(
            "SELECT layer_id, name, type, metadata, bounds, geometry_type, s3_key "
            "FROM map_layers WHERE layer_id = $1",
            layer_id,
        )
        if not layer:
            logger.warning("Layer %s not found, skipping", layer_id)
            continue

        s3_key = layer["s3_key"]
        if not s3_key:
            logger.warning("Layer %s has no s3_key, skipping", layer_id)
            continue

        # Try to read features from the file on S3
        n = await _create_pages_from_s3_vector(
            conn, brain, layer_id, s3_key, layer["name"] or layer_name, user_id,
            layer.get("geometry_type"),
        )
        total_pages += n

        # Log the ingest
        if n > 0:
            await brain.log_ingest(
                conn,
                source_type="vector_upload",
                source_ref=layer_id,
                pages_updated=[f"layer-{layer_id}-f{i}" for i in range(n)],
                summary=f"Created {n} pages from vector layer '{layer['name'] or layer_name}'",
            )

    return total_pages


async def _create_pages_from_s3_vector(
    conn: asyncpg.Connection,
    brain: BrainService,
    layer_id: str,
    s3_key: str,
    layer_name: str,
    user_id: str,
    geometry_type: Optional[str] = None,
) -> int:
    """Download vector file from S3 and create brain pages from features."""
    import fiona
    from shapely.geometry import mapping, shape

    from src.utils import get_s3_client

    s3_client = get_s3_client()
    bucket = os.environ.get("S3_BUCKET", "mundi-uploads")

    # Check for PMTiles key (we need the original source file)
    # PMTiles are derived files, look for the original upload
    if s3_key.endswith(".pmtiles"):
        # Try common source patterns
        for ext in [".fgb", ".geojson", ".gpkg"]:
            candidate = s3_key.rsplit(".", 1)[0] + ext
            try:
                s3_client.head_object(Bucket=bucket, Key=candidate)
                s3_key = candidate
                break
            except Exception:
                continue
        else:
            # Also check uploads/ prefix variants
            logger.info("No source file found for PMTiles %s, skipping feature extraction", s3_key)
            return await _create_layer_summary_page(conn, brain, layer_id, layer_name, user_id, geometry_type)

    # Download to temp file
    suffix = "." + s3_key.rsplit(".", 1)[-1] if "." in s3_key else ".fgb"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=True) as tmp:
        try:
            s3_client.download_file(bucket, s3_key, tmp.name)
        except Exception:
            logger.warning("Failed to download %s from S3, creating summary page only", s3_key)
            return await _create_layer_summary_page(conn, brain, layer_id, layer_name, user_id, geometry_type)

        # Read features with fiona
        try:
            pages_created = 0
            with fiona.open(tmp.name) as src:
                for i, feature in enumerate(src):
                    if i >= MAX_FEATURES_PER_LAYER:
                        logger.info("Hit feature cap (%d) for layer %s", MAX_FEATURES_PER_LAYER, layer_id)
                        break

                    props = dict(feature.get("properties", {}))
                    geom = feature.get("geometry")

                    # Build page content
                    name = _extract_feature_name(props, i, layer_name)
                    slug = _validate_slug(f"layer-{layer_id}-f{i}")
                    page_type = _infer_page_type(props, geometry_type)
                    truth = _build_feature_truth(props, layer_name, page_type)

                    geom_json = json.dumps(geom) if geom else None

                    await brain.put_page(
                        conn,
                        slug,
                        PageInput(
                            type=page_type,
                            title=name,
                            compiled_truth=truth,
                            frontmatter=_clean_frontmatter(props),
                            geom_geojson=geom_json,
                        ),
                        owner_uuid=user_id,
                    )

                    # Add timeline entry for creation
                    await brain.add_timeline_entry(
                        conn,
                        slug,
                        TimelineInput(
                            date=date.today(),
                            summary=f"Created from vector upload: {layer_name}",
                            source="vector_upload",
                        ),
                        owner_uuid=user_id,
                    )

                    # Link feature page to layer summary page
                    layer_slug = _validate_slug(f"layer-{layer_id}")
                    try:
                        await brain.add_link(conn, layer_slug, slug, link_type="contains")
                    except ValueError:
                        pass  # Layer summary page doesn't exist yet

                    pages_created += 1

            # Also create a summary page for the layer itself
            await _create_layer_summary_page(conn, brain, layer_id, layer_name, user_id, geometry_type)

            return pages_created

        except Exception:
            logger.exception("Failed to read features from %s", s3_key)
            return await _create_layer_summary_page(conn, brain, layer_id, layer_name, user_id, geometry_type)


async def _create_layer_summary_page(
    conn: asyncpg.Connection,
    brain: BrainService,
    layer_id: str,
    layer_name: str,
    user_id: str,
    geometry_type: Optional[str] = None,
) -> int:
    """Create a single summary brain page for a layer (fallback when features can't be read)."""
    slug = _validate_slug(f"layer-{layer_id}")

    # Get bounds from map_layers
    layer = await conn.fetchrow(
        "SELECT bounds, feature_count, metadata FROM map_layers WHERE layer_id = $1",
        layer_id,
    )
    bounds = layer["bounds"] if layer else None
    feature_count = layer["feature_count"] if layer else None
    metadata = layer["metadata"] if layer else {}
    if isinstance(metadata, str):
        metadata = json.loads(metadata)

    truth_parts = [f"Vector layer: {layer_name}"]
    if geometry_type:
        truth_parts.append(f"Geometry: {geometry_type}")
    if feature_count:
        truth_parts.append(f"Features: {feature_count}")
    if bounds and len(bounds) == 4:
        truth_parts.append(f"Bounds: [{bounds[0]:.4f}, {bounds[1]:.4f}, {bounds[2]:.4f}, {bounds[3]:.4f}]")

    # Convert bounds to bbox GeoJSON polygon
    geom_json = None
    if bounds and len(bounds) == 4:
        geom_json = json.dumps({
            "type": "Polygon",
            "coordinates": [[
                [bounds[0], bounds[1]],
                [bounds[2], bounds[1]],
                [bounds[2], bounds[3]],
                [bounds[0], bounds[3]],
                [bounds[0], bounds[1]],
            ]],
        })

    await brain.put_page(
        conn,
        slug,
        PageInput(
            type="field",
            title=layer_name,
            compiled_truth=". ".join(truth_parts),
            frontmatter={"layer_id": layer_id, "source": "vector_upload"},
            geom_geojson=geom_json,
        ),
        owner_uuid=user_id,
    )

    await brain.add_timeline_entry(
        conn,
        slug,
        TimelineInput(
            date=date.today(),
            summary=f"Layer uploaded: {layer_name}",
            source="vector_upload",
        ),
        owner_uuid=user_id,
    )

    return 1


async def _process_raster_hook(
    conn: asyncpg.Connection,
    brain: BrainService,
    payload: dict,
) -> int:
    """Create a brain page for a raster upload with bounds as geometry."""
    layer_id = payload.get("layer_id", "")
    layer_name = payload.get("layer_name", "Unknown Raster")
    user_id = payload.get("user_id", "")
    bounds = payload.get("bounds")

    if not layer_id or not user_id:
        logger.warning("raster_upload hook missing layer_id or user_id")
        return 0

    # Get additional metadata from DB
    layer = await conn.fetchrow(
        "SELECT metadata, bounds FROM map_layers WHERE layer_id = $1",
        layer_id,
    )
    metadata = {}
    if layer and layer["metadata"]:
        metadata = layer["metadata"] if isinstance(layer["metadata"], dict) else json.loads(layer["metadata"])
    if not bounds and layer:
        bounds = layer["bounds"]

    slug = _validate_slug(f"raster-{layer_id}")

    # Build compiled truth
    truth_parts = [f"Raster layer: {layer_name}"]
    if bounds and len(bounds) == 4:
        truth_parts.append(f"Bounds: [{bounds[0]:.4f}, {bounds[1]:.4f}, {bounds[2]:.4f}, {bounds[3]:.4f}]")
    if metadata.get("band_count"):
        truth_parts.append(f"Bands: {metadata['band_count']}")
    if metadata.get("original_srid"):
        truth_parts.append(f"SRID: {metadata['original_srid']}")
    if metadata.get("raster_value_stats_b1"):
        stats = metadata["raster_value_stats_b1"]
        truth_parts.append(f"Value range: {stats.get('min', '?')} to {stats.get('max', '?')}")

    geom_json = None
    if bounds and len(bounds) == 4:
        geom_json = json.dumps({
            "type": "Polygon",
            "coordinates": [[
                [bounds[0], bounds[1]],
                [bounds[2], bounds[1]],
                [bounds[2], bounds[3]],
                [bounds[0], bounds[3]],
                [bounds[0], bounds[1]],
            ]],
        })

    await brain.put_page(
        conn,
        slug,
        PageInput(
            type="field",
            title=layer_name,
            compiled_truth=". ".join(truth_parts),
            frontmatter={
                "layer_id": layer_id,
                "source": "raster_upload",
                **{k: v for k, v in metadata.items() if k in (
                    "band_count", "width", "height", "original_srid",
                    "raster_value_stats_b1",
                )},
            },
            geom_geojson=geom_json,
        ),
        owner_uuid=user_id,
    )

    await brain.add_timeline_entry(
        conn,
        slug,
        TimelineInput(
            date=date.today(),
            summary=f"Raster uploaded: {layer_name}",
            source="raster_upload",
        ),
        owner_uuid=user_id,
    )

    await brain.log_ingest(
        conn,
        source_type="raster_upload",
        source_ref=layer_id,
        pages_updated=[slug],
        summary=f"Created brain page for raster '{layer_name}'",
    )

    return 1


async def _process_partner_url_hook(
    conn: asyncpg.Connection,
    brain: BrainService,
    payload: dict,
) -> int:
    """Fetch a URL and create a partner-private brain page."""
    import httpx

    url = payload.get("url", "")
    slug = payload.get("slug", "")
    title = payload.get("title", url[:80])
    org_id = payload.get("org_id", "")
    user_id = payload.get("user_id", "")

    if not url or not slug or not org_id or not user_id:
        logger.warning("partner_url_fetch hook missing required fields")
        return 0

    from src.routes.partner_routes import _validate_url_safety

    async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
        for _attempt in range(5):
            resp = await client.get(url)
            if resp.status_code in (301, 302, 303, 307, 308):
                redirect_url = str(resp.headers.get("location", ""))
                if not redirect_url:
                    break
                if redirect_url.startswith("/"):
                    from urllib.parse import urlparse, urlunparse
                    parsed = urlparse(url)
                    redirect_url = urlunparse((parsed.scheme, parsed.netloc, redirect_url, "", "", ""))
                _validate_url_safety(redirect_url)
                url = redirect_url
                continue
            break
        resp.raise_for_status()

    text = resp.text
    if not text.strip():
        logger.warning("partner_url_fetch: empty content from %s", url)
        return 0

    # Truncate very large pages
    if len(text) > 500_000:
        text = text[:500_000]

    from src.services.brain_service import PageInput, TimelineInput, _validate_slug

    slug = _validate_slug(slug)
    now = date.today()

    async with conn.transaction():
        await brain.put_page(
            conn,
            slug,
            PageInput(
                type="source_document",
                title=title,
                compiled_truth=text,
                frontmatter={
                    "source_type": "partner_url_fetch",
                    "source_url": url,
                },
            ),
            owner_uuid=user_id,
        )

        await conn.execute(
            """
            UPDATE brain_pages
            SET access_scope = 'partner_internal',
                partner_id   = $2::uuid,
                source_id    = $3,
                fetched_at   = now()
            WHERE slug = $1
            """,
            slug,
            org_id,
            f"partner-url-{org_id[:8]}",
        )

        await brain.add_timeline_entry(
            conn,
            slug,
            TimelineInput(
                date=now,
                summary=f"Fetched from partner URL: {url[:100]}",
                source="partner_url_fetch",
            ),
            owner_uuid=user_id,
        )

    return 1


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _extract_feature_name(props: dict, index: int, layer_name: str) -> str:
    """Try to find a human-readable name from feature properties."""
    for key in ("name", "Name", "NAME", "title", "Title", "label", "Label",
                "id", "ID", "fid", "FID", "gid"):
        val = props.get(key)
        if val and str(val).strip():
            return str(val).strip()[:200]
    return f"{layer_name} #{index + 1}"


def _infer_page_type(props: dict, geometry_type: Optional[str] = None) -> str:
    """Infer brain page type from feature properties."""
    # Check for explicit type hints
    for key in ("type", "Type", "TYPE", "category", "Category"):
        val = props.get(key, "")
        if isinstance(val, str) and val.lower() in ("field", "farmer", "district", "crop"):
            return val.lower()

    # Infer from geometry type
    if geometry_type and geometry_type.lower() in ("polygon", "multipolygon"):
        return "field"
    if geometry_type and geometry_type.lower() in ("point", "multipoint"):
        return "farmer"  # Points are often farmer locations

    return "field"


def _build_feature_truth(props: dict, layer_name: str, page_type: str) -> str:
    """Build a compiled_truth summary from feature attributes."""
    parts = [f"Feature from layer '{layer_name}', type: {page_type}."]

    # Add interesting attributes
    skip_keys = {"geometry", "geom", "shape", "ogc_fid", "fid", "gid"}
    for key, val in props.items():
        if key.lower() in skip_keys or val is None or str(val).strip() == "":
            continue
        parts.append(f"{key}: {val}")
        if len(parts) > 15:
            break

    return " ".join(parts)


def _clean_frontmatter(props: dict) -> dict:
    """Clean feature properties for storage as frontmatter."""
    clean = {}
    for k, v in props.items():
        if v is None:
            continue
        # Skip binary/geometry fields
        if isinstance(v, (bytes, memoryview)):
            continue
        # Truncate long strings
        if isinstance(v, str) and len(v) > 1000:
            v = v[:1000]
        clean[k] = v
    return clean


async def run_hook_processor_once(limit: int = 10) -> dict:
    """Convenience function to run one batch of hook processing.

    Creates its own DB connection with empty app.user_id (bypasses RLS).
    After processing upload hooks, backfills embeddings for any pages whose
    compiled_truth/timeline was written (by hooks or the ingestion fetchers)
    but has no chunks yet. This keeps search/sage retrieval consistent with
    the continuous-ops SLA — freshly ingested pages become queryable on the
    next hook tick (30s), not on the next nightly batch.
    """
    from src.database.pool import _build_postgres_url
    from src.services.brain_embeddings import embed_all_stale

    url = _build_postgres_url()
    conn = await asyncpg.connect(url)
    try:
        # Empty user_id bypasses RLS (background worker mode)
        await conn.execute("SELECT set_config('app.user_id', '', false)")
        brain = BrainService()
        hook_result = await process_pending_hooks(conn, brain, limit=limit)

        # Embedding backfill. Separate try/except so a single bad page
        # doesn't stall the hook loop.
        try:
            embed_result = await embed_all_stale(conn, brain, limit=limit)
        except Exception:
            logger.exception("embed_all_stale failed in hook loop")
            embed_result = {"embedded": 0, "skipped": 0, "errors": -1}

        return {**hook_result, "embeddings": embed_result}
    finally:
        await conn.close()
