"""Scoring and statistics for the Sage routing eval. Stdlib only.

Each case expects either a first tool from an accepted set (``any_of``) or a
plain-text answer (``no_tool``). Optionally it also expects argument values
(``expect.args``) and, for multi-step cases, a set of tools the run must call
within a step budget (``chain``). Each case is run ``repeats`` times; every
attempt is classified, and the case is scored on the majority of its
non-error attempts. Rates carry 95% Wilson intervals. Two runs are compared
case-by-case with an exact McNemar test and a bootstrap interval that
resamples whole intents, so paraphrases of one request are not counted as
independent evidence.
"""
from __future__ import annotations

import json
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
KINDS = ("single", "multi_turn", "chain")
LAYER_TYPES = ("raster", "vector", "postgis", "point_cloud")
_JSON_TYPES = {
    "string": str, "integer": int, "number": (int, float), "boolean": bool,
    "array": list, "object": dict,
}


class CorpusError(ValueError):
    """A corpus row is malformed."""


# ---------------------------------------------------------------------------
# Corpus validation
# ---------------------------------------------------------------------------

def case_kind(case: dict[str, Any]) -> str:
    if case.get("chain"):
        return "chain"
    if case.get("history"):
        return "multi_turn"
    return "single"


def validate_case(case: dict[str, Any], known_tools: set[str] | None = None) -> None:
    """Raise CorpusError if a corpus row is malformed or names unknown tools."""
    cid = case.get("id", "?")
    for key in ("id", "intent", "text", "category", "stratum", "lang", "expect"):
        if key not in case:
            raise CorpusError(f"{cid}: missing {key!r}")
    if case["stratum"] not in STRATA:
        raise CorpusError(f"{cid}: stratum must be one of {STRATA}")
    if case["lang"] not in LANGS:
        raise CorpusError(f"{cid}: lang must be one of {LANGS}")
    expect = case["expect"]
    any_of = expect.get("any_of")
    no_tool = expect.get("no_tool") is True
    if bool(any_of) == no_tool:
        raise CorpusError(f"{cid}: expect needs exactly one of any_of / no_tool")

    def check_tools(names: Any, where: str) -> None:
        if not isinstance(names, list) or not names or not all(isinstance(t, str) and t for t in names):
            raise CorpusError(f"{cid}: {where} must be a non-empty list of tool names")
        if known_tools is not None:
            unknown = sorted(set(names) - known_tools)
            if unknown:
                raise CorpusError(f"{cid}: unknown tools {unknown} in {where}")

    if any_of is not None:
        check_tools(any_of, "any_of")
    for tool, params in (expect.get("args") or {}).items():
        if not any_of or tool not in any_of:
            raise CorpusError(f"{cid}: expect.args names {tool!r}, which is not in any_of")
        if not isinstance(params, dict) or not params:
            raise CorpusError(f"{cid}: expect.args[{tool!r}] must map params to expected text")

    for i, msg in enumerate(case.get("history") or []):
        if msg.get("role") not in ("user", "assistant", "tool"):
            raise CorpusError(f"{cid}: history[{i}] has an invalid role")
    if case.get("history") and case["history"][-1]["role"] == "user":
        raise CorpusError(f"{cid}: history must end before the current user turn")

    for layer in (case.get("map_state") or {}).get("layers", []):
        if not layer.get("name") or layer.get("type") not in LAYER_TYPES:
            raise CorpusError(f"{cid}: map_state layers need a name and a type in {LAYER_TYPES}")

    chain = case.get("chain")
    if chain is not None:
        check_tools(chain.get("must_call"), "chain.must_call")
        if not isinstance(chain.get("max_steps"), int) or chain["max_steps"] < len(chain["must_call"]):
            raise CorpusError(f"{cid}: chain.max_steps must be an int >= len(must_call)")
        stubs = chain.get("stubs") or {}
        if known_tools is not None and set(stubs) - known_tools:
            raise CorpusError(f"{cid}: chain.stubs names unknown tools {sorted(set(stubs) - known_tools)}")


