# Changelog

All notable changes to mundi.ai will be documented in this file.

## [0.5.0.0] - 2026-05-04

### Added
- Sage's analytical tools now paint the map. Ask "show me NDVI in Cyampirita" or "any flood damage in Eastern Province?" and Sage paints the actual layer instead of just typing back numbers. Two new tools — `display_layer` (raster overlays from public COG URLs) and `display_geojson_layer` (vector polygons with categorical fills) — backed by 28 typed style presets covering soil chemistry, vegetation indices, drought, soil moisture, evapotranspiration, SAR backscatter, food security IPC phases, insurance trigger scores, and visual similarity gradients.
- 12 existing analytical tools retrofitted to surface map output: `get_soil_properties`, `get_soil_moisture`, `get_evapotranspiration`, `evaluate_insurance_trigger`, `interpret_raster_health`, `analyze_rgb_field`, `find_stress_zones`, `find_similar_tiles`, `detect_water_bodies`, `detect_flood_extent`, `compute_zonal_stats`, `get_food_security_alerts`, `get_alos_l_band_stats`. Each now returns a `displayable_layers` or `displayable_geojson` payload that Sage dispatches via the new display tools.
- AOI grounding for every spatial tool call. New `<CurrentAOI>` system block synthesizes the user's spatial focus from `selected_feature` (parcel clicked on map) → `viewport_bounds` → country default. Sage stops defaulting to district names when a parcel is selected.
- Drone multispectral display path. `describe_user_raster` surfaces a 6-hour presigned COG URL plus per-band style hints for known layouts: 4-band [R, NDVI, NDRE, alpha] drone exports auto-suggest band 2 (`ndvi_band` style) and band 3 (`ndre_band` style); single-band NDVI/NDRE rasters auto-detect from filename. Multispectral with unknown band semantics asks the user to confirm.
- New raster style presets: `ndvi_band`, `ndre_band`, `sar_backscatter_db`. New vector style presets: `insurance_composite_score`, `field_health`, `rgb_field_health`, `stress_zones`, `outline`, `water`, `flood_extent`, `similarity_score`, `food_security_ipc`.
- COG tile router (`/api/cog-tiles/{z}/{x}/{y}.png`) gains a `single_band` rendering mode with per-request `colormap`, `rescale`, and `band_index` params. Reads any band of any public COG and renders it with a colormap, regardless of the source raster's intended band layout.
- Frontend handler in `ProjectView.tsx` for the new `add_geojson_layer` WebSocket action. Renders a fill + stroke layer pair per source, using a MapLibre `step` color expression keyed off categorical stops.

### Changed
- `cog_tile_router` `single_band` mode now uses rio-tiler's `ImageData.mask` instead of hardcoded `band == 0` for nodata. Fixes an issue where valid bare-soil pixels (NDRE ≈ 0), at-mean anomaly z-scores (z = 0), and 0°C temperature pixels would render as transparent holes in the new style presets. iSDAsoil rasters keep masking 0 correctly because their COG nodata tag says so.
- `pool.py:get_async_db_connection` and the related read-replica/sync helpers now thread `viewport_bounds` through to `map_state.get_system_messages` so the AOI block reflects the actual user view at the time of the chat turn.

