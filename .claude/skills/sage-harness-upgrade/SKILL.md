---
name: sage-harness-upgrade
description: "Use when the user asks to make Sage/Hermes smarter, convert repeated agent work into a reusable skill, add resolver/context routing, decide what belongs in the LLM versus deterministic tools, or prevent one-off workflows. Examples: \"make this permanent\", \"we had to ask twice\", \"thin harness fat skills\", \"how should Sage use this new tool\", \"add a skill or cron for this\"."
---

# Sage/Hermes Harness Upgrade

## Principle

Keep the harness thin, make skills explicit, and push heavy execution into deterministic tools. Do not solve repeated workflows by adding broad prompt text.

## Inputs

- User goal or repeated failure.
- Existing tools, services, docs, telemetry, and tests.
- Target runtime: Sage, Hermes, Codex/Claude, Dagster, PostHog, or browser map.
- Evidence needed to prove the workflow works.

## Workflow

1. Decide whether the request is one-off or repeated. If the user has asked twice, treat it as repeated.
2. Split the work into latent and deterministic parts.
   - Latent: intent detection, synthesis, explanation, prioritization, and action wording.
   - Deterministic: database queries, raster/H3 work, PostGIS/GDAL/Rust sidecars, tool execution, tests, telemetry, and benchmarks.
3. Inventory existing skills, tools, docs, and tests before adding new ones.
4. Run the workflow manually on representative examples when possible.
5. Codify the repeated part:
   - Skill file for procedure and judgment.
   - Pydantic/FastAPI/Rust tool for deterministic execution.
   - Resolver pointer in `CLAUDE.md`, `AGENTS.md`, or runtime prompt only when it helps selection.
   - Dagster job or cron only when the workflow must run automatically.
6. Add evidence gates so Sage/Hermes cannot claim success without data.
7. Add telemetry for success, latency, inputs, and failure reason where the workflow affects the user experience.
8. Test with focused unit tests first, then browser/PostHog smoke checks when applicable.

## Geospatial Defaults

- Users should not need to know H3, PMTiles, GeoParquet, rasterd, or PostGIS names.
- Sage should choose H3 internally when the question needs fast aggregation, risk zones, neighborhood comparison, or map bins.
- Use official admin boundaries for reporting and accountability.
- Use raster/orthophoto evidence for drone imagery; do not infer housing, crop, or damage truth from satellite basemap alone.
- If the system lacks evidence, say what is missing and run the smallest tool that can verify it.

## Done Checklist

- A repeated workflow is represented as a skill, tool, scheduled job, or documented resolver.
- The system prompt only points to the procedure; it does not contain the whole procedure.
- Tests prove selection or execution.
- Telemetry can distinguish real success from a smoke-screen response.
- User-facing copy describes the result and evidence, not implementation jargon.
