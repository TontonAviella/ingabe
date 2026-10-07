# CODING_STANDARDS.md

The single home for how code is written in this repo. AGENTS.md and CLAUDE.md
point here; do not copy these rules anywhere else.

This file is meant to grow. Every meaningful thing an agent (or a human) gets
wrong goes into the **Lessons log** at the bottom, via the `standards-retro`
skill (`.claude/skills/standards-retro/SKILL.md`) or by hand. A file that stays
unchanged for weeks means mistakes are being fixed without being learned from.

## Layers in this repo

Name the one layer of the file you are editing before you edit it.

| Layer | Where | May import |
|---|---|---|
| Domain | `src/services/<domain>.py` (and subpackages) | other domain modules, `src/database`, stdlib, third-party compute libs |
| Persistence | `src/database/models.py`, `alembic/` | SQLAlchemy only |
| Orchestration | `src/pipelines/` (Dagster), `src/cron/` | domain |
| Adapters / routing | `src/routes/`, `src/tools/` (Sage tool handlers), `src/dependencies/`, `src/senders/`, `src/wsgi.py` | domain, persistence |
| Presentation | `frontendts/`, `src/renderer/` | API responses only |

## Design principles

Constraints, not a checklist. When two conflict, choose the option with the
lowest future cost for this repo and say which one you chose in the commit
message. H1–H3 below are the hard, CI-enforced form of these.

- **Separation of concerns.** One layer per file (table above).
- **Encapsulation.** Use other modules through their public functions. Never
  read another module's tables, caches, or `_private` helpers directly.
  (Gated: importing a `_name` from another `src.*` module fails CI.)
- **Cohesion / coupling.** One rule change touches one module.
- **DRY, correctly.** Search before writing logic: one home per rule,
  threshold, format, or schema fact. Do not merge code that is only
  coincidentally similar.
- **KISS / YAGNI.** Simplest shape that works; a function before a class; no
  speculative hooks, flags, or frameworks.
- **Single responsibility.** If describing a function needs "and", split it.
  Names state intent; comments state why.
- **Depend on contracts.** Domain code takes and returns plain values
  (dicts, dataclasses, Pydantic models, numpy/xarray). It never imports
  FastAPI, Starlette, `Request`, WebSocket helpers, or `src.routes` /
  `src.dependencies`.
- **Composition over inheritance.** Open/closed only where a change has
  already happened twice. No `a.b.c.d` reach-through chains.
- **Fail fast.** Validate at the edges (tool arguments, uploads, external API
  responses). Never swallow an exception; a `except Exception: pass` or a
  silent default on a data fetch is a bug (see Lessons log).
- **Missing is not zero, and zero is not missing.** Only None means "no
  data". A total over days, pixels or areas with gaps is unknown below its
  coverage threshold, never the sum of what arrived; a real 0 is a value.
  Both mistakes are in the Lessons log (2026-10-04).
- **Optimise for deletion.** Prefer code that is easy to remove over code
  that is easy to extend.
- **Boring tech.** Reuse the stack already in the repo before adding a
  dependency or a service.

## Hard invariants (never violate; enforced by `scripts/check_standards.py`)

**H1. One owning module per domain.** Each business domain's logic lives in
one module, with its rules doc, and everyone else calls it. Consolidate a
scattered domain before adding to it. Extend the domain's existing module;
create a new one only when you can state why the existing one cannot own it.
A new rule goes in its domain owner, not in the first feature that needs it.
A function-level import used to dodge an import cycle means the logic is in
the wrong module. (A lazy import for a genuinely heavy or optional
dependency is allowed when the line carries `# lazy: <reason>`.)

| Domain | Owner | Status |
|---|---|---|
| Insurance triggers, indices, payouts | `src/services/insurance_engine.py` | Scattered: logic also in `src/routes/message_routes.py`, `src/tools/raster_interpret.py`, `src/services/legacy_tool_shim.py`, `src/dependencies/system_prompt.py` |
| Vegetation-index classes (NDVI/EVI breaks, labels, colours, expected NDVI per crop stage) | **none yet** | Scattered across 13 files with conflicting breaks (0.15/0.2/0.3/0.35/0.4/0.6…), plus a crop-stage NDVI table living in the adapter `src/tools/raster_interpret.py`. Create one owner before any new NDVI rule. |
| Weather forecast + fusion | `src/services/forecast_service.py`, `forecast_fusion.py` | |
| Forecast accuracy metrics (POD/FAR/HSS/CSI) | `src/services/weather_accuracy.py` | |
| Administrative boundaries | `src/services/admin_boundaries.py` | |
| Rwanda crop calendar (planting date, days to harvest, current season) | `src/services/crop_calendar.py` | |
| Rain impact | `src/services/rain_impact.py` | |
| H3 aggregation / risk levels | `src/services/h3_spatial_insight.py` | `_risk_level` duplicated in `src/tools/raster_h3_context.py` |

