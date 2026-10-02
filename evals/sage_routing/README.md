# Sage routing eval

Measures whether Sage picks the right **first tool** for a request, or answers
in plain text when no tool is needed. It is step 1 of the routing work: every
later change (tool shortlist, abdication guard, result checks) is judged by a
paired comparison against a baseline run of this eval, not by intuition.

## What is measured

For each case in `corpus.jsonl`, the runner builds the request exactly as the
live chat loop does, using the same code:

1. `sage_routing.build_fast_tool_call` decides whether a deterministic fast
   path handles the turn without a model call (admin boundaries, orthophoto
   object/fact/context requests). Those cases are recorded as `fast_path`.
2. Otherwise `sage_turn_request.plan_sage_turn` applies Sage routing (small
   talk, category filter, exclusions) to the full tool list from
   `build_sage_tools_payload`, and the request goes to the configured model
   (`OPENAI_MODEL`, today Nemotron-3-Super free via OpenRouter).

Each case runs `--repeats` times (default 3). An attempt is one of:

| Outcome | Meaning |
|---|---|
| `correct` | first tool is in the case's `any_of`, or plain text when `no_tool` |
| `abdicated` | plain text when a tool was expected |
| `wrong_tool` | a tool outside `any_of` |
| `false_tool` | a tool when plain text was expected |
| `error` | the call failed after retries; excluded from rates and reported |

A case is scored on the majority of its non-error attempts; a tie that
includes `correct` does not count as correct. Cases whose attempts disagree
are reported as `unstable`.

Headline numbers, each with a 95% Wilson interval:

- **tool_accuracy**: share of tool-expected cases with a correct first tool
- **abdication_rate**: share of tool-expected cases answered in prose
- **wrong_tool_rate**, **no_tool_accuracy**, overall **accuracy**
- breakdowns by stratum, language and category

## Running it

Inside the app container (it needs the app's env and packages):

```bash
docker compose exec app python scripts/eval_sage_routing.py run --variant baseline --repeats 3
```

The run is written to `evals/sage_routing/runs/<timestamp>-<variant>.json`
(gitignored) and printed as a report. Useful flags: `--case <id>` (repeatable),
`--limit N`, `--model-only` (skip the fast paths and measure the model on every
case), `--pace` (seconds between calls; the free tier rate-limits).

Compare two runs, case by case:

```bash
docker compose exec app python scripts/eval_sage_routing.py compare runs/A.json runs/B.json
```

`compare` pairs the cases both runs scored and prints the accuracy difference
with a bootstrap 95% interval and an exact McNemar test on the cases that
changed. A change is an improvement only if the interval excludes zero and
p < 0.05. Never report a single-run number as a lift.

To try a change before building it into Sage, add an entry to `VARIANTS` in
`scripts/eval_sage_routing.py`. `tool_choice_required` (force a tool on every
non-small-talk turn) is included as the first comparison.

## The corpus

`corpus.jsonl`, one case per line:

```json
{"id": "cov-weather-01", "text": "will it rain in Musanze next week?",
 "category": "weather", "stratum": "coverage", "lang": "en",
 "expect": {"any_of": ["get_forecast"]}}
```

- `stratum: observed` cases are anonymized from real local usage (field and
  layer names replaced, public district and sector names kept). `coverage`
  cases cover each capability in the tool catalog.
- `expect.any_of` lists every tool that is a reasonable *first* step. Label by
  what the request needs, never by what Sage happened to do.
- `rw` / `fr` cases carry a note until a native speaker has reviewed them.
- The repo is public: never commit raw user messages, names, phone numbers or
  private field names.

Changing the corpus changes the numbers. Runs record `corpus_sha`; compare only
runs on the same corpus (`compare` warns otherwise). Add cases in their own
commit and re-run the baseline.

`tool_catalog.json` is the snapshot of tool names the labels are written
against. `src/dependencies/test_sage_turn_request.py` fails when the live tool
list drifts from it; update the labels, then run
`python scripts/eval_sage_routing.py catalog --write`.

## Gates

- CI `standards` job: `tests/test_sage_routing_eval.py` validates the corpus
  against the snapshot and tests the scoring and statistics (stdlib only).
- CI `build-and-test`: `src/dependencies/test_sage_turn_request.py` checks the
  live catalog against the snapshot.
- Model runs are not in CI: they need a model key, cost time, and are
  nondeterministic. Run them before and after a routing change.

## Limits

- **First tool only.** It does not check arguments, later steps of a
  multi-step chain, or the final answer.
- **Single turn.** Follow-ups that depend on earlier turns ("do the same for
  Huye") are not covered yet.
- **Fast paths assume the map state they need.** A fast-path case counts as
  handled; in the app the handler can still fall through to the model when, for
  example, no orthophoto is on the map.
- **Small corpus.** With ~110 cases the 95% interval is about ±9 points; a
  real change smaller than that will not show as significant.
- The Hermes runtime path is not measured; it is off by default.
