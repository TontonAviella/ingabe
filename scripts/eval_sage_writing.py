"""Sage writing eval: does a new answer style explain better without losing a fact?

Each case (evals/sage_writing/cases.jsonl) is a real question with the real tool results Sage got back. For each
style, Sage's model writes the final answer from the live system prompt with only its <AnswerStyle> block swapped
(evals/sage_writing/styles.py). A judge (the same model, blind, random order) picks the answer the reader would
understand better and the first line that holds attention; code checks every number against the data.

    python scripts/eval_sage_writing.py gen --styles current,feynman_v1 --repeats 3 --out run.jsonl   # in the app
    python scripts/eval_sage_writing.py judge --run run.jsonl --a current --b feynman_v1 --out judged.jsonl
    python scripts/eval_sage_writing.py score --run run.jsonl --judged judged.jsonl --a current --b feynman_v1
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import random
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
CASES = ROOT / "evals" / "sage_writing" / "cases.jsonl"
MAX_TOKENS = 4096  # what the live loop asks for (message_routes _DESIRED_OUTPUT_TOKENS)
JUDGE_DATA_CHARS = 9000
TODAY = __import__("datetime").date.today().isoformat()
JARGON = re.compile(r"\b(NDVI|NDWI|BSI|anomal\w*|z-?score|CHIRPS|WaPOR|evapotranspiration|raster|pixels?|dekad\w*|"
                    r"percentile|SPI|H3|AgERA5|ensemble|phenolog\w*|iSDA\w*|reanalysis|STAC|Sentinel-?\d?\w*)\b",
                    re.IGNORECASE)
HYPE = re.compile(r"!|\b(amazing|fascinating|incredible|remarkable|exciting)\b", re.IGNORECASE)
NUMBER = re.compile(r"(?<![\w.])\d[\d,]*(?:\.\d+)?")


def load_cases() -> list[dict[str, Any]]:
    return [json.loads(line) for line in CASES.read_text().splitlines() if line.strip()]


# --- generation (inside the app container) -------------------------------------------------------------------------

def system_prompt(style: str) -> str:
    from evals.sage_writing.styles import STYLES
    from src.dependencies.system_prompt import DefaultSystemPromptProvider
    prompt = DefaultSystemPromptProvider().get_system_prompt()
    if style == "current":
        return prompt
    # The block itself starts on its own line: rule 2 of the prompt also names "<AnswerStyle>" in its text, and
    # replacing from there (as attempts 1-5 did, 2026-10-26) silently dropped rules 2-6, "never fabricate" included.
    assert prompt.count("\n<AnswerStyle>\n") == 1 and prompt.count("</AnswerStyle>") == 1
    start, end = prompt.index("\n<AnswerStyle>\n") + 1, prompt.index("</AnswerStyle>") + len("</AnswerStyle>")
    swapped = prompt[:start] + STYLES[style] + prompt[end:]
    assert len(swapped) - len(STYLES[style]) == len(prompt) - (end - start), "only the style block may change"
    return swapped


def messages_for(case: dict[str, Any], system: str) -> list[dict[str, Any]]:
    calls = [{"id": f"call_{i}", "type": "function",
              "function": {"name": t["tool"], "arguments": t.get("arguments") or "{}"}} for i, t in enumerate(case["tools"])]
    return ([{"role": "system", "content": system}, {"role": "user", "content": case["question"]},
             {"role": "assistant", "content": None, "tool_calls": calls}]
            + [{"role": "tool", "tool_call_id": f"call_{i}", "content": t["result"]} for i, t in enumerate(case["tools"])])


def client_and_model():
    import os

    from openai import AsyncOpenAI

    from src.llm_defaults import resolve_chat_endpoint
    endpoint = resolve_chat_endpoint(os.environ.get("OPENAI_MODEL"), api_key=os.environ.get("OPENAI_API_KEY"),
                                     base_url=os.environ.get("OPENAI_BASE_URL"),
                                     ollama_base_url=os.environ.get("OLLAMA_BASE_URL"))
    return AsyncOpenAI(base_url=endpoint.base_url, api_key=endpoint.api_key, max_retries=4), endpoint.model


async def gen(styles: list[str], repeats: int, out: Path, only: set[str] | None) -> None:
    client, model = client_and_model()
    cases = [c for c in load_cases() if not only or c["id"] in only]
    systems = {s: system_prompt(s) for s in styles}
    limit = asyncio.Semaphore(4)
    usage = {"in": 0, "out": 0}

    async def one(case: dict[str, Any], style: str, rep: int) -> dict[str, Any]:
        answer, tries = "", 0
        while not answer and tries < 3:  # an empty answer (the model wanted another tool) is asked again, twice
            tries += 1
            async with limit:
                r = await client.chat.completions.create(model=model, max_tokens=MAX_TOKENS,
                                                         messages=messages_for(case, systems[style]))
            if r.usage:
                usage["in"] += r.usage.prompt_tokens or 0
                usage["out"] += r.usage.completion_tokens or 0
            answer = (r.choices[0].message.content or "").strip()
        return {"case": case["id"], "style": style, "rep": rep, "model": model, "answer": answer, "tries": tries}

    rows = await asyncio.gather(*(one(c, s, k) for c in cases for s in styles for k in range(repeats)))
    with out.open("w") as f:
        for row in rows:
            f.write(json.dumps(row) + "\n")
    print(json.dumps({"generated": len(rows), "model": model, "usage": usage}))


JUDGE_PROMPT = """You compare two answers an assistant wrote for {reader}, who asked:
"{question}"

