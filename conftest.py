import pytest
import os
from contextlib import contextmanager
from typing import Dict, Generator, Optional

# Set fast timeout for postgres connections in tests
os.environ["MUNDI_POSTGIS_TIMEOUT_SEC"] = "0.5"
os.environ["MUNDI_AUTH_MODE"] = "edit"

# docker-compose defaults OPENAI_MODEL to an Ollama model. The production chat
# loop routes ollama:* models through a direct AsyncOpenAI client pointed at the
# Ollama service, bypassing tests that patch get_openai_client(). The CI pytest
# container does not start Ollama, so keep the test default on the patchable
# client path unless an individual test deliberately overrides it.
os.environ["OPENAI_MODEL"] = "test-chat-model"
os.environ["SAGE_SMALL_TALK_MODEL"] = "test-chat-model"
os.environ["OPENROUTER_FALLBACK_MODEL"] = ""
os.environ["OPENROUTER_FALLBACK_MODELS"] = ""
# Tests never spend money: the local .env holds the real OpenRouter key, and with it any test that reaches a
# model (Brain query expansion, drone vision, a live-key test) was billed on every local run, while CI has no
# key at all (2026-10-09: five local suite runs drained the credits). A fake key gets a free 401 instead. Live
# model tests opt in with MUNDI_RUN_LIVE_LLM_TESTS=1, which keeps the real key and models.
if os.environ.get("MUNDI_RUN_LIVE_LLM_TESTS") != "1":
    os.environ["OPENAI_API_KEY"] = "test-api-key"
    os.environ["DRONE_VISION_MODEL"] = "test-vision-model"
    os.environ["BRAIN_QUERY_EXPANSION_MODEL"] = "test-expansion-model"
elif not os.environ.get("OPENAI_API_KEY"):
    os.environ["OPENAI_API_KEY"] = "test-api-key"
# The test key above would switch MUNDI_USE_HERMES=auto on and route every chat
# test through Hermes before the legacy loop those tests patch. Hermes tests
# monkeypatch hermes_is_enabled() explicitly.
os.environ["MUNDI_USE_HERMES"] = "0"
os.environ["POSTHOG_BACKEND_DISABLED"] = "1"
# Autonomous ingestion is tested directly. Running it in every TestClient
# lifespan consumes hooks created by unrelated tests and makes suite results
# depend on timing and database leftovers.
os.environ["MUNDI_BACKGROUND_WORKERS_ENABLED"] = "0"

# The local .env sets AUTH_PROVIDER=workos for the running app; tests that need
# WorkOS turn it on themselves (monkeypatch), so the suite matches CI locally.
os.environ.pop("AUTH_PROVIDER", None)
from httpx_ws.transport import ASGIWebSocketTransport
from httpx import AsyncClient
from pathlib import Path
import random
import asyncio
from concurrent.futures import ThreadPoolExecutor
from starlette.testclient import TestClient
from alembic import command
from alembic.config import Config
import asyncpg

from src.wsgi import app
from src.database.pool import _build_postgres_url

# The OpenAI SDK closes a garbage-collected AsyncOpenAI client by scheduling
# aclose() on whatever event loop is running at that moment. Tests run each
# coroutine on its own loop, so a client leaked by one test was closed on a later
# test's loop; closing its TLS socket touched the first, already-closed loop and
# raised "Event loop is closed" inside an unrelated fixture (seen as random
# "ERROR at setup" of test_mbgl_idaho / test_view_map_with_bounds in CI).
# In tests a leaked client is simply dropped; production runs on one loop.
import openai._base_client as _openai_base_client

_openai_base_client.AsyncHttpxClientWrapper.__del__ = lambda self: None


@pytest.fixture
def run_alembic_operation():
    async def _run_alembic_operation(operation, target=None):
        project_root = Path(__file__).parent
        alembic_cfg = Config(project_root / "alembic.ini")
        alembic_cfg.set_main_option("script_location", str(project_root / "alembic"))

        def run_operation():
            if operation == "upgrade":
                command.upgrade(alembic_cfg, target or "head")
            elif operation == "downgrade":
                command.downgrade(alembic_cfg, target)
            else:
                raise ValueError(f"Unknown operation: {operation}")

        loop = asyncio.get_running_loop()
        with ThreadPoolExecutor() as executor:
            await loop.run_in_executor(executor, run_operation)

    return _run_alembic_operation


