"""Sage routing eval: does Sage pick the right first tool (or answer in text)?

Builds each turn's model request with the live code
(src/dependencies/sage_turn_request.py, sage_routing.build_fast_tool_call),
sends it to the configured model, and scores the first tool call against
evals/sage_routing/corpus.jsonl. See evals/sage_routing/README.md.

Commands (run inside the app container, which has the env and packages):

    python scripts/eval_sage_routing.py run --variant baseline --repeats 3
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
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from evals.sage_routing import scoring  # noqa: E402

EVAL_DIR = ROOT / "evals" / "sage_routing"
CORPUS_PATH = EVAL_DIR / "corpus.jsonl"
CATALOG_PATH = EVAL_DIR / "tool_catalog.json"
RUNS_DIR = EVAL_DIR / "runs"

# Same output budget the live loop asks for (message_routes
# _DESIRED_OUTPUT_TOKENS). Reasoning models spend part of it thinking.
MAX_TOKENS = 4096
RETRYABLE_STATUS = {408, 409, 429, 500, 502, 503, 504}

logger = logging.getLogger("eval_sage_routing")


def load_corpus(path: Path = CORPUS_PATH) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def corpus_sha(path: Path = CORPUS_PATH) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:12]


# ---------------------------------------------------------------------------
# Variants: how a turn's request differs from the live loop. "baseline" is
# exactly what Sage sends today; later harness steps add their own entry.
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


async def _first_tool(client: Any, model: str, messages: list[dict], tools: list[dict],
                      tool_choice: str | None, retries: int) -> str:
    kwargs: dict[str, Any] = {"model": model, "messages": messages, "max_tokens": MAX_TOKENS}
    if tools:
        kwargs.update(tools=tools, tool_choice=tool_choice, parallel_tool_calls=False)
    delay = 5.0
    for attempt in range(retries + 1):
        try:
            resp = await client.chat.completions.create(**kwargs)
            calls = getattr(resp.choices[0].message, "tool_calls", None) or []
            return calls[0].function.name if calls else scoring.TEXT_ONLY
        except Exception as exc:  # recorded as an error outcome, never as a score
            status = getattr(exc, "status_code", None)
            if status in RETRYABLE_STATUS and attempt < retries:
                logger.warning("retryable %s (%s); sleeping %.0fs", status, type(exc).__name__, delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 120.0)
                continue
            return f"{scoring.ERROR_PREFIX}{type(exc).__name__}:{status}>"
    raise AssertionError("unreachable")


async def run(args: argparse.Namespace) -> Path:
    from src.dependencies.pydantic_tools import get_pydantic_tool_calls
    from src.dependencies.sage_routing import FAST_PATH_TOOLS, build_fast_tool_call
    from src.dependencies.sage_turn_request import build_sage_tools_payload, plan_sage_turn
    from src.dependencies.system_prompt import get_system_prompt_provider
    from src.utils import get_chat_client_for_model

    cases = load_corpus()
    catalog = live_tool_catalog()
    scoring.validate_corpus(cases, known_tools(catalog))
    if args.case:
        cases = [c for c in cases if c["id"] in set(args.case)]
    if args.limit:
        cases = cases[: args.limit]

    choose = VARIANTS[args.variant]
    tools_payload = build_sage_tools_payload(get_pydantic_tool_calls(), {})
    prompt_provider = get_system_prompt_provider()
    default_model = os.environ.get("OPENAI_MODEL", "")

    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_path = Path(args.out) if args.out else RUNS_DIR / f"{stamp}-{args.variant}.json"

    records: list[dict[str, Any]] = []
    for i, case in enumerate(cases, 1):
        text = case["text"]
        fast = build_fast_tool_call(text)
        if fast and fast.tool_name in FAST_PATH_TOOLS and not args.model_only:
            # message_routes runs these without a model call (assuming the map
            # state they need exists - see README "Limits").
            source, firsts, tools_sent, model = "fast_path", [fast.tool_name] * args.repeats, 0, None
        else:
            plan = plan_sage_turn(
                text, [{"role": "user", "content": text}], tools_payload,
                prompt_provider.get_system_prompt,
            )
            client, model = get_chat_client_for_model(None, plan.model_override or default_model)
            messages = [
                {"role": "system", "content": plan.system_prompt},
                {"role": "user", "content": text},
            ]
            source, tools_sent, firsts = "model", len(plan.tools), []
            for _ in range(args.repeats):
                firsts.append(await _first_tool(
                    client, model, messages, plan.tools, choose(plan), args.retries))
                if args.pace:
                    await asyncio.sleep(args.pace)
        result = scoring.score_case(case, firsts)
        records.append({
            "id": case["id"], "source": source, "model": model, "tools_sent": tools_sent,
            "first_tools": firsts, "outcomes": list(result.outcomes), "majority": result.majority,
        })
        logger.info("[%3d/%d] %-8s %-14s %-28s %s", i, len(cases), result.majority,
                    source, ",".join(sorted(set(firsts)))[:28], case["id"])
        _write(out_path, args, cases, records)
    return out_path


def _git_sha() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT,
                              capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return os.environ.get("GIT_SHA", "unknown")


def _write(path: Path, args: argparse.Namespace, cases: list[dict], records: list[dict]) -> None:
    by_id = {c["id"]: c for c in cases}
    results = [scoring.score_case(by_id[r["id"]], r["first_tools"]) for r in records]
    payload = {
        "meta": {
            "variant": args.variant, "repeats": args.repeats, "model_only": args.model_only,
            "model": os.environ.get("OPENAI_MODEL", ""), "corpus_sha": corpus_sha(),
            "git_sha": _git_sha(), "written_at": datetime.now(timezone.utc).isoformat(),
            "fast_path_cases": sum(r["source"] == "fast_path" for r in records),
        },
        "summary": scoring.summarize(results),
        "cases": records,
    }
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
          f"corpus={meta['corpus_sha']}  git={meta['git_sha']}  fast_path={meta['fast_path_cases']}")
    print(f"cases     {s['cases']}  errored={s['errored_cases']}  unstable={s['unstable_cases']}")
    for key in ("accuracy", "tool_accuracy", "abdication_rate", "wrong_tool_rate", "no_tool_accuracy"):
        print(f"{key:<17} {_pct(s[key])}")
    for group in ("by_stratum", "by_lang", "by_category"):
        print(f"-- {group}")
        for name, r in s[group].items():
            print(f"   {name:<15} {_pct(r)}")


def compare_runs(a_path: Path, b_path: Path) -> None:
    a, b = (json.loads(p.read_text()) for p in (a_path, b_path))
    if a["meta"]["corpus_sha"] != b["meta"]["corpus_sha"]:
        print("WARNING: runs used different corpus versions; only shared case ids are paired")
    def correct(run: dict) -> dict[str, bool]:
        return {r["id"]: r["majority"] == scoring.CORRECT
                for r in run["cases"] if r["majority"] != scoring.ERROR}
    c = scoring.compare(correct(a), correct(b))
    print(f"A {a['meta']['variant']} ({a_path.name})  vs  B {b['meta']['variant']} ({b_path.name})")
    print(f"paired cases      {c['paired_cases']}")
    print(f"accuracy A -> B   {c['a_accuracy'] * 100:.1f}% -> {c['b_accuracy'] * 100:.1f}%")
    lo, hi = c["diff_ci95"]
    print(f"difference        {c['diff'] * 100:+.1f} pp  (95% CI {lo * 100:+.1f} to {hi * 100:+.1f})")
    print(f"discordant        B fixed {c['b_right_a_wrong']}, B broke {c['a_right_b_wrong']}  "
          f"McNemar p={c['mcnemar_p']:.3f}")
    verdict = "significant" if c["mcnemar_p"] < 0.05 and (lo > 0 or hi < 0) else "NOT significant"
    print(f"verdict           {verdict} at 95%")


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
              "update the corpus labels, then run `catalog --write`")
        return 1
    print("tool catalog matches the snapshot")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_run = sub.add_parser("run", help="call the model on the corpus")
    p_run.add_argument("--variant", choices=sorted(VARIANTS), default="baseline")
    p_run.add_argument("--repeats", type=int, default=3)
    p_run.add_argument("--pace", type=float, default=2.0, help="seconds between model calls")
    p_run.add_argument("--retries", type=int, default=6)
    p_run.add_argument("--model-only", action="store_true",
                       help="skip deterministic fast paths; measure the model on every case")
    p_run.add_argument("--case", action="append", help="run only this case id (repeatable)")
    p_run.add_argument("--limit", type=int, default=0)
    p_run.add_argument("--out")
    sub.add_parser("report").add_argument("run_file", type=Path)
    p_cmp = sub.add_parser("compare")
    p_cmp.add_argument("run_a", type=Path)
    p_cmp.add_argument("run_b", type=Path)
    sub.add_parser("catalog").add_argument("--write", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    if args.cmd == "run":
        path = asyncio.run(run(args))
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
