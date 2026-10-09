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

import re
from typing import Any, Optional

from src.database.pool import get_request_industry

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
    """Whether a capability may be used for a project of this industry. An unknown capability is agriculture-only;
    an unknown industry (None: the project or layer could not be resolved) gets only what every industry may use,
    so a failed lookup never unlocks agriculture (audit R1-17)."""
    served = CAPABILITIES.get(capability, AGRICULTURE)
    if industry is None:
        return served == SHARED
    return industry in served


def request_is_agriculture() -> bool:
    """Whether the current request works for an agriculture project (also outside a request: the default)."""
    return (get_request_industry() or DEFAULT_INDUSTRY) == "agriculture"


def tools_for(tools: list[dict], industry: Optional[str]) -> list[dict]:
    """The tool schemas a project of this industry may be offered."""
    return [t for t in tools if serves(t.get("function", {}).get("name", ""), industry)]


def tool_refusal(tool_name: str, industry: Optional[str]) -> Optional[dict]:
    """None if the tool may run for this project; otherwise the error the model gets back instead of running it."""
    if serves(tool_name, industry):
        return None
    if tool_name not in CAPABILITIES:  # still refused (fail closed), but not blamed on the industry
        reason = f"There is no tool named {tool_name} here. Use one of the tools you were given."
    elif industry not in INDUSTRIES:
        reason = f"{tool_name} cannot run here: this project's industry could not be found, so only the map and imagery tools are available."
    else:
        label = INDUSTRIES[industry]["label"]
        reason = (f"{tool_name} is for agriculture projects; this project is {label}. Answer with the map and imagery "
                  f"tools, and say plainly what Ingabe cannot do for {label} yet.")
    return {"status": "error", "error_kind": "wrong_industry", "error": reason}


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


_FARM_INTRO = "specialising in Rwanda agriculture, satellite imagery analysis, and geospatial data processing."
_FARM_BLOCKS = re.compile(r"<(AgricultureCapabilities|DroneAndSatellite|UserUploadedRasters)>.*?</\1>\n?", re.S)
_NEUTRAL_RASTERS = """<UserUploadedRasters>
When the user asks about a raster they uploaded (drone orthophotos, other GeoTIFFs), read the pixels with
describe_user_raster (the file itself: bands, area, date), compute_zonal_stats, get_value_distribution and
read_pixel_at; find visible objects (structures, roads, trees, vegetation) with analyze_raster_object_candidates;
map attention zones with create_raster_h3_context_layer. Say what the pixels show and what they cannot show.
</UserUploadedRasters>
"""


def prompt_for(prompt: str, industry: Optional[str]) -> str:
    """Sage's base prompt for a project's industry. Agriculture (and no request at all) keeps it unchanged; other
    industries lose the farm-only instructions (crop, insurance and NDVI workflows that name agriculture tools)
    and get the industry note, so Sage is never told to call tools it does not have (audit R1-19)."""
    if industry is None or industry == "agriculture":
        return prompt
    label = INDUSTRIES[industry]["label"]
    out = prompt.replace(_FARM_INTRO, f"for {label} work: maps, imagery and geospatial data processing in Rwanda.")
    out = _FARM_BLOCKS.sub("", out, count=0)
    # Any other line that names an agriculture-only tool is a farm instruction: drop it with its indented
    # continuation lines, wherever it sits (examples, citation table, intent rules).
    farm_tool = re.compile(r"\b(?:" + "|".join(sorted(n for n, v in CAPABILITIES.items() if v == AGRICULTURE)) + r")\b")
    kept: list[str] = []
    dropping_indent: Optional[int] = None
    for line in out.split("\n"):
        indent = len(line) - len(line.lstrip())
        if dropping_indent is not None and line.strip() and indent > dropping_indent:
            continue  # continuation of a dropped line
        dropping_indent = None
        if farm_tool.search(line):
            dropping_indent = indent
            continue
        kept.append(line)
    out = "\n".join(kept)
    return f"{out.rstrip()}\n\n{_NEUTRAL_RASTERS}\n{prompt_note(industry)}\n"


def small_talk_prompt(industry: Optional[str]) -> Optional[str]:
    """The small-talk prompt for non-agriculture projects (None: keep the default, which mentions agriculture)."""
    if industry is None or industry == "agriculture":
        return None
    label = INDUSTRIES[industry]["label"]
    return (f"You are Sage, a friendly AI GIS assistant for Ingabe, working on a {label} project. Reply in 1-2 short "
            "sentences. If the user has a real question about maps, imagery or their project, ask them to clarify.")


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


COMPANY_ADMIN_ROLES = frozenset({"owner", "admin"})


async def company_industry(conn: Any, org_id: Optional[str]) -> Optional[str]:
    """The industry a company works in; None when there is no company or it has not chosen yet."""
    if not org_id:
        return None
    value = await conn.fetchval("SELECT industry FROM organizations WHERE id::text = $1", org_id)
    return value if value in INDUSTRIES else None


async def save_company_industry(conn: Any, org_id: str, industry: str) -> bool:
    """Set the company's industry (its owners and admins decide). ValueError for an unknown one."""
    check_industry(industry)
    status = await conn.execute("UPDATE organizations SET industry = $2 WHERE id::text = $1", org_id, industry)
    return status.endswith(" 1")


async def industry_for_new_project(conn: Any, user_id: Optional[str], org_id: Optional[str]) -> str:
    """A new project's industry: its company's; the creator's own choice when the company has none yet (or there is
    no company); agriculture until anyone chooses."""
    return (await company_industry(conn, org_id) or await industry_of(conn, user_id) or DEFAULT_INDUSTRY)


async def industry_of_project(conn: Any, project_id: Optional[str]) -> Optional[str]:
    """The project's industry; None when the project cannot be found (callers fail closed)."""
    if not project_id:
        return None
    value = await conn.fetchval("SELECT industry FROM user_mundiai_projects WHERE id = $1", project_id)
    return value if value in INDUSTRIES else None


async def industry_of_map(conn: Any, map_id: Optional[str]) -> Optional[str]:
    """The industry of the project a map belongs to; None when it cannot be found."""
    if not map_id:
        return None
    value = await conn.fetchval(
        "SELECT p.industry FROM user_mundiai_maps m JOIN user_mundiai_projects p ON p.id = m.project_id "
        "WHERE m.id = $1", map_id)
    return value if value in INDUSTRIES else None


async def industry_of_layer(conn: Any, layer_id: str) -> Optional[str]:
    """The industry a layer belongs to: that of the project it was created in (map_layers.source_map_id). A layer
    with no recorded origin takes the one industry of the live projects showing it; several or none -> None.
    Never "whichever map was edited last" (audit R1-15)."""
    value = await conn.fetchval(
        "SELECT p.industry FROM map_layers l JOIN user_mundiai_maps m ON m.id = l.source_map_id "
        "JOIN user_mundiai_projects p ON p.id = m.project_id WHERE l.layer_id = $1", layer_id)
    if value is None:
        rows = await conn.fetch(
            "SELECT DISTINCT p.industry FROM user_mundiai_maps m JOIN user_mundiai_projects p ON p.id = m.project_id "
            "WHERE $1 = ANY(m.layers) AND m.soft_deleted_at IS NULL AND p.soft_deleted_at IS NULL", layer_id)
        value = rows[0]["industry"] if len(rows) == 1 else None
    return value if value in INDUSTRIES else None
