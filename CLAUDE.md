# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What is Mundi.ai

AI-native web GIS by Ingabe, credited to Roger. Supports vector, raster, and point cloud data. Connects to PostGIS databases and uses LLMs (OpenAI function calling) to invoke geoprocessing algorithms and edit symbology.

**Multi-partner platform.** Mundi.ai serves N partner organizations (BK Insurance is the first active pilot). Every architecture decision must pass the "what if Partner #2 shows up next month" test. Per-partner isolation via Clerk org + Postgres RLS on `app.partner_id` GUC. Sage's identity stays "Sage" to every partner; per-partner skills/prompts/context are composed on top of a base persona.

## Production reality (verified 2026-05-14)

- **Image**: `mundi-public:local` built on prod host (Hetzner CPX42 178.104.18.44, `/home/deploy/mundi.ai`). No registry push. Deploy = SCP source or pull migrations + `docker compose -f docker-compose.yml -f docker-compose.prod.yml build app && up -d app`.
- **Sage LLM**: openai SDK 2.36 → OpenRouter (`https://openrouter.ai/api/v1`) → `nvidia/nemotron-3-super-120b-a12b:free` primary, `openai/gpt-4o-mini` then local `ollama:qwen2.5:7b-64k` as fallbacks.
- **Turn loop**: `process_chat_interaction_task` in `src/routes/message_routes.py:1109` (6500-line file). Hand-rolled. Hermes Agent is declared in `pyproject.toml` but NOT YET installed in prod and NOT YET powering Sage. Cutover planned (see "Hermes Phase 2" below).
- **DB roles**: `mundiuser` (app, `rolsuper=False`, `rolbypassrls=False`); RLS is FULLY ENFORCED on brain/maps/layers tables since ~Apr 22, 2026. NO superuser/admin role exists right now — if RLS locks the app out, recovery is shell-into-container with image-bootstrap creds only. A `mundi_admin` safety net is on the to-do list.
- **PostHog observability**: traces exist for `$ai_generation` events. Current p50 latency ~26s, p95 ~60s. Input tokens 124k-136k/turn (system prompt + tools dominates). 0 errors over the last 7 days. `traceName` / `partner_id` not yet attached to traces — observability work pending.

## Hermes Phase 2 — runtime swap plan

Goal: replace the hand-rolled chat loop in `process_chat_interaction_task` with Hermes Agent (`v2026.5.7` tag, GitHub-only, MIT) running the `ingabe-sage` plugin from `hermes_integration/plugins/ingabe-sage/`. User-visible name stays "Sage". 75 tool schemas already declared in `generated_tools.py` as stubs; Phase 2 wires real handlers via a universal-shim pattern that delegates to existing dispatch in `src/dependencies/pydantic_tools.py`.

Cutover is **flag-gated**: `MUNDI_USE_HERMES=0` keeps the existing path; `=1` routes through `src/services/hermes_runtime.py` (the runtime module). Default OFF until verified. Rollback = set flag to 0 and restart.

Hermes does NOT ship a WhatsApp adapter (only Telegram, Discord, Slack, Matrix, DingTalk, Feishu). BK WhatsApp inbound is a separate native track, not gated on Phase 2.

## Build & Run Commands

### Docker (primary development method)
```bash
docker compose up                              # Start all services (app, postgres+pgvector, redis, minio, qgis-processing, qdrant, ollama)
docker compose build                           # Rebuild images
docker compose run app pytest -xvs -n auto     # Run all tests in Docker
```

### Dep upgrades — Dockerfile installs from requirements.txt, NOT pyproject.toml

When bumping any Python dep, you MUST update `requirements.txt` (the actual install source for the image). Bumping `pyproject.toml` + `uv.lock` alone does not change what's in the running container. Audit transitive deps too: `openai==2.36.0` needs `typing-extensions>=4.14` and `jiter>=0.10.0`, not the older pins. Memory `feedback_dockerfile_uses_requirements_txt` has the full story.

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

### Test isolation under pytest-xdist

Every shared identifier in tests that touch postgres must be namespaced per worker with `RUN_TAG = uuid.uuid4().hex[:8]` (module-level). Hardcoded slugs collide across workers on the shared DB. Pattern used in `tests/brain/test_partner_isolation.py`. See `feedback_xdist_run_tag_pattern` memory.

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
- `src/tools/` contains Pydantic tool handler modules (ALOS, CYGNSS, SAR, WaPOR, food security, insurance, spectral index, raster interpretation, similarity search, etc.)
- `src/symbology/llm.py` generates MapLibre styles via LLM
- WebSocket streaming for real-time chat completions and tool execution updates

