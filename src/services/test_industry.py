"""Industries stay apart: every capability is labelled, a project gets only its industry's tools, automation and
cards, and Brain notes of one industry are invisible from another (row-level security on app.industry)."""

import json
import uuid
from pathlib import Path

import pytest

from src.database.pool import get_async_db_connection, set_request_industry
from src.services import industry

RUN_TAG = uuid.uuid4().hex[:8]
CATALOG = json.loads((Path(__file__).resolve().parents[2] / "evals" / "sage_routing" / "tool_catalog.json").read_text())
AGRI_ONLY = {name for name, served in industry.CAPABILITIES.items() if served == industry.AGRICULTURE}


# --- The capability list --------------------------------------------------------------------------------------------

def test_every_sage_tool_is_labelled_with_its_industries():
    """A new tool must say which industries it serves before it ships (unlabelled = agriculture only)."""
    tools = set(CATALOG["model_tools"]) | set(CATALOG["fast_path_tools"])
    assert sorted(tools - set(industry.CAPABILITIES)) == []


def test_every_live_tool_is_labelled():
    from src.dependencies.pydantic_tools import get_pydantic_tool_calls
    from src.services.legacy_tool_shim import LEGACY_HANDLERS

    live = set(get_pydantic_tool_calls()) | set(LEGACY_HANDLERS)
    assert sorted(live - set(industry.CAPABILITIES)) == []


@pytest.mark.parametrize("other", ["power_grid", "telecom"])
def test_other_industries_get_no_agriculture_tool(other):
    tools = [{"function": {"name": n}} for n in CATALOG["model_tools"]]
    offered = {t["function"]["name"] for t in industry.tools_for(tools, other)}
    assert offered and not offered & AGRI_ONLY
    assert {"search_location", "display_layer", "describe_user_raster", "search_brain"} <= offered
    assert {t["function"]["name"] for t in industry.tools_for(tools, "agriculture")} == set(CATALOG["model_tools"])


def test_a_wrong_industry_tool_is_refused_when_called_anyway():
    refusal = industry.tool_refusal("get_ndvi_stats", "power_grid")
    assert refusal and refusal["error_kind"] == "wrong_industry" and "Power Grid" in refusal["error"]
    assert industry.tool_refusal("get_ndvi_stats", "agriculture") is None
    assert industry.tool_refusal("search_location", "telecom") is None
    assert industry.tool_refusal("a_tool_nobody_labelled", "telecom") is not None  # unknown: agriculture only


def test_only_other_industries_get_a_prompt_note():
    assert industry.prompt_note("agriculture") is None and industry.prompt_note(None) is None
    note = industry.prompt_note("telecom")
    assert note and "Telecom Towers" in note and "crop" in note


def test_farm_procedures_stay_out_of_other_industries_prompts(monkeypatch):
    from src.services import life_harness

    monkeypatch.setattr(life_harness, "life_harness_enabled", lambda: True)
    question = "check maize stress in Nyagatare after the dry spell"
    try:
        set_request_industry("power_grid")
        grid = life_harness.apply_life_harness_system_prompt("P", question)
        set_request_industry("agriculture")
        farm = life_harness.apply_life_harness_system_prompt("P", question)
    finally:
        set_request_industry(None)
    assert "agriculture work" not in grid and "retrieved procedural skills" not in grid
    assert "agriculture work" in farm


# --- Projects, cards, first look -----------------------------------------------------------------------------------

async def _project(conn, owner: str, project_industry: str, layer_id: str) -> str:
    unique = uuid.uuid4().hex[:11]
    project_id, map_id = f"P{unique}", f"M{unique}"
    await conn.execute("INSERT INTO user_mundiai_projects (id, owner_uuid, maps, industry) VALUES ($1, $2, ARRAY[$3], $4)",
                       project_id, owner, map_id, project_industry)
    await conn.execute("INSERT INTO user_mundiai_maps (id, project_id, owner_uuid, title, layers) VALUES ($1, $2, $3, 't', ARRAY[$4])",
                       map_id, project_id, owner, layer_id)
    return map_id


