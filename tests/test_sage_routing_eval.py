"""Gate for the Sage routing eval: corpus integrity and scoring math.

Stdlib only, so it runs in CI's `standards` job without the app stack.
The live tool catalog is checked against tool_catalog.json by
src/dependencies/test_sage_turn_request.py (needs the app).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from evals.sage_routing import scoring  # noqa: E402

EVAL_DIR = ROOT / "evals" / "sage_routing"


def _corpus() -> list[dict]:
    return [json.loads(line) for line in (EVAL_DIR / "corpus.jsonl").read_text().splitlines() if line.strip()]


def _catalog() -> dict:
    return json.loads((EVAL_DIR / "tool_catalog.json").read_text())


def test_corpus_is_valid_against_the_tool_snapshot() -> None:
    catalog = _catalog()
    scoring.validate_corpus(_corpus(), set(catalog["model_tools"]) | set(catalog["fast_path_tools"]))


def test_corpus_keeps_both_strata_and_text_cases() -> None:
    cases = _corpus()
    strata = {c["stratum"] for c in cases}
    assert strata == set(scoring.STRATA)
    assert sum(c["expect"].get("no_tool", False) for c in cases) >= 5


@pytest.mark.parametrize(
    ("case", "message"),
    [
        ({"id": "x", "text": "t", "category": "c", "stratum": "observed", "lang": "en", "expect": {}}, "exactly one"),
        ({"id": "x", "text": "t", "category": "c", "stratum": "other", "lang": "en", "expect": {"no_tool": True}}, "stratum"),
        ({"id": "x", "text": "t", "category": "c", "stratum": "observed", "lang": "en", "expect": {"any_of": ["nope"]}}, "unknown tools"),
        ({"id": "x", "text": "t", "stratum": "observed", "lang": "en", "expect": {"no_tool": True}}, "category"),
    ],
)
def test_validate_case_rejects_bad_rows(case: dict, message: str) -> None:
    with pytest.raises(scoring.CorpusError, match=message):
        scoring.validate_case(case, {"get_forecast"})


def test_duplicate_ids_rejected() -> None:
    row = {"id": "x", "text": "t", "category": "c", "stratum": "observed", "lang": "en", "expect": {"no_tool": True}}
    with pytest.raises(scoring.CorpusError, match="duplicate"):
        scoring.validate_corpus([row, dict(row)])


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


def _case(any_of=("a",)) -> dict:
    return {"id": "c1", "category": "cat", "stratum": "coverage", "lang": "en", "expect": {"any_of": list(any_of)}}


def test_majority_ignores_errors_and_flags_instability() -> None:
    r = scoring.score_case(_case(), ["a", "<error:X:429>", scoring.TEXT_ONLY, "a"])
    assert r.majority == scoring.CORRECT
    assert r.unstable


def test_tie_with_correct_is_not_correct() -> None:
    r = scoring.score_case(_case(), ["a", scoring.TEXT_ONLY])
    assert r.majority == scoring.ABDICATED
    assert not r.correct


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
