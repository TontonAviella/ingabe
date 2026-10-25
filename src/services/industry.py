"""Industries: Ingabe serves agriculture, power grids and telecom towers, and keeps them apart.

The settings (Roger, 2026-10-25: a partner company works in one industry, and only Ingabe sees all three):
- organizations.industry: a company's industry. Ingabe staff set it when they add the company (Companies page); a
  partner company that has none yet takes the first choice one of its people makes at sign-in, once
  (claim_company_industry).
- organizations.tier = 'internal': Ingabe's own company. Its people see every industry and switch freely; for them
  the company's industry is only the default until they choose their own.
- users.industry: the industry a user chose (asked at sign-in, switched from the sidebar). It counts only where the
  user may choose: in Ingabe's own company, in a partner company that has no industry yet, or with no company.
- The industry a user works in now (current_industry, the one place this is decided): a partner company's industry
  when it has one (locked: no choice, no switch, no other industries shown); else their own choice, else their
  company's; None until anyone chooses, and in the legacy single-user mode. It is copied onto each project they
  create (agriculture when None), and their home lists only its projects, a project shared with them under its own
  industry; with None the home lists every industry. The home also says how many of their projects are in the other
  industries (other_industries), so a switch never hides maps without a trace. A project still opens by its link
  whatever the industry: this decides what the home lists, not who may open what.
- user_mundiai_projects.industry: the industry a project belongs to (one per project). Everything Sage, the cards
  and the upload automation do for a project follows it.

Every capability declares the industries it serves (CAPABILITIES below; SHARED = general map work every industry
uses). A capability is used for a project only when the project's industry is in its set, and the check is made in
code (tool list, tool execution, automation, cards) and, for Brain notes, by the database (row-level security on
brain_pages keyed on the app.industry setting). An unlabelled tool is treated as agriculture-only, and a test fails
until it is labelled. A capability behind a switch (SWITCHES: the tower work, TOWERS) is used only while it is on
(`available`); with the switch off nothing of it is offered, run or shown.
"""

from __future__ import annotations

import re
from typing import Any, Callable, Optional

from src.database.pool import get_request_industry
from src.services import tower_inspection

INDUSTRIES: dict[str, dict[str, str]] = {
    "agriculture": {"label": "Agriculture", "note": "Farms, plots and crops"},
    "power_grid": {"label": "Power Grid", "note": "Transmission and distribution lines"},
    "telecom": {"label": "Telecom Towers", "note": "Masts, antennas and sites"},
}
DEFAULT_INDUSTRY = "agriculture"  # projects made before industries existed, and users who have not chosen

AGRICULTURE = frozenset({"agriculture"})
TELECOM = frozenset({"telecom"})
POWER_GRID = frozenset({"power_grid"})
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
    "district_ndvi_map": AGRICULTURE,  # district vegetation colours and NDVI in the admin outlines tooltip
    # --- Telecom towers: the work on a photo inspection's photos (switch TOWERS) ---
    # marks, the finder's suggestions and their review (/api/towers/...): tower parts in Telecom, maize plants in
    # Agriculture, pylons, insulators and nests in Power Grid (tower_inspection.PARTS_BY_INDUSTRY)
    "tower_inspection": TELECOM | AGRICULTURE | POWER_GRID,
    "tower_cards": TELECOM | POWER_GRID,  # the inspection question cards (each industry its own: tower_cards.ASSETS)
    "get_tower_inspection_findings": TELECOM,
    "tower_photos_needing_review": TELECOM,
    # --- Power Grid: the same photo inspection work on power lines (switch TOWERS) ---
    "get_power_line_inspection_findings": POWER_GRID,
    "power_line_photos_needing_review": POWER_GRID,
}