Add a row when a new domain appears. Each owner gets a rules doc under
`docs/rules/<DOMAIN>_RULES.md` when it is consolidated.

**H2. Never duplicate logic.** On the second use of existing logic:
(1) move it to the owning or shared module, (2) switch the original caller
with tests green and no behaviour change, (3) only then build the new use.
First search for the expression itself (the arithmetic, the format string,
the threshold) and list every copy; the move switches all of them or the
commit names each one left and why. A new helper next to old copies is one
more duplicate. The move preserves each caller's exact results (guards,
rounding, clamps, nodata handling, CRS); any behaviour change is a separate
commit. Same for schemas (one fact, one column) and UI (one component per
repeated piece).

**H3. No business logic in rendering.** Routes, Sage tool handlers, React
components and view builders only display values. They never compute
indices, thresholds, classifications, risk levels, or payouts. Arithmetic or
rules on business data there is a bug: move it to the owning domain module
and return the result (value, class, label, colour key) in the API response.
Which user or partner sees a value is decided in Python, not by a template or
component conditional.

## How to work

**Refactor first, then change.** Make a behaviour-preserving refactor with
tests green, then make the change, as separate commits. For domains where
numbers matter (insurance indices, payouts, NDVI classes, accuracy metrics),
the refactor commit also includes a before/after output dump on fixed inputs
showing identical results. Never mix both in one unverifiable diff.

**Gates, not promises.** A rule written only as "never" in a prompt does not
protect the source of truth. A rule that matters is enforced by CI, a test,
or a hook. When you add a rule here, add or extend its gate in
`scripts/check_standards.py` or a test, or write in the rule why it cannot be
checked mechanically.

**The Docker VM is a shared memory budget.** The local stack runs in one
Docker VM (12 GB with 4 GB of swap since 2026-10-04; it was 7.7 GB with 1 GB
when Postgres crashed), and Postgres is the first thing to fail
when it runs short (backends exit with code 2, then crash recovery). Before
starting a service or a heavy job, check swap as well as available memory:
if `DockerVMSwapNearlyFull` is firing, do not start more load, whatever
`free -m` says. Every long-running service sets `mem_limit`. Gate: the
`compose-mem-limit` check and the `DockerVMSwapNearlyFull` /
`PostgresRecoveredRecently` alerts; starting load is a judgement call.

### The standards gate

- `python scripts/check_standards.py` runs in CI (`lint.yml`, job
  `standards`) and as a Claude Code Stop hook (`.claude/settings.json`).
- Existing debt is listed in `scripts/standards_baseline.json`. The baseline
  is a ratchet: new violations fail; fixed violations must be deleted from the
  baseline in the same commit; on PRs the baseline may not grow versus the
  base branch.
- `--write-baseline` exists only to bootstrap. Never use it to admit new
  debt.

Debt at adoption (2026-10-02), 290 entries: 138 cycle-dodging
function-level imports in `src/services/`, 50 web-layer imports in domain
modules (mostly `legacy_tool_shim.py` and `hermes_runtime.py`), 60 imports of
another module's private names, 26 business-threshold comparisons in routes,
Sage tool handlers and React components, 16 duplicated function bodies.
`python scripts/check_standards.py --list` prints every entry with its line.

Hygiene checks (no baseline debt): `caplog` (a test takes pytest's `caplog`
fixture), `compose-mem-limit` (an opt-in compose service without `mem_limit`),
`compose-restart` (a long-running compose service without `restart:`),
`zero-as-missing` (a number tested for truth before rounding) and
`agents-md-sync` (CLAUDE.md must only import AGENTS.md, the single source
of agent guidance).

What the gate cannot see, so review and `standards-retro` must: duplicated
expressions inside larger functions (H2 only matches whole function bodies),
domain rules on terms outside its keyword list (H3), and the judgment
principles (single responsibility, KISS, Demeter, refactor-then-change).

