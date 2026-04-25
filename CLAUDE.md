# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What is Mundi.ai

AI-native web GIS by Ingabe, credited to Roger. Supports vector, raster, and point cloud data. Connects to PostGIS databases and uses LLMs (OpenAI function calling) to invoke geoprocessing algorithms and edit symbology.

## Build & Run Commands

### Docker (primary development method)
```bash
docker compose up                              # Start all services (app, postgres, redis, minio, qgis-processing)
docker compose build                           # Rebuild images
docker compose run app pytest -xvs -n auto     # Run all tests in Docker
```

### Python Backend
```bash
basedpyright                                   # Type checking
ruff check .                                   # Linting
pytest -xvs -n auto                            # Run all tests (parallel)
pytest -xvs src/routes/test_postgres_routes.py  # Single test file
pytest -xvs -k "test_name"                     # Single test by name
alembic upgrade head                           # Run migrations
alembic revision -m "description"              # Create migration
```

### Frontend (frontendts/)
```bash
npm run dev                                    # Vite dev server
npm run build                                  # Production build (tsc + vite)
npm run lint                                   # Biome linter
npm run watch                                  # Watch mode (tsc + vite)
```

## Architecture

### Backend: FastAPI + async PostgreSQL
- **Entry point**: `src/wsgi.py` — FastAPI app with lifespan that auto-runs Alembic migrations on startup
- **Routes** (`src/routes/`): API endpoints mounted under `/api`. The largest are `postgres_routes.py` (map/layer CRUD) and `message_routes.py` (LLM chat streaming)
- **Dependencies** (`src/dependencies/`): FastAPI dependency injection modules for auth, DB pools, LLM clients, map state, PostGIS connections, session management
- **Database** (`src/database/`): SQLAlchemy 2.0 async with asyncpg driver. Connection pool min=1, max=10 with per-request exclusive connections
- **Models**: 8+ tables — projects, maps, layers, styles, conversations, chat messages. Maps use a DAG structure (`parent_map_id`) for version lineage

### Frontend: React + TypeScript + MapLibre GL
- **Location**: `frontendts/`
- **Stack**: React 18, Vite, Tailwind CSS 4, Radix UI + shadcn/ui, TanStack Query, MapLibre GL + deck.gl for 3D visualization
- **Linter**: Biome (configured in `biome.json`)
- **Built SPA** served by FastAPI at `/` with catchall routing for client-side routes

### AI/LLM Integration
- OpenAI function calling with tools defined in `src/geoprocessing/tools.json`
- `src/dependencies/chat_completions.py` and `src/dependencies/pydantic_tools.py` wire up tool dispatch
- `src/tools/` contains Pydantic tool handler modules (ALOS, CYGNSS, SAR, WaPOR, food security, insurance, spectral index, etc.)
- `src/symbology/llm.py` generates MapLibre styles via LLM
- WebSocket streaming for real-time chat completions and tool execution updates

### Storage & Services
- **PostgreSQL 15**: App metadata + PostGIS for spatial data
- **MinIO/S3**: File storage for layers (FlatGeoBuf, GeoJSON, LAZ, GeoTIFF)
- **Redis**: Caching layer
- **QGIS Processing**: Separate FastAPI service (`qgis-processing/server.py`) exposing QGIS algorithms over HTTP

### GIS Toolchain (built in Dockerfile)
- GDAL 3.11.3, Tippecanoe (vector tiles), PMTiles CLI, LAStools (point clouds), MapLibre GL Style Spec validator

## Testing

- **Framework**: pytest with pytest-asyncio (async mode: auto) and pytest-xdist (parallel)
- **Timeout**: 30 seconds per test
- **Markers**: `s3`, `postgres`, `anyio`, `asyncio`
- **Fixtures** in `conftest.py`: `client` (async httpx), `auth_client` (requires `MUNDI_AUTH_MODE=edit`), `test_map_with_vector_layers` (pre-populated Barcelona/Idaho layers)
- **Test data**: `test_fixtures/` contains FlatGeoBuf, GeoJSON, and reference images
- Tests live alongside source in `src/` (pattern: `test_*.py`)

## CI/CD

- **cicd.yml**: Docker build via Depot → run tests → push to GCP Artifact Registry
- **lint.yml**: Ruff + basedpyright + Biome (runs on push to main and PRs)

## Key Environment Variables

