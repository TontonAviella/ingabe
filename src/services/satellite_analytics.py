# Copyright (C) 2025 Ingabe Ltd.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

"""Unified satellite analytics facade.

Tries Digital Earth Africa (free, public, no auth) first. Falls back to
Sentinel Hub Statistical API only when DE Africa returns no usable scenes
or errors. This is the single import point for analysis-path callers
that previously imported sentinel_hub_service directly.

Why a facade instead of patching every call site:
    1. One place to control fallback policy
    2. Output shape is identical between the two backends
    3. Lets us flip the priority (or remove SH entirely) without
       touching consumer code
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


def _has_useful_intervals(result: Dict[str, Any]) -> bool:
    """Return True if the result contains at least one interval with valid pixels."""
    if not result or "error" in result:
        return False
    intervals = result.get("intervals") or []
    if not intervals:
        return False
    for iv in intervals:
        for v in iv.values():
            if isinstance(v, dict) and v.get("valid_pixels", 0) > 0:
                return True
    return False


def get_field_stats(
    geometry: Dict[str, Any],
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    index: str = "ndvi",
    collection: Optional[str] = None,
) -> Dict[str, Any]:
    """Compute vegetation index stats. DE Africa primary, Sentinel Hub fallback."""
    # Primary: DE Africa
    try:
        from src.services.deafrica_stac import get_deafrica_service
        de_result = get_deafrica_service().get_field_stats(
            geometry=geometry,
            date_from=date_from,
            date_to=date_to,
            index=index,
            collection=collection,
        )
        if _has_useful_intervals(de_result):
            de_result["backend"] = "deafrica"
            return de_result
        logger.info(
            "DE Africa returned no usable scenes for %s/%s — falling back to Sentinel Hub",
            date_from, date_to,
        )
    except Exception as e:
        logger.warning("DE Africa primary path failed: %s — falling back to Sentinel Hub", e)

    # Fallback: Sentinel Hub
    try:
        from src.services.sentinel_hub_service import get_sentinel_hub_service
        sh = get_sentinel_hub_service()
        if sh is None or not sh.is_configured():
            return {
                "error": "Both DE Africa and Sentinel Hub are unavailable",
                "backend": "none",
            }
        sh_result = sh.get_field_stats(
            geometry=geometry,
            date_from=date_from,
            date_to=date_to,
            index=index,
            collection=collection,
        )
        sh_result["backend"] = "sentinel_hub"
        return sh_result
    except Exception as e:
        logger.exception("Sentinel Hub fallback also failed")
        return {"error": f"All satellite backends failed: {e}", "backend": "none"}


def get_field_timeseries(
    geometry: Dict[str, Any],
    months: Optional[int] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    index: str = "ndvi",
) -> Dict[str, Any]:
    """Index timeseries. DE Africa primary, Sentinel Hub fallback."""
    try:
        from src.services.deafrica_stac import get_deafrica_service
        de_result = get_deafrica_service().get_field_timeseries(
            geometry=geometry,
            date_from=date_from,
            date_to=date_to,
            index=index,
            months=months,
        )
        if _has_useful_intervals(de_result):
            de_result["backend"] = "deafrica"
            return de_result
        logger.info("DE Africa timeseries empty — falling back to Sentinel Hub")
    except Exception as e:
        logger.warning("DE Africa timeseries failed: %s — falling back to Sentinel Hub", e)

    try:
        from src.services.sentinel_hub_service import get_sentinel_hub_service
        sh = get_sentinel_hub_service()
        if sh is None or not sh.is_configured():
            return {
                "error": "Both DE Africa and Sentinel Hub are unavailable",
                "backend": "none",
            }
        # SH service signature uses months=N
        sh_result = sh.get_field_timeseries(geometry=geometry, months=months or 6)
        sh_result["backend"] = "sentinel_hub"
        return sh_result
    except Exception as e:
        logger.exception("Sentinel Hub timeseries fallback failed")
        return {"error": f"All satellite backends failed: {e}", "backend": "none"}
