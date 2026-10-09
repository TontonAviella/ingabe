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
    invented = industry.tool_refusal("a_tool_nobody_labelled", "telecom")  # unknown: refused, but not "for agriculture"
    assert invented and "no tool named" in invented["error"] and "agriculture" not in invented["error"]


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
    from types import SimpleNamespace

    # Layers created on those maps (their source map decides their industry).
    async with get_async_db_connection() as conn:
        for layer_id, source_map in ((grid_layer, grid_map), (farm_layer, None)):
            await conn.execute("INSERT INTO map_layers (layer_id, owner_uuid, name, type, source_map_id) "
                               "VALUES ($1, $2, 'photo', 'raster', $3)", layer_id, owner, source_map)
    with pytest.raises(HTTPException) as refused:
        await _agriculture_project(SimpleNamespace(layer_id=grid_layer))
    assert refused.value.status_code == 404 and "Power Grid" in refused.value.detail
    await _agriculture_project(SimpleNamespace(layer_id=farm_layer))  # allowed: no exception
    with pytest.raises(HTTPException):  # a layer on no map at all: unknown industry, no crop cards (R1-17)
        await _agriculture_project(SimpleNamespace(layer_id=f"L{RUN_TAG}orph"[:12]))
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
        hook_id = await BrainService().enqueue_hook(conn, "raster_upload", {
            "layer_id": layer_id, "layer_name": "Mast site", "user_id": owner, "bounds": [30.0, -2.0, 30.01, -1.99]})

        class OnlyThisHook(BrainService):  # other tests' hooks in the shared test database are not ours to run
            async def get_pending_hooks(self, conn, limit=10):
                return [h for h in await super().get_pending_hooks(conn, limit=1000) if h["id"] == hook_id]

        await process_pending_hooks(conn, OnlyThisHook(), limit=10)
        page = await conn.fetchrow("SELECT industry, type FROM brain_pages WHERE slug = $1", f"raster-{layer_id}".lower())
        assert page["industry"] == "telecom"
        assert page["type"] == "layer"  # not 'field': no farm links or field-in-district edges for a mast photo
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


def test_other_industries_get_no_farm_instructions_and_no_forced_tool_after_an_honest_no():
    """Prompt per industry (R1-19, Hermes R1-18 shares the provider) and the abdication guard (R1-20)."""
    from src.dependencies.sage_routing import RoutingDecision
    from src.dependencies.sage_turn_request import SageTurnPlan, _small_talk_prompt, is_abdication
    from src.dependencies.system_prompt import DefaultSystemPromptProvider

    farm_prompt = DefaultSystemPromptProvider().get_system_prompt()
    try:
        set_request_industry("telecom")
        grid_prompt = DefaultSystemPromptProvider().get_system_prompt()
        for farm_block in ("<AgricultureCapabilities>", "<DroneAndSatellite>", "interpret_raster_health",
                           "specialising in Rwanda agriculture"):
            assert farm_block not in grid_prompt
        assert "Telecom Towers" in grid_prompt and "<UserUploadedRasters>" in grid_prompt  # the neutral block
        assert "agricultu" not in _small_talk_prompt().lower()
        plan = SageTurnPlan(routing=RoutingDecision(is_small_talk=False, selected_categories=frozenset({"agriculture"}),
                                                    primary_model_override=None, reason="intent:agriculture"),
                            system_prompt="P", tools=[{"function": {"name": "get_forecast"}}], model_override=None)
        assert not is_abdication(plan, "is the maize stressed near the mast?", "Ingabe cannot assess crops for a "
                                 "Telecom Towers project yet.", has_tool_calls=False)
    finally:
        set_request_industry(None)
    assert "<AgricultureCapabilities>" in farm_prompt  # agriculture unchanged


