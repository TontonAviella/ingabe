#!/usr/bin/env python3
"""Smoke-test the configured Sage/Hermes brain model.

The script prints JSON lines and never prints API keys. It validates both a
plain chat response and, unless disabled, OpenAI-compatible tool calling.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

from starlette.requests import Request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.utils import get_chat_client_for_model


def _load_env(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key, value)


def _print(payload: dict[str, object]) -> None:
    print(json.dumps(payload, sort_keys=True))


async def _plain_chat(client, model: str) -> bool:
    started = time.perf_counter()
    try:
        resp = await client.chat.completions.create(
            model=model,
            messages=[
                {
                    "role": "system",
                    "content": "You are a concise agriculture operations assistant.",
                },
                {"role": "user", "content": "Reply with exactly: BRAIN_MODEL_OK"},
            ],
            temperature=0,
            max_tokens=200,
        )
    except Exception as exc:
        _print(
            {
                "plain_chat_ok": False,
                "latency_ms": round((time.perf_counter() - started) * 1000, 1),
                "error_type": type(exc).__name__,
                "error": str(exc)[:500],
            }
        )
        return False

    if not resp.choices:
        _print(
            {
                "plain_chat_ok": False,
                "latency_ms": round((time.perf_counter() - started) * 1000, 1),
                "error": "chat response contained no choices",
            }
        )
        return False

    choice = resp.choices[0]
    content = (choice.message.content or "").strip()
    ok = content == "BRAIN_MODEL_OK"
    _print(
        {
            "plain_chat_ok": ok,
            "latency_ms": round((time.perf_counter() - started) * 1000, 1),
            "content": content,
            "finish_reason": choice.finish_reason,
        }
    )
    return ok


async def _tool_call(client, model: str, *, index: int = 1) -> bool:
    started = time.perf_counter()
    try:
        resp = await client.chat.completions.create(
            model=model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You must call report_field_status exactly once, not answer in normal text. "
                        "The tool has three required arguments: field_name, risk, and action. "
                        "Extract all three from the user text and do not omit any required argument."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        "Report Kabarama field status: rain risk high, "
                        "action scout drainage today."
                    ),
                },
            ],
            tools=[
                {
                    "type": "function",
                    "function": {
                        "name": "report_field_status",
                        "description": (
                            "Report field risk status for Hermes. Always include field_name, "
                            "risk, and action."
                        ),
                        "strict": True,
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "field_name": {
                                    "type": "string",
                                    "description": (
                                        "Required. Exact field or farm name from the user, "
                                        "e.g. Kabarama."
                                    ),
                                },
                                "risk": {
                                    "type": "string",
                                    "description": (
                                        "Required. Risk phrase from the user, e.g. rain risk high."
                                    ),
                                },
                                "action": {
                                    "type": "string",
                                    "description": (
                                        "Required. Recommended action phrase from the user, "
                                        "e.g. scout drainage today."
                                    ),
                                },
                            },
                            "required": ["field_name", "risk", "action"],
                            "additionalProperties": False,
                        },
                    },
                }
            ],
            tool_choice={"type": "function", "function": {"name": "report_field_status"}},
            temperature=0,
            max_tokens=120,
        )
    except Exception as exc:
        _print(
            {
                "tool_call_ok": False,
                "latency_ms": round((time.perf_counter() - started) * 1000, 1),
                "error_type": type(exc).__name__,
                "error": str(exc)[:500],
            }
        )
        return False

    msg = resp.choices[0].message
    calls = msg.tool_calls or []
    args_raw = calls[0].function.arguments if calls else ""
    try:
        args = json.loads(args_raw) if args_raw else {}
    except json.JSONDecodeError:
        args = {}
    field_name = str(args.get("field_name", "")).strip().lower() if isinstance(args, dict) else ""
    risk = str(args.get("risk", "")).strip().lower() if isinstance(args, dict) else ""
    action = str(args.get("action", "")).strip().lower() if isinstance(args, dict) else ""
    args_ok = (
        field_name == "kabarama"
        and "high" in risk
        and "scout" in action
        and "drainage" in action
        and "today" in action
    )
    _print(
        {
            "tool_call_index": index,
            "tool_call_ok": bool(calls) and args_ok,
            "tool_args_ok": args_ok,
            "latency_ms": round((time.perf_counter() - started) * 1000, 1),
            "finish_reason": resp.choices[0].finish_reason,
            "tool_name": calls[0].function.name if calls else None,
            "tool_arguments": args_raw or None,
            "content": (msg.content or "").strip(),
        }
    )
    return bool(calls) and args_ok


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--model", default=None)
    parser.add_argument("--skip-tool-call", action="store_true")
    parser.add_argument("--tool-call-repeats", type=int, default=1)
    args = parser.parse_args()

    _load_env(Path(args.env_file))
    request = Request({"type": "http", "method": "POST", "headers": []})
    client, model = get_chat_client_for_model(request, args.model)
    _print(
        {
            "resolved_model": model,
            "base_url": str(client.base_url),
            "has_api_key": bool(os.environ.get("OPENAI_API_KEY") or model.startswith("gemma4:")),
        }
    )

    ok = await _plain_chat(client, model)
    if ok and not args.skip_tool_call:
        for idx in range(1, max(1, args.tool_call_repeats) + 1):
            ok = await _tool_call(client, model, index=idx)
            if not ok:
                break
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
