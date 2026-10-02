"""Sage routing eval: right first tool, right arguments, whole chains.

Builds each turn's model request with the live code
(src/dependencies/sage_turn_request.py, sage_routing.build_fast_tool_call /
select_fast_raster_layer, DefaultMapStateProvider), sends it to the
configured model, and scores it against evals/sage_routing/cases/*.jsonl.
See evals/sage_routing/README.md.

Commands (run inside the app container, which has the env and packages):

    python scripts/eval_sage_routing.py run --variant baseline --repeats 3
    python scripts/eval_sage_routing.py run --resume evals/sage_routing/runs/<run>.json
    python scripts/eval_sage_routing.py report evals/sage_routing/runs/<run>.json
    python scripts/eval_sage_routing.py compare <run-a>.json <run-b>.json
    python scripts/eval_sage_routing.py catalog --write   # refresh the tool snapshot
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from evals.sage_routing import scoring  # noqa: E402

EVAL_DIR = ROOT / "evals" / "sage_routing"
CASES_DIR = EVAL_DIR / "cases"
CATALOG_PATH = EVAL_DIR / "tool_catalog.json"
RUNS_DIR = EVAL_DIR / "runs"

# Same output budget the live loop asks for (message_routes
# _DESIRED_OUTPUT_TOKENS). Reasoning models spend part of it thinking.
MAX_TOKENS = 4096
RETRYABLE_STATUS = {408, 409, 429, 500, 502, 503, 504}
# Stop (and allow --resume) after this many consecutive fully-errored cases:
# usually the provider's daily request cap.
MAX_CONSECUTIVE_ERRORED_CASES = 3
DEFAULT_STUB = {"status": "success", "note": "eval stub result"}
EVAL_POSTGIS_ID = "EVALRWANDA01"

logger = logging.getLogger("eval_sage_routing")


class ProviderUnavailable(RuntimeError):
    """The model provider keeps failing; stop and resume later."""


def case_files() -> list[Path]:
    return sorted(CASES_DIR.glob("*.jsonl"))


def load_cases() -> list[dict[str, Any]]:
    return [json.loads(line) for path in case_files()
            for line in path.read_text().splitlines() if line.strip()]


def corpus_sha() -> str:
    digest = hashlib.sha256()
    for path in case_files():
        digest.update(path.name.encode() + b"\0" + path.read_bytes())
    return digest.hexdigest()[:12]


# ---------------------------------------------------------------------------
# Variants: how a turn's request differs from the live loop. "baseline" is
# exactly what Sage sends today. A variant changes the FIRST model call of a
# turn; chain follow-up steps always use the live "auto".
# ---------------------------------------------------------------------------

def _baseline_tool_choice(plan: Any) -> str | None:
    return plan.tool_choice


def _required_tool_choice(plan: Any) -> str | None:
    return "required" if plan.tools else None


VARIANTS: dict[str, Callable[[Any], str | None]] = {
    "baseline": _baseline_tool_choice,
    "tool_choice_required": _required_tool_choice,
}


def live_tool_catalog() -> dict[str, Any]:
    """Every tool name the live loop can offer, plus the fast-path-only tools."""
    from src.dependencies.pydantic_tools import get_pydantic_tool_calls
    from src.dependencies.sage_routing import FAST_PATH_TOOLS
    from src.dependencies.sage_turn_request import build_sage_tools_payload

    tools = build_sage_tools_payload(get_pydantic_tool_calls(), {})
    return {
        "model_tools": sorted(t["function"]["name"] for t in tools),
        "fast_path_tools": sorted(FAST_PATH_TOOLS),
    }


def known_tools(catalog: dict[str, Any]) -> set[str]:
    return set(catalog["model_tools"]) | set(catalog["fast_path_tools"])


# ---------------------------------------------------------------------------
# Map state: the <MapState> context the live loop sends with every turn
# ---------------------------------------------------------------------------

def _layer_id(name: str) -> str:
    return "L" + hashlib.sha1(name.encode()).hexdigest()[:11]


def map_layers(case: dict[str, Any]) -> list[dict[str, str]]:
    return [{"layer_id": _layer_id(layer["name"]), "name": layer["name"], "type": layer["type"]}
            for layer in (case.get("map_state") or {}).get("layers", [])]


def map_description(case: dict[str, Any]) -> str:
    """Same structure as postgres_routes.get_map_description, with the
    project's internal Rwanda PostGIS connection every map gets."""
    from src.routes.message_routes import (
        INTERNAL_RWANDA_ALLOWED_TABLES,
        RWANDA_INTERNAL_CONNECTION_NAME,
    )

    content = [
        f"<PostGISConnection id={EVAL_POSTGIS_ID}>",
        f'\n## PostGIS "{RWANDA_INTERNAL_CONNECTION_NAME}" (ID {EVAL_POSTGIS_ID})\n',
        "No documentation available for this database connection.",
        "\n**Available Tables:** " + ", ".join(sorted(INTERNAL_RWANDA_ALLOWED_TABLES)),
        f"</PostGISConnection id={EVAL_POSTGIS_ID}>",
        "# Map: Eval map\n",
    ]
    for layer in map_layers(case):
        content += [f"<{layer['layer_id']}>", f"# Layer: {layer['name']}\n",
                    f"ID: {layer['layer_id']}", f"Type: {layer['type']}", f"</{layer['layer_id']}>"]
    return "\n".join(content)