## Lessons log

One entry per real mistake: date, what went wrong, the rule that prevents
it, and the gate if there is one. Newest first. Keep each entry to three
lines; promote a lesson that recurs into the sections above.

- **2026-10-07** A Brain page with no access_scope was public: RLS granted NULL ("legacy rows pre-backfill"; the backfill never came) and put_page wrote
  NULL by default, so one user's 33 pages (orthophotos, an insurance report) were readable by every user and partner through search_brain. Rule: a missing
  value never grants access; access columns are NOT NULL with a fail-closed default. Gate: NOT NULL + `test_no_policy_grants_a_page_by_its_missing_scope`.
- **2026-10-07** Sage's memory packet padded an empty result with the 8 newest Brain pages RLS showed: 7 were other owners' test pages
  ("Rwanda has two rainy seasons"), and the user's own orthophoto page had been dropped by a filter that parsed only `layer-` slugs. Rule: context
  put into a turn matches the question or the viewport and is in the user's scope; never pad it. Gate: `test_brain_context_packet.py`, `test_brain_user_scope.py`.
- **2026-10-07** The live-database guard trusted an override flag (MUNDI_TEST_DB_IS_DISPOSABLE=1) that the CI command passes, so that
  command copied to a laptop would run the suite on mundidb. Rule: a guard that protects live data has no override a copied command
  can carry; CI gets its own database name. Gate: conftest `_refuse_the_live_database`, `tests/test_refuse_live_database.py`.
- **2026-10-07** `get_cell_ndvi_stats` ran a blocking 40 s satellite read per sector inside `async def`: one Sage question froze
  every request (one uvicorn worker) for ~10 min, and no `wait_for` limit could fire. Rule: blocking I/O in async code goes
  through `asyncio.to_thread` with a cap and a deadline. Gate: review only (blocking calls hide behind library functions).
- **2026-10-05** `src/duckdb.py` shadowed the `duckdb` package: src/ has no `__init__.py`, so pytest put it on sys.path and
  `import duckdb` in layer_describer loaded our module; once it stopped re-exporting duckdb names, attribute sampling failed silently.
  Rule: no module directly under src/ is named like a dependency. Gate: `shadow-package`.
- **2026-10-05** WorkOS sign-in never worked: the cookie secret was 64 hex chars but the SDK feeds it to Fernet, and every login test
  mocked the code exchange, so no test ever sealed a cookie; the failed callback then bounced back to the provider in a loop. Rule: an auth
  or crypto path has one test that runs the real library on a realistic secret, and a failure page never auto-redirects. Gate: review only (test_workos_auth seals for real).
- **2026-10-05** Local test runs used the live database (mundidb): 4,727 test projects and ~431,000 brain pages (Barcelona shops,
  US counties) piled up among real data and were nearly assigned to BK as its knowledge. Rule: tests never touch the live database; run
  them on a copy. Gate: conftest `_refuse_the_live_database` (no override; CI runs on its own database, mundidb_ci).
- **2026-10-04** Migration b2c3d4e5f6a7 downloaded Rwanda boundaries from geoboundaries.org and raised on
  failure; the API timed out and CI failed on unchanged code. Rule: migrations read seed data from vendored
  files, never the network. Gate: `tests/test_rwanda_boundary_seed_offline.py` (network blocked).
