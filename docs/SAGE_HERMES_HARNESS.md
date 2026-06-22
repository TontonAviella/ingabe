# Sage/Hermes Harness Architecture

The operating model for Sage/Hermes is a thin harness with explicit reusable skills and deterministic tools underneath it.

This means Sage should not carry every rule in one giant prompt. The prompt should identify the goal, choose the right procedure, call tools, and explain results. The repeatable procedures live in skills and docs. The heavy work lives in code, Rust sidecars, PostGIS, GDAL/rasterd, H3/geokernel, Dagster jobs, tests, benchmarks, and telemetry.

## Why This Matters

When a workflow repeats, the system should stop improvising. If a user asks twice for the same class of work, convert it into one of these:

- A skill, when the work is judgment and procedure.
- A typed tool, when the work must execute deterministically.
- A Dagster job or cron, when the work must happen automatically.
- A PostHog event/query, when the work must be observed and proved.
- A focused test, when correctness must not depend on memory.

This is the difference between a clever chat response and an operating system for geospatial intelligence.

## What Belongs Where

| Layer | Responsibility |
| --- | --- |
| Sage/Hermes prompt | Identify intent, ask missing questions, call tools, explain evidence and action. |
| Skills | Reusable procedures: rain impact, drone raster context, H3 risk maps, PESTEL, pipeline proof, review workflows. |
| Tools/services | Deterministic work: raster reads, H3 aggregation, PostGIS queries, uploads, tile generation, telemetry writes. |
| Dagster/cron | Freshness pipelines for satellite, weather, embeddings, cache refresh, and scheduled reports. |
| PostHog | Runtime truth: tool success, latency, failure reason, visible map output, user workflow completion. |
| Tests/benchmarks | Prevent regressions and compare speed/accuracy claims. |

## Resolver Rules

- Repeated Sage/Hermes behavior, "make this permanent", "thin harness/fat skills", or "we asked twice" -> `.claude/skills/sage-harness-upgrade/SKILL.md`.
- Architecture trace, blast radius, or code ownership -> GitNexus skills.
- Broken map/tool behavior -> investigation or review skill, plus focused tests.
- Performance claim -> benchmark skill and recorded numbers.
- PESTEL shift across agriculture, housing, infrastructure, or environment -> `docs/PESTEL_ANALYSIS.md`.
- Pipeline truth claim -> inspect Dagster/job state or `get_pipeline_evidence_status`, then verify with PostHog telemetry if available.

## Geospatial Intelligence Rules

- Users should not need to know H3, PMTiles, GeoParquet, rasterd, or PostGIS to get a useful answer.
- H3 is an internal analysis unit for fast aggregation and visual comparison, not a user-facing requirement.
- Admin boundaries remain the official reporting frame for villages, cells, sectors, and districts.
- GeoJSON is acceptable as an interchange/debug format, but persisted or high-volume vector work should move through GeoParquet, MVT, PMTiles, FlatGeobuf, or database-backed paths.
- Drone/satellite claims require evidence from the uploaded raster, derived layers, model output, or trusted external data. Basemap appearance alone is not enough.
- If evidence is missing or stale, Sage should say that plainly and run the smallest available check.

## Runtime Harness Link

`src/services/life_harness.py` is the active Sage/Hermes runtime guard. It keeps the model on track by:

- Validating required tool arguments before execution.
- Making each tool contract explicit.
- Blocking repeated non-progressing tool calls.
- Retrieving compact task-relevant procedures for agriculture, drone raster, admin/H3, Brain memory, spatial evidence, and pipeline proof questions.

Keep that file compact. Add full procedures as skills or docs, then add only short retrieval hints to the runtime harness when Sage needs them during normal chat.
