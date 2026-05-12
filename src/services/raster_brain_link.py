"""Best-effort helpers that wire drone raster tools into Brain memory.

Without these, every Sage verdict (interpret_raster_health, compare_rasters,
evaluate_insurance_trigger, find_stress_zones, analyze_rgb_field) is ephemeral
— shown to the user once, then lost. With them, each verdict appends a
timeline entry to the raster-{layer_id} brain page (created earlier by
brain_hook_processor._process_raster_upload), so Brain accumulates a real
analysis history.

Same idea for Clay v1.5 embeddings: when embed_layer finishes, this module
stamps the brain page's frontmatter with `clay_tiles_embedded: N` and
`clay_collection: clay_tiles_v1` so a future query can see at-a-glance which
layers are searchable by visual similarity (without round-tripping to Milvus).

ALL functions are best-effort. Brain failures (page missing, DB hiccup, RLS
block) never propagate. The verdict the tool returns to Sage is the source
of truth for the user; Brain logging is additive.
"""

import json
import logging
import re
from datetime import date as _date
from typing import Any, Optional

logger = logging.getLogger(__name__)


def _slug_for_layer(layer_id: str) -> str:
    """Match brain_service._validate_slug normalization so we hit the same row
    that brain_hook_processor._process_raster_upload created."""
    slug = f"raster-{layer_id}".strip().lower()
    slug = re.sub(r"[^a-z0-9\-_]", "-", slug)
    slug = re.sub(r"-+", "-", slug).strip("-")
    return slug


async def record_raster_analysis(
    layer_id: str,
    summary: str,
    source: str,
    detail: str = "",
    owner_uuid: Optional[str] = None,
) -> bool:
    """Append a Brain timeline entry to raster-{layer_id}.

    summary: 1-line verdict (e.g. "Maize at flowering: moderate stress, NDVI 0.42")
    source : tool name producing the verdict
             (interpret_raster_health / compare_rasters / etc).
    detail : optional longer text (full evidence dict serialized, etc).

    Returns True on success, False otherwise. Never raises.
    """
    try:
        from src.structures import get_async_db_connection
        from src.services.brain_service import BrainService, TimelineInput
        slug = _slug_for_layer(layer_id)
        async with get_async_db_connection(user_id=owner_uuid) as conn:
            brain = BrainService()
            await brain.add_timeline_entry(
                conn, slug,
                TimelineInput(
                    date=_date.today(),
                    summary=summary[:500],
                    source=source,
                    detail=detail[:4000] if detail else "",
                ),
                owner_uuid=owner_uuid,
            )
        return True
    except Exception:
        # Common skip reasons: page hasn't been ingested yet (raster_upload
        # hook still pending), or owner_uuid mismatch with RLS, or transient
        # DB error. Verdict is already returned to user; Brain is additive.
        logger.debug(
            "record_raster_analysis: skipped for layer %s (source=%s)",
            layer_id, source, exc_info=True,
        )
        return False


async def record_clay_embedding_status(
    layer_id: str,
    tile_count: int,
    collection_name: str = "clay_tiles_v1",
    embedding_dim: int = 1024,
    owner_uuid: Optional[str] = None,
) -> bool:
    """Stamp the raster-{layer_id} brain page's frontmatter with Clay
    embedding metadata so cross-system queries can locate the visual index.

    Performs a JSONB merge on brain_pages.frontmatter — only the
    clay_* keys are added, every other key is preserved.
    """
    try:
        from src.structures import get_async_db_connection
        slug = _slug_for_layer(layer_id)
        patch = {
            "clay_tiles_embedded": int(tile_count),
            "clay_collection": collection_name,
            "clay_embedding_dim": int(embedding_dim),
            "clay_embedded_at": _date.today().isoformat(),
        }
        async with get_async_db_connection(user_id=owner_uuid) as conn:
            await conn.execute(
                """
                UPDATE brain_pages
                   SET frontmatter = COALESCE(frontmatter, '{}'::jsonb) || $2::jsonb,
                       updated_at  = now()
                 WHERE slug = $1
                """,
                slug, json.dumps(patch),
            )
            # Also append a one-line timeline entry so the embedding event
            # shows up in the layer's history.
            from src.services.brain_service import BrainService, TimelineInput
            brain = BrainService()
            await brain.add_timeline_entry(
                conn, slug,
                TimelineInput(
                    date=_date.today(),
                    summary=f"Clay v1.5 visual embeddings ready: {tile_count} tiles in {collection_name}",
                    source="clay_embedding",
                    detail="",
                ),
                owner_uuid=owner_uuid,
            )
        return True
    except Exception:
        logger.debug(
            "record_clay_embedding_status: skipped for layer %s",
            layer_id, exc_info=True,
        )
        return False


def record_clay_embedding_status_sync(
    layer_id: str,
    tile_count: int,
    collection_name: str = "clay_tiles_v1",
    embedding_dim: int = 1024,
    owner_uuid: Optional[str] = None,
) -> bool:
    """Sync sibling of record_clay_embedding_status, callable from
    clay_embedding._embed_layer_sync without an event loop."""
    try:
        import os
        import psycopg2
        slug = _slug_for_layer(layer_id)
        patch = {
            "clay_tiles_embedded": int(tile_count),
            "clay_collection": collection_name,
            "clay_embedding_dim": int(embedding_dim),
            "clay_embedded_at": _date.today().isoformat(),
        }
        pg_url = (
            f"host={os.environ.get('POSTGRES_HOST', 'postgresdb')} "
            f"port={os.environ.get('POSTGRES_PORT', '5432')} "
            f"dbname={os.environ.get('POSTGRES_DB', 'mundidb')} "
            f"user={os.environ.get('POSTGRES_USER', 'mundiuser')} "
            f"password={os.environ.get('POSTGRES_PASSWORD', 'changeme')}"
        )
        conn = psycopg2.connect(pg_url)
        try:
            with conn.cursor() as cur:
                # Set the partner GUC the RLS policies expect; user_id too
                # so writes are owned correctly.
                if owner_uuid:
                    cur.execute("SELECT set_config('app.user_id', %s, false)", (owner_uuid,))
                cur.execute(
                    """
                    UPDATE brain_pages
                       SET frontmatter = COALESCE(frontmatter, '{}'::jsonb) || %s::jsonb,
                           updated_at  = now()
                     WHERE slug = %s
                     RETURNING id
                    """,
                    (json.dumps(patch), slug),
                )
                page_row = cur.fetchone()
                if page_row:
                    cur.execute(
                        """
                        INSERT INTO brain_timeline_entries
                            (page_id, date, source, summary, detail, owner_uuid)
                        VALUES (%s, %s, %s, %s, %s, %s)
                        """,
                        (
                            page_row[0],
                            _date.today(),
                            "clay_embedding",
                            f"Clay v1.5 visual embeddings ready: {tile_count} tiles in {collection_name}",
                            "",
                            owner_uuid,
                        ),
                    )
            conn.commit()
        finally:
            conn.close()
        return True
    except Exception:
        logger.debug(
            "record_clay_embedding_status_sync: skipped for layer %s",
            layer_id, exc_info=True,
        )
        return False