def validate_corpus(cases: list[dict[str, Any]], known_tools: set[str] | None = None) -> None:
    ids = Counter(c.get("id") for c in cases)
    dupes = sorted(i for i, n in ids.items() if n > 1)
    if dupes:
        raise CorpusError(f"duplicate case ids: {dupes}")
    for case in cases:
        validate_case(case, known_tools)


# ---------------------------------------------------------------------------
# Arguments
# ---------------------------------------------------------------------------

def check_arguments(
    parameters: dict[str, Any] | None,
    arguments_json: str,
    expected: dict[str, Any] | None = None,
) -> list[str]:
    """Problems with one tool call's arguments; empty means they look right.

    Checks that the arguments parse as a JSON object, include every required
    parameter, match declared basic types and enums, and contain the expected
    text for each parameter in ``expected`` (case-insensitive substring; a list
    means any of them). It is a pre-execution sanity check, not a full JSON
    Schema validator.
    """
    try:
        args = json.loads(arguments_json or "{}")
    except json.JSONDecodeError:
        return ["arguments are not valid JSON"]
    if not isinstance(args, dict):
        return ["arguments are not a JSON object"]
    problems: list[str] = []
    params = parameters or {}
    props = params.get("properties") or {}
    for name in params.get("required") or []:
        if name not in args:
            problems.append(f"missing required {name!r}")
    for name, value in args.items():
        spec = props.get(name) or {}
        declared = spec.get("type")
        types = declared if isinstance(declared, list) else [declared] if declared else []
        allowed = tuple(t for n in types if n != "null" for t in _iter_types(n))
        if value is None and "null" in types:
            continue
        if allowed:
            # bool is an int subclass; only accept it where boolean is declared.
            ok = isinstance(value, allowed) and (not isinstance(value, bool) or bool in allowed)
            if not ok:
                problems.append(f"{name!r} should be {declared}")
        if "enum" in spec and value not in spec["enum"]:
            problems.append(f"{name!r} not in its enum")
    for name, want in (expected or {}).items():
        options = want if isinstance(want, list) else [want]
        got = json.dumps(args.get(name, ""), ensure_ascii=False).lower()
        if name not in args or not any(str(o).lower() in got for o in options):
            problems.append(f"{name!r} should mention {options}")
    return problems


def _iter_types(name: str) -> tuple[type, ...]:
    t = _JSON_TYPES.get(name)
    if t is None:
        return ()
    return t if isinstance(t, tuple) else (t,)


# ---------------------------------------------------------------------------
# Attempts and cases
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Attempt:
    """One model run of a case."""

    first_tool: str
    # None when not checked (text answer, wrong tool, or no schema available).
    args_ok: bool | None = None
    tools_called: tuple[str, ...] = ()

    @classmethod
    def of(cls, value: "Attempt | str | dict[str, Any]") -> "Attempt":
        if isinstance(value, Attempt):
            return value
        if isinstance(value, str):
            return cls(first_tool=value, tools_called=(value,) if _is_tool(value) else ())
        return cls(
            first_tool=value["first_tool"],
            args_ok=value.get("args_ok"),
            tools_called=tuple(value.get("tools_called") or ()),
        )


def _is_tool(first_tool: str) -> bool:
    return first_tool != TEXT_ONLY and not first_tool.startswith(ERROR_PREFIX)


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
    intent: str
    kind: str
    category: str
    stratum: str
    lang: str
    expects_tool: bool
    outcomes: tuple[str, ...]  # one per attempt
    majority: str  # majority outcome over non-error attempts; ERROR if none
    unstable: bool  # non-error attempts disagree on correctness
    full_correct: bool  # majority of attempts had the right tool AND right arguments
    chain_ok: bool | None  # chain cases: majority called every must_call tool

    @property
    def correct(self) -> bool:
        return self.majority == CORRECT


def _majority(flags: list[bool]) -> bool:
    """True only on a strict majority; a tie is not a pass."""
    return sum(flags) * 2 > len(flags)


