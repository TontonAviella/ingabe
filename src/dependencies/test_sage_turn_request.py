"""Sage's per-turn request: tool catalog snapshot and routing plan."""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from src.dependencies.pydantic_tools import get_pydantic_tool_calls
from src.dependencies.sage_routing import FAST_PATH_TOOLS, SMALL_TALK_SYSTEM_PROMPT
from src.dependencies.sage_turn_request import build_sage_tools_payload, plan_sage_turn
from src.dependencies import sage_turn_request

CATALOG = Path(__file__).resolve().parents[2] / "evals" / "sage_routing" / "tool_catalog.json"


def _names(tools: list[dict]) -> list[str]:
    return [t["function"]["name"] for t in tools]


def _eval_runner():
    path = Path(__file__).resolve().parents[2] / "scripts" / "eval_sage_routing.py"
    spec = importlib.util.spec_from_file_location("eval_sage_routing", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # @dataclass resolves its module via sys.modules
    spec.loader.exec_module(module)
    return module


def test_tool_catalog_matches_eval_snapshot() -> None:
    """The routing eval's labels and labelling rules use this snapshot.

    If this fails, a tool was added, renamed, removed, or its coordinate /
    geometry parameters changed: update the labels in evals/sage_routing/cases,
    then run `python scripts/eval_sage_routing.py catalog --write`.
    """
    snapshot = json.loads(CATALOG.read_text())
    assert _eval_runner().live_tool_catalog() == snapshot
    assert sorted(FAST_PATH_TOOLS) == snapshot["fast_path_tools"]


def test_eval_map_context_lists_the_internal_rwanda_tables() -> None:
    """The eval builds every model case's <MapState> with this; a stale import made it crash at the
    first model case (2026-10-09), so no routing eval could run."""
    from src.database.rwanda_reader import INTERNAL_RWANDA_ALLOWED_TABLES

    text = _eval_runner().map_description({"id": "x", "map_state": {"layers": [{"name": "Field", "type": "raster"}]}})
    assert ", ".join(sorted(INTERNAL_RWANDA_ALLOWED_TABLES)) in text
    assert "# Layer: Field" in text


def test_layer_enum_is_kept_only_when_layers_exist() -> None:
    def layer_param(tools: list[dict]) -> dict:
        tool = next(t for t in tools if t["function"]["name"] == "add_layer_to_map")
        return tool["function"]["parameters"]["properties"]["layer_id"]

    calls = get_pydantic_tool_calls()
    assert "enum" not in layer_param(build_sage_tools_payload(calls, {}))
    assert layer_param(build_sage_tools_payload(calls, {"L1": "Field"}))["enum"] == ["L1"]


def test_small_talk_sends_no_tools_and_skips_the_full_prompt() -> None:
    called = []
    plan = plan_sage_turn("hi", [{"role": "user", "content": "hi"}],
                          [{"function": {"name": "x"}}], lambda: called.append(1) or "FULL")
    assert plan.tools == [] and plan.tool_choice is None
    assert plan.system_prompt == SMALL_TALK_SYSTEM_PROMPT
    assert called == []


def test_domain_turn_gets_full_prompt_and_auto_tool_choice() -> None:
    tools = build_sage_tools_payload(get_pydantic_tool_calls(), {})
    text = "what is the NDVI of my fields in Huye"
    plan = plan_sage_turn(text, [{"role": "user", "content": text}], tools, lambda: "FULL PROMPT")
    assert plan.system_prompt.startswith("FULL PROMPT")
    assert plan.tools and plan.tool_choice == "auto"
    assert set(_names(plan.tools)) <= set(_names(tools))


# --- per-turn tool shortlist ------------------------------------------------

def test_tool_shortlist_k_parses_strictly(monkeypatch) -> None:
    from src.dependencies.sage_turn_request import tool_shortlist_k

    monkeypatch.delenv("SAGE_TOOL_SHORTLIST_K", raising=False)
    assert tool_shortlist_k() == 0
    monkeypatch.setenv("SAGE_TOOL_SHORTLIST_K", "15")
    assert tool_shortlist_k() == 15
    for bad in ("ten", "-1"):
        monkeypatch.setenv("SAGE_TOOL_SHORTLIST_K", bad)
        with pytest.raises(ValueError):
            tool_shortlist_k()


@pytest.mark.asyncio
async def test_apply_tool_shortlist_ranks_the_full_catalog_and_skips_small_talk() -> None:
    from src.dependencies.sage_turn_request import apply_tool_shortlist

    tools = build_sage_tools_payload(get_pydantic_tool_calls(), {})
    text = "NDVI by sector in Huye"
    plan = plan_sage_turn(text, [{"role": "user", "content": text}], tools, lambda: "P")
    shortlisted = await apply_tool_shortlist(plan, text, [], tools, k=10, embed=None)
    names = _names(shortlisted.tools)
    assert len(names) == 10 and shortlisted.shortlist == "bm25"
    # The shortlist ranks this tool in; since 2026-10-08 the category filter keeps it too
    # (vegetation words bring in the agriculture tools).
    assert "get_cell_ndvi_stats" in names and "get_cell_ndvi_stats" in _names(plan.tools)

    small_talk = plan_sage_turn("hi", [{"role": "user", "content": "hi"}], tools, lambda: "P")
    assert await apply_tool_shortlist(small_talk, "hi", [], tools, k=10, embed=None) is small_talk


def test_bm25_shortlist_recall_on_eval_cases_stays_high() -> None:
    """Regression floor, offline and deterministic (BM25 only, no embeddings):
    for eval cases that reach the model, the top-15 shortlist must contain an
    accepted first tool in >= 97% of them (99.5% when written)."""
    from evals.sage_routing import scoring
    from src.dependencies.sage_routing import FAST_PATH_TOOLS, build_fast_tool_call
    from src.services.sage_tool_shortlist import shortlist_tools

    eval_dir = Path(__file__).resolve().parents[2] / "evals" / "sage_routing"
    catalog = json.loads((eval_dir / "tool_catalog.json").read_text())
    tools = build_sage_tools_payload(get_pydantic_tool_calls(), {})
    hits = total = 0
    for path in sorted((eval_dir / "cases").glob("*.jsonl")):
        for line in path.read_text().splitlines():
            if not line.strip():
                continue
            case = scoring.effective_case(json.loads(line), catalog)
            accepted = set(case["expect"].get("any_of") or [])
            fast = build_fast_tool_call(case["text"])
            if not accepted or (fast and fast.tool_name in FAST_PATH_TOOLS):
                continue
            history = case.get("history") or []
            plan = plan_sage_turn(case["text"], history + [{"role": "user", "content": case["text"]}],
                                  tools, lambda: "P")
            if not plan.tools:
                continue
            total += 1
            hits += bool(accepted & set(_names(shortlist_tools(case["text"], history, tools, k=15))))
    assert total > 150
    assert hits / total >= 0.97, f"{hits}/{total}"


def test_the_eval_takes_a_fast_path_only_where_the_live_gate_would() -> None:
    """The routing eval's runner uses the live gate: tower questions and photos the fast paths cannot read (no
    place on the map, COG not ready) go to the model; a named, ready orthophoto still takes the fast path."""
    from evals.sage_routing import scoring
    from src.dependencies.sage_routing import build_project_fast_tool_call
    from src.services import industry

    runner = _eval_runner()
    catalog = json.loads(CATALOG.read_text())
    applies = {}
    for case in (scoring.effective_case(c, catalog) for c in runner.load_cases()):
        fast = build_project_fast_tool_call(case["text"], case.get("industry") or industry.DEFAULT_INDUSTRY)
        applies[case["intent"], case["id"]] = runner.fast_path_applies(fast, case["text"], case)
    telecom = {key: value for key, value in applies.items() if key[0].startswith("tel-")}
    assert len(telecom) >= 12 and not any(telecom.values())
    assert applies["cov-objects-cog-pending", "cov-objects-cog-pending"] is False
    assert applies["find-count-houses", "obs-objects-02"] is True  # "count the houses from Farm_A_Orthophoto ?"


# --- abdication guard ------------------------------------------------------

def _plan_for(text: str):
    tools = build_sage_tools_payload(get_pydantic_tool_calls(), {})
    return plan_sage_turn(text, [{"role": "user", "content": text}], tools, lambda: "P")


@pytest.mark.parametrize(
    ("text", "reply", "has_tools", "expected"),
    [
        ("will it rain in Musanze next week?", "I can check the forecast for Musanze for you.", False, True),
        ("will it rain in Musanze next week?", "", False, True),
        ("will it rain in Musanze next week?", "Calling the forecast.", True, False),   # tool called
        ("hi", "Hello! How can I help?", False, False),                                # small talk
        ("what is NDVI?", "NDVI is a vegetation index...", False, False),              # explanation
        ("explain the payout trigger", "A payout trigger is ...", False, False),
        ("create management zones for this field", "Which field should I use?", False, False),  # clarify
        ("thanks!", "You're welcome. Anything else?", False, False),                     # acknowledgement
        ("ok great", "Glad it helped.", False, False),
    ],
)
def test_is_abdication(text: str, reply: str, has_tools: bool, expected: bool) -> None:
    from src.dependencies.sage_turn_request import is_abdication

    assert is_abdication(_plan_for(text), text, reply, has_tools) is expected


def _abdication_on(project_industry: str, text: str, tools: list[dict] | None = None):
    """(is_abdication of an honest no, the turn's plan) for a turn on a project of this industry, as the live loop
    builds it: the industry's tools, then routing."""
    from src.database.pool import set_request_industry
    from src.dependencies.sage_turn_request import is_abdication
    from src.services import industry

    offered = industry.tools_for(tools or build_sage_tools_payload(get_pydantic_tool_calls(), {}), project_industry)
    plan = plan_sage_turn(text, [{"role": "user", "content": text}], offered, lambda: "P")
    try:
        set_request_industry(project_industry)
        return is_abdication(plan, text, "Ingabe cannot do this for this project yet.", False), plan
    finally:
        set_request_industry(None)


@pytest.mark.parametrize(
    ("project_industry", "text", "needed"),
    [
        # What the exemption is for: a farm question outside agriculture; "Ingabe cannot do this yet" is right.
        ("telecom", "is the maize stressed near the mast?", None),
        ("telecom", "Is there drought in Kayonza this season?", None),
        ("power_grid", "Give me the maize yield risk for Gicumbi", None),
        ("power_grid", "Is vegetation growing into the Jabana line corridor? Check the NDVI.", None),
        # The over-reach it had: weather and flood words select the agriculture category too, and their tools
        # serve every industry, so a prose answer is still abdication.
        ("telecom", "Will heavy rain hit the Rebero tower site this week?", "get_forecast"),
        ("telecom", "Did the river flood near Rubavu after last week's rain?", "detect_flood_extent"),
        ("power_grid", "What temperature is forecast for Musanze tomorrow?", "get_forecast"),
        # Words every industry says ("field visit", "field team", "field crew", "trigger") select the agriculture
        # category too, but they do not make a farm question: the notes and point tools serve every industry.
        ("telecom", "Log a field visit to the Rebero mast: the shelter door is broken", "add_observation"),
        ("telecom", "Search our notes from the field team about the Kigali tower audit", "search_brain"),
        ("telecom", "Remember that the field engineer replaced the RRU on the Gasabo mast", "add_observation"),
        ("telecom", "Add a point for the generator yard where the field crew parks at -1.94, 30.08",
         "create_point_layer"),
        ("power_grid", "Log a field visit to the Jabana substation: the fence is down", "add_observation"),
        ("power_grid", "Remember the trigger level our field crews use for the Jabana line", "add_observation"),
        # A farm word does not excuse prose on a request to keep a note.
        ("power_grid", "Log a field visit: the maize under the Jabana line was cut back", "add_observation"),
        # Agriculture is unchanged.
        ("agriculture", "is the maize stressed in Nyagatare?", "get_field_health"),
        ("agriculture", "Will heavy rain hit Musanze this week?", "get_forecast"),
        ("agriculture", "Log a field visit to Kabarama: the beans are flowering", "add_observation"),
    ],
)
def test_the_abdication_exemption_covers_farm_questions_only(project_industry: str, text: str,
                                                             needed: str | None) -> None:
    abdicated, plan = _abdication_on(project_industry, text)
    assert "agriculture" in plan.routing.selected_categories
    if needed is None:
        assert abdicated is False
    else:
        # Still guarded, and the guard has a tool this industry is offered to force.
        assert abdicated is True
        assert needed in _names(plan.tools)


@pytest.mark.parametrize(
    ("project_industry", "text"),
    [
        # With TOWERS off no tool reads tower equipment (the towers category offers nothing), so "Ingabe cannot
        # count antennas yet" stands instead of a forced raster tool (the fast paths' house masks drew screening
        # cells for these).
        ("telecom", "How many antennas are on this tower photo?"),
        ("telecom", "Combien d'antennes y a-t-il sur cette photo du pylône ?"),
        ("telecom", "Ni antene zingahe ziri ku munara uri kuri iyi foto?"),
        ("telecom", "Y a-t-il des dégâts sur le mât dans cette image ?"),
        ("telecom", "Umunara w'itumanaho uri kuri iyi foto ufite ibyangiritse?"),
        ("telecom", "Count the RRUs and microwave dishes on Mast_site_Kicukiro"),
        ("power_grid", "How many antennas are on this tower photo?"),
        ("agriculture", "How many antennas are on this tower photo?"),  # no industry has a tower tool
    ],
)
def test_prose_stands_on_tower_questions_no_tool_here_answers(project_industry: str, text: str, monkeypatch) -> None:
    monkeypatch.delenv("TOWERS", raising=False)
    abdicated, plan = _abdication_on(project_industry, text)
    assert plan.routing.named_categories == frozenset({"towers"})
    assert plan.routing.selected_categories == frozenset() and plan.tools  # the full list is still sent
    assert abdicated is False


@pytest.mark.parametrize(
    ("text", "needed"),
    [
        # A tower question that also names something a tool here does: still guarded.
        ("Will heavy rain hit the Rebero tower site this week?", "get_forecast"),
        ("Search our notes for the Kigali tower audit", "search_brain"),
        ("Show me the Rebero mast on the map", "search_location"),
        ("Describe the image of the Rebero mast I uploaded", "describe_user_raster"),
    ],
)
def test_tower_questions_that_name_a_tool_here_stay_guarded(text: str, needed: str) -> None:
    abdicated, plan = _abdication_on("telecom", text)
    assert "towers" in plan.routing.named_categories
    assert abdicated is True and needed in _names(plan.tools)


@pytest.mark.parametrize("text", ["How many antennas are on this tower photo?",
                                  "Ni antene zingahe ziri ku munara uri kuri iyi foto?",
                                  "Which photos of the Rebero mast still need checking?"])
def test_with_towers_on_a_telecom_tower_question_is_offered_the_tower_tools_and_guarded(text, monkeypatch) -> None:
    monkeypatch.setenv("TOWERS", "1")
    abdicated, plan = _abdication_on("telecom", text)
    assert plan.routing.selected_categories == frozenset({"towers"})
    assert {"get_tower_inspection_findings", "tower_photos_needing_review"} <= set(_names(plan.tools))
    assert not {"describe_user_raster", "analyze_raster_object_candidates"} & set(_names(plan.tools))
    assert abdicated is True  # a tool here answers it: prose is abdication
    # Power Grid has its own inspection tools in the category (pylons, insulators, nests), never the tower ones.
    abdicated, plan = _abdication_on("power_grid", text)
    assert "get_tower_inspection_findings" not in _names(plan.tools)
    assert {"get_power_line_inspection_findings", "power_line_photos_needing_review"} <= set(_names(plan.tools))
    abdicated, plan = _abdication_on("agriculture", text)  # no inspection tool there: the honest no stands
    assert abdicated is False and plan.routing.selected_categories == frozenset()
    assert "get_tower_inspection_findings" not in _names(plan.tools)


@pytest.mark.parametrize(("text", "needed"), [
    # Tower crews' weather, flood and photo questions name "tower" too: the tools that answer them stay offered.
    ("Is strong wind or a storm expected in Karongi in the next days? We have tower climbs planned.", "get_forecast"),
    ("Describe the Mast_site_Kicukiro photo", "describe_user_raster"),
    ("Describe the tower photo", "describe_user_raster"),
    ("Is there flooding near our tower in Rubavu?", "detect_flood_extent"),
])
def test_with_towers_on_tower_words_alone_keep_the_tools_that_answer(text: str, needed: str, monkeypatch) -> None:
    monkeypatch.setenv("TOWERS", "1")
    _, plan = _abdication_on("telecom", text)
    assert plan.routing.named_categories == frozenset({"towers"}) and plan.routing.selected_categories == frozenset()
    assert needed in _names(plan.tools) and "get_tower_inspection_findings" in _names(plan.tools)


@pytest.mark.parametrize("text", ["Y a-t-il des dégâts sur le mât dans cette image ?",
                                  "Umunara w'itumanaho uri kuri iyi foto ufite ibyangiritse?"])
def test_with_towers_on_prose_still_stands_on_what_no_tower_tool_answers(text: str, monkeypatch) -> None:
    """Damage on a mast: the tower tools count equipment, they do not see damage, so "Ingabe cannot tell" stands."""
    monkeypatch.setenv("TOWERS", "1")
    abdicated, plan = _abdication_on("telecom", text)
    assert abdicated is False and plan.routing.selected_categories == frozenset()


def test_with_towers_on_every_telecom_tower_case_is_offered_its_expected_tool(monkeypatch) -> None:
    """The routing eval's Telecom cases that say "tower" (or mast, antenna, ...), run as the eval runs them with
    TOWERS=1 up to the model call: a fast path to an expected tool, or an expected tool in the first request. The
    model's choice itself needs the eval (scripts/eval_sage_routing.py with TOWERS=1)."""
    from src.dependencies.sage_routing import build_project_fast_tool_call, mentions_tower_equipment

    monkeypatch.setenv("TOWERS", "1")
    cases = Path(__file__).resolve().parents[2] / "evals" / "sage_routing" / "cases" / "non_agriculture.jsonl"
    checked = []
    for line in cases.read_text().splitlines():
        case = json.loads(line)
        expected = set(case["expect"].get("any_of") or [])
        if case.get("industry") != "telecom" or not expected or not mentions_tower_equipment(case["text"]):
            continue
        fast = build_project_fast_tool_call(case["text"], "telecom")
        if fast is not None and fast.tool_name in expected:
            continue
        _, plan = _abdication_on("telecom", case["text"])
        assert expected & set(_names(plan.tools)), (case["id"], plan.routing.reason)
        checked.append(case["id"])
    assert {"ind-tel-03", "ind-tel-04", "ind-tel-11", "ind-tel-21", "ind-tel-25"} <= set(checked)


def test_tower_questions_are_guarded_again_once_a_tower_tool_exists(monkeypatch) -> None:
    """The exemption follows what the project can do: a tower tool for telecom ends it there, not elsewhere."""
    from src.dependencies import sage_routing
    from src.services import industry

    monkeypatch.setitem(sage_routing._TOOL_CATEGORIES, "count_tower_equipment", sage_routing.TOWERS)
    monkeypatch.setitem(industry.CAPABILITIES, "count_tower_equipment", frozenset({"telecom"}))
    tools = build_sage_tools_payload(get_pydantic_tool_calls(), {}) + [
        {"type": "function", "function": {"name": "count_tower_equipment", "parameters": {"type": "object"}}}]
    text = "How many antennas are on this tower photo?"
    abdicated, plan = _abdication_on("telecom", text, tools)
    assert abdicated is True and "count_tower_equipment" in _names(plan.tools)
    abdicated, plan = _abdication_on("power_grid", text, tools)
    assert abdicated is False and "count_tower_equipment" not in _names(plan.tools)


def test_abdication_guard_flag(monkeypatch) -> None:
    from src.dependencies.sage_turn_request import abdication_guard_enabled

    monkeypatch.delenv("SAGE_ABDICATION_GUARD", raising=False)
    assert abdication_guard_enabled() is False
    monkeypatch.setenv("SAGE_ABDICATION_GUARD", "1")
    assert abdication_guard_enabled() is True


@pytest.mark.asyncio
async def test_guard_tools_are_a_short_ranked_list() -> None:
    from src.dependencies.sage_turn_request import GUARD_TOOL_COUNT, KEEP_ANSWER_TOOL, guard_tools

    tools = build_sage_tools_payload(get_pydantic_tool_calls(), {})
    picked = await guard_tools("will it rain in Musanze next week?", [], tools, embed=None)
    assert len(picked) == GUARD_TOOL_COUNT + 1  # the best tools, and the way to keep a written answer
    assert "get_forecast" in _names(picked) and _names(picked)[-1] == KEEP_ANSWER_TOOL


def test_the_guard_keeps_the_written_answer_when_the_retry_says_no_tool_fits() -> None:
    from types import SimpleNamespace as NS

    from src.dependencies.sage_turn_request import KEEP_ANSWER_TOOL, guard_tool_calls

    forecast = NS(function=NS(name="get_forecast"))
    assert guard_tool_calls([forecast]) == [forecast]
    assert guard_tool_calls([NS(function=NS(name=KEEP_ANSWER_TOOL))]) == []


class _RateLimitError(Exception):
    def __init__(self, message: str, *, status: int = 429, body=None, headers=None) -> None:
        super().__init__(message)
        self.status_code = status
        self.body = body
        self.response = type("R", (), {"headers": headers or {}})()


def test_rate_limit_retry_after_reads_openrouter_reset_from_the_body() -> None:
    import time as _time

    reset_ms = str(int((_time.time() + 4) * 1000))
    err = _RateLimitError(
        "Rate limit exceeded: free-models-per-min.",
        body={"metadata": {"headers": {"X-RateLimit-Limit": "20", "X-RateLimit-Reset": reset_ms}}},
    )
    wait = sage_turn_request.rate_limit_retry_after(err)
    assert wait is not None and 2.5 <= wait <= 4.5


def test_rate_limit_retry_after_prefers_retry_after_and_caps_it() -> None:
    assert sage_turn_request.rate_limit_retry_after(
        _RateLimitError("too many", headers={"Retry-After": "3"})) == 3.0
    assert sage_turn_request.rate_limit_retry_after(
        _RateLimitError("too many", headers={"retry-after": "600"})) == 15.0
    assert sage_turn_request.rate_limit_retry_after(_RateLimitError("too many")) == 5.0


def test_rate_limit_retry_after_ignores_daily_caps_and_other_errors() -> None:
    assert sage_turn_request.rate_limit_retry_after(
        _RateLimitError("Rate limit exceeded: free-models-per-day.")) is None
    assert sage_turn_request.rate_limit_retry_after(_RateLimitError("boom", status=500)) is None
    assert sage_turn_request.rate_limit_retry_after(ValueError("nope")) is None


def test_rate_limit_user_message_explains_daily_and_minute_limits() -> None:
    daily = sage_turn_request.rate_limit_user_message(
        _RateLimitError("Rate limit exceeded: free-models-per-day-high-balance."))
    assert daily and "02:00 Kigali" in daily
    minute = sage_turn_request.rate_limit_user_message(
        _RateLimitError("Rate limit exceeded: free-models-per-min."))
    assert minute and "in a minute" in minute
    assert sage_turn_request.rate_limit_user_message(_RateLimitError("boom", status=500)) is None


_GOOGLE = "https://generativelanguage.googleapis.com/v1beta/openai/"


def test_google_per_minute_limit_waits_into_the_next_minute() -> None:
    # Google documents only "429 RESOURCE_EXHAUSTED, retry with backoff": no wait in the body or headers.
    assert sage_turn_request.rate_limit_retry_after(_RateLimitError("Resource has been exhausted"), _GOOGLE) == 20.0
    assert sage_turn_request.rate_limit_retry_after(
        _RateLimitError("too many", headers={"retry-after": "600"}), _GOOGLE) == 60.0


def test_google_daily_quota_is_not_retried_and_names_the_pacific_midnight_reset() -> None:
    daily = _RateLimitError("Quota exceeded for quota id GenerateRequestsPerDayPerProjectPerModel-FreeTier")
    assert sage_turn_request.rate_limit_retry_after(daily, _GOOGLE) is None
    text = sage_turn_request.rate_limit_user_message(daily, _GOOGLE)
    assert text and "midnight Pacific time" in text and "Kigali" in text


# Gemini's streamed tool calls, exactly as gemini-3.5-flash-lite sent them (2026-10-10, signature shortened).
_GEMINI_DELTAS = [
    {"index": None, "id": "call_824201", "type": "function",
     "function": {"arguments": "{\"district\":\"Huye\"}", "name": "get_forecast"},
     "extra_content": {"google": {"thought_signature": "EmAKXgFpFH0T"}}},
    {"index": None, "id": "call_824202", "type": "function",
     "function": {"arguments": "{\"district\":\"Musanze\"}", "name": "get_forecast"}},
]


def _deltas(raw: list[dict]):
    """Built the way the OpenAI SDK builds streamed chunks (no validation), so `index` None survives as it did."""
    from openai._models import construct_type
    from openai.types.chat.chat_completion_chunk import ChoiceDeltaToolCall
    return [construct_type(type_=ChoiceDeltaToolCall, value=d) for d in raw]


def test_gemini_whole_tool_calls_without_index_stay_separate_and_keep_the_signature() -> None:
    acc: dict = {}
    for d in _deltas(_GEMINI_DELTAS):
        sage_turn_request.add_tool_call_delta(acc, d)
    assert [acc[k]["function"]["arguments"] for k in sorted(acc)] == ['{"district":"Huye"}', '{"district":"Musanze"}']
    assert [acc[k]["id"] for k in sorted(acc)] == ["call_824201", "call_824202"]
    assert acc[0]["extra_content"] == {"google": {"thought_signature": "EmAKXgFpFH0T"}}
    assert "extra_content" not in acc[1]


def test_openai_style_split_tool_call_deltas_still_join() -> None:
    acc: dict = {}
    for d in _deltas([
        {"index": 0, "id": "call_a", "type": "function", "function": {"name": "get_forecast", "arguments": ""}},
        {"index": 0, "function": {"arguments": "{\"district\":"}},
        {"index": 0, "function": {"arguments": "\"Huye\"}"}},
    ]):
        sage_turn_request.add_tool_call_delta(acc, d)
    assert acc == {0: {"id": "call_a", "type": "function",
                       "function": {"name": "get_forecast", "arguments": "{\"district\":\"Huye\"}"}}}


def test_signature_goes_back_only_to_google() -> None:
    call = {"id": "call_1", "type": "function", "function": {"name": "f", "arguments": "{}"},
            "extra_content": {"google": {"thought_signature": "abc"}}}
    google = "https://generativelanguage.googleapis.com/v1beta/openai/"
    assert sage_turn_request.tool_call_for_provider(call, google) is call
    assert "extra_content" not in sage_turn_request.tool_call_for_provider(call, "https://openrouter.ai/api/v1")
    assert "extra_content" in call  # the stored call is not changed