@pytest.mark.anyio
async def test_parcel_ndvi_reads_only_this_projects_parcels():
    """ndvi_parcel_cache has every user's parcels and no owner column (audit round 2, critical)."""
    from src.services.legacy_tool_shim import LegacyToolContext, execute_legacy_tool

    owner = str(uuid.uuid4())
    mine, theirs = f"L{RUN_TAG}pm"[:12], f"L{RUN_TAG}pt"[:12]
    async with get_async_db_connection() as conn:
        my_map = await _project(conn, owner, "agriculture", mine)
        await _project(conn, str(uuid.uuid4()), "agriculture", theirs)
        project_id = await conn.fetchval("SELECT project_id FROM user_mundiai_maps WHERE id = $1", my_map)
        for layer, name in ((mine, f"my-field-{RUN_TAG}"), (theirs, f"their-field-{RUN_TAG}")):
            await conn.execute("INSERT INTO ndvi_parcel_cache (parcel_id, parcel_name, layer_id, week_start, mean_ndvi, "
                               "computed_at) VALUES ($1, $2, $3, CURRENT_DATE, 0.5, now())", f"{layer}-1", name, layer)
        try:
            result = await execute_legacy_tool("get_parcel_ndvi_stats", LegacyToolContext(
                user_id=owner, partner_id="", conversation_id=0, map_id=my_map, project_id=project_id, conn=conn,
                arguments={"parcel_name": f"field-{RUN_TAG}"}))
            text = json.dumps(result, default=str)
            assert f"my-field-{RUN_TAG}" in text and f"their-field-{RUN_TAG}" not in text
        finally:
            await conn.execute("DELETE FROM ndvi_parcel_cache WHERE layer_id = ANY($1::text[])", [mine, theirs])


@pytest.mark.anyio
async def test_background_writes_link_only_to_notes_the_author_could_read():
    """Hooks write with no user or industry set; a [[slug]] or a frontmatter reference must still not link a note to
    another partner's private note or another industry's note (audit 2026-10-09, round 2)."""
    from src.services.brain_service import BrainService, PageInput

    brain = BrainService()
    a, b = str(uuid.uuid4()), str(uuid.uuid4())
    mine, theirs, grid, open_ = (f"{name}-{RUN_TAG}" for name in ("mine", "theirs", "grid", "open"))
    async with get_async_db_connection() as conn:  # a worker connection: no user, partner or industry
        try:
            await brain.put_page(conn, mine, PageInput(type="concept", title="A's note", compiled_truth="a"), owner_uuid=a)
            await brain.put_page(conn, theirs, PageInput(type="farmer", title="B's farmer", compiled_truth="b"), owner_uuid=b)
            await brain.put_page(conn, open_, PageInput(type="concept", title="Public", compiled_truth="p"),
                                 owner_uuid=b, access_scope="public")
            await conn.execute("UPDATE brain_pages SET industry = 'agriculture' WHERE slug = ANY($1)", [mine, theirs, open_])
            await conn.execute("SELECT set_config('app.industry', 'agriculture', false)")
            page = await brain.put_page(conn, f"src-{RUN_TAG}", PageInput(
                type="field", title="Upload", compiled_truth=f"see [[{mine}]] [[{theirs}]] [[{open_}]]",
                frontmatter={"related": [theirs]}), owner_uuid=a)
            linked = {r["slug"] for r in await conn.fetch(
                "SELECT t.slug FROM brain_links l JOIN brain_pages t ON t.id = l.to_page_id WHERE l.from_page_id = $1",
                page.id)}
            assert linked == {mine, open_}  # never B's private farmer

            await conn.execute("SELECT set_config('app.industry', 'power_grid', false)")
            grid_page = await brain.put_page(conn, grid, PageInput(
                type="asset", title="Substation", compiled_truth=f"near [[{open_}]] [[{mine}]]"), owner_uuid=a)
            assert await conn.fetchval("SELECT count(*) FROM brain_links WHERE from_page_id = $1", grid_page.id) == 0
        finally:
            await conn.execute("RESET app.industry")
            await conn.execute("DELETE FROM brain_pages WHERE slug = ANY($1)", [mine, theirs, open_, grid, f"src-{RUN_TAG}"])


# --- A project acts for one organization (audit 2026-10-09, round 2) ------------------------------------------------

