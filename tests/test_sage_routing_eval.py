"""Gate for the Sage routing eval: case integrity and scoring math.

Stdlib only, so it runs in CI's `standards` job without the app stack.
The live tool catalog is checked against tool_catalog.json by
src/dependencies/test_sage_turn_request.py (needs the app).
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from evals.sage_routing import scoring  # noqa: E402

EVAL_DIR = ROOT / "evals" / "sage_routing"


def _cases() -> list[dict]:
    return [json.loads(line) for path in sorted((EVAL_DIR / "cases").glob("*.jsonl"))
            for line in path.read_text().splitlines() if line.strip()]


def _catalog() -> dict:
    return json.loads((EVAL_DIR / "tool_catalog.json").read_text())


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, EVAL_DIR / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --- cases ---------------------------------------------------------------

def test_cases_are_valid_against_the_tool_snapshot() -> None:
    catalog = _catalog()
    scoring.validate_corpus(_cases(), set(catalog["model_tools"]) | set(catalog["fast_path_tools"]))


def test_cases_cover_every_kind_stratum_and_text_answers() -> None:
    cases = _cases()
    assert {scoring.case_kind(c) for c in cases} == set(scoring.KINDS)
    assert {c["stratum"] for c in cases} == set(scoring.STRATA)
    assert sum(c["expect"].get("no_tool", False) for c in cases) >= 10
    assert len({c["intent"] for c in cases}) >= 150


@pytest.mark.parametrize(("builder", "files"), [
    ("build_paraphrases", ["paraphrases.jsonl"]),
    ("build_conversations", ["multi_turn.jsonl", "chains.jsonl"]),
])
def test_generated_case_files_are_up_to_date(builder: str, files: list[str], tmp_path, monkeypatch) -> None:
    """Edit the builder, not the generated .jsonl; then re-run the builder."""
    module = _load(builder)
    if hasattr(module, "OUT"):
        monkeypatch.setattr(module, "OUT", tmp_path / files[0])
    if hasattr(module, "CASES"):
        monkeypatch.setattr(module, "CASES", tmp_path)
    module.main()
    for name in files:
        assert (tmp_path / name).read_text() == (EVAL_DIR / "cases" / name).read_text(), name


BASE = {"id": "x", "intent": "i", "text": "t", "category": "c", "stratum": "observed", "lang": "en"}


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        ({"expect": {}}, "exactly one"),
        ({"stratum": "other", "expect": {"no_tool": True}}, "stratum"),
        ({"expect": {"any_of": ["nope"]}}, "unknown tools"),
        ({"expect": {"any_of": ["get_forecast"], "args": {"search_location": {"query": "x"}}}}, "not in any_of"),
        ({"expect": {"no_tool": True}, "history": [{"role": "user", "content": "hi"}]}, "end before"),
        ({"expect": {"no_tool": True}, "map_state": {"layers": [{"name": "A", "type": "tiff"}]}}, "map_state"),
        ({"expect": {"any_of": ["get_forecast"]}, "chain": {"must_call": ["get_forecast"], "max_steps": 0}}, "max_steps"),
    ],
)
def test_validate_case_rejects_bad_rows(extra: dict, message: str) -> None:
    with pytest.raises(scoring.CorpusError, match=message):
        scoring.validate_case({**BASE, **extra}, {"get_forecast", "search_location"})


def test_missing_intent_and_duplicate_ids_rejected() -> None:
    row = {**BASE, "expect": {"no_tool": True}}
    with pytest.raises(scoring.CorpusError, match="intent"):
        scoring.validate_case({k: v for k, v in row.items() if k != "intent"})
    with pytest.raises(scoring.CorpusError, match="duplicate"):
        scoring.validate_corpus([row, dict(row)])


# --- attempts ------------------------------------------------------------

@pytest.mark.parametrize(
    ("expect", "first", "outcome"),
    [
        ({"any_of": ["a", "b"]}, "b", scoring.CORRECT),
        ({"any_of": ["a"]}, "z", scoring.WRONG_TOOL),
        ({"any_of": ["a"]}, scoring.TEXT_ONLY, scoring.ABDICATED),
        ({"no_tool": True}, scoring.TEXT_ONLY, scoring.CORRECT),
        ({"no_tool": True}, "a", scoring.FALSE_TOOL),
        ({"any_of": ["a"]}, "<error:RateLimitError:429>", scoring.ERROR),
    ],
)
def test_classify_attempt(expect: dict, first: str, outcome: str) -> None:
    assert scoring.classify_attempt(expect, first) == outcome


SCHEMA = {
    "type": "object",
    "properties": {"district": {"type": "string"}, "days": {"type": "integer"},
                   "level": {"type": "string", "enum": ["district", "sector"]}},
    "required": ["district"],
}


@pytest.mark.parametrize(
    ("args", "expected", "problem"),
    [
        ('{"district": "Huye"}', {"district": "huye"}, None),
        ('{"district": "Huye District"}', {"district": ["nyanza", "huye"]}, None),
        ("not json", None, "not valid JSON"),
        ("[]", None, "not a JSON object"),
        ("{}", None, "missing required 'district'"),
        ('{"district": "Huye", "days": "7"}', None, "'days' should be integer"),
        ('{"district": "Huye", "days": true}', None, "'days' should be integer"),
        ('{"district": "Huye", "level": "cell"}', None, "not in its enum"),
        ('{"district": "Nyanza"}', {"district": "huye"}, "should mention"),
    ],
)
def test_check_arguments(args: str, expected: dict | None, problem: str | None) -> None:
    problems = scoring.check_arguments(SCHEMA, args, expected)
    if problem is None:
        assert problems == []
    else:
        assert any(problem in p for p in problems), problems


def _case(any_of=("a",), **extra) -> dict:
    return {"id": "c1", "intent": "i1", "category": "cat", "stratum": "coverage", "lang": "en",
            "expect": {"any_of": list(any_of)}, **extra}


def test_majority_ignores_errors_and_flags_instability() -> None:
    r = scoring.score_case(_case(), ["a", "<error:X:429>", scoring.TEXT_ONLY, "a"])
    assert r.majority == scoring.CORRECT
    assert r.unstable


def test_tie_with_correct_is_not_correct() -> None:
    r = scoring.score_case(_case(), ["a", scoring.TEXT_ONLY])
    assert r.majority == scoring.ABDICATED and not r.correct


def test_right_tool_with_wrong_arguments_is_not_full_correct() -> None:
    bad = {"first_tool": "a", "args_ok": False, "tools_called": ["a"]}
    good = {"first_tool": "a", "args_ok": True, "tools_called": ["a"]}
    assert scoring.score_case(_case(), [bad, bad, good]).correct
    assert not scoring.score_case(_case(), [bad, bad, good]).full_correct
    assert scoring.score_case(_case(), [good, good, bad]).full_correct


def test_chain_needs_every_must_call_tool() -> None:
    case = _case(any_of=("a", "b"), chain={"must_call": ["a", "b"], "max_steps": 3, "stubs": {}})
    done = {"first_tool": "a", "tools_called": ["a", "b"]}
    half = {"first_tool": "a", "tools_called": ["a"]}
    assert scoring.score_case(case, [done, done, half]).chain_ok is True
    assert scoring.score_case(case, [half, half, done]).chain_ok is False
    assert scoring.score_case(_case(), ["a"]).chain_ok is None


def test_all_errors_scores_as_error_and_is_excluded() -> None:
    r = scoring.score_case(_case(), ["<error:X:500>"] * 3)
    assert r.majority == scoring.ERROR
    s = scoring.summarize([r])
    assert s["errored_cases"] == 1 and s["accuracy"]["n"] == 0


def test_summary_rates_and_wilson_bounds() -> None:
    results = [scoring.score_case(_case(), ["a"])] * 7 + [scoring.score_case(_case(), [scoring.TEXT_ONLY])] * 3
    s = scoring.summarize(results)
    assert s["tool_accuracy"]["k"] == 7 and s["tool_accuracy"]["n"] == 10
    assert s["abdication_rate"]["k"] == 3
    lo, hi = s["tool_accuracy"]["ci95"]
    assert lo == pytest.approx(0.3968, abs=1e-3) and hi == pytest.approx(0.8922, abs=1e-3)


# --- comparison ----------------------------------------------------------

def test_mcnemar_exact() -> None:
    assert scoring.mcnemar_exact_p(0, 0) == 1.0
    assert scoring.mcnemar_exact_p(0, 6) == pytest.approx(0.03125)
    assert scoring.mcnemar_exact_p(3, 3) == 1.0


def test_compare_is_paired_and_deterministic() -> None:
    a = {f"c{i}": i < 20 for i in range(40)}
    b = {f"c{i}": i < 32 for i in range(40)}
    c1, c2 = scoring.compare(a, b), scoring.compare(a, b)
    assert c1 == c2
    assert c1["diff"] == pytest.approx(0.3)
    assert c1["b_right_a_wrong"] == 12 and c1["a_right_b_wrong"] == 0
    assert c1["diff_ci95"][0] > 0
    assert c1["mcnemar_p"] < 0.001


def test_small_gain_on_few_cases_is_not_significant() -> None:
    a = {f"c{i}": i < 5 for i in range(10)}
    b = {f"c{i}": i < 8 for i in range(10)}
    c = scoring.compare(a, b)
    assert c["mcnemar_p"] > 0.05 and c["diff_ci95"][0] <= 0


def test_clustered_bootstrap_widens_the_interval_for_paraphrases() -> None:
    # 12 fixes that all come from 3 intents (4 paraphrases each) are weaker
    # evidence than 12 fixes from 12 different intents.
    a = {f"c{i}": i < 20 for i in range(40)}
    b = {f"c{i}": i < 32 for i in range(40)}
    independent = scoring.compare(a, b)
    clusters = {f"c{i}": f"fix{(i - 20) // 4}" if 20 <= i < 32 else f"c{i}" for i in range(40)}
    clustered = scoring.compare(a, b, clusters=clusters)
    width = lambda c: c["diff_ci95"][1] - c["diff_ci95"][0]  # noqa: E731
    assert clustered["clusters"] == 31
    assert width(clustered) > width(independent)


# --- labelling rules derived from tool schemas ---------------------------

CAT = {"bbox_or_point_tools": ["display_satellite_layer", "get_soil_properties"],
       "geometry_tools": ["create_management_zones"]}


def test_place_name_for_a_bbox_tool_allows_geocoding_first_then_requires_the_tool() -> None:
    case = {**BASE, "text": "show satellite imagery of Musanze", "expect": {"any_of": ["display_satellite_layer"]}}
    eff = scoring.effective_case(case, CAT)
    assert eff["expect"]["any_of"] == ["display_satellite_layer", "search_location"]
    assert eff["chain"]["must_call"] == [["display_satellite_layer"]]
    geocode_only = {"first_tool": "search_location", "tools_called": ["search_location"]}
    geocode_then_tool = {"first_tool": "search_location", "tools_called": ["search_location", "display_satellite_layer"]}
    assert scoring.score_case(eff, [geocode_then_tool] * 3).chain_ok is True
    assert scoring.score_case(eff, [geocode_only] * 3).chain_ok is False
    assert scoring.score_case(eff, [geocode_only] * 3).correct  # first step itself is fine


def test_coordinates_in_the_request_keep_the_strict_label() -> None:
    case = {**BASE, "text": "soil at -2.5, 29.7", "expect": {"any_of": ["get_soil_properties"]}}
    assert scoring.effective_case(case, CAT) == case


def test_missing_geometry_accepts_a_clarifying_question() -> None:
    case = {**BASE, "text": "create management zones for this field", "expect": {"any_of": ["create_management_zones"]}}
    eff = scoring.effective_case(case, CAT)
    assert scoring.score_case(eff, [scoring.TEXT_ONLY]).correct
    assert not scoring.score_case(case, [scoring.TEXT_ONLY]).correct


def test_chain_step_with_alternatives() -> None:
    case = _case(any_of=("a", "b", "c"), chain={"must_call": ["a", ["b", "c"]], "max_steps": 3, "stubs": {}})
    scoring.validate_case({**BASE, **{k: case[k] for k in ("expect", "chain")}}, {"a", "b", "c"})
    assert scoring.score_case(case, [{"first_tool": "a", "tools_called": ["a", "c"]}]).chain_ok is True
    assert scoring.score_case(case, [{"first_tool": "a", "tools_called": ["a"]}]).chain_ok is False