# ---------------------------------------------------------------------------
# Model calls
# ---------------------------------------------------------------------------

async def _complete(client: Any, kwargs: dict[str, Any], retries: int) -> Any:
    delay = 5.0
    for attempt in range(retries + 1):
        try:
            return await client.chat.completions.create(**kwargs)
        except Exception as exc:
            status = getattr(exc, "status_code", None)
            if status in RETRYABLE_STATUS and attempt < retries:
                logger.warning("retryable %s (%s); sleeping %.0fs", status, type(exc).__name__, delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 120.0)
                continue
            raise
    raise AssertionError("unreachable")


def _schema(tools: list[dict], name: str) -> dict[str, Any] | None:
    for tool in tools:
        if tool["function"]["name"] == name:
            return tool["function"].get("parameters")
    return None


async def run_attempt(
    case: dict[str, Any], plan: Any, messages: list[dict], client: Any, model: str,
    first_choice: str | None, retries: int,
) -> dict[str, Any]:
    """One attempt: the first model call, and for chain cases the full loop."""
    chain = case.get("chain")
    steps = chain["max_steps"] if chain else 1
    stubs = (chain or {}).get("stubs") or {}
    convo = list(messages)
    called: list[str] = []
    first_tool, args_ok, problems = None, None, []
    for step in range(steps):
        kwargs: dict[str, Any] = {"model": model, "messages": convo, "max_tokens": MAX_TOKENS}
        if plan.tools:
            kwargs.update(tools=plan.tools, parallel_tool_calls=False,
                          tool_choice=first_choice if step == 0 else plan.tool_choice)
        try:
            resp = await _complete(client, kwargs, retries)
        except Exception as exc:
            marker = f"{scoring.ERROR_PREFIX}{type(exc).__name__}:{getattr(exc, 'status_code', None)}>"
            if step == 0:
                return {"first_tool": marker, "args_ok": None, "args_problems": [], "tools_called": []}
            break  # a later-step failure ends the chain; what ran still counts
        message = resp.choices[0].message
        calls = getattr(message, "tool_calls", None) or []
        if not calls:
            if step == 0:
                first_tool = scoring.TEXT_ONLY
            break
        call = calls[0]
        called.append(call.function.name)
        if step == 0:
            first_tool = call.function.name
            if first_tool in (case["expect"].get("any_of") or []):
                problems = scoring.check_arguments(
                    _schema(plan.tools, first_tool), call.function.arguments,
                    (case["expect"].get("args") or {}).get(first_tool),
                )
                args_ok = not problems
        if not chain:
            break
        convo.append({"role": "assistant", "content": message.content, "tool_calls": [{
            "id": call.id, "type": "function",
            "function": {"name": call.function.name, "arguments": call.function.arguments},
        }]})
        convo.append({"role": "tool", "tool_call_id": call.id,
                      "content": json.dumps(stubs.get(call.function.name, DEFAULT_STUB))})
    return {"first_tool": first_tool, "args_ok": args_ok, "args_problems": problems,
            "tools_called": called}