# Capabilities that exist only while their switch is on: off, they are not offered, not run and not shown, for any
# industry (each switch is read by its domain's one function).
SWITCHES: dict[str, Callable[[], bool]] = {
    name: tower_inspection.enabled
    for name in ("tower_inspection", "tower_cards", "get_tower_inspection_findings", "tower_photos_needing_review",
                 "get_power_line_inspection_findings", "power_line_photos_needing_review")
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


def switched_on(capability: str) -> bool:
    """Whether a capability's switch is on (a capability without a switch always is)."""
    switch = SWITCHES.get(capability)
    return switch is None or switch()


def available(capability: str, industry: Optional[str]) -> bool:
    """Whether a capability may be used for a project of this industry now: it serves the industry and its switch
    is on. What is offered, run and shown follows this; `serves` alone says which industries a capability is for."""
    return switched_on(capability) and serves(capability, industry)


def request_is_agriculture() -> bool:
    """Whether the current request works for an agriculture project (also outside a request: the default)."""
    return (get_request_industry() or DEFAULT_INDUSTRY) == "agriculture"


def tools_for(tools: list[dict], industry: Optional[str]) -> list[dict]:
    """The tool schemas a project of this industry may be offered (switched-off tools are not)."""
    return [t for t in tools if available(t.get("function", {}).get("name", ""), industry)]


def tool_refusal(tool_name: str, industry: Optional[str]) -> Optional[dict]:
    """None if the tool may run for this project; otherwise the error the model gets back instead of running it."""
    if available(tool_name, industry):
        return None
    # Refused (fail closed) without naming an industry: an unknown tool, and a tool behind a switch (the tower tools),
    # which is offered nowhere but where it is available, so for any other project it simply does not exist.
    if tool_name not in CAPABILITIES or tool_name in SWITCHES:
        reason = f"There is no tool named {tool_name} here. Use one of the tools you were given."
    elif industry not in INDUSTRIES:
        reason = f"{tool_name} cannot run here: this project's industry could not be found, so only the map and imagery tools are available."
    else:
        label = INDUSTRIES[industry]["label"]
        reason = (f"{tool_name} is for {_labels(CAPABILITIES[tool_name])} projects; this project is {label}. Answer "
                  f"with the map and imagery tools, and say plainly what Ingabe cannot do for {label} yet.")
    return {"status": "error", "error_kind": "wrong_industry", "error": reason}


def _labels(industries: frozenset[str]) -> str:
    """The industries' labels in INDUSTRIES order: "Agriculture", "Agriculture and Telecom Towers". A refused tool
    serves at most two of the three industries."""
    return " and ".join(INDUSTRIES[name]["label"] for name in INDUSTRIES if name in industries)


# What Sage is told about each industry other than agriculture: whom it works for, the industry's words, what Ingabe
# answers there (and with which tools), and what is not measured yet. Measured values are read from answer_levels'
# table when the note is made, never copied here.
_BRIEFINGS: dict[str, dict[str, Any]] = {
    "telecom": {
        "readers": "tower owners and asset managers (what is on the tower, is it safe, what changed) and field "
                   "technicians (which photos to check, where to climb)",
        "words": "Panel (sector) antenna: points along an azimuth (compass direction) with a downward tilt; microwave "
                 "(MW) dish: a point-to-point backhaul link; radio unit (RRU/RRH): the radio box behind an antenna; "
                 "tenancy: the equipment each operator has on the tower, checked against its contract; lattice "
                 "tower, monopole, guyed mast; joint bolts hold the lattice together and carry the wind load.",
        "tools": "For a photo inspection (a layer of camera points): get_tower_inspection_findings (equipment counts, "
                 "missing and loose bolts, where the photos were taken from) and tower_photos_needing_review (what "
                 "a technician checks next). A single uploaded image (a raster, not a photo inspection) cannot be "
                 "searched for equipment or damage: say so, and suggest uploading the tower photos as an inspection "
                 "(a ZIP of photos); describe_user_raster reads only the file, never where things are on it.",
        "parts": ("tower_antenna", "tower_dish", "tower_radio_unit", "tower_bolt_missing", "tower_bolt_loose"),
        "not_yet": "antenna azimuth and tilt values (the photos' own camera direction and tilt are known, the "
                   "antennas' are not), rust, cracked welds or bent steel, cable and connector damage, hot spots "
                   "(needs a thermal camera), the aviation obstruction light, theft or tampering at the fence and "
                   "shelter",
    },
    "power_grid": {
        "readers": "line owners and asset managers (what is on the line, what puts it at risk) and line crews (which "
                   "pylons to visit)",
        "words": "Pylon (transmission tower) carries the conductors on insulator strings; a span is the conductor "
                 "between two pylons; sag is how low it hangs; the right-of-way is the corridor kept clear of trees "
                 "and buildings; a bird nest near an insulator can short a live conductor to the steel (a flashover: "
                 "an outage, and a fire risk in dry season).",
        "tools": "For a photo inspection (a layer of camera points): get_power_line_inspection_findings (pylons, "
                 "insulators and bird nests counted, where the photos were taken from) and "
                 "power_line_photos_needing_review (what a crew checks next).",
        "parts": ("power_pylon", "power_insulator", "power_nest"),
        "not_yet": "conductor sag and clearance, vegetation in the right-of-way, broken or flashed-over insulator "
                   "discs, corrosion, hot spots (needs a thermal camera)",
    },
}


def _measured_words(parts: tuple[str, ...]) -> str:
    """What the finder that runs for each part was measured to do, read from answer_levels (never copied)."""
    return "; ".join(f"{noun} {value:.2f} on {n} test photos"
                     for noun, value, n in tower_inspection.finder_measurements(parts))


def prompt_note(industry: Optional[str]) -> Optional[str]:
    """Sage's briefing for a non-agriculture project (agriculture keeps the prompt unchanged): the industry's readers
    and words, the tools that answer its questions, what was measured (AP50, read from answer_levels) and what is not."""
    if (industry or DEFAULT_INDUSTRY) == "agriculture":
        return None
    label = INDUSTRIES[industry]["label"]  # type: ignore[index]
    note = (
        f"This project belongs to the {label} industry ({INDUSTRIES[industry]['note'].lower()}). "  # type: ignore[index]
        "Never use crop, farming, soil or vegetation-health reasoning here, and do not mention farm findings."
    )
    brief = _BRIEFINGS.get(industry or "")
    inspection_tool = {"telecom": "get_tower_inspection_findings",
                       "power_grid": "get_power_line_inspection_findings"}.get(industry or "")
    if brief is None or inspection_tool is None or not available(inspection_tool, industry):
        return note + (f" Ingabe's dedicated {label} analysis is still being built: help with the map, places, "
                       "imagery, the user's own uploads, weather and the project's notes, and say plainly when a "
                       "question needs analysis Ingabe cannot do yet.")
    measured = _measured_words(brief["parts"])
    return note + (
        f"\n<{label.replace(' ', '')}Briefing>\nYou work for {brief['readers']}. Speak their language: "
        f"{brief['words']}\n{brief['tools']} Give only the counts these tools return: a person checked each one. "
        "Never count equipment or defects from a picture yourself, never present suggestions as a count, and never "
        "say there are no defects where nobody marked them.\n"
        + (f"What the finders were measured to do (AP50 from 0 to 1, against people's boxes on public test photos, "
           f"not the user's): {measured}. A person still checks every box.\n" if measured else "")
        + f"Not measured yet, so say plainly Ingabe cannot answer it today: {brief['not_yet']}. Weather, floods, "
        "places, imagery and the user's own uploads work here as for any project.\n"
        f"</{label.replace(' ', '')}Briefing>"
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


async def company_industry(conn: Any, org_id: Optional[str]) -> Optional[str]:
    """The industry a company works in; None when there is no company or it has not chosen yet."""
    if not org_id:
        return None
    value = await conn.fetchval("SELECT industry FROM organizations WHERE id::text = $1", org_id)
    return value if value in INDUSTRIES else None


async def save_company_industry(conn: Any, org_id: str, industry: str) -> bool:
    """Set the company's industry (Ingabe staff, on the Companies page): a partner company's people then work only in
    it; for Ingabe's own company it is only the default. ValueError for an unknown one."""
    check_industry(industry)
    status = await conn.execute("UPDATE organizations SET industry = $2 WHERE id::text = $1", org_id, industry)
    return status.endswith(" 1")


async def locked_industry(conn: Any, org_id: Optional[str]) -> Optional[str]:
    """The one industry a partner company's people work in; None when they may choose: no company, Ingabe's own
    company (tier 'internal'), or a partner company not given an industry yet."""
    if not org_id:
        return None
    row = await conn.fetchrow("SELECT industry, tier FROM organizations WHERE id::text = $1", org_id)
    if row is None or row["tier"] == "internal":
        return None
    return row["industry"] if row["industry"] in INDUSTRIES else None


async def claim_company_industry(conn: Any, org_id: Optional[str], industry: str) -> bool:
    """A partner company with no industry takes this one (its first person's choice at sign-in), once; True when it
    did. Ingabe's own company is never locked this way."""
    if not org_id:
        return False
    check_industry(industry)
    status = await conn.execute(
        "UPDATE organizations SET industry = $2 WHERE id::text = $1 AND industry IS NULL AND tier <> 'internal'",
        org_id, industry)
    return status.endswith(" 1")


async def current_industry_and_source(conn: Any, user_id: Optional[str],
                                      org_id: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """(industry, source): the industry the user works in now and where it comes from. A partner company's industry
    when it has one ("company", locked); else their own choice ("you"), else their company's industry ("company"),
    the default for members who never chose; (None, None) when neither has chosen or there is no account (legacy
    single-user mode)."""
    locked = await locked_industry(conn, org_id)
    if locked:
        return locked, "company"
    own = await industry_of(conn, user_id)
    if own:
        return own, "you"
    company = await company_industry(conn, org_id)
    if company:
        return company, "company"
    return None, None


async def current_industry(conn: Any, user_id: Optional[str], org_id: Optional[str]) -> Optional[str]:
    """The industry the user works in now (current_industry_and_source); None when nobody has chosen."""
    current, _source = await current_industry_and_source(conn, user_id, org_id)
    return current


async def industry_for_new_project(conn: Any, user_id: Optional[str], org_id: Optional[str]) -> str:
    """A new project's industry: the industry its creator works in now; agriculture until anyone chooses."""
    return await current_industry(conn, user_id, org_id) or DEFAULT_INDUSTRY


def other_industries(counts: dict[str, int], shown: Optional[str]) -> list[dict[str, Any]]:
    """The user's projects the home leaves out because they are in another industry than the one it shows:
    [{industry, label, count}] in INDUSTRIES order, from their project count per industry. Empty when the home shows
    every industry (shown is None)."""
    if shown is None:
        return []
    return [{"industry": key, "label": value["label"], "count": counts[key]}
            for key, value in INDUSTRIES.items() if key != shown and counts.get(key, 0) > 0]


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
