"""Industries: Ingabe serves agriculture, power grids and telecom towers, and keeps them apart.

Two settings:
- users.industry: the industry a user works in, asked once at sign-in; it is copied onto each project they create.
- user_mundiai_projects.industry: the industry a project belongs to (one per project). Everything Sage, the cards
  and the upload automation do for a project follows it.

Every capability declares the industries it serves (CAPABILITIES below; SHARED = general map work every industry
uses). A capability is used for a project only when the project's industry is in its set, and the check is made in
code (tool list, tool execution, automation, cards) and, for Brain notes, by the database (row-level security on
brain_pages keyed on the app.industry setting). An unlabelled tool is treated as agriculture-only, and a test fails
until it is labelled.
"""

from __future__ import annotations

from typing import Any, Optional

INDUSTRIES: dict[str, dict[str, str]] = {
    "agriculture": {"label": "Agriculture", "note": "Farms, plots and crops"},
    "power_grid": {"label": "Power Grid", "note": "Transmission and distribution lines"},
    "telecom": {"label": "Telecom Towers", "note": "Masts, antennas and sites"},
}
DEFAULT_INDUSTRY = "agriculture"  # projects made before industries existed, and users who have not chosen

AGRICULTURE = frozenset({"agriculture"})
SHARED = frozenset(INDUSTRIES)

# Sage's tools and the automatic jobs, each with the industries it serves.
CAPABILITIES: dict[str, frozenset[str]] = {
    # --- Map work every industry uses ---
    "add_land_cover_layer": SHARED,
    "add_layer_to_map": SHARED,
    "create_point_layer": SHARED,
    "display_geojson_layer": SHARED,
    "display_layer": SHARED,
    "new_layer_from_postgis": SHARED,
    "set_layer_style": SHARED,
    "zoom_to_bounds": SHARED,
    "show_admin_boundary": SHARED,
    "list_admin_units": SHARED,
    "search_location": SHARED,
    "reverse_geocode_coordinates": SHARED,
    # Imagery and the user's own rasters, read as pixels (no crop meaning attached).
    "display_satellite_layer": SHARED,
    "search_satellite_imagery": SHARED,
    "describe_user_raster": SHARED,
    "compute_zonal_stats": SHARED,
    "get_value_distribution": SHARED,
    "read_pixel_at": SHARED,
    "analyze_raster_object_candidates": SHARED,
    "create_raster_h3_context_layer": SHARED,
    "query_worldcover_stats": SHARED,
    # Weather and hazards matter to lines and masts as much as to farms.
    "get_forecast": SHARED,
    "get_forecast_accuracy": SHARED,
    "get_weather_stats": SHARED,
    "detect_flood_extent": SHARED,
    # Brain: the tools are shared; each note belongs to one industry and the database shows only the project's.
    "search_brain": SHARED,
    "add_observation": SHARED,
    "get_entity": SHARED,
    "brain_graph_query": SHARED,
    "brain_trajectory": SHARED,
    # --- Agriculture: crops, vegetation, soil, water for crops, crop insurance ---
    "analyze_rgb_field": AGRICULTURE,
    "compare_rasters": AGRICULTURE,
    "compute_spectral_index": AGRICULTURE,
    "count_plants_in_plot": AGRICULTURE,
    "detect_dry_spells": AGRICULTURE,
    "evaluate_insurance_trigger": AGRICULTURE,
    "find_stress_zones": AGRICULTURE,
    "get_agri_indices": AGRICULTURE,
    "get_anomaly_alerts": AGRICULTURE,
    "get_cell_ndvi_stats": AGRICULTURE,
    "get_crop_growth_stage": AGRICULTURE,
    "get_drone_photo_findings": AGRICULTURE,
    "get_drought_status": AGRICULTURE,
    "get_evapotranspiration": AGRICULTURE,
    "get_field_health": AGRICULTURE,
    "get_insurance_accuracy": AGRICULTURE,
    "get_insurance_intelligence": AGRICULTURE,
    "get_ndvi_stats": AGRICULTURE,
    "get_parcel_ndvi_stats": AGRICULTURE,
    "get_soil_moisture": AGRICULTURE,
    "get_soil_properties": AGRICULTURE,
    "get_yield_risk": AGRICULTURE,
    "interpret_raster_health": AGRICULTURE,
    "predict_ndvi_from_sar": AGRICULTURE,
    # --- Automatic jobs on a drone photo ---
    "drone_first_look": AGRICULTURE,  # Sage's first message about green cover and plots
    "drone_cards": AGRICULTURE,  # the question cards: plots, crops, bare ground, the crop survey
    # (Power Grid and Telecom capabilities are added here as they are built.)
}