def fast_path_attempt(case: dict[str, Any], fast: Any) -> dict[str, Any]:
    expected = (case["expect"].get("args") or {}).get(fast.tool_name)
    in_any_of = fast.tool_name in (case["expect"].get("any_of") or [])
    problems = (scoring.check_arguments(None, json.dumps(fast.arguments), expected)
                if in_any_of else [])
    return {"first_tool": fast.tool_name, "args_ok": (not problems) if in_any_of else None,
            "args_problems": problems, "tools_called": [fast.tool_name]}


def fast_path_applies(fast: Any, text: str, case: dict[str, Any]) -> bool:
    """Mirror message_routes' fall-through: raster fast paths need a matching
    raster on the map; the admin-boundary path needs the map's project, which
    every map has."""
    from src.dependencies.sage_routing import (
        ADMIN_BOUNDARY_TOOL,
        FAST_PATH_TOOLS,
        select_fast_raster_layer,
    )

    if fast is None or fast.tool_name not in FAST_PATH_TOOLS:
        return False
    if fast.tool_name == ADMIN_BOUNDARY_TOOL:
        return True
    rasters = [layer for layer in map_layers(case) if layer["type"] == "raster"]
    return select_fast_raster_layer(text, rasters) is not None


async def run(args: argparse.Namespace) -> Path:
    from src.dependencies.map_state import DefaultMapStateProvider
    from src.dependencies.pydantic_tools import get_pydantic_tool_calls
    from src.dependencies.sage_routing import build_fast_tool_call
    from src.dependencies.sage_turn_request import build_sage_tools_payload, plan_sage_turn
    from src.dependencies.system_prompt import get_system_prompt_provider
    from src.utils import get_chat_client_for_model

    cases = load_cases()
    catalog = live_tool_catalog()
    scoring.validate_corpus(cases, known_tools(catalog))
    if args.case:
        cases = [c for c in cases if c["id"] in set(args.case)]
    if args.kind:
        cases = [c for c in cases if scoring.case_kind(c) in set(args.kind)]
    if args.limit:
        cases = cases[: args.limit]

    meta = {
        "variant": args.variant, "repeats": args.repeats, "model_only": args.model_only,
        "model": os.environ.get("OPENAI_MODEL", ""), "corpus_sha": corpus_sha(),
        "git_sha": _git_sha(), "started_at": datetime.now(timezone.utc).isoformat(),
    }
    records: list[dict[str, Any]] = []
    if args.resume:
        out_path = Path(args.resume)
        previous = json.loads(out_path.read_text())
        meta = previous["meta"]
        if meta["corpus_sha"] != corpus_sha():
            raise SystemExit("cannot resume: the corpus changed since this run started")
        args.variant, args.repeats, args.model_only = meta["variant"], meta["repeats"], meta["model_only"]
        records = previous["cases"]
    else:
        RUNS_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        out_path = Path(args.out) if args.out else RUNS_DIR / f"{stamp}-{args.variant}.json"
    done = {r["id"] for r in records}

    choose = VARIANTS[args.variant]
    tools_payload = build_sage_tools_payload(get_pydantic_tool_calls(), {})
    prompt_provider = get_system_prompt_provider()
    map_provider = DefaultMapStateProvider()
    default_model = os.environ.get("OPENAI_MODEL", "")
    errored_streak = 0

    todo = [c for c in cases if c["id"] not in done]
    for i, case in enumerate(todo, 1):
        text = case["text"]
        history = case.get("history") or []
        user_msg = {"role": "user", "content": text}
        fast = None if args.model_only else build_fast_tool_call(text)
        if fast_path_applies(fast, text, case):
            source, model, tools_sent = "fast_path", None, 0
            attempts = [fast_path_attempt(case, fast)] * args.repeats
        else:
            plan = plan_sage_turn(text, history + [user_msg], tools_payload,
                                  prompt_provider.get_system_prompt)
            client, model = get_chat_client_for_model(None, plan.model_override or default_model)
            map_msgs = await map_provider.get_system_messages(
                history + [user_msg], map_description(case), None, None)
            messages = [{"role": "system", "content": plan.system_prompt}, *history,
                        *map_msgs, user_msg]
            source, tools_sent, attempts = "model", len(plan.tools), []
            for _ in range(args.repeats):
                attempts.append(await run_attempt(
                    case, plan, messages, client, model, choose(plan), args.retries))
                if args.pace:
                    await asyncio.sleep(args.pace)
        result = scoring.score_case(case, attempts)
        records.append({"id": case["id"], "source": source, "model": model,
                        "tools_sent": tools_sent, "attempts": attempts,
                        "outcomes": list(result.outcomes), "majority": result.majority})
        logger.info("[%3d/%d] %-10s args=%-5s chain=%-5s %-9s %-26s %s", i, len(todo),
                    result.majority, result.full_correct, result.chain_ok, source,
                    ",".join(sorted({a["first_tool"] for a in attempts}))[:26], case["id"])
        _write(out_path, meta, cases, records)
        errored_streak = errored_streak + 1 if result.majority == scoring.ERROR else 0
        if errored_streak >= MAX_CONSECUTIVE_ERRORED_CASES:
            logger.error("%d consecutive cases failed (likely the provider's daily cap); "
                         "stopping. Resume later with: run --resume %s", errored_streak, out_path)
            for _ in range(errored_streak):  # don't keep the errored rows; resume retries them
                records.pop()
            _write(out_path, meta, cases, records)
            raise ProviderUnavailable(str(out_path))
    return out_path