- **2026-10-04** Insurance season rainfall summed only the CHIRPS days it downloaded: with the final product weeks behind,
  every Season A report read 0 mm and fired the rainfall trigger, and late in a season the unfetched early weeks undercounted.
  Rule: "Missing is not zero" (Design principles). Gate: review only (needs the data's coverage); tests in `test_insurance_engine.py`.
- **2026-10-04** 63 places formatted numbers with `round(x, n) if x else None`, so a real 0 became "missing":
  NDVI 0.0, z-score 0 and VCI 0 (the most extreme drought) vanished from results. Rule: only None is missing
  (`src.services.numbers.round_or_none`). Gate: `zero-as-missing`.
- **2026-10-04** I ran `git worktree remove --force` on a worktree holding uncommitted scripts and lost them
  (rebuilt from the session). Rule: commit (or push a WIP commit) before removing a worktree; never `--force`
  without `git status` first. Gate: review only.
- **2026-10-04** A verification script re-downloaded 16 months of Open-Meteo forecasts for 120 district-model
  pairs twice in an hour and hit the hourly limit the live app's forecasts share (429 for up to an hour).
  Rule: analysis scripts on a shared external quota cache responses and stop on 429. Gate: review only.
- **2026-10-04** Insurance rainfall normals labelled "CHIRPS v2.0 2000-2023" were hand-entered and 23-28%
  too low; the season SPI also compared rainfall so far with full-season normals (drought on normal rain).
  Rule: reference data derived from a dataset is generated by a committed script, stored with its source. Gate: review only.
- **2026-10-04** I ran test jobs with `dagster job execute` against the shared Dagster instance: the
  runs held the queue's single slot (one orphaned when I removed its container) and re-runs duplicated cache rows.
  Rule: out-of-band runs use a throwaway `DAGSTER_HOME`. Gate: review only (operator action).
- **2026-10-04** Applying new Docker Desktop resources restarted the engine; Postgres, the app, Redis
  and QGIS had no restart policy and stayed down until started by hand.
  Rule: every long-running compose service sets `restart:`. Gate: `compose-restart`.
- **2026-10-04** I restarted Dagster and ran test jobs while swap was 100% full (2.3 GB "available");
  Postgres crashed twice (02:13, 04:14 UTC). Second time after 2026-10-03: promoted to
  "The Docker VM is a shared memory budget" under How to work. Gate: alerts, plus judgement.
- **2026-10-04** The Dagster daemon (no restart policy, no mem_limit) stopped running schedules on
  2026-08-12 and exited on 2026-10-02; nobody noticed, so Sage answered from 7 weeks of missing weather/NDVI.
  Rule: long-running compose services set `restart:` and `mem_limit`. Gate: alert `DataPipelineStale` (cache age).
- **2026-10-03** I checked a new index's plan as the superuser (RLS bypassed); under the
  app role's RLS the planner switched to a nested-loop anti-join that ran 33+ minutes.
  Rule: time and EXPLAIN queries as the app role. Gate: review only (plans need a live DB).
- **2026-10-03** Adding self-hosted Langfuse (~2.5 GB) to the 7.7 GB Docker VM filled
  swap; Postgres background workers exited with code 2 and the DB looped through crash
  recovery. Rule: every opt-in compose service sets `mem_limit`. Gate: `compose-mem-limit`.
- **2026-10-03** A test asserted a warning through `caplog`; it passed alone and failed in
  CI, because once lifespan runs, `src` loggers stop propagating to caplog's handler.
  Rule: assert on a module's logs by patching its logger. Gate: `caplog`.
- **2026-10-02** Sage's deterministic fast paths matched generic words ("layer",
  "field", "zoom to X"): 25 of 279 eval requests took the wrong tool with no model
  call. Rule: a deterministic route needs negative cases too. Gate: eval-corpus test.
- **2026-10-02** CI workers all died at 60 s (`node down`). I first blamed OOM; the
  evidence said pytest-timeout killed them while an autouse fixture ran migrations.
  Rule: capture the evidence (dmesg, timings) before fixing a crash. Gate: CI diagnose step.
- **2026-10-02** The forecast outlook told users "the payout threshold is 300 mm"
  while evaluation applied the 100 mm maize trigger from `insurance_triggers`. Rule
  (H2): a threshold shown to users reads from the source evaluation uses. Gate: test.
- **2026-10-02** `Dockerfile.postgres` builds on Debian 11 (EOL); apt.postgresql.org
  archived `bullseye-pgdg`, so CI failed on unchanged code. Rule: an image on an
  EOL distro points apt at the archive explicitly; plan the base upgrade. Gate: review only.
- **2026-10-02** The ruff CI action was unpinned, so it silently moved from
  0.15.21 to 0.16.10 and failed on 3,383 findings in unchanged code. Rule:
  pin every CI tool version; upgrade it in its own PR. Gate: review only.
- **2026-10-02** Seeded from CHANGELOG. Seven service-status checks in
  `insurance_engine.py` compared against `"ok"` while services return
  `"success"`, silently dropping accuracy metrics from every report.
  Rule: status values are constants owned by the producing module; compare
  against the constant, never a string literal.
- **2026-10-02** Seeded from CHANGELOG. A missing import in brain context
  injection failed silently on every chat message. Rule: fail fast; an
  exception handler around a data fetch must log and re-raise or return an
  explicit error value the caller checks, never a silent default.
