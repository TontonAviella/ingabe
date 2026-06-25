"""Best-effort helpers that wire drone raster tools into Brain memory.

Without these, every Sage verdict (interpret_raster_health, compare_rasters,
evaluate_insurance_trigger, find_stress_zones, analyze_rgb_field) is ephemeral
— shown to the user once, then lost. With them, each verdict appends a
timeline entry to the raster-{layer_id} brain page (created earlier by
brain_hook_processor._process_raster_upload), so Brain accumulates a real
analysis history.

ALL functions are best-effort. Brain failures (page missing, DB hiccup, RLS
block) never propagate. The verdict the tool returns to Sage is the source
of truth for the user; Brain logging is additive.
"""

import logging
import re
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