Today's date is {today}. The assistant is told to name its sources with these labels, so a source named this way is
not an unsupported claim:
{attribution}

The data the assistant had (the only facts it may use):
<data>
{data}
</data>

<answer_A>
{a}
</answer_A>

<answer_B>
{b}
</answer_B>

Judge as that reader would, not as a specialist. Reply with JSON only:
{{"understood": "A" | "B" | "tie",   // which one this reader would understand more clearly and remember
  "attention": "A" | "B" | "tie",    // whose first line makes this reader want to read on
  "unsupported": [],                 // "A" and/or "B" if that answer states something the data does not support
  "why": "one short sentence"}}"""


def reader_of(question: str) -> str:
    q = question.lower()
    if "agronomist" in q:
        return "an agronomist in Rwanda"
    if "insur" in q:
        return "an insurance officer in Rwanda"
    return "a farmer or farm manager in Rwanda (not a specialist)"


async def judge(run: Path, a: str, b: str, out: Path) -> None:
    client, model = client_and_model()
    cases = {c["id"]: c for c in load_cases()}
    rows = [json.loads(line) for line in run.read_text().splitlines() if line.strip()]
    by = {(r["case"], r["style"], r["rep"]): r["answer"] for r in rows}
    pairs = sorted({(r["case"], r["rep"]) for r in rows})
    limit = asyncio.Semaphore(4)

    async def one(case_id: str, rep: int) -> dict[str, Any]:
        case = cases[case_id]
        flip = random.Random(f"{case_id}:{rep}").random() < 0.5  # the new style is A half the time
        first, second = (by[(case_id, b, rep)], by[(case_id, a, rep)]) if flip else (by[(case_id, a, rep)], by[(case_id, b, rep)])
        data = "\n\n".join(f"[{t['tool']}]\n{t['result']}" for t in case["tools"])[:JUDGE_DATA_CHARS]
        if not first or not second:
            return {"case": case_id, "rep": rep, "flip": flip, "understood": "error", "attention": "error",
                    "unsupported": [], "why": "an answer was empty"}
        prompt = JUDGE_PROMPT.format(reader=reader_of(case["question"]), question=case["question"], data=data,
                                     a=first, b=second, today=TODAY, attribution=attribution)
        async with limit:
            r = await client.chat.completions.create(model=model, max_tokens=2048,
                                                     messages=[{"role": "user", "content": prompt}])
        text = r.choices[0].message.content or ""
        try:
            verdict = json.loads(text[text.index("{"): text.rindex("}") + 1])
        except ValueError:
            verdict = {"understood": "error", "attention": "error", "unsupported": [], "why": text[:200]}
        to_style = {"A": b if flip else a, "B": a if flip else b, "tie": "tie"}
        return {"case": case_id, "rep": rep, "flip": flip,
                "understood": to_style.get(str(verdict.get("understood")), "error"),
                "attention": to_style.get(str(verdict.get("attention")), "error"),
                "unsupported": [to_style[x] for x in verdict.get("unsupported") or [] if x in ("A", "B")],
                "why": verdict.get("why", "")}

    prompt_text = system_prompt("current")
    attribution = prompt_text[prompt_text.index("<DataAttribution>"): prompt_text.index("</DataAttribution>")]
    judged = await asyncio.gather(*(one(c, k) for c, k in pairs if c in cases))
    with out.open("w") as f:
        for row in judged:
            f.write(json.dumps(row) + "\n")
    print(json.dumps({"judged": len(judged), "model": model}))


# --- scoring (plain Python, no app needed) -------------------------------------------------------------------------

def _numbers(text: str) -> list[str]:
    return [m.group(0).rstrip(".,") for m in NUMBER.finditer(text)]


def _value(s: str) -> float:
    return float(s.replace(",", ""))


def unsupported_numbers(answer: str, sources: str) -> list[str]:
    """Numbers in the answer (its Sources line left out) that match no number in the question or the data, allowing
    for the answer's own rounding, percentages written as fractions or the other way round, and 0-2."""
    body = "\n".join(line for line in answer.splitlines() if not line.lower().startswith("sources"))
    known = [_value(n) for n in _numbers(sources.replace(",", ""))]
    # A share worked out from two numbers in the data (35 ha of plots in a 124 ha photo is 28%) is not invented.
    shares = {round(100 * x / y, 1) for x in known for y in known if 0 < x < y}
    bad = []
    for raw in _numbers(body):
        n = _value(raw)
        if n <= 2:
            continue
        decimals = len(raw.split(".")[1]) if "." in raw else 0
        tol = 0.5 * 10 ** -decimals
        if not any(abs(n - s) <= max(tol, 0.01 * abs(s)) or abs(n - s * 100) <= max(tol, 0.01 * abs(s * 100))
                   or abs(n - s / 100) <= tol for s in known) and not any(abs(n - r) <= max(tol, 0.5) for r in shares):
            bad.append(raw)
    return bad


