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

Legacy, outside the layering: top-level `services/*.py` (`api_insurance.py`,
`insurance_report.py`, `api_monitor.py`, `monitor_field_v3.py`) duplicate
domain logic that belongs in `src/services/`. Do not extend them; consolidate
into the owning module first (H1).

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
| Insurance triggers, indices, payouts | `src/services/insurance_engine.py` | Scattered: logic also in `src/routes/message_routes.py`, `src/tools/raster_interpret.py`, `src/services/legacy_tool_shim.py`, `src/dependencies/system_prompt.py`, `services/api_insurance.py`, `services/insurance_report.py` |
| Vegetation-index classes (NDVI/EVI breaks, labels, colours, expected NDVI per crop stage) | **none yet** | Scattered across 13 files with conflicting breaks (0.15/0.2/0.3/0.35/0.4/0.6…), plus a crop-stage NDVI table living in the adapter `src/tools/raster_interpret.py`. Create one owner before any new NDVI rule. |
| Weather forecast + fusion | `src/services/forecast_service.py`, `forecast_fusion.py` | |
| Forecast accuracy metrics (POD/FAR/HSS/CSI) | `src/services/weather_accuracy.py` | |
| Administrative boundaries | `src/services/admin_boundaries.py` | |
| Crop modelling (DSSAT) | `src/services/dssat_service.py` | |
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

What the gate cannot see, so review and `standards-retro` must: duplicated
expressions inside larger functions (H2 only matches whole function bodies),
domain rules on terms outside its keyword list (H3), and the judgment
principles (single responsibility, KISS, Demeter, refactor-then-change).

## Lessons log

One entry per real mistake: date, what went wrong, the rule that prevents
it, and the gate if there is one. Newest first. Keep each entry to three
lines; promote a lesson that recurs into the sections above.

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
