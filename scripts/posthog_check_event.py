#!/usr/bin/env python3
"""Run a build/test/check command and report its result to PostHog.

This is intentionally standalone: it uses only the Python standard library so
CI, a laptop, or a Rust/frontend-only environment can report compiler health
without importing the FastAPI app.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import Any


def _truthy_env(name: str, default: str = "0") -> bool:
    return os.environ.get(name, default).strip().lower() in {"1", "true", "yes", "on"}


def _posthog_key() -> str:
    return (
        os.environ.get("POSTHOG_API_KEY", "").strip()
        or os.environ.get("POSTHOG_PROJECT_API_KEY", "").strip()
        or os.environ.get("VITE_POSTHOG_KEY", "").strip()
    )


def _posthog_host() -> str:
    return (
        os.environ.get("POSTHOG_HOST", "").strip()
        or os.environ.get("VITE_POSTHOG_HOST", "").strip()
        or "https://us.i.posthog.com"
    ).rstrip("/")


def _git_value(args: Sequence[str]) -> str | None:
    try:
        result = subprocess.run(
            ["git", *args],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
    except Exception:
        return None
    value = result.stdout.strip()
    return value or None


def _base_properties(
    *,
    check_name: str,
    stage: str,
    command: Sequence[str],
    started_at: float,
    exit_code: int,
) -> dict[str, Any]:
    command_name = Path(command[0]).name if command else "none"
    commit_sha = (
        os.environ.get("GITHUB_SHA", "").strip()
        or _git_value(["rev-parse", "HEAD"])
        or ""
    )
    branch = (
        os.environ.get("GITHUB_REF_NAME", "").strip()
        or _git_value(["branch", "--show-current"])
        or ""
    )
    return {
        "source": "compiler",
        "service": "ingabe",
        "check_name": check_name,
        "stage": stage,
        "status": "success" if exit_code == 0 else "failed",
        "exit_code": exit_code,
        "duration_ms": int((time.monotonic() - started_at) * 1000),
        "command_name": command_name[:80],
        "run_id": (
            os.environ.get("GITHUB_RUN_ID", "").strip()
            or os.environ.get("POSTHOG_CHECK_RUN_ID", "").strip()
            or f"local-{uuid.uuid4().hex[:12]}"
        ),
        "ci": bool(os.environ.get("CI")),
        "branch": branch[:120],
        "commit_sha": commit_sha[:40],
        "commit_short_sha": commit_sha[:12],
        "workflow": os.environ.get("GITHUB_WORKFLOW", "")[:120],
        "job": os.environ.get("GITHUB_JOB", "")[:120],
        "actor": os.environ.get("GITHUB_ACTOR", "")[:120],
    }


def capture_posthog(event: str, properties: dict[str, Any]) -> bool:
    if _truthy_env("POSTHOG_COMPILER_DISABLED"):
        return False

    api_key = _posthog_key()
    if not api_key:
        print("[posthog-check] PostHog API key missing; skipped telemetry", file=sys.stderr)
        return False

    payload = {
        "api_key": api_key,
        "event": event,
        "distinct_id": properties.get("run_id") or "compiler-check",
        "properties": properties,
    }
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        f"{_posthog_host()}/capture/",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return 200 <= response.status < 300
    except (OSError, urllib.error.URLError) as exc:
        print(f"[posthog-check] PostHog capture failed: {exc}", file=sys.stderr)
        return False


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--event", default="compiler_check_completed")
    parser.add_argument("--stage", required=True, help="backend, frontend, rust, e2e, ci, etc.")
    parser.add_argument("--check", required=True, help="Low-cardinality check name.")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    if args.command and args.command[0] == "--":
        args.command = args.command[1:]
    if not args.command:
        parser.error("a command is required after --")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    started_at = time.monotonic()
    exit_code = 127
    try:
        exit_code = subprocess.run(args.command).returncode
        return exit_code
    finally:
        properties = _base_properties(
            check_name=args.check,
            stage=args.stage,
            command=args.command,
            started_at=started_at,
            exit_code=exit_code,
        )
        capture_posthog(args.event, properties)


if __name__ == "__main__":
    raise SystemExit(main())