@pytest.mark.anyio
async def test_new_projects_take_their_creators_industry(auth_client):
    async with get_async_db_connection() as conn:
        await conn.execute("INSERT INTO users (internal_uuid) VALUES ('00000000-0000-0000-0000-000000000000') "
                           "ON CONFLICT DO NOTHING")
        await conn.execute("UPDATE users SET industry = 'telecom' WHERE internal_uuid = '00000000-0000-0000-0000-000000000000'")
    try:
        made = (await auth_client.post("/api/maps/create", json={"title": f"Mast site {RUN_TAG}"})).json()
        async with get_async_db_connection() as conn:
            assert await industry.industry_of_project(conn, made["project_id"]) == "telecom"
            assert await industry.industry_of_map(conn, made["id"]) == "telecom"
    finally:
        async with get_async_db_connection() as conn:
            await conn.execute("UPDATE users SET industry = NULL WHERE internal_uuid = '00000000-0000-0000-0000-000000000000'")


@pytest.mark.anyio
async def test_cards_and_first_look_are_for_agriculture_projects_only():
    from fastapi import HTTPException

    from src.routes.drone_card_routes import _agriculture_project
    from src.services.drone_first_look import post_first_look

    owner = str(uuid.uuid4())
    grid_layer, farm_layer = f"L{RUN_TAG}grd", f"L{RUN_TAG}frm"
    async with get_async_db_connection() as conn:
        grid_map = await _project(conn, owner, "power_grid", grid_layer)
        await _project(conn, owner, "agriculture", farm_layer)
    with pytest.raises(HTTPException) as refused:
        await _agriculture_project(grid_layer)
    assert refused.value.status_code == 404 and "Power Grid" in refused.value.detail
    await _agriculture_project(farm_layer)  # allowed: no exception
    # The first look stops before it even looks for the photo.
    assert await post_first_look(grid_layer, grid_map, owner, None, conversation_id=0, wait_s=0) is None


# --- Brain notes ----------------------------------------------------------------------------------------------------

@pytest.mark.anyio
async def test_brain_notes_of_one_industry_are_invisible_from_another():
    farm_slug, general_slug, grid_slug = f"farm-{RUN_TAG}", f"general-{RUN_TAG}", f"grid-{RUN_TAG}"
    owner = str(uuid.uuid4())
    async with get_async_db_connection() as conn:
        await conn.execute("INSERT INTO brain_pages (slug, type, title, compiled_truth, industry, owner_uuid) VALUES "
                           "($1, 'note', 'Maize at plot 4', 'planted 3 March', 'agriculture', $3), "
                           "($2, 'note', 'Gatsibo district', 'in Eastern Province', NULL, $3)", farm_slug, general_slug, owner)
        farm_id = await conn.fetchval("SELECT id FROM brain_pages WHERE slug = $1", farm_slug)
        await conn.execute("INSERT INTO brain_timeline_entries (page_id, date, summary) VALUES ($1, CURRENT_DATE, 'visit')",
                           farm_id)
    try:
        set_request_industry("power_grid")
        async with get_async_db_connection() as conn:
            seen = {r["slug"] for r in await conn.fetch(
                "SELECT slug FROM brain_pages WHERE slug = ANY($1::text[])", [farm_slug, general_slug])}
            assert seen == {general_slug}  # the farm note is invisible; general knowledge is shared
            assert await conn.fetchval("SELECT count(*) FROM brain_timeline_entries WHERE page_id = $1", farm_id) == 0
            # A note written during a grid turn belongs to the grid.
            await conn.execute("INSERT INTO brain_pages (slug, type, title, compiled_truth, owner_uuid) "
                               "VALUES ($1, 'note', 'Span 12', 'sag', $2)", grid_slug, owner)
            assert await conn.fetchval("SELECT industry FROM brain_pages WHERE slug = $1", grid_slug) == "power_grid"
            # It cannot write into another industry either.
            with pytest.raises(Exception):
                await conn.execute("INSERT INTO brain_pages (slug, type, title, compiled_truth, industry, owner_uuid) "
                                   "VALUES ($1, 'note', 'x', 'y', 'agriculture', $2)", f"sneak-{RUN_TAG}", owner)
        set_request_industry("agriculture")
        async with get_async_db_connection() as conn:
            seen = {r["slug"] for r in await conn.fetch(
                "SELECT slug FROM brain_pages WHERE slug = ANY($1::text[])", [farm_slug, general_slug, grid_slug])}
            assert seen == {farm_slug, general_slug}  # and the grid note is invisible from farms
    finally:
        set_request_industry(None)
        async with get_async_db_connection() as conn:  # no industry set: maintenance sees everything
            await conn.execute("DELETE FROM brain_timeline_entries WHERE page_id = $1", farm_id)
            await conn.execute("DELETE FROM brain_pages WHERE slug = ANY($1::text[])", [farm_slug, general_slug, grid_slug])