| Variable | Purpose |
|----------|---------|
| `MUNDI_AUTH_MODE` | `edit` or `view_only` |
| `S3_*` | S3/MinIO connection (ACCESS_KEY_ID, SECRET_ACCESS_KEY, ENDPOINT_URL, BUCKET, DEFAULT_REGION) |
| `POSTGRES_*` | Database connection (HOST, PORT, DB, USER, PASSWORD) |
| `REDIS_HOST/PORT` | Redis cache |
| `OPENAI_API_KEY` | LLM provider key |
| `OPENAI_BASE_URL` | Optional, for local LLMs (e.g., Ollama) |
| `QGIS_PROCESSING_URL` | QGIS service endpoint |
| `POSTGIS_LOCALHOST_POLICY` | `docker_rewrite` or `disallow` |

<!-- gitnexus:start -->
# GitNexus — Code Intelligence

This project is indexed by GitNexus as **mundi.ai** (5527 symbols, 9515 relationships, 238 execution flows). Use the GitNexus MCP tools to understand code, assess impact, and navigate safely.

> If any GitNexus tool warns the index is stale, run `npx gitnexus analyze` in terminal first.

## Always Do

- **MUST run impact analysis before editing any symbol.** Before modifying a function, class, or method, run `gitnexus_impact({target: "symbolName", direction: "upstream"})` and report the blast radius (direct callers, affected processes, risk level) to the user.
- **MUST run `gitnexus_detect_changes()` before committing** to verify your changes only affect expected symbols and execution flows.
- **MUST warn the user** if impact analysis returns HIGH or CRITICAL risk before proceeding with edits.
- When exploring unfamiliar code, use `gitnexus_query({query: "concept"})` to find execution flows instead of grepping. It returns process-grouped results ranked by relevance.
- When you need full context on a specific symbol — callers, callees, which execution flows it participates in — use `gitnexus_context({name: "symbolName"})`.

## When Debugging

1. `gitnexus_query({query: "<error or symptom>"})` — find execution flows related to the issue
2. `gitnexus_context({name: "<suspect function>"})` — see all callers, callees, and process participation
3. `READ gitnexus://repo/mundi.ai/process/{processName}` — trace the full execution flow step by step
4. For regressions: `gitnexus_detect_changes({scope: "compare", base_ref: "main"})` — see what your branch changed

## When Refactoring

- **Renaming**: MUST use `gitnexus_rename({symbol_name: "old", new_name: "new", dry_run: true})` first. Review the preview — graph edits are safe, text_search edits need manual review. Then run with `dry_run: false`.
- **Extracting/Splitting**: MUST run `gitnexus_context({name: "target"})` to see all incoming/outgoing refs, then `gitnexus_impact({target: "target", direction: "upstream"})` to find all external callers before moving code.
- After any refactor: run `gitnexus_detect_changes({scope: "all"})` to verify only expected files changed.

## Never Do

- NEVER edit a function, class, or method without first running `gitnexus_impact` on it.
- NEVER ignore HIGH or CRITICAL risk warnings from impact analysis.
- NEVER rename symbols with find-and-replace — use `gitnexus_rename` which understands the call graph.
- NEVER commit changes without running `gitnexus_detect_changes()` to check affected scope.

## Tools Quick Reference

| Tool | When to use | Command |
|------|-------------|---------|
| `query` | Find code by concept | `gitnexus_query({query: "auth validation"})` |
| `context` | 360-degree view of one symbol | `gitnexus_context({name: "validateUser"})` |
| `impact` | Blast radius before editing | `gitnexus_impact({target: "X", direction: "upstream"})` |
| `detect_changes` | Pre-commit scope check | `gitnexus_detect_changes({scope: "staged"})` |
| `rename` | Safe multi-file rename | `gitnexus_rename({symbol_name: "old", new_name: "new", dry_run: true})` |
| `cypher` | Custom graph queries | `gitnexus_cypher({query: "MATCH ..."})` |

## Impact Risk Levels

| Depth | Meaning | Action |
|-------|---------|--------|
| d=1 | WILL BREAK — direct callers/importers | MUST update these |
| d=2 | LIKELY AFFECTED — indirect deps | Should test |
| d=3 | MAY NEED TESTING — transitive | Test if critical path |

## Resources

| Resource | Use for |
|----------|---------|
| `gitnexus://repo/mundi.ai/context` | Codebase overview, check index freshness |
| `gitnexus://repo/mundi.ai/clusters` | All functional areas |
| `gitnexus://repo/mundi.ai/processes` | All execution flows |
| `gitnexus://repo/mundi.ai/process/{name}` | Step-by-step execution trace |

## Self-Check Before Finishing

Before completing any code modification task, verify:
1. `gitnexus_impact` was run for all modified symbols
2. No HIGH/CRITICAL risk warnings were ignored
3. `gitnexus_detect_changes()` confirms changes match expected scope
4. All d=1 (WILL BREAK) dependents were updated

## CLI

- Re-index: `npx gitnexus analyze`
- Check freshness: `npx gitnexus status`
- Generate docs: `npx gitnexus wiki`

<!-- gitnexus:end -->