### Deferred
- CYGNSS family display (service computes water_fraction from netCDF in-memory but doesn't preserve a rasterio transform to vectorize from). Service-level change required.
- `compare_rasters` display (diff lives in numpy memory, no public URL). Would need to write the diff to S3 first.
- Hyperspectral support. `describe_user_raster` does not auto-suggest layers for >>10 band cubes. Would need a dedicated tool to pick RGB-equivalent bands from spectral cubes.

## [0.4.0.0] - 2026-05-02

### Added
- Phase 1 raster interpretation tool surface for Sage. Five mechanical tools (`describe_user_raster`, `compute_zonal_stats`, `get_value_distribution`, `read_pixel_at`, `find_stress_zones`) plus three semantic verdict tools (`interpret_raster_health`, `compare_rasters`, `evaluate_insurance_trigger`) that turn drone NDVI/RGB orthophotos into queryable insurance-grade verdicts.
- Phase 2 visual similarity search via Clay v1.5 foundation model + Qdrant. New Sage tool `find_similar_tiles` lets a user point at a stressed patch in one drone flight and surface visually similar tiles across all their other rasters. Embeddings are auto-generated when COG conversion completes; partner-isolated by default.
- Brain pipeline now runs entirely on local infrastructure. Embeddings switch from OpenAI `text-embedding-3-large` to local Ollama `nomic-embed-text` (768-dim), removing the cloud auth dependency for partner-internal documents and silencing the upstream 401 storm.
- Brain timeline auto-write from Phase 1 verdict tools and Phase 2 Clay status, so every Sage tool call leaves a permanent trace on the relevant `raster-{layer_id}` brain page.

### Changed
- Replaced Milvus standalone with Qdrant 1.17.1 for Clay tile embeddings. Single Rust binary, ~50-100 MiB resident vs Milvus's ~180 MiB plus bundled etcd/minio. Same public API in `qdrant_client.py` so call sites needed only import-line changes.
- Insurance Intelligence Engine refactored: 638-line gut renovation removing pre-Phase-1 prototype paths in favor of the composition layer that ships in this PR.
- Drone orthophoto rendering now serves at full resolution with no edge flicker, even for large uploads (multi-gigabyte COGs handled cleanly through proxy_request_buffering).
- All Sage chat completions now route through `OPENAI_BASE_URL=https://ollama.com/v1` with `gemma4:31b` as primary and `ollama:qwen2.5:7b-64k` (local) as fallback. Frees ~12 GB on the Hetzner box that previously hosted Milvus.

### Fixed
- Brain maintenance scheduler stops crashing on every tick. Frontmatter is now defensively parsed when arriving as a JSON string, and `embed_all_stale` SELECT applies the same partner-aware filter that `get_page` applies, eliminating the "page not found" WARN spam at ~36 lines/min.
- Brain hook processor (which runs every 30s in each of 6 uvicorn workers) now uses `pg_try_advisory_lock` to ensure only one worker per tick runs the embed cycle, mirroring the per-source ingest job pattern.
- pgvector dimension migrated from `vector(1536)` to `vector(768)` with HNSW index rebuilt for the new Ollama embeddings.
- Multiple Sage tools converted to strict-mode arg models (Field(...) sentinels) to match OpenAI tool-calling format.

### Removed
- `src/services/milvus_client.py` and `milvus/*.yaml` configs. Milvus standalone container retired in favor of Qdrant.

## [0.3.0.0] - 2026-04-24

### Added
- Insurance Intelligence Engine (`src/services/insurance_engine.py`) — composition layer connecting 7 existing satellite/weather services (CHIRPS rainfall, crop calendars, dry spells, NDVI anomaly, NDVI concordance, WaPOR ET, WaPOR soil moisture) into unified parametric insurance reports for BK Insurance.
- Four audience-specific formatters: farmer (WhatsApp-ready), insurance worker (trigger assessment table), agronomist (technical + recommendations), scientist (full JSON with methodology and provenance).
- Growth-phase rainfall accumulation connecting CHIRPS daily precipitation to crop calendars, splitting the growing season into planting/vegetative/flowering/grain_fill/maturity phases with per-phase cumulative totals.
- Parametric trigger evaluation engine loading thresholds from `insurance_triggers` table (data, not code) so BK Insurance can customize per district without engineering involvement.
- Alembic migration `a1b2c3d4e5f7` creating `insurance_triggers` table with 568 seed rows covering 52 crops across Seasons A and B. Check constraints, composite unique index, and `updated_at` trigger.
- New Sage tool `get_insurance_intelligence` registered in `tools.json` and wired in `message_routes.py` with Brain persistence (put_page + timeline entry) for audit trail.
- `brain_service.py` `put_page()` extended with `access_scope` and `partner_id` parameters for future partner isolation support.
- 162 unit tests covering pure functions, async mocked integration, and migration integrity.

### Fixed
- Status string mismatch in insurance engine made `accuracy_components` dead code. Seven upstream service checks compared against `"ok"` when services return `"success"`, so weather accuracy metrics (POD/FAR/HSS/CSI) were silently dropped from every report. Now properly hoisted from nested `components.binary_accuracy.overall_binary`.

## [0.2.1.1] - 2026-04-21

### Fixed
- Partner users asking Sage questions now see their own private documents. The org context from Clerk was not flowing through to the 4 brain database connections in `send_map_message()`, so the `app.partner_id` GUC was never set during chat interactions.

## [0.2.1.0] - 2026-04-18

### Added
- ALOS-2 PALSAR-2 L-band SAR service (`src/services/alos_palsar.py`) via Digital Earth Africa STAC, with annual 25m gamma-naught mosaics, HH/HV ratio, and multi-year temporal variation. Sees through clouds and into canopy for biomass and structural monitoring.
- NASA CYGNSS GNSS-R service (`src/services/cygnss.py`) via NASA CMR + PO.DAAC using earthaccess auth. Exposes 9km/36km soil moisture and 1km watermask products. Returns `auth_required` gracefully when Earthdata credentials are missing.
- Five new Sage (LLM) tools wired through `src/geoprocessing/tools.json` and `src/routes/message_routes.py`: `get_alos_l_band_stats`, `get_alos_temporal_variation`, `check_cygnss_availability`, `get_cygnss_soil_moisture`, `get_cygnss_watermask`.
- 24 mocked unit tests in `src/services/test_alos_cygnss_services.py` covering the gamma-naught conversion, band stats, ALOS + CYGNSS service methods, and full tool-registration integrity (tools.json schema, message_routes dispatch, system prompt capabilities).
- Capability and data-attribution entries for the two new sensors in `src/dependencies/system_prompt.py` so Sage can cite sources correctly.

## [0.2.0.0] - 2026-04-17

### Added
- Brain ingestion Phase 0: per-source APScheduler cron jobs that fetch HTML content on a schedule and write it into the brain as pages with full partner-scoped access control
- HTML fetcher with conditional GET (ETag / Last-Modified), robots.txt compliance, per-host rate limiting, and exponential-backoff retry
- Source registry + config builder that materializes DB rows into typed SourceConfig objects (public/partner_internal access scopes)
- Normalizer that persists FetchedContent as brain_pages + brain_timeline_entries atomically in a single transaction, so partner_internal content cannot leak as public on a mid-write crash
- Postgres advisory-lock coordination (pg_try_advisory_lock on a per-source key) so uvicorn's 6 workers don't multiply every cron tick by 6
- Concurrency + cost guardrails module for future OCR (semaphore, token-bucket rate limit, Redis daily-budget counters, per-source 40% cap)
- scripts/deploy.sh rsync-based deployer for the Hetzner prod host with bounded boot-health poll (`--since` log scan, 120s deadline)
- 17 brain tests covering scheduler happy path, all-items-failed stamping last_error, paused-source skip, unknown-fetcher-type no-op, advisory-lock contention, partner isolation, and concurrency guards

### Fixed
- Scheduler held one asyncpg connection for an entire ingest run; split into dedicated lock-holder + per-item pool connections so slow HTTP fetches no longer starve the writer pool
- Advisory lock released only via implicit session close; under lifespan shutdown(wait=False) or future PgBouncer transaction pooling this stranded the lock indefinitely. Now explicitly unlocked in finally, guarded by lock_held
- Scheduler recorded fetch_success when every item in a run crashed (items_fetched=0, items_failed=N), masking broken sources behind a green last_success timestamp. Now calls record_fetch_failure on all-items-failed runs
- write_page's three writes (put_page, UPDATE access_scope/partner_id, add_timeline_entry) ran without a transaction; wrapped in `async with conn.transaction():` so access_scope is never NULL between steps (RLS treats NULL as public)
- deploy.sh used `sleep 8` + `docker logs --tail=200 | grep` which false-failed on cold starts and could match a previous container's log. Replaced with bounded poll against container Up status + `docker logs --since ${DEPLOY_START_TS}`

## [0.1.0.0] - 2026-04-13

### Added
- Postgres-native knowledge brain (gbrain) for Sage AI, with full CRUD, keyword/hybrid search, timeline, entity graph, tags, versioning, and spatial queries
- 3 new Sage tools: search_brain, get_entity, add_observation for querying and updating the knowledge brain
- Brain context injection into every Sage chat message (spatial-aware, 2000-token budget)
- Background hook processor that auto-creates brain pages from vector and raster layer uploads
- Brain embedding pipeline with text chunking and OpenAI text-embedding-3-large (1536 dims)
- 10 brain tables via Alembic migration with Row Level Security
- 19 brain unit tests covering service CRUD, search, spatial queries, and hook processor helpers
- SAR-based water body detection from Sentinel-1 radar imagery, works through clouds and vegetation canopy for aquaculture pond monitoring
- Flood extent delineation by comparing pre/post SAR imagery for insurance claim validation
- SAR-to-NDVI prediction using 30-day Sentinel-1 backscatter trajectory when Sentinel-2 optical imagery is blocked by clouds
- Shared Sentinel-1 RTC access layer via Planetary Computer STAC (gamma-naught backscatter, VV/VH polarizations)
- Weather accuracy engine for agricultural insurance with multi-model fusion, terrain correction, and GPM satellite calibration
- WaPOR evapotranspiration, soil moisture, and FEWS NET food security tools
- DE Africa STAC integration as free fallback when Sentinel Hub credentials expire
- Satellite analytics service for vegetation index time series
- Vitest framework with 19 auth token lifecycle tests for frontend
- 27 unit tests for SAR service pure-computation functions

### Fixed
- Missing await on async _create_layer_summary_page in PMTiles fallback path (brain hook processor)
- Missing import for get_async_db_connection in brain context injection (was silently failing on every chat message)
- WebSocket 403 storm and infinite spinner when Clerk session expires
- Auto-refresh expired presigned S3 URLs that cause 403 tile errors
- TokenManager consolidates Clerk JWT refresh, prevents blank tiles on tab return
- Style.json refetch oscillation from stale TanStack Query cache
- Guard selectFeature against deleted sources and auto-reload on stale chunks
- Degrade broken MVT layer queries to 422 with actionable error instead of 500
- Force-refresh Sentinel Hub OAuth token on 401 to recover from server-side revocation
- FEWS NET service crash bugs (HTTP errors, type coercion, scenario matching)
- Prevent Sage from fabricating data update schedules via DataFreshness system prompt section

### Changed
- Agri indices now route through DE Africa (free) with Sentinel Hub fallback
- Satellite analytics route through DE Africa to bypass SH token expiry