def pytest_sessionstart(session):
    """Run Alembic migrations once per pytest process before any test
    executes.

    This is a hook, not an autouse session fixture, on purpose. A fixture's
    setup runs inside the first test's pytest-timeout budget (pytest.ini:
    timeout = 60, method = thread). A fresh database runs the whole
    migration chain, including the Rwanda boundary seeds (~17k features
    inserted one by one), while every other xdist worker waits on the
    advisory lock. When that took over 60 s, pytest-timeout `os._exit()`-ed
    every worker at once (`[gwN] node down: Not properly terminated` on each
    worker's first test; PRs #77, #78, #79 on 2026-10-02). Hooks run outside
    the per-test timeout.

    Why migrations must run up front: many tests connect to PostgreSQL directly
    via `asyncpg.connect(_build_postgres_url())` and expect cache tables
    (weather_daily_cache, ndvi_district_cache, etc.) to exist. Previously
    they got lucky — the `client` session fixture would call
    `run_migrations()` and tests on the same worker would benefit. Under
    pytest-xdist's `--dist=loadfile` (introduced after the OOM cascade
    fix in PR #57 surfaced ordering bugs), a worker that runs only
    direct-asyncpg test files never touches the `client` fixture, so
    its tests race against migrations from other workers and fail with
    `UndefinedTableError: relation "weather_daily_cache" does not exist`.

    `run_migrations` already uses a Postgres advisory lock so multiple
    workers calling it concurrently is safe — only one actually applies
    the upgrade, the others wait then no-op. Running it here forces every
    session to wait on that lock before tests start.
    """
    from src.database.migrate import run_migrations

    _refuse_the_live_database()
    asyncio.run(run_migrations())


_LIVE_DATABASE = "mundidb"


def _refuse_the_live_database() -> None:
    """Never run the suite against the live local database (mundidb).

    Until 2026-10 local runs used mundidb itself and left 4,727 test projects and
    ~431,000 brain pages (Barcelona shops, US counties, re-ingested every run)
    mixed in with real data; 76 leftover test pages still reached Sage's memory
    packet on 2026-10-07. There is no override: an environment flag that let CI
    through also let a local copy of the CI command through. CI runs on its own
    database name (POSTGRES_DB=mundidb_ci in cicd.yml). An unset POSTGRES_DB is
    refused too, because several modules fall back to mundidb when it is unset.
    Locally, point POSTGRES_DB at a copy, e.g.
    `createdb -T mundidb_pytest_wos mundidb_pytest_x`.
    """
    database = os.environ.get("POSTGRES_DB", "")
    if database in ("", _LIVE_DATABASE):
        pytest.exit(
            f"POSTGRES_DB={database or '(unset)'} means the live database ({_LIVE_DATABASE}): "
            "refusing to run tests against it. Use a copy (POSTGRES_DB=mundidb_pytest_...), "
            "see conftest._refuse_the_live_database.",
            returncode=2,
        )


@pytest.fixture(scope="session")
def anyio_backend():
    return "asyncio"


@pytest.fixture(scope="session")
async def client():
    # Run database migrations before tests
    from src.database.migrate import run_migrations

    await run_migrations()

    transport = ASGIWebSocketTransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


@pytest.fixture(scope="session")
async def auth_client(client):
    # Requests run as the legacy edit-mode user (no sign-in provider in tests)
    assert os.environ.get("MUNDI_AUTH_MODE") == "edit"

    yield client


@pytest.fixture(scope="session")
async def test_map_with_vector_layers(auth_client):
    map_payload = {
        "title": "Geoprocessing Test Map",
        "description": "Test map for geoprocessing operations with vector layers",
    }
    map_response = await auth_client.post("/api/maps/create", json=map_payload)
    assert map_response.status_code == 200, f"Failed to create map: {map_response.text}"
    current_map_id = map_response.json()["id"]
    layer_ids = {}

    async def _upload_layer(file_name, layer_name_in_db, target_map_id):
        file_path = str(Path(__file__).parent / "test_fixtures" / file_name)
        if not os.path.exists(file_path):
            pytest.skip(f"Test file {file_path} not found")
        with open(file_path, "rb") as f:
            layer_response = await auth_client.post(
                f"/api/maps/{target_map_id}/layers",
                files={"file": (file_name, f, "application/octet-stream")},
                data={"layer_name": layer_name_in_db},
            )
            assert layer_response.status_code == 200, (
                f"Failed to upload layer {file_name}: {layer_response.text}"
            )
            response_data = layer_response.json()
            print(response_data)
            return response_data["id"], response_data["dag_child_map_id"]

    random.seed(42)
    layer_id, current_map_id = await _upload_layer(
        "barcelona_beaches.fgb", "Barcelona Beaches", current_map_id
    )
    layer_ids["beaches_layer_id"] = layer_id

    layer_id, current_map_id = await _upload_layer(
        "barcelona_cafes.fgb", "Barcelona Cafes", current_map_id
    )
    layer_ids["cafes_layer_id"] = layer_id

    layer_id, current_map_id = await _upload_layer(
        "idaho_weatherstations.geojson", "Idaho Weather Stations", current_map_id
    )
    layer_ids["idaho_stations_layer_id"] = layer_id

    return {
        "map_id": current_map_id,
        "project_id": map_response.json()["project_id"],
        **layer_ids,
    }


@pytest.fixture
async def test_project_with_multiple_origins(auth_client):
    """Create a test project for testing multiple origin domains."""
    response = await auth_client.post(
        "/api/maps/create",
        json={
            "title": "Test Project for Multiple Origins",
        },
    )
    assert response.status_code == 200
    return response.json()


