# Stream 1 Prerequisite: openai 1.78 → 2.36 upgrade

**Status:** Audit complete. Recommendation: do it. ~1 day of focused work.
**Why:** Hermes Agent (v2026.5.7) requires `openai>=2.21.0,<3`. mundi.ai pins
`openai==1.78.1`. Cannot pip-install Hermes into mundi.ai's venv without
resolving this first.

## Blast radius audit

**14 files import openai** (10 tests + 4 production). The production files:

| File | What it does | openai surface used |
|---|---|---|
| `src/utils.py:13,235-241` | `get_openai_client()` factory used by everything | `AsyncOpenAI(base_url, default_headers)` constructor |
| `src/routes/message_routes.py:1588-1590` | Per-attempt client for the Sage dispatch loop (provider fallback routing) | `AsyncOpenAI(base_url, api_key)` |
| `src/dependencies/database_documenter.py:12-43` | LLM-driven PostGIS schema documentation | `AsyncOpenAI` parameter + `chat.completions.create()` |
| `src/services/brain_embeddings.py:349-407` | Multi-query expansion + auth-failed handling | `AsyncOpenAI`, `AuthenticationError` |

**Patterns checked against 2.x breaking changes:**

| Pattern | 1.x | 2.x | Verdict |
|---|---|---|---|
| `AsyncOpenAI(base_url=..., api_key=..., default_headers=...)` constructor | ✓ | ✓ | **Unchanged** |
| `await client.chat.completions.create(...)` | ✓ | ✓ | **Unchanged** |
| Streaming: `async for chunk in stream: chunk.choices[0].delta.content` | ✓ | ✓ | **Unchanged** |
| `chunk.choices[0].delta.tool_calls[i].function.name / .arguments` | ✓ | ✓ | **Unchanged** |
| `response.choices[0].message.content` access | ✓ | ✓ | **Unchanged** |
| `response.choices[0].message.tool_calls` access | ✓ | ✓ | **Unchanged** |
| `from openai import APIError` (try/except) | ✓ | ✓ | **Unchanged** |
| `from openai import AuthenticationError` (brain_embeddings.py:349) | ✓ | ✓ | **Unchanged** |
| Removed: `openai.Completion` (legacy) | used | removed | **mundi.ai doesn't use** |
| Removed: `openai.api_key = "..."` module-level config | used | removed | **mundi.ai doesn't use** |

**Conclusion:** the upgrade should be transparent for mundi.ai's code. Bump the
version, run tests, deploy. The risk is **test files**, not production code.

## Tests that may need attention

10 test files import openai. Most are mocking via `openai.Stream` or building
fake response objects. Pydantic-based response models in `openai` were renamed
between 1.x and 2.x for some niche cases, so test mocks may need updating.

Targets to spot-check before merging:
- `src/test_context_length_error.py` — exercises `APIError` paths
- `src/_test_streaming_mock.py` — fakes a stream
- `src/test_remote_uri_mock.py` — mocks an OpenAI response
- `src/geoprocessing/test_geoprocessing.py` — full integration test

## Recommended PR shape

**Title:** `chore(deps): bump openai 1.78.1 → 2.36.0 to unblock Hermes embedding`

**Diff:**
1. `pyproject.toml`: `"openai==1.78.1"` → `"openai==2.36.0"` (match what Hermes
   pulled — predictable resolution)
2. `uv.lock`: regenerated via `uv lock`
3. Test files: fix any mock-related breakage
4. **NO** production source changes if the audit holds

**Validation:**
- `pytest` green
- Local docker compose stack handles a chat completion end-to-end
- Quick prod canary: deploy to one canary instance, watch error rate for 1h

**Why this should ship as its OWN PR, not bundled with Hermes:**
- Reversible: a focused dep bump is easy to revert if it surfaces issues
- Reviewable: reviewers can see "this is just the openai bump, nothing else"
- Independently shippable: if the team needs the openai 2.x security fixes
  anyway, this lands without waiting on Hermes work

## After this upgrade ships

Stream 1 unblocks: add `hermes-agent @ git+https://github.com/NousResearch/hermes-agent.git@v2026.5.7`
to mundi.ai's `pyproject.toml`. Resolve transitive deps (anthropic, edge-tts,
firecrawl, etc. — accept the image bloat for now; slim down in Stream 9
cleanup if image size is an issue).

Then Stream 1b: write `src/services/hermes_runtime.py` (skeleton already
landed in feat/hermes-migration; fill in real Hermes API calls once the
import works).

## Alternative considered + rejected: sidecar pattern

Run Hermes in a separate Python venv inside the same docker image, talk to
it via unix socket. Avoids the openai version conflict.

**Rejected because:**
1. Tools running inside Hermes need access to mundi.ai's asyncpg pool,
   Redis client, request-scoped session — all in-process state. Sidecar =
   HTTP shim for every tool call. Adds latency, breaks RLS GUC scoping.
2. The Phase 2 design doc explicitly chose in-process for these reasons.
3. The upgrade is small enough (1 day) that the sidecar's extra complexity
   isn't worth saving 1 day of dep work.