def score_case(case: dict[str, Any], attempts: Iterable["Attempt | str | dict[str, Any]"]) -> CaseResult:
    attempts = [Attempt.of(a) for a in attempts]
    outcomes = tuple(classify_attempt(case["expect"], a.first_tool) for a in attempts)
    valid = [(o, a) for o, a in zip(outcomes, attempts) if o != ERROR]
    if not valid:
        majority = ERROR
    else:
        counts = Counter(o for o, _ in valid)
        top = max(counts.values())
        winners = sorted(o for o, n in counts.items() if n == top)
        # A tie between correct and anything else is not a correct majority.
        majority = winners[0] if len(winners) == 1 else next(o for o in winners if o != CORRECT)
    full = _majority([o == CORRECT and a.args_ok is not False for o, a in valid]) if valid else False
    chain = case.get("chain")
    chain_ok = None
    if chain and valid:
        must = set(chain["must_call"])
        chain_ok = _majority([must <= set(a.tools_called) for _, a in valid])
    return CaseResult(
        case_id=case["id"],
        intent=case.get("intent", case["id"]),
        kind=case_kind(case),
        category=case["category"],
        stratum=case["stratum"],
        lang=case["lang"],
        expects_tool=not case["expect"].get("no_tool", False),
        outcomes=outcomes,
        majority=majority,
        unstable=len({o == CORRECT for o, _ in valid}) > 1,
        full_correct=full and majority == CORRECT,
        chain_ok=chain_ok,
    )


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------

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
    chain_cases = [r for r in scored if r.chain_ok is not None]

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
        "intents": len({r.intent for r in scored}),
        "errored_cases": len(results) - len(scored),
        "accuracy": rate(sum(r.correct for r in scored), len(scored)),
        "tool_accuracy": rate(sum(r.correct for r in tool_cases), len(tool_cases)),
        "tool_and_args_accuracy": rate(sum(r.full_correct for r in tool_cases), len(tool_cases)),
        "abdication_rate": rate(sum(r.majority == ABDICATED for r in tool_cases), len(tool_cases)),
        "wrong_tool_rate": rate(sum(r.majority == WRONG_TOOL for r in tool_cases), len(tool_cases)),
        "no_tool_accuracy": rate(sum(r.correct for r in text_cases), len(text_cases)),
        "chain_completion": rate(sum(bool(r.chain_ok) for r in chain_cases), len(chain_cases)),
        "unstable_cases": sum(r.unstable for r in scored),
        "by_kind": group("kind"),
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
    clusters: dict[str, str] | None = None,
    resamples: int = 10_000,
    seed: int = 0,
) -> dict[str, Any]:
    """Paired comparison of per-case correctness for the cases both runs scored.

    ``clusters`` maps case id -> intent. The bootstrap resamples whole intents
    (cases without a cluster are their own), which widens the interval when
    paraphrases of one request move together.
    """
    ids = sorted(set(a) & set(b))
    if not ids:
        raise ValueError("the two runs share no scored cases")
    pairs = {i: (a[i], b[i]) for i in ids}
    a_only = sum(1 for x, y in pairs.values() if x and not y)
    b_only = sum(1 for x, y in pairs.values() if y and not x)
    groups: dict[str, list[int]] = defaultdict(list)
    for i in ids:
        x, y = pairs[i]
        groups[(clusters or {}).get(i, i)].append(int(y) - int(x))
    cluster_list = list(groups.values())
    rng = random.Random(seed)
    boot = []
    for _ in range(resamples):
        draw = [rng.choice(cluster_list) for _ in cluster_list]
        n = sum(len(g) for g in draw)
        boot.append(sum(sum(g) for g in draw) / n)
    boot.sort()
    diffs = [y - x for x, y in ((int(p), int(q)) for p, q in pairs.values())]
    return {
        "paired_cases": len(ids),
        "clusters": len(cluster_list),
        "a_accuracy": sum(x for x, _ in pairs.values()) / len(ids),
        "b_accuracy": sum(y for _, y in pairs.values()) / len(ids),
        "diff": sum(diffs) / len(ids),
        "diff_ci95": [boot[int(0.025 * resamples)], boot[int(0.975 * resamples) - 1]],
        "a_right_b_wrong": a_only,
        "b_right_a_wrong": b_only,
        "mcnemar_p": mcnemar_exact_p(a_only, b_only),
    }
