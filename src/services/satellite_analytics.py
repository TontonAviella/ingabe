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

Uses Digital Earth Africa exclusively. DE Africa serves analysis-ready
Sentinel-2 L2A COGs from a public S3 bucket in af-south-1. No credentials,
no rate limits, no processing units to budget.

This is the single import point for analysis-path callers.
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


def _enrich_field_stats(result: Dict[str, Any], geometry: Dict[str, Any]) -> Dict[str, Any]:
    """Enrich field stats with cropland fraction from DE Africa."""
    try:
        from src.services.deafrica_stac import _bbox_from_geojson, _cached_cropland, _round_bbox
        bbox = _round_bbox(_bbox_from_geojson(geometry))
        crop = _cached_cropland(bbox)
        if crop is not None:
            result["cropland_fraction"] = crop[0]
            result["validation_data_year"] = crop[1]
    except Exception as e:
        logger.warning("Cropland enrichment failed for field stats: %s", e)
    return result


def get_field_stats(
    geometry: Dict[str, Any],
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    index: str = "ndvi",
    collection: Optional[str] = None,
) -> Dict[str, Any]:
    """Compute vegetation index stats via Digital Earth Africa."""
    try:
        from src.services.deafrica_stac import get_deafrica_service
        result = get_deafrica_service().get_field_stats(
            geometry=geometry,
            date_from=date_from,
            date_to=date_to,
            index=index,
            collection=collection,
        )
        result["backend"] = "deafrica"
        if _has_useful_intervals(result):
            return _enrich_field_stats(result, geometry)
        return result
    except Exception as e:
        logger.exception("DE Africa field stats failed")
        return {"error": f"Satellite analytics failed: {e}", "backend": "none"}


def get_agri_stats(
    geometry: Dict[str, Any],
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    collection: Optional[str] = None,
) -> Dict[str, Any]:
    """All agri indices (ndvi, evi, ndwi, savi, ndre, ndbi) via Digital Earth Africa."""
    try:
        from src.services.deafrica_stac import get_deafrica_service
        result = get_deafrica_service().get_agri_stats(
            geometry=geometry,
            date_from=date_from,
            date_to=date_to,
            collection=collection,
        )
        result["backend"] = "deafrica"
        return result
    except Exception as e:
        logger.exception("DE Africa agri stats failed")
        return {"error": f"Satellite analytics failed: {e}", "backend": "none"}


def get_field_timeseries(
    geometry: Dict[str, Any],
    months: Optional[int] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    index: str = "ndvi",
) -> Dict[str, Any]:
    """Index timeseries via Digital Earth Africa."""
    try:
        from src.services.deafrica_stac import get_deafrica_service
        result = get_deafrica_service().get_field_timeseries(
            geometry=geometry,
            date_from=date_from,
            date_to=date_to,
            index=index,
            months=months,
        )
        result["backend"] = "deafrica"
        return result
    except Exception as e:
        logger.exception("DE Africa timeseries failed")
        return {"error": f"Satellite analytics failed: {e}", "backend": "none"}
