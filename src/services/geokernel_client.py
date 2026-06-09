from __future__ import annotations

import logging
import os
from typing import Any

import httpx

from src.services.admin_h3 import AdminH3Options

logger = logging.getLogger(__name__)


async def admin_geojson_to_h3_via_geokernel(
    geojson: dict[str, Any],
    *,
    options: AdminH3Options,
) -> dict[str, Any] | None:
    """Try the Rust geokernel admin/H3 overlap path.

    Returning None is deliberate: the caller should fall back to the existing
    Python implementation whenever the sidecar is disabled, unavailable, or not
    confident about the request.
    """
    base_url = _geokernel_url()
    if not base_url:
        return None

    timeout = _float_env("GEOKERNEL_TIMEOUT", 5.0)
    containment_mode = os.environ.get("GEOKERNEL_H3_CONTAINMENT_MODE", "centroid")
    payload = {
        "geojson": geojson,
        "resolution": options.resolution,
        "admin_level": options.admin_level,
        "id_property": options.id_property,
        "name_property": options.name_property,
        "max_hexes": options.max_hexes,
        "min_overlap_ratio": options.min_overlap_ratio,
        "include_geometry": options.include_geometry,
        "containment_mode": containment_mode,
    }

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(f"{base_url}/admin/h3-overlap", json=payload)
    except httpx.HTTPError as exc:
        logger.warning("Rust geokernel unavailable for admin H3 overlap: %s", exc)
        return None

    if response.status_code >= 400:
        logger.warning(
            "Rust geokernel rejected admin H3 overlap status=%s body=%s",
            response.status_code,
            response.text[:500],
        )
        return None

    try:
        result = response.json()
    except ValueError as exc:
        logger.warning("Rust geokernel returned invalid JSON: %s", exc)
        return None

    if not _looks_like_feature_collection(result):
        logger.warning("Rust geokernel returned unexpected admin H3 response shape")
        return None
    return result


def _geokernel_url() -> str | None:
    raw = os.environ.get("GEOKERNEL_URL", "").strip()
    if not raw:
        return None
    return raw.rstrip("/")


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _looks_like_feature_collection(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and value.get("type") == "FeatureCollection"
        and isinstance(value.get("features"), list)
    )