### Sage Tool Surface
- **Phase 1 raster interpretation** (`src/tools/raster_interpret.py`): mechanical (`describe_user_raster`, `compute_zonal_stats`, `get_value_distribution`, `read_pixel_at`, `find_stress_zones`) + verdicts (`interpret_raster_health`, `compare_rasters`, `evaluate_insurance_trigger`)
- **Phase 2 visual similarity** (`src/tools/similarity.py` + `src/services/clay_embedding.py`): Clay v1.5 cls_token embeddings → Qdrant cosine search, drone tile auto-embedding on COG completion
- **Insurance intelligence** (`src/services/insurance_engine.py`): location-based agricultural reports combining CHIRPS, NDVI, WaPOR, SAR, soil moisture across Rwanda's Season A/B/C calendar

### Storage & Services
- **PostgreSQL 15 + pgvector 0.8.2**: App metadata, PostGIS spatial data, brain page embeddings (768-dim from Ollama nomic-embed-text)
- **MinIO/S3**: File storage for layers (FlatGeoBuf, GeoJSON, LAZ, GeoTIFF)
- **Cloudflare R2** (optional, when 4 R2_* env vars set): transit upload layer for fast uploads from Africa edges; background worker pulls R2 → MinIO. 1-day lifecycle delete on R2 bucket.
- **Redis**: Caching layer
- **Qdrant 1.17.1**: Visual similarity index for Clay v1.5 tile embeddings (1024-dim cosine HNSW). Replaces Milvus.
- **Ollama**: Local LLM container hosting nomic-embed-text (Brain embeddings) + qwen2.5:7b-64k (Sage fallback). Sage primary uses gemma4:31b via Ollama Cloud direct API.
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
| `OPENAI_API_KEY` | LLM provider key (used as bearer for `OPENAI_BASE_URL`). Prod uses an OpenRouter key (`sk-or-v1-…`). Rotate via OpenRouter dashboard → update `/home/deploy/mundi.ai/.env` → restart `mundi-app` with prod compose files. |
| `OPENAI_BASE_URL` | LLM endpoint. **Prod: `https://openrouter.ai/api/v1`.** (Old CLAUDE.md said Ollama Cloud; that's wrong, we switched.) |
| `OPENAI_MODEL` | Primary chat model. **Prod: `nvidia/nemotron-3-super-120b-a12b:free`.** (Old CLAUDE.md said `gemma4:31b`; that's wrong.) Reasoning model → reasoning tokens count toward `max_tokens`; floor `max_tokens` at 150 for short outputs. |
| `OPENROUTER_FALLBACK_MODELS` | Comma-separated fallback chain. `ollama:<tag>` entries route to local Ollama container. Prod: `openai/gpt-4o-mini,ollama:qwen2.5:7b-64k`. |
| `MUNDI_USE_HERMES` | `0` (default) → existing hand-rolled chat loop in `process_chat_interaction_task`. `1` → route through `src/services/hermes_runtime.py` (Hermes Agent runtime). Toggle this flag for the Phase 2 cutover. Rollback = back to `0` + restart. |
| `OLLAMA_BASE_URL` | Local Ollama OpenAI-compat endpoint, e.g. `http://ollama:11434/v1` |
| `BRAIN_EMBEDDINGS_PROVIDER` | `ollama` (default, local nomic-embed-text 768-dim) or `openai` |
| `BRAIN_EMBEDDINGS_API_KEY` | Required when `BRAIN_EMBEDDINGS_PROVIDER=openai`. Distinct from `OPENAI_API_KEY` so Brain auth is isolated from Sage chat auth. |
| `QDRANT_HOST` / `QDRANT_PORT` / `QDRANT_GRPC_PORT` | Qdrant connection. Defaults: `qdrant` / `6333` / `6334` |
| `R2_ENDPOINT_URL` / `R2_ACCESS_KEY_ID` / `R2_SECRET_ACCESS_KEY` / `R2_BUCKET` | All four required to activate Cloudflare R2 transit upload layer. Without them, uploads go direct to MinIO. |
| `QGIS_PROCESSING_URL` | QGIS service endpoint |
| `POSTGIS_LOCALHOST_POLICY` | `docker_rewrite` or `disallow` |

<!-- gitnexus:start -->
# GitNexus — Code Intelligence

This project is indexed by GitNexus as **ingabe** (13879 symbols, 19534 relationships, 282 execution flows). Use the GitNexus MCP tools to understand code, assess impact, and navigate safely.

> If any GitNexus tool warns the index is stale, run `npx gitnexus analyze` in terminal first.

## Always Do

- **MUST run impact analysis before editing any symbol.** Before modifying a function, class, or method, run `gitnexus_impact({target: "symbolName", direction: "upstream"})` and report the blast radius (direct callers, affected processes, risk level) to the user.
- **MUST run `gitnexus_detect_changes()` before committing** to verify your changes only affect expected symbols and execution flows.
- **MUST warn the user** if impact analysis returns HIGH or CRITICAL risk before proceeding with edits.
- When exploring unfamiliar code, use `gitnexus_query({query: "concept"})` to find execution flows instead of grepping. It returns process-grouped results ranked by relevance.
- When you need full context on a specific symbol — callers, callees, which execution flows it participates in — use `gitnexus_context({name: "symbolName"})`.

## Never Do

- NEVER edit a function, class, or method without first running `gitnexus_impact` on it.
- NEVER ignore HIGH or CRITICAL risk warnings from impact analysis.
- NEVER rename symbols with find-and-replace — use `gitnexus_rename` which understands the call graph.
- NEVER commit changes without running `gitnexus_detect_changes()` to check affected scope.

## Resources

| Resource | Use for |
|----------|---------|
| `gitnexus://repo/ingabe/context` | Codebase overview, check index freshness |
| `gitnexus://repo/ingabe/clusters` | All functional areas |
| `gitnexus://repo/ingabe/processes` | All execution flows |
| `gitnexus://repo/ingabe/process/{name}` | Step-by-step execution trace |

## CLI

| Task | Read this skill file |
|------|---------------------|
| Understand architecture / "How does X work?" | `.claude/skills/gitnexus/gitnexus-exploring/SKILL.md` |
| Blast radius / "What breaks if I change X?" | `.claude/skills/gitnexus/gitnexus-impact-analysis/SKILL.md` |
| Trace bugs / "Why is X failing?" | `.claude/skills/gitnexus/gitnexus-debugging/SKILL.md` |
| Rename / extract / split / refactor | `.claude/skills/gitnexus/gitnexus-refactoring/SKILL.md` |
| Tools, resources, schema reference | `.claude/skills/gitnexus/gitnexus-guide/SKILL.md` |
| Index, status, clean, wiki CLI commands | `.claude/skills/gitnexus/gitnexus-cli/SKILL.md` |

<!-- gitnexus:end -->

## Skill routing

When the user's request matches an available skill, invoke it via the Skill tool. The
skill has multi-step workflows, checklists, and quality gates that produce better
results than an ad-hoc answer. When in doubt, invoke the skill. A false positive is
cheaper than a false negative.

Key routing rules:
- Product ideas, "is this worth building", brainstorming → invoke /office-hours
- Strategy, scope, "think bigger", "what should we build" → invoke /plan-ceo-review
- Architecture, "does this design make sense" → invoke /plan-eng-review
- Design system, brand, "how should this look" → invoke /design-consultation
- Design review of a plan → invoke /plan-design-review
- Developer experience of a plan → invoke /plan-devex-review
- "Review everything", full review pipeline → invoke /autoplan
- Bugs, errors, "why is this broken", "wtf", "this doesn't work" → invoke /investigate
- Test the site, find bugs, "does this work" → invoke /qa (or /qa-only for report only)
- Code review, check the diff, "look at my changes" → invoke /review
- Visual polish, design audit, "this looks off" → invoke /design-review
- Developer experience audit, try onboarding → invoke /devex-review
- Ship, deploy, create a PR, "send it" → invoke /ship
- Merge + deploy + verify → invoke /land-and-deploy
- Configure deployment → invoke /setup-deploy
- Post-deploy monitoring → invoke /canary
- Update docs after shipping → invoke /document-release
- Weekly retro, "how'd we do" → invoke /retro
- Second opinion, codex review → invoke /codex
- Safety mode, careful mode, lock it down → invoke /careful or /guard
- Restrict edits to a directory → invoke /freeze or /unfreeze
- Upgrade gstack → invoke /gstack-upgrade
- Save progress, "save my work" → invoke /context-save
- Resume, restore, "where was I" → invoke /context-restore
- Security audit, OWASP, "is this secure" → invoke /cso
- Make a PDF, document, publication → invoke /make-pdf
- Launch real browser for QA → invoke /open-gstack-browser
- Import cookies for authenticated testing → invoke /setup-browser-cookies
- Performance regression, page speed, benchmarks → invoke /benchmark
- Review what gstack has learned → invoke /learn
- Tune question sensitivity → invoke /plan-tune
- Code quality dashboard → invoke /health
