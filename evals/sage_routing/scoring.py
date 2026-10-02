"""Scoring and statistics for the Sage routing eval. Stdlib only.

A case expects either a first tool from an accepted set (``any_of``) or a
plain-text answer (``no_tool``). Each case is run ``repeats`` times; an
attempt is classified, the case is scored on the majority of its non-error
attempts, and results are reported with 95% Wilson intervals. Two runs are
compared case-by-case (paired), with an exact McNemar test and a bootstrap
interval on the accuracy difference.
"""
from __future__ import annotations

import math
import random
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Any, Iterable

TEXT_ONLY = "<text-only>"
ERROR_PREFIX = "<error:"

# Attempt outcomes. Producer-owned constants: compare against these, never
# against string literals (CODING_STANDARDS Lessons log).
CORRECT = "correct"
WRONG_TOOL = "wrong_tool"
ABDICATED = "abdicated"  # answered in prose when a tool was expected
FALSE_TOOL = "false_tool"  # called a tool when plain text was expected
ERROR = "error"

STRATA = ("observed", "coverage")
LANGS = ("en", "rw", "fr")


class CorpusError(ValueError):
    """A corpus row is malformed."""


def validate_case(case: dict[str, Any], known_tools: set[str] | None = None) -> None:
    """Raise CorpusError if a corpus row is malformed or names unknown tools."""
    for key in ("id", "text", "category", "stratum", "lang", "expect"):
        if key not in case:
            raise CorpusError(f"{case.get('id', '?')}: missing {key!r}")
    if case["stratum"] not in STRATA:
        raise CorpusError(f"{case['id']}: stratum must be one of {STRATA}")
    if case["lang"] not in LANGS:
        raise CorpusError(f"{case['id']}: lang must be one of {LANGS}")
    expect = case["expect"]
    any_of = expect.get("any_of")
    no_tool = expect.get("no_tool") is True
    if bool(any_of) == no_tool:
        raise CorpusError(f"{case['id']}: expect needs exactly one of any_of / no_tool")
    if any_of is not None:
        if not isinstance(any_of, list) or not all(isinstance(t, str) and t for t in any_of):
            raise CorpusError(f"{case['id']}: any_of must be a non-empty list of tool names")
        if known_tools is not None:
            unknown = sorted(set(any_of) - known_tools)
            if unknown:
                raise CorpusError(f"{case['id']}: unknown tools {unknown}")


def validate_corpus(cases: list[dict[str, Any]], known_tools: set[str] | None = None) -> None:
    ids = Counter(c.get("id") for c in cases)
    dupes = sorted(i for i, n in ids.items() if n > 1)
    if dupes:
        raise CorpusError(f"duplicate case ids: {dupes}")
    for case in cases:
        validate_case(case, known_tools)


def classify_attempt(expect: dict[str, Any], first_tool: str) -> str:
    """Classify one attempt's first tool (or TEXT_ONLY / an error marker)."""
    if first_tool.startswith(ERROR_PREFIX):
        return ERROR
    if expect.get("no_tool"):
        return CORRECT if first_tool == TEXT_ONLY else FALSE_TOOL
    if first_tool == TEXT_ONLY:
        return ABDICATED
    return CORRECT if first_tool in expect["any_of"] else WRONG_TOOL


@dataclass(frozen=True)
class CaseResult:
    case_id: str
    category: str
    stratum: str
    lang: str
    expects_tool: bool
    outcomes: tuple[str, ...]  # one per attempt
    majority: str  # majority outcome over non-error attempts; ERROR if none
    unstable: bool  # non-error attempts disagree on correctness

    @property
    def correct(self) -> bool:
        return self.majority == CORRECT


