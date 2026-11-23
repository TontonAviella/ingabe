"""The questions Sage's start screen offers are the routing test's own cases, word for word: a question is offered
to a reader only while Sage is measured to take it to the right tool."""
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCREEN = ROOT / "frontendts" / "src" / "lib" / "sageStarters.ts"
CASES = ROOT / "evals" / "sage_routing" / "cases" / "starters.jsonl"


_QUOTED = re.compile(r"""'((?:[^'\\\n]|\\.)*)'|"((?:[^"\\\n]|\\.)*)\"""")


def _questions(body: str) -> list[str]:
    """The strings of one list, in either kind of quotes (the formatter writes double quotes around a question
    that holds an apostrophe), with comments left out and escapes read."""
    body = re.sub(r"//[^\n]*", "", body)
    return [re.sub(r"\\(.)", r"\1", single or double) for single, double in _QUOTED.findall(body)]


def _offered() -> dict[str, list[str]]:
    """Each list of the screen's file: its name and its questions."""
    lists = re.findall(r"export const (\w+_STARTERS) = \[(.*?)\];", SCREEN.read_text(), re.S)
    return {name: _questions(body) for name, body in lists}


def test_a_question_with_an_apostrophe_or_a_comment_beside_it_is_read_whole():
    body = """
  "What's growing in each plot?", // the crop card's question
  'Is it drier than normal here right now?',
  'Which plots aren\\'t green?',
"""
    assert _questions(body) == ["What's growing in each plot?", "Is it drier than normal here right now?",
                                "Which plots aren't green?"]


def _cases() -> list[dict]:
    return [json.loads(line) for line in CASES.read_text().splitlines() if line.strip()]


def test_the_screen_offers_three_questions_for_each_kind_of_project():
    offered = _offered()
    assert sorted(offered) == ["FARM_PHOTO_STARTERS", "FARM_STARTERS", "TOWER_STARTERS"]
    assert all(len(questions) == 3 for questions in offered.values())


def test_every_offered_question_is_a_case_of_the_routing_test_and_no_case_is_left_over():
    offered = sorted(q for questions in _offered().values() for q in questions)
    assert offered == sorted(case["text"] for case in _cases())


def test_each_case_runs_as_the_project_its_question_is_offered_in():
    offered = _offered()
    by_text = {case["text"]: case for case in _cases()}
    for question in offered["TOWER_STARTERS"]:
        assert by_text[question].get("industry") == "telecom", question
    for question in offered["FARM_PHOTO_STARTERS"]:
        case = by_text[question]
        assert "industry" not in case and any(layer["type"] == "raster" for layer in case["map_state"]["layers"]), question
    for question in offered["FARM_STARTERS"]:
        case = by_text[question]
        assert "industry" not in case and case["map_state"]["layers"] == [], question
