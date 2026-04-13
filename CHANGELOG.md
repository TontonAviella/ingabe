# Changelog

All notable changes to mundi.ai will be documented in this file.

## [0.1.0.0] - 2026-04-13

### Added
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