def score_case(case: dict[str, Any], first_tools: Iterable[str]) -> CaseResult:
    outcomes = tuple(classify_attempt(case["expect"], t) for t in first_tools)
    valid = [o for o in outcomes if o != ERROR]
    if not valid:
        majority = ERROR
    else:
        counts = Counter(valid)
        top = max(counts.values())
        winners = sorted(o for o, n in counts.items() if n == top)
        # A tie between correct and anything else is not a correct majority.
        majority = winners[0] if len(winners) == 1 else next(o for o in winners if o != CORRECT)
    return CaseResult(
        case_id=case["id"],
        category=case["category"],
        stratum=case["stratum"],
        lang=case["lang"],
        expects_tool=not case["expect"].get("no_tool", False),
        outcomes=outcomes,
        majority=majority,
        unstable=len({o == CORRECT for o in valid}) > 1,
    )


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """95% Wilson score interval for k successes in n trials."""
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, center - half), min(1.0, center + half))


def rate(k: int, n: int) -> dict[str, Any]:
    lo, hi = wilson(k, n)
    return {"k": k, "n": n, "rate": (k / n) if n else None, "ci95": [lo, hi]}


def summarize(results: list[CaseResult]) -> dict[str, Any]:
    """Headline metrics over scored (non-error) cases, plus breakdowns."""
    scored = [r for r in results if r.majority != ERROR]
    tool_cases = [r for r in scored if r.expects_tool]
    text_cases = [r for r in scored if not r.expects_tool]

    def group(key: str) -> dict[str, Any]:
        buckets: dict[str, list[CaseResult]] = defaultdict(list)
        for r in scored:
            buckets[getattr(r, key)].append(r)
        return {
            name: rate(sum(r.correct for r in rs), len(rs))
            for name, rs in sorted(buckets.items())
        }

    return {
        "cases": len(results),
        "errored_cases": len(results) - len(scored),
        "accuracy": rate(sum(r.correct for r in scored), len(scored)),
        "tool_accuracy": rate(sum(r.correct for r in tool_cases), len(tool_cases)),
        "abdication_rate": rate(sum(r.majority == ABDICATED for r in tool_cases), len(tool_cases)),
        "wrong_tool_rate": rate(sum(r.majority == WRONG_TOOL for r in tool_cases), len(tool_cases)),
        "no_tool_accuracy": rate(sum(r.correct for r in text_cases), len(text_cases)),
        "unstable_cases": sum(r.unstable for r in scored),
        "by_stratum": group("stratum"),
        "by_category": group("category"),
        "by_lang": group("lang"),
    }


def mcnemar_exact_p(b: int, c: int) -> float:
    """Two-sided exact McNemar p-value from the discordant pair counts."""
    n = b + c
    if n == 0:
        return 1.0
    k = min(b, c)
    tail = sum(math.comb(n, i) for i in range(k + 1)) / 2**n
    return min(1.0, 2 * tail)


def compare(
    a: dict[str, bool],
    b: dict[str, bool],
    *,
    resamples: int = 10_000,
    seed: int = 0,
) -> dict[str, Any]:
    """Paired comparison of per-case correctness for the cases both runs scored."""
    ids = sorted(set(a) & set(b))
    if not ids:
        raise ValueError("the two runs share no scored cases")
    pairs = [(a[i], b[i]) for i in ids]
    a_only = sum(1 for x, y in pairs if x and not y)
    b_only = sum(1 for x, y in pairs if y and not x)
    diffs = [int(y) - int(x) for x, y in pairs]
    rng = random.Random(seed)
    boot = sorted(
        sum(rng.choice(diffs) for _ in diffs) / len(diffs) for _ in range(resamples)
    )
    return {
        "paired_cases": len(ids),
        "a_accuracy": sum(x for x, _ in pairs) / len(ids),
        "b_accuracy": sum(y for _, y in pairs) / len(ids),
        "diff": sum(diffs) / len(ids),
        "diff_ci95": [boot[int(0.025 * resamples)], boot[int(0.975 * resamples) - 1]],
        "a_right_b_wrong": a_only,
        "b_right_a_wrong": b_only,
        "mcnemar_p": mcnemar_exact_p(a_only, b_only),
    }
