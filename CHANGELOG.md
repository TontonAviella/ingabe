# Changelog

All notable changes to mundi.ai will be documented in this file.

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
