"""Two users asking Sage for the same insurance report each keep their own
Brain page.

The page slug was insurance-<location>-<season>-<date>, the same for everyone.
While pages with no scope were public, the second user's save silently
replaced the first user's report (owner_uuid is not updated on conflict).
Once such pages became private (alembic b8d4f0a2c6e1), the second save hit a
page it cannot see, raised, and was only logged: the second user's report
never reached their Brain. The slug now names the owner.

Runs the real get_insurance_intelligence handler and the real engine, with
the engine's data sources stubbed (as in test_insurance_engine.py), and the
real Brain save under RLS. Needs a database copy (conftest refuses mundidb).
"""

import uuid
from contextlib import ExitStack
from unittest.mock import AsyncMock, MagicMock, patch

import asyncpg
import pytest
import pytest_asyncio

from src.database.pool import _build_postgres_url
from src.services import insurance_engine
from src.services.brain_service import BrainService
from src.services.legacy_tool_shim import LEGACY_HANDLERS, LegacyToolContext

pytestmark = pytest.mark.asyncio(loop_scope="module")

RUN_TAG = uuid.uuid4().hex[:8]
USER_A = str(uuid.uuid4())
USER_B = str(uuid.uuid4())
REPORT = {"crop": "maize", "district": "Gatsibo", "season": "A", "audience": "insurance"}

_compute_for_real = insurance_engine.compute_insurance_intelligence


def _engine_data_stubbed() -> ExitStack:
    """The engine's network fetchers and its own table reads, stubbed; the
    report and its slug are still computed by the real engine."""
    stack = ExitStack()
    stack.enter_context(patch("src.services.admin_boundaries.lookup_admin_geometry", new_callable=AsyncMock, return_value=None))
    stack.enter_context(patch("src.services.insurance_engine.compute_insurance_accuracy_safe", new_callable=AsyncMock, return_value=None))
    stack.enter_context(patch("src.services.weather_accuracy.detect_dry_spells", new_callable=AsyncMock, return_value=None))
    stack.enter_context(patch("src.services.weather_accuracy.compute_ndvi_concordance", new_callable=AsyncMock, return_value=None))
    stack.enter_context(patch("src.services.forecast_fusion.fetch_chirps_daily", return_value=({}, set())))
    stack.enter_context(patch("src.services.wapor_service.query_et", return_value=None))
    stack.enter_context(patch("src.services.wapor_service.query_soil_moisture", return_value=None))
    sar = MagicMock()
    sar.get_backscatter.return_value = {"status": "success", "statistics": {"vh": {"mean": 0.05}, "vv": {"mean": 0.3}}}
    stack.enter_context(patch("src.services.sentinel1_service.get_sentinel1_service", return_value=sar))
    sar_ndvi = MagicMock()
    sar_ndvi.predict_ndvi.return_value = {"status": "success", "predicted_ndvi": 0.45}
    stack.enter_context(patch("src.services.sar_ndvi.get_sar_ndvi_predictor", return_value=sar_ndvi))

    engine_conn = AsyncMock()
    engine_conn.fetch.return_value = [
        {"signal": "rainfall_cumulative", "direction": "below", "threshold": 100.0, "weight": 1.0, "description": "Low rain"},
    ]
    engine_conn.fetchrow.return_value = {"mean_z": -0.5}

    async def engine_on_stubbed_tables(conn, **kwargs):
        return await _compute_for_real(engine_conn, **kwargs)

    stack.enter_context(patch.object(insurance_engine, "compute_insurance_intelligence", engine_on_stubbed_tables))
    return stack


async def _session(user_id: str) -> asyncpg.Connection:
    conn = await asyncpg.connect(_build_postgres_url())
    await conn.execute("SELECT set_config('app.user_id', $1, false)", user_id)
    await conn.execute("SELECT set_config('app.partner_id', '', false)")
    await conn.execute("SELECT set_config('app.role', '', false)")
    return conn


async def _ask_for_the_report(user_id: str) -> dict:
    conn = await _session(user_id)
    try:
        ctx = LegacyToolContext(
            user_id=user_id, partner_id="", conversation_id=0,
            map_id=f"M{RUN_TAG}", project_id=f"P{RUN_TAG}",
            conn=conn, arguments=dict(REPORT),
        )
        with _engine_data_stubbed():
            return await LEGACY_HANDLERS["get_insurance_intelligence"](ctx)
    finally:
        await conn.close()


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def both_reports():
    results = {USER_A: await _ask_for_the_report(USER_A)}
    results[USER_B] = await _ask_for_the_report(USER_B)

    yield results

    worker = await _session("")
    try:
        await worker.execute(
            "DELETE FROM brain_pages WHERE slug = ANY($1::text[]) AND owner_uuid = ANY($2::uuid[])",
            [r.get("slug", "") for r in results.values()], [USER_A, USER_B],
        )
    finally:
        await worker.close()


@pytest.mark.postgres
async def test_both_users_get_a_report(both_reports):
    assert [r["status"] for r in both_reports.values()] == ["ok", "ok"]


@pytest.mark.postgres
async def test_each_user_keeps_their_own_brain_page(both_reports):
    brain = BrainService()
    for user_id, result in both_reports.items():
        conn = await _session(user_id)
        try:
            page = await brain.get_page(conn, result["slug"])
        finally:
            await conn.close()
        assert page is not None, f"the report Sage was told about is not in {user_id}'s Brain"
        assert page.owner_uuid == user_id
        assert page.type == "insurance_intelligence"


@pytest.mark.postgres
async def test_the_two_reports_are_two_pages(both_reports):
    slug_a, slug_b = (both_reports[u]["slug"] for u in (USER_A, USER_B))
    assert slug_a != slug_b
    assert slug_a.startswith("insurance-gatsibo-a-")
    assert slug_b.startswith("insurance-gatsibo-a-")