def wilson(k: float, n: int, z: float = 1.96) -> tuple[float, float, float]:
    if n == 0:
        return 0.0, 0.0, 0.0
    p = k / n
    centre = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return p, max(0.0, centre - half), min(1.0, centre + half)


def score(run: Path, judged: Path, a: str, b: str) -> dict[str, Any]:
    cases = {c["id"]: c for c in load_cases()}
    rows = [r for r in (json.loads(line) for line in run.read_text().splitlines() if line.strip()) if r["case"] in cases]
    verdicts = [v for v in (json.loads(line) for line in judged.read_text().splitlines() if line.strip()) if v["case"] in cases]

    def source(case_id: str) -> str:
        c = cases[case_id]
        return c["question"] + "\n" + "\n".join((t.get("arguments") or "") + "\n" + t["result"] for t in c["tools"])

    per: dict[str, dict[str, Any]] = {}
    for style in (a, b):
        mine = [r for r in rows if r["style"] == style]
        words = sorted(len(r["answer"].split()) for r in mine)
        per[style] = {
            "answers": len(mine),
            "faithful": sum(not unsupported_numbers(r["answer"], source(r["case"])) for r in mine) / max(1, len(mine)),
            "median_words": words[len(words) // 2] if words else 0,
            "jargon_per_answer": sum(len(JARGON.findall(r["answer"].split("Sources")[0])) for r in mine) / max(1, len(mine)),
            "hype": sum(len(HYPE.findall(r["answer"])) for r in mine),
            "empty": sum(1 for r in mine if not r["answer"]),
        }
    valid = [v for v in verdicts if v["understood"] != "error"]
    wins_u = sum(1 for v in valid if v["understood"] == b) + 0.5 * sum(1 for v in valid if v["understood"] == "tie")
    wins_a = sum(1 for v in valid if v["attention"] == b) + 0.5 * sum(1 for v in valid if v["attention"] == "tie")
    return {"pairs": len(valid), "errors": len(verdicts) - len(valid), "understood": wilson(wins_u, len(valid)),
            "attention": wilson(wins_a, len(valid)), "styles": per,
            "judge_unsupported": {s: sum(1 for v in valid if s in v["unsupported"]) for s in (a, b)}}


def main() -> None:
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("gen")
    g.add_argument("--styles", default="current,feynman_v1")
    g.add_argument("--repeats", type=int, default=3)
    g.add_argument("--only", default="")
    g.add_argument("--out", type=Path, required=True)
    j = sub.add_parser("judge")
    s = sub.add_parser("score")
    for q in (j, s):
        q.add_argument("--run", type=Path, required=True)
        q.add_argument("--a", default="current")
        q.add_argument("--b", default="feynman_v1")
    j.add_argument("--out", type=Path, required=True)
    s.add_argument("--judged", type=Path, required=True)
    args = p.parse_args()
    if args.cmd == "gen":
        asyncio.run(gen(args.styles.split(","), args.repeats, args.out, set(filter(None, args.only.split(",")))))
    elif args.cmd == "judge":
        asyncio.run(judge(args.run, args.a, args.b, args.out))
    else:
        print(json.dumps(score(args.run, args.judged, args.a, args.b), indent=1))


if __name__ == "__main__":
    main()
