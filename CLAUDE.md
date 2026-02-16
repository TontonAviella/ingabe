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