@pytest.mark.anyio
async def test_a_project_acts_for_its_own_organization_whichever_one_is_active():
    from src.services.project_partner import partner_for_project

    user, outsider = str(uuid.uuid4()), str(uuid.uuid4())
    alpha, beta = str(uuid.uuid4()), str(uuid.uuid4())
    project_id = f"P{RUN_TAG}pp"[:12]
    async with get_async_db_connection() as conn:
        await conn.execute("INSERT INTO users (internal_uuid) VALUES ($1)", user)
        for org in (alpha, beta):
            await conn.execute("INSERT INTO organizations (id, name, slug) VALUES ($1::uuid, $2, $2)", org, f"o-{org[:8]}")
            await conn.execute("INSERT INTO user_organizations (user_id, org_id, role) VALUES ($1, $2::uuid, 'member')",
                               user, org)
        await conn.execute("INSERT INTO user_mundiai_projects (id, owner_uuid, maps) VALUES ($1, $2::uuid, '{}')",
                           project_id, user)
        try:
            assert await partner_for_project(conn, project_id, user, alpha) == alpha  # first turn binds it
            assert await partner_for_project(conn, project_id, user, beta) == alpha   # switching org changes nothing
            assert await partner_for_project(conn, project_id, outsider, beta) is None  # not a member: no partner notes
            assert await partner_for_project(conn, None, user, beta) is None
        finally:
            await conn.execute("DELETE FROM user_mundiai_projects WHERE id = $1", project_id)
            await conn.execute("DELETE FROM user_organizations WHERE user_id = $1", user)
            await conn.execute("DELETE FROM organizations WHERE id = ANY($1::uuid[])", [alpha, beta])
            await conn.execute("DELETE FROM users WHERE internal_uuid = $1", user)


@pytest.mark.anyio
async def test_a_placeholder_partner_does_not_bind_a_project():
    from src.services.project_partner import partner_for_project

    user, project_id = str(uuid.uuid4()), f"P{RUN_TAG}ph"[:12]
    async with get_async_db_connection() as conn:
        await conn.execute("INSERT INTO user_mundiai_projects (id, owner_uuid, maps) VALUES ($1, $2::uuid, '{}')",
                           project_id, user)
        try:
            placeholder = str(uuid.uuid4())  # Hermes' local stand-in when no sign-in provider is set
            assert await partner_for_project(conn, project_id, user, placeholder) == placeholder
            assert await partner_for_project(conn, project_id, user, None) is None
            assert await conn.fetchval("SELECT partner_id FROM user_mundiai_projects WHERE id = $1", project_id) is None
        finally:
            await conn.execute("DELETE FROM user_mundiai_projects WHERE id = $1", project_id)


def test_a_session_acting_for_a_partner_keeps_its_class():
    from src.dependencies.session import LegacyUserContext, WorkOSUserContext

    workos = WorkOSUserContext("u", "w", org_id="beta").for_partner("alpha")
    assert isinstance(workos, WorkOSUserContext) and workos.get_org_id() == "alpha" and workos.get_user_id() == "u"
    legacy = LegacyUserContext().for_partner(None)
    assert isinstance(legacy, LegacyUserContext) and legacy.get_org_id() is None


@pytest.mark.anyio
async def test_semantic_search_finds_a_small_industrys_notes_among_many_others():
    """The vector index returns only its 40 nearest chunks before row security drops other industries' ones; the
    search must keep reading until it finds the session's own (audit R2-5)."""
    from src.services.brain_service import BrainService

    import random

    rng = random.Random(42)  # realistic, distinct embeddings: identical ones make a degenerate index graph

    def vec(lead: float) -> str:  # lead: how close to the query (along the first axis)
        return "[" + ",".join([str(lead)] + [str((rng.random() - 0.5) * 0.4) for _ in range(767)]) + "]"

    farm = [f"farm-{RUN_TAG}-{i}" for i in range(300)]  # close to the query
    masts = [f"mast-{RUN_TAG}-{i}" for i in range(5)]  # further away
    async with get_async_db_connection() as conn:  # worker: seeds every industry
        for slugs, ind, lead in ((farm, "agriculture", 3.0), (masts, "telecom", 0.5)):
            ids = await conn.fetch(
                "INSERT INTO brain_pages (slug, type, title, compiled_truth, owner_uuid, access_scope, industry) "
                "SELECT s, 'concept', s, s, gen_random_uuid(), 'public', $2 FROM unnest($1::text[]) s RETURNING id",
                slugs, ind)
            await conn.executemany(
                "INSERT INTO brain_content_chunks (page_id, chunk_index, chunk_text, embedding) VALUES ($1, 0, 'x', $2::vector)",
                [(r["id"], vec(lead)) for r in ids])
    try:
        set_request_industry("telecom")
        async with get_async_db_connection(user_id=str(uuid.uuid4())) as conn:
            await conn.execute("SET enable_seqscan = off")  # the index plan, as on a large Brain
            found = await BrainService().search_vector(conn, [1.0] + [0.0] * 767, limit=50)
            await conn.execute("RESET enable_seqscan")
        # All of this run's masts (another run's leftovers may come back too; the old search found none of them)
        assert set(masts) <= {r.slug for r in found}
    finally:
        set_request_industry(None)
        async with get_async_db_connection() as conn:
            await conn.execute("DELETE FROM brain_pages WHERE slug = ANY($1)", farm + masts)


