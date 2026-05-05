# TODOS

## Deferred from display_layer Pattern (2026-05-04)

### CYGNSS family — display path
- **What**: `get_cygnss_soil_moisture` and `get_cygnss_watermask` compute aggregated stats (water_fraction, water_pixels, water_area_km2) from netCDF in-memory. They don't preserve a rasterio transform from the lat/lon coordinate arrays, so there's no way to vectorize the watermask into polygons or render the soil moisture as a tile layer.
- **Why deferred**: A retrofit needs the existing return shape preserved; building polygons requires modifying the service to compute an affine from the netCDF coordinate arrays and call `rasterio.features.shapes`. That's a service-level change, not a return-dict enrichment.
- **Depends on**: Nothing. Mechanical work in `src/services/cygnss.py:get_watermask` (around line 461) to vectorize the mask before returning.
- **When to revisit**: When BK Insurance asks "show me the saturated soil zones" and the answer needs to be a map, not a single number.

### compare_rasters — display path
- **What**: `compare_rasters` computes a change-detection diff array entirely in numpy. There's no public URL for the diff so `display_layer` has nothing to point at.
- **Why deferred**: Would need to write the diff array as a COG to S3, presign for 6h, surface URL via `displayable_layers`. Worth it once partners actually ask for visual diffs.
- **Depends on**: A short helper that writes a numpy array as a COG and returns a presigned URL.
- **When to revisit**: First time a partner wants "show me what changed between week 8 and week 12 of the season."

### Hyperspectral support
- **What**: `describe_user_raster` does not auto-suggest displayable layers for hyperspectral cubes (>>10 bands). The default user-raster tile endpoint shows the first 3 bands as RGB, which is meaningless for spectral cubes.
- **Why deferred**: Needs a dedicated tool to pick RGB-equivalent bands (e.g., 50nm windows around 650/550/450nm) from arbitrary spectral configurations. Not in current partner roadmap.
- **Depends on**: Knowing the sensor's wavelength-to-band mapping. Currently no metadata pipeline for this.
- **When to revisit**: When a partner shows up with a hyperspectral drone export and asks for analysis.

### Tests for the display_layer pattern (low confidence on payload shapes)
- **What**: 11 retrofits + 1 fix landed in v0.5.0.0, ~1145 insertions, zero new test files. Pydantic schemas validate at startup but `displayable_geojson` / `displayable_layers` payload shapes have no test coverage. A bug in any retrofit's enrichment logic would break visualization but not the analytical answer, so it would silently degrade the UX.
- **Why deferred**: Each test is ~20 lines (Pydantic round-trip + helper unit test). Total ~5h for 12 tools. Real partner risk is low because the LLM ignores malformed `displayable_*` payloads.
- **Depends on**: Nothing.
- **When to revisit**: First time a tool's display payload misbehaves in production, or before a major refactor.

## Deferred from Insurance Intelligence Engine (2026-04-24)

### accuracy_components not serialized in InsuranceReport.to_dict()
- **Completed:** v0.3.0.0 (2026-04-25)
- **What was done**: `to_dict()` serialization added (commit `51958b2`). Status string mismatch (`"ok"` vs `"success"`) that made accuracy_components dead code fixed. POD/FAR/HSS/CSI now properly hoisted from `components.binary_accuracy.overall_binary`.

### SPI simplified from gamma-fit to z-score approximation
- **What**: `_compute_spi()` uses `(rainfall - mean) / std` with hardcoded national normals instead of the planned gamma distribution fit (scipy.stats.gamma) on 20-year CHIRPS monthly totals. No SPI-1/SPI-3 distinction.
- **Why deferred**: The z-score approximation is sufficient for trigger evaluation at current scale. Proper gamma-fit SPI requires historical CHIRPS data aggregation pipeline that doesn't exist yet.
- **Depends on**: Historical CHIRPS monthly aggregation (20+ years per district).
- **When to revisit**: When BNR requests WMO-standard SPI for regulatory compliance, or when district-level thresholds need calibration.

## Deferred from gbrain Adoption (2026-04-13)

### Dream Cycles (Phase 2)
- **What**: Nightly batch rewrite of compiled_truth via LLM. For each brain_page, aggregate recent timeline entries + raw_data, call LLM to produce updated compiled_truth, store old version in brain_page_versions.
- **Why deferred**: Effort L. Core brain needs to prove value first. No point rewriting compiled truth if no one is reading it yet.
- **Depends on**: brain_pages, brain_timeline_entries, brain_page_versions all shipping in Phase 1.
- **When to revisit**: After Phase 1 ships and at least 100 brain pages exist with timeline entries.

### Tool Dispatch Registry Refactor (partial)
- **What**: Refactor `src/routes/message_routes.py` tool dispatch from elif chain to a registry/dispatch pattern. 11 tools migrated to Pydantic registry in `src/dependencies/pydantic_tools.py` with handler functions in `src/tools/` (ALOS, CYGNSS, SAR, WaPOR, food security, OSM download, create point, display layer, search place, spectral index, zoom). ~30 tools remain in the elif chain (~5,900 lines).
- **Why deferred**: Remaining migration is incremental. Each tool is ~30 min to extract. Not blocking development.
- **Depends on**: Nothing. Can be done anytime.
- **When to revisit**: Continue migrating tools as they're touched for bug fixes or feature work.

## Deferred from Brain Ingestion Phase 0 (2026-04-17)

### _seconds_until_midnight_utc bug in concurrency.py
- **What**: `src/services/brain_ingestion/concurrency.py:106-113` has broken TTL math. `tomorrow.replace(day=tomorrow.day)` is a no-op, so the fallback branch always fires and the TTL computation is wrong on every call. Fix: compute `(now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0) - now`.
- **Why deferred**: Phase 0 is HTML-only, the OCR budget path that consumes this TTL never runs in production yet.
- **Depends on**: Nothing, trivial fix.
- **When to revisit**: Before the PDF fetcher lands in Phase 1, or sooner if anyone wires up `reserve_ocr_budget`.

### URL credential leak in last_error
- **What**: When a fetch fails, `record_fetch_failure` stores `repr(e)[:500]` which can include the full URL with query params. If partner sources ever embed an API key in the URL, that key ends up in `brain_sources.last_error` (visible to anyone with source-table read). Fix: strip query strings and basic-auth userinfo before logging.
- **Why deferred**: Phase 0 has no partner_internal sources with URL-embedded credentials. Public HTML fetches only.
- **Depends on**: partner_internal fetcher onboarding.
- **When to revisit**: Before the first partner_internal source is registered in brain_sources.

### Source hot-reload
- **What**: Scheduler only reads `brain_sources` at startup. Adding/pausing/retiring a source requires an app restart before the change takes effect.
- **Why deferred**: Operators already restart the app on deploys (daily cadence is fine). Not blocking Phase 0's single-source ops.
- **Depends on**: Nothing.
- **When to revisit**: When operators start managing sources via an admin UI instead of SQL + redeploy, or when source churn exceeds ~1/week.

### Cron-tick jitter across replicas
- **What**: All workers fire cron ticks at the same UTC second; only one wins the advisory lock but 5 lose a DB round-trip every tick. Adding `jitter=30` to `CronTrigger` spreads the thundering herd.
- **Why deferred**: Noise only, no correctness issue. Five `pg_try_advisory_lock` calls/tick is cheap.
- **Depends on**: Nothing.
- **When to revisit**: If cron-tick contention ever shows up in pg_stat_activity, or the source count grows past ~50.
