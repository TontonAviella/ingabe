import asyncio
import logging

from pydantic import BaseModel, Field

from src.tools.pyd import IngabeToolCallMetaArgs

logger = logging.getLogger(__name__)


class GetFoodSecurityAlertsArgs(BaseModel):
    district: str = Field(
        ...,
        description="Rwanda district name to filter by. Pass empty string '' for all districts.",
    )
    period: str = Field(
        ...,
        description="Reporting period: 'current' for the present situation, 'projected' for forecast. Empty string '' defaults to 'current'.",
    )


async def get_food_security_alerts(
    args: GetFoodSecurityAlertsArgs, meta: IngabeToolCallMetaArgs
) -> dict:
    """Get FEWS NET food security alerts for Rwanda. Returns IPC phase classifications by district. When district-level data is present, the response includes a 'displayable_geojson' payload — pass it to display_geojson_layer with style_hint='food_security_ipc' to paint the districts with the standard IPC color scale (green=Minimal → dark red=Famine)."""
    from src.services.fewsnet_service import get_food_security

    district = args.district.strip() if args.district else None
    period = (args.period.strip() or "current") if args.period else "current"

    result = await asyncio.get_running_loop().run_in_executor(
        None,
        lambda: get_food_security(
            district=district,
            period=period,
        ),
    )

    # Enrich with displayable_geojson when we have district-level admin entries.
    try:
        if isinstance(result, dict) and result.get("status") == "success":
            from src.services.admin_boundaries import lookup_admin_geometry
            from shapely.geometry import shape as _shape
            classifications = result.get("classifications") or []
            features = []
            min_lon = min_lat = float("inf")
            max_lon = max_lat = float("-inf")
            seen_districts: set[str] = set()
            for c in classifications:
                # Only admin/district entries are spatially renderable
                if c.get("unit_type") not in ("fsc_admin", "fsc_rm_admin"):
                    continue
                area = (c.get("area") or "").strip()
                if not area or area.lower() == "rwanda" or area in seen_districts:
                    continue
                seen_districts.add(area)
                geom = await lookup_admin_geometry(district=area)
                if not geom:
                    continue
                phase = c.get("ipc_phase")
                if phase is None:
                    continue
                features.append({
                    "type": "Feature",
                    "geometry": geom,
                    "properties": {
                        "ipc_phase": int(phase),
                        "ipc_label": c.get("ipc_label"),
                        "area": area,
                        "scenario": c.get("scenario"),
                    },
                })
                try:
                    b = _shape(geom).bounds
                    min_lon, min_lat = min(min_lon, b[0]), min(min_lat, b[1])
                    max_lon, max_lat = max(max_lon, b[2]), max(max_lat, b[3])
                except Exception:
                    pass
            if features and min_lon < float("inf"):
                result["displayable_geojson"] = {
                    "geojson": {"type": "FeatureCollection", "features": features},
                    "style_hint": "food_security_ipc",
                    "title": (
                        f"Food Security ({result.get('reporting_date', '')}) — "
                        f"{len(features)} district{'s' if len(features) != 1 else ''}"
                    ),
                    "bbox": f"{min_lon},{min_lat},{max_lon},{max_lat}",
                }
    except Exception:
        logger.debug("displayable_geojson build skipped for food_security", exc_info=True)

    return result