@pytest.mark.anyio
async def test_brain_fails_closed_for_a_user_request_without_an_industry():
    """A signed-in request that never set an industry sees and writes only general notes (audit R1-4)."""
    owner = str(uuid.uuid4())
    farm, general = f"farm2-{RUN_TAG}", f"general2-{RUN_TAG}"
    async with get_async_db_connection() as conn:  # worker: unrestricted
        await conn.execute("INSERT INTO brain_pages (slug, type, title, compiled_truth, industry, owner_uuid, access_scope) VALUES "
                           "($1, 'note', 'farm', 'x', 'agriculture', $3, 'public'), ($2, 'note', 'general', 'y', NULL, $3, 'public')",
                           farm, general, owner)
    try:
        async with get_async_db_connection(user_id=owner) as conn:  # signed in, no industry set
            seen = {r["slug"] for r in await conn.fetch("SELECT slug FROM brain_pages WHERE slug = ANY($1::text[])", [farm, general])}
            assert seen == {general}
            with pytest.raises(Exception):  # and it cannot write an industry's note
                await conn.execute("INSERT INTO brain_pages (slug, type, title, compiled_truth, industry, owner_uuid) "
                                   "VALUES ($1, 'note', 'x', 'y', 'agriculture', $2)", f"sneak2-{RUN_TAG}", owner)
    finally:
        async with get_async_db_connection() as conn:
            await conn.execute("DELETE FROM brain_pages WHERE slug = ANY($1::text[])", [farm, general])


@pytest.mark.anyio
async def test_an_industry_cannot_write_into_a_general_note():
    """Observations from a Power Grid turn cannot land on a general note that every industry reads (audit R1-3)."""
    owner = str(uuid.uuid4())
    general = f"general3-{RUN_TAG}"
    async with get_async_db_connection() as conn:
        await conn.execute("INSERT INTO brain_pages (slug, type, title, compiled_truth, owner_uuid) VALUES ($1, 'note', 'g', 'z', $2)",
                           general, owner)
        page_id = await conn.fetchval("SELECT id FROM brain_pages WHERE slug = $1", general)
    try:
        set_request_industry("power_grid")
        async with get_async_db_connection() as conn:
            assert await conn.fetchval("SELECT count(*) FROM brain_pages WHERE id = $1", page_id) == 1  # readable
            with pytest.raises(Exception):
                await conn.execute("INSERT INTO brain_timeline_entries (page_id, date, summary) VALUES ($1, CURRENT_DATE, 'span 12 sag')",
                                   page_id)
            with pytest.raises(Exception):
                await conn.execute("UPDATE brain_pages SET compiled_truth = 'grid text' WHERE id = $1", page_id)
    finally:
        set_request_industry(None)
        async with get_async_db_connection() as conn:
            await conn.execute("DELETE FROM brain_pages WHERE id = $1", page_id)