def check_industry(industry: str) -> str:
    """The industry if known; ValueError otherwise."""
    if industry not in INDUSTRIES:
        raise ValueError(f"industry must be one of {', '.join(INDUSTRIES)}")
    return industry


def serves(capability: str, industry: Optional[str]) -> bool:
    """Whether a capability may be used for a project of this industry (unknown capability: agriculture only)."""
    return (industry or DEFAULT_INDUSTRY) in CAPABILITIES.get(capability, AGRICULTURE)


def tools_for(tools: list[dict], industry: Optional[str]) -> list[dict]:
    """The tool schemas a project of this industry may be offered."""
    return [t for t in tools if serves(t.get("function", {}).get("name", ""), industry)]


def tool_refusal(tool_name: str, industry: Optional[str]) -> Optional[dict]:
    """None if the tool may run for this project; otherwise the error the model gets back instead of running it."""
    if serves(tool_name, industry):
        return None
    label = INDUSTRIES[industry or DEFAULT_INDUSTRY]["label"]
    return {
        "status": "error",
        "error_kind": "wrong_industry",
        "error": f"{tool_name} is for agriculture projects; this project is {label}. "
                 f"Answer with the map and imagery tools, and say plainly what Ingabe cannot do for {label} yet.",
    }


def prompt_note(industry: Optional[str]) -> Optional[str]:
    """A line for Sage's system prompt on non-agriculture projects (agriculture keeps the prompt unchanged)."""
    if (industry or DEFAULT_INDUSTRY) == "agriculture":
        return None
    label = INDUSTRIES[industry]["label"]  # type: ignore[index]
    return (
        f"This project belongs to the {label} industry ({INDUSTRIES[industry]['note'].lower()}). "  # type: ignore[index]
        "Never use crop, farming, soil or vegetation-health reasoning here, and do not mention farm findings. "
        f"Ingabe's dedicated {label} analysis is still being built: help with the map, places, imagery, the user's "
        "own uploads, weather and the project's notes, and say plainly when a question needs analysis Ingabe "
        "cannot do yet."
    )


async def industry_of(conn: Any, user_id: Optional[str]) -> Optional[str]:
    """The user's industry, or None when they have not chosen one (or have no account row)."""
    if not user_id:
        return None
    value = await conn.fetchval("SELECT industry FROM users WHERE internal_uuid = $1", user_id)
    return value if value in INDUSTRIES else None


async def save_industry(conn: Any, user_id: str, industry: str) -> bool:
    """Save the user's industry. ValueError for an unknown one; False if the user has no account row."""
    check_industry(industry)
    status = await conn.execute("UPDATE users SET industry = $2 WHERE internal_uuid = $1", user_id, industry)
    return status.endswith(" 1")


async def industry_of_project(conn: Any, project_id: Optional[str]) -> str:
    """The project's industry (agriculture for an unknown project)."""
    if not project_id:
        return DEFAULT_INDUSTRY
    value = await conn.fetchval("SELECT industry FROM user_mundiai_projects WHERE id = $1", project_id)
    return value if value in INDUSTRIES else DEFAULT_INDUSTRY


async def industry_of_map(conn: Any, map_id: Optional[str]) -> str:
    """The industry of the project a map belongs to."""
    if not map_id:
        return DEFAULT_INDUSTRY
    value = await conn.fetchval(
        "SELECT p.industry FROM user_mundiai_maps m JOIN user_mundiai_projects p ON p.id = m.project_id "
        "WHERE m.id = $1", map_id)
    return value if value in INDUSTRIES else DEFAULT_INDUSTRY


async def industry_of_layer(conn: Any, layer_id: str) -> str:
    """The industry of the project whose maps show this layer (agriculture if none is found)."""
    value = await conn.fetchval(
        "SELECT p.industry FROM user_mundiai_maps m JOIN user_mundiai_projects p ON p.id = m.project_id "
        "WHERE $1 = ANY(m.layers) AND m.soft_deleted_at IS NULL ORDER BY m.last_edited DESC NULLS LAST LIMIT 1",
        layer_id)
    return value if value in INDUSTRIES else DEFAULT_INDUSTRY