# --- Low-priority follow-ups (audit 2026-10-09) ---------------------------------------------------------------------

def test_history_replay_drops_another_industrys_tool_calls_and_their_results():
    """A farm tool call stored in a Telecom conversation is not replayed, nor its result (audit R2-14)."""
    from src.routes.message_routes import _without_other_industries_calls

    farm = sorted(AGRI_ONLY)[0]
    history = [
        {"role": "user", "content": "how are the fields?"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": farm, "arguments": "{}"}},
            {"id": "c2", "type": "function", "function": {"name": "search_location", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "maize stress in plot 4"},
        {"role": "tool", "tool_call_id": "c2", "content": "Kigali"},
    ]
    telecom = _without_other_industries_calls(history, "telecom")
    assert [c["id"] for c in telecom[1]["tool_calls"]] == ["c2"]
    assert all(m.get("tool_call_id") != "c1" for m in telecom)
    assert "maize" not in json.dumps(telecom)
    assert _without_other_industries_calls(history, "agriculture") == history


@pytest.mark.anyio
async def test_writing_another_industrys_slug_fails_with_one_plain_error():
    """A slug held by a note this session cannot see: a plain NoteNotWritable, no policy name, and the caller's
    transaction keeps working (audit R1-26)."""
    from src.services.brain_service import BrainService, NoteNotWritable, PageInput

    slug = f"shared-name-{RUN_TAG}"
    brain = BrainService()
    async with get_async_db_connection() as conn:  # worker: an agriculture note holds the slug
        await brain.put_page(conn, slug, PageInput(type="concept", title="farm", compiled_truth="farm"),
                             owner_uuid=str(uuid.uuid4()), access_scope="public")
        await conn.execute("UPDATE brain_pages SET industry = 'agriculture' WHERE slug = $1", slug)
    try:
        set_request_industry("telecom")
        async with get_async_db_connection(user_id=str(uuid.uuid4())) as conn:
            async with conn.transaction():
                with pytest.raises(NoteNotWritable) as refused:
                    await brain.put_page(conn, slug, PageInput(type="asset", title="mast", compiled_truth="mast"),
                                         owner_uuid=str(uuid.uuid4()))
                assert "policy" not in str(refused.value) and "industry" not in str(refused.value)
                assert await conn.fetchval("SELECT 1") == 1  # the transaction is still usable
    finally:
        set_request_industry(None)
        async with get_async_db_connection() as conn:
            await conn.execute("DELETE FROM brain_pages WHERE slug = $1", slug)


def test_routing_eval_cases_expect_only_tools_their_industry_is_offered():
    """A Power Grid or Telecom eval case expecting a farm tool could never pass (audit R1-33)."""
    import importlib.util
    import sys

    root = Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location("eval_sage_routing", root / "scripts" / "eval_sage_routing.py")
    harness = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = harness
    spec.loader.exec_module(harness)
    cases = [json.loads(line) for path in sorted((root / "evals" / "sage_routing" / "cases").glob("*.jsonl"))
             for line in path.read_text().splitlines() if line.strip()]
    harness.check_industry_cases(cases)
    from evals.sage_routing import scoring

    with pytest.raises(scoring.CorpusError):
        harness.check_industry_cases([{"id": "x", "industry": "telecom", "expect": {"any_of": ["get_drought_status"]}}])
