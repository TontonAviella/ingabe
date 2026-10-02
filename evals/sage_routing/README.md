# Sage routing eval

Measures whether Sage picks the right **first tool** with the right
**arguments**, finishes **multi-step requests**, handles **follow-ups** that
depend on earlier turns, and answers in plain text when no tool is needed. Every
routing change (tool shortlist, abdication guard, result checks) is judged by a
paired comparison against a baseline run of this eval, not by intuition.

## What one case runs

The runner builds the request with the live code, not a copy:

1. **Fast paths.** `sage_routing.build_fast_tool_call` proposes a deterministic
   tool. The raster paths only fire when `select_fast_raster_layer` finds a
   matching raster in the case's `map_state` (the same rule the handlers use);
   otherwise the turn falls through to the model, exactly as in the app. The
   admin-boundary path needs only the map's project, which every map has.
2. **Routing.** `sage_turn_request.plan_sage_turn` applies small-talk,
   category filtering and exclusions to the full tool list from
   `build_sage_tools_payload`, using the history plus the new message.
3. **Context.** Messages are: system prompt, the case's `history`, the map
   context from `DefaultMapStateProvider` (`<MapState>` with the internal
   Rwanda PostGIS connection and the case's layers, `<NoSelectedFeature />`,
   `<CurrentAOI>`), then the user message.
4. **Model.** The configured model (`OPENAI_MODEL`; today Nemotron-3-Super
   free via OpenRouter), with the live `tool_choice`, or the variant's.
5. **Chains.** For `chain` cases the loop continues: each tool call gets the
   case's stub result (or a generic success stub), up to `max_steps` model
   calls. No real tools run, so no data is read or written.

Each case runs `--repeats` times (default 3).

## Scores

| Outcome of an attempt | Meaning |
|---|---|
| `correct` | first tool is in `any_of`, or plain text when `no_tool` |
| `abdicated` | plain text when a tool was expected |
| `wrong_tool` | a tool outside `any_of` |
| `false_tool` | a tool when plain text was expected |
| `error` | the call failed after retries; excluded and reported |

A case is scored on the strict majority of its non-error attempts (a tie is
not a pass). Reported with 95% Wilson intervals:

- **tool_accuracy**: right first tool
- **tool_and_args_accuracy**: right first tool AND arguments that parse, include
  every required parameter, match declared types/enums, and mention what the
  case expects (`expect.args`, case-insensitive substring)
- **abdication_rate**, **wrong_tool_rate**, **no_tool_accuracy**
- **chain_completion**: chain cases where every `must_call` tool was called
- breakdowns by kind (single / multi_turn / chain), stratum, language, category

## Running

Inside the app container:

```bash
docker compose exec app python scripts/eval_sage_routing.py run --variant baseline
docker compose exec app python scripts/eval_sage_routing.py run --variant tool_choice_required
docker compose exec app python scripts/eval_sage_routing.py compare evals/sage_routing/runs/A.json evals/sage_routing/runs/B.json
```

Runs go to `evals/sage_routing/runs/` (gitignored). Flags: `--case <id>`,
`--kind single|multi_turn|chain`, `--limit N`, `--model-only` (skip fast
paths), `--pace` (seconds between calls).

**Daily caps.** OpenRouter's free models allow a fixed number of requests per
day. A full run is ~280 cases x 3 repeats. After 3 consecutive cases fail the
runner stops, drops those rows, and prints
`run --resume <file>`; resuming skips finished cases. A resume refuses to
continue if the cases changed.

**Comparing.** `compare` pairs cases both runs scored and reports the accuracy
difference with a bootstrap 95% interval that resamples whole **intents** (all
wordings of one request move together, so paraphrases are not counted as
independent evidence), plus an exact McNemar test. A change is an improvement
only when that interval excludes zero and p < 0.05.

Try a change before building it into Sage by adding an entry to `VARIANTS` in
`scripts/eval_sage_routing.py`; `tool_choice_required` (force a tool call on
every non-small-talk turn) is the first one.

## Cases

`cases/*.jsonl`, one case per line:

| File | Source | What it tests |
|---|---|---|
| `single.jsonl` | hand-written; 42 anonymized from real local usage | one request |
| `paraphrases.jsonl` | `build_paraphrases.py` | 3 wordings per request type, districts varied |
| `multi_turn.jsonl` | `build_conversations.py` | follow-ups: "do the same for", corrections, drill-downs |
| `chains.jsonl` | `build_conversations.py` | requests needing 2+ tools, with stub tool results |

```json
{"id": "mt-ndvi-same-for", "intent": "mt-same-for-place", "text": "do the same for Nyanza",
 "category": "ndvi", "stratum": "coverage", "lang": "en", "history": [...],
 "expect": {"any_of": ["get_ndvi_stats", "get_agri_indices"],
            "args": {"get_ndvi_stats": {"district": "nyanza"}}}}
```

- `intent` groups wordings of the same request (the bootstrap cluster).
- `expect.any_of` lists every reasonable *first* step. Label by what the
  request needs, never by what Sage happened to do.
- `expect.args` only for parameters with one right reading (a place name, a
  district), never where several argument shapes are valid.
- `map_state.layers` lists the layers on the map (`raster`, `vector`,
  `postgis`, `point_cloud`); fast paths and `<MapState>` use them.
- Generated files are rebuilt from their builder; a CI test fails if they
  were edited by hand.
- `rw` / `fr` cases carry a note until a native speaker reviews them.
- The repo is public: never commit raw user messages, names, phone numbers
  or private field names.

Runs record `corpus_sha` (all case files); compare only runs on the same
corpus. Add cases in their own commit and re-run the baseline.

`tool_catalog.json` is the tool-name snapshot the labels are written against.
`src/dependencies/test_sage_turn_request.py` fails when the live tool list
drifts; update the labels, then `python scripts/eval_sage_routing.py catalog --write`.

## Gates

- CI `standards` job: `tests/test_sage_routing_eval.py` validates every case
  against the snapshot, checks generated files are current, and tests scoring,
  argument checks, chains and the clustered comparison (stdlib only).
- CI `build-and-test`: `src/dependencies/test_sage_turn_request.py` checks the
  live catalog against the snapshot.
- Model runs are not in CI: they need a key, take hours on the free tier,
  and are nondeterministic. Run them before and after a routing change.

## What it still does not do

- **Stub results, not real tools.** Chains check that the model calls the
  needed tools in a plausible order given plausible results; they do not check
  the final answer against real data.
- **Final answer quality** (is the reply correct and well written) is not
  scored.
- **Precision.** ~280 cases in ~160 intents gives a 95% interval of about
  ±6 points on overall accuracy; per-category numbers are much wider. Treat
  small categories as signals, not results.
- The Hermes runtime path is not measured; it is off by default.