@pytest.mark.anyio
async def test_notes_made_by_the_upload_hook_take_the_projects_industry():
    """The background hook that turns an upload into a note labels it with the upload's project (audit R1-2)."""
    from src.services.brain_hook_processor import process_pending_hooks
    from src.services.brain_service import BrainService

    owner = str(uuid.uuid4())
    layer_id = f"L{RUN_TAG}hk"[:12]
    async with get_async_db_connection() as conn:
        await _project(conn, owner, "telecom", layer_id)
        brain = BrainService()
        await brain.enqueue_hook(conn, "raster_upload", {"layer_id": layer_id, "layer_name": "Mast site", "user_id": owner,
                                                         "bounds": [30.0, -2.0, 30.01, -1.99]})
        await process_pending_hooks(conn, brain, limit=50)
        assert await conn.fetchval("SELECT industry FROM brain_pages WHERE slug = $1", f"raster-{layer_id}".lower()) == "telecom"
        assert await conn.fetchval("SELECT current_setting('app.industry', true)") in (None, "")  # reset after the hook
        await conn.execute("DELETE FROM brain_pages WHERE slug = $1", f"raster-{layer_id}".lower())


@pytest.mark.anyio
async def test_a_message_request_is_scoped_before_the_brain_packet_is_built():
    """send_map_message scopes the request to the map's industry before reading Brain (audit R1-1)."""
    from src.database.pool import get_request_industry
    from src.routes.message_routes import _scope_request_to_map_industry

    owner = str(uuid.uuid4())
    async with get_async_db_connection() as conn:
        grid_map = await _project(conn, owner, "power_grid", f"L{RUN_TAG}sm"[:12])
    try:
        assert await _scope_request_to_map_industry(grid_map) == "power_grid"
        assert get_request_industry() == "power_grid"
    finally:
        set_request_industry(None)


# --- Shared tools never run farm modes or give farm advice outside agriculture (audit R1-10..R1-12) ---------------

def test_shared_raster_tools_drop_farm_modes_outside_agriculture():
    from src.tools.raster_h3_context import CreateRasterH3ContextLayerArgs
    from src.tools.raster_h3_context import args_for_industry as h3_args
    from src.tools.raster_object_candidates import AnalyzeRasterObjectCandidatesArgs
    from src.tools.raster_object_candidates import args_for_industry as object_args

    h3 = CreateRasterH3ContextLayerArgs.model_construct(layer_id="Labc", domain="farm", analysis_goal="screen vegetation stress")
    assert h3_args(h3, agriculture=False).domain == "environment"
    assert h3_args(h3, agriculture=True).domain == "farm"
    objects = AnalyzeRasterObjectCandidatesArgs.model_construct(layer_id="Labc", target_classes=["crops", "trees"])
    grid = object_args(objects, agriculture=False).target_classes
    assert "crop_patch" not in grid and "vegetation_patch" in grid and "tree_canopy" in grid
    assert object_args(objects, agriculture=True).target_classes == ["crops", "trees"]


def test_forecasts_lose_crop_advice_outside_agriculture():
    from src.services.forecast_openmeteo import without_farm_advice

    farm = {"briefing": {"headline": "Dry spell ahead — 6 consecutive days with little to no rain. Crops without "
                                     "irrigation could face water stress. Also, 2 day(s) with temperatures high enough to stress crops.",
                         "risks": [{"heat_stress_detail": "Max temperature 33°C — crop heat stress likely."}]}}
    neutral = without_farm_advice(farm)
    text = json.dumps(neutral)
    assert "crop" not in text.lower() and "irrigation" not in text.lower()
    assert "Dry spell ahead" in text and "high temperatures" in text and "very hot" in text
    assert "Crops without irrigation" in farm["briefing"]["headline"]  # the cached original is untouched
