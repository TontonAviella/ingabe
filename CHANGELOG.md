# Changelog

All notable changes to mundi.ai will be documented in this file.

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