def _git_sha() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT,
                              capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return os.environ.get("GIT_SHA", "unknown")


def _results(cases: list[dict], records: list[dict]) -> list[scoring.CaseResult]:
    by_id = {c["id"]: c for c in cases}
    return [scoring.score_case(by_id[r["id"]], r["attempts"]) for r in records if r["id"] in by_id]


def _write(path: Path, meta: dict, cases: list[dict], records: list[dict]) -> None:
    meta = {**meta, "written_at": datetime.now(timezone.utc).isoformat(),
            "fast_path_cases": sum(r["source"] == "fast_path" for r in records)}
    payload = {"meta": meta, "summary": scoring.summarize(_results(cases, records)), "cases": records}
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")


def _pct(r: dict[str, Any]) -> str:
    if not r["n"]:
        return "n/a"
    lo, hi = r["ci95"]
    return f"{r['rate'] * 100:5.1f}%  [{lo * 100:4.1f}-{hi * 100:4.1f}]  ({r['k']}/{r['n']})"


def report(path: Path) -> None:
    data = json.loads(path.read_text())
    meta, s = data["meta"], data["summary"]
    print(f"run       {path.name}")
    print(f"variant   {meta['variant']}  repeats={meta['repeats']}  model={meta['model']}  "
          f"corpus={meta['corpus_sha']}  git={meta['git_sha']}  fast_path={meta.get('fast_path_cases')}")
    print(f"cases     {s['cases']}  intents={s.get('intents')}  errored={s['errored_cases']}  "
          f"unstable={s['unstable_cases']}")
    for key in ("accuracy", "tool_accuracy", "tool_and_args_accuracy", "abdication_rate",
                "wrong_tool_rate", "no_tool_accuracy", "chain_completion"):
        if key in s:
            print(f"{key:<23} {_pct(s[key])}")
    for group in ("by_kind", "by_stratum", "by_lang", "by_category"):
        print(f"-- {group}")
        for name, r in s.get(group, {}).items():
            print(f"   {name:<15} {_pct(r)}")


