# Changelog

All notable changes to mundi.ai will be documented in this file.

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