@pytest.fixture(scope="session")
def _migrations_done():
    """Run migrations once per xdist worker (session-scoped).

    Using a dedicated fixture avoids the race condition where every
    function-scoped sync_client would call run_migrations() concurrently
    under xdist, competing for the Redis migration lock.
    """
    from src.database.migrate import run_migrations

    asyncio.run(run_migrations())


@pytest.fixture(scope="function")
def sync_client(_migrations_done):
    client = TestClient(app)
    client.__enter__()
    yield client
    try:
        client.__exit__(None, None, None)
    except RuntimeError:
        pass  # Starlette TestClient teardown race with closed event loop


@pytest.fixture(scope="function")
def sync_auth_client(sync_client):
    assert os.environ.get("MUNDI_AUTH_MODE") == "edit"
    yield sync_client


@pytest.fixture
def websocket_url_for_map(sync_auth_client):
    def _get_url(map_id, conversation_id):
        return f"/api/maps/ws/{conversation_id}/messages/updates"

    return _get_url


@pytest.fixture
async def test_project(auth_client):
    response = await auth_client.post(
        "/api/maps/create",
        json={
            "title": "Test Project",
        },
    )
    assert response.status_code == 200
    map_data = response.json()
    return {"project_id": map_data["project_id"], "map_id": map_data["id"]}


@pytest.fixture
async def test_project_with_map(auth_client):
    response = await auth_client.post(
        "/api/maps/create",
        json={
            "title": "Test Project with Map",
        },
    )
    assert response.status_code == 200
    map_data = response.json()
    return {"project_id": map_data["project_id"], "map_id": map_data["id"]}


def pytest_configure(config):
    """Configure pytest markers."""
    config.addinivalue_line("markers", "s3: mark test as requiring S3/MinIO access")
    config.addinivalue_line(
        "markers", "postgres: mark test as requiring PostgreSQL access"
    )
    config.addinivalue_line("markers", "anyio: mark a test as asynchronous using AnyIO")

    # monkey patch ssl.create_default_context to use a cached version
    # ssl.py create_default_context takes a HUGE amount of time with openssl v3.0.0,
    # and it's a known issue: https://github.com/python/cpython/issues/95031
    # https://github.com/psf/requests/pull/6667
    # https://github.com/boto/botocore/issues/3171
    # this saves like 10 seconds of clock time on tests...
    import ssl
    from functools import lru_cache

    original_create_default_context = ssl.create_default_context

    @lru_cache(maxsize=8)
    def cached_create_default_context(*args, **kwargs):
        return original_create_default_context(*args, **kwargs)

    ssl.create_default_context = cached_create_default_context


@pytest.hookimpl(optionalhook=True)
def pytest_xdist_auto_num_workers(config):
    """Cap xdist workers at 4 to prevent resource exhaustion in Docker.

    When ``-n auto`` is used, xdist normally spawns one worker per CPU.
    Inside a Docker container the shared PostgreSQL, Redis, and MinIO
    services become bottlenecks long before CPU saturation.  Capping
    at 4 keeps parallelism high while avoiding connection storms and
    migration lock contention.
    """
    return min(4, os.cpu_count() or 4)


@pytest.fixture
def expected_basemaps():
    return {
        "available_styles": ["esri_satellite", "openstreetmap", "openfreemap"],
        "first_style": "esri_satellite",
        "default_style_name": "Esri Satellite",
    }


# ---------------------------------------------------------------------------
# Test isolation fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def db_conn():
    """Provide a direct asyncpg connection wrapped in a savepoint.

    Any data written through this connection is rolled back automatically
    when the test ends.  Useful for tests that need to verify or set up
    DB state directly (outside the HTTP client).

    Usage::

        async def test_something(db_conn):
            await db_conn.execute("INSERT INTO ...")
            row = await db_conn.fetchrow("SELECT ...")
            assert row is not None
            # rolled back automatically — no cleanup needed
    """
    conn: asyncpg.Connection = await asyncpg.connect(_build_postgres_url())
    tr = conn.transaction()
    await tr.start()
    # Create a savepoint so the test can issue its own transactions
    sp = conn.transaction()
    await sp.start()
    try:
        yield conn
    finally:
        # Roll back savepoint, then outer transaction
        await sp.rollback()
        await tr.rollback()
        await conn.close()


@pytest.fixture
def env_override():
    """Temporarily override environment variables, restoring originals on exit.

    Replaces error-prone manual try/finally patterns in tests.

    Usage::

        def test_something(env_override):
            with env_override(MUNDI_AUTH_MODE="view_only", AUTH_PROVIDER=None):
                # MUNDI_AUTH_MODE is set; AUTH_PROVIDER is removed
                ...
            # originals restored automatically
    """

    @contextmanager
    def _override(**overrides: Optional[str]) -> Generator[None, None, None]:
        originals: Dict[str, Optional[str]] = {}
        for key, value in overrides.items():
            originals[key] = os.environ.get(key)
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        try:
            yield
        finally:
            for key, original in originals.items():
                if original is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = original

    return _override