def compare_runs(a_path: Path, b_path: Path) -> None:
    a, b = (json.loads(p.read_text()) for p in (a_path, b_path))
    if a["meta"]["corpus_sha"] != b["meta"]["corpus_sha"]:
        print("WARNING: runs used different corpus versions; only shared case ids are paired")
    clusters = {c["id"]: c.get("intent", c["id"]) for c in load_cases()}

    def correct(run: dict) -> dict[str, bool]:
        return {r["id"]: r["majority"] == scoring.CORRECT
                for r in run["cases"] if r["majority"] != scoring.ERROR}

    c = scoring.compare(correct(a), correct(b), clusters=clusters)
    print(f"A {a['meta']['variant']} ({a_path.name})  vs  B {b['meta']['variant']} ({b_path.name})")
    print(f"paired cases      {c['paired_cases']}  (intent clusters: {c['clusters']})")
    print(f"accuracy A -> B   {c['a_accuracy'] * 100:.1f}% -> {c['b_accuracy'] * 100:.1f}%")
    lo, hi = c["diff_ci95"]
    print(f"difference        {c['diff'] * 100:+.1f} pp  (95% CI by intent {lo * 100:+.1f} to {hi * 100:+.1f})")
    print(f"discordant        B fixed {c['b_right_a_wrong']}, B broke {c['a_right_b_wrong']}  "
          f"McNemar p={c['mcnemar_p']:.3f}")
    verdict = "significant" if c["mcnemar_p"] < 0.05 and (lo > 0 or hi < 0) else "NOT significant"
    print(f"verdict           {verdict} at 95%")
    for key in ("abdication_rate", "tool_and_args_accuracy", "chain_completion"):
        ra, rb = a["summary"].get(key), b["summary"].get(key)
        if ra and rb and ra["n"] and rb["n"]:
            print(f"{key:<17} {ra['rate'] * 100:.1f}% -> {rb['rate'] * 100:.1f}%  (unpaired, for context)")


def catalog_cmd(args: argparse.Namespace) -> int:
    live = live_tool_catalog()
    if args.write:
        CATALOG_PATH.write_text(json.dumps(live, indent=2) + "\n")
        print(f"wrote {CATALOG_PATH} ({len(live['model_tools'])} model tools)")
        return 0
    snap = json.loads(CATALOG_PATH.read_text())
    if snap != live:
        added = sorted(set(live["model_tools"]) - set(snap["model_tools"]))
        removed = sorted(set(snap["model_tools"]) - set(live["model_tools"]))
        print(f"tool catalog drifted: added={added} removed={removed}; "
              "update the case labels, then run `catalog --write`")
        return 1
    print("tool catalog matches the snapshot")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_run = sub.add_parser("run", help="call the model on the cases")
    p_run.add_argument("--variant", choices=sorted(VARIANTS), default="baseline")
    p_run.add_argument("--repeats", type=int, default=3)
    p_run.add_argument("--pace", type=float, default=2.0, help="seconds between model calls")
    p_run.add_argument("--retries", type=int, default=6)
    p_run.add_argument("--model-only", action="store_true",
                       help="skip deterministic fast paths; measure the model on every case")
    p_run.add_argument("--case", action="append", help="run only this case id (repeatable)")
    p_run.add_argument("--kind", action="append", choices=scoring.KINDS)
    p_run.add_argument("--limit", type=int, default=0)
    p_run.add_argument("--out")
    p_run.add_argument("--resume", help="continue an interrupted run file")
    sub.add_parser("report").add_argument("run_file", type=Path)
    p_cmp = sub.add_parser("compare")
    p_cmp.add_argument("run_a", type=Path)
    p_cmp.add_argument("run_b", type=Path)
    sub.add_parser("catalog").add_argument("--write", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    if args.cmd == "run":
        try:
            path = asyncio.run(run(args))
        except ProviderUnavailable as stopped:
            print(f"\nstopped early; resume with: run --resume {stopped}")
            return 3
        print()
        report(path)
        return 0
    if args.cmd == "report":
        report(args.run_file)
        return 0
    if args.cmd == "compare":
        compare_runs(args.run_a, args.run_b)
        return 0
    return catalog_cmd(args)


if __name__ == "__main__":
    raise SystemExit(main())
