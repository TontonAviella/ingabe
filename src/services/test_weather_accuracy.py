# Copyright (C) 2025 Ingabe Ltd.
# AGPL-3.0-or-later

"""Tests for weather accuracy engine (insurance metrics)."""

from __future__ import annotations

import json
from datetime import date, timedelta

import asyncpg
import pytest

from src.database.pool import _build_postgres_url

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _get_conn() -> asyncpg.Connection:
    return await asyncpg.connect(_build_postgres_url())


async def _seed_weather(conn, district: str, days: int = 30) -> None:
    """Insert synthetic daily weather data into weather_daily_cache."""
    today = date.today()
    for i in range(days):
        d = today - timedelta(days=days - i)
        precip = 8.0 if i % 2 == 0 else 0.5
        await conn.execute(
            "INSERT INTO weather_daily_cache "
            "(district, observation_date, temperature_mean, temperature_max, "
            "temperature_min, precipitation, solar_radiation) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7)",
            district, d, 20.0, 25.0, 15.0, precip, 18.0,
        )


async def _seed_ndvi(conn, admin_name: str, weeks: int = 12) -> None:
    """Insert synthetic weekly NDVI data into agri_indices_cache."""
    today = date.today()
    base_ndvi = 0.55
    for i in range(weeks):
        ws = today - timedelta(weeks=weeks - i)
        ndvi = base_ndvi - (i * 0.01)
        await conn.execute(
            "INSERT INTO agri_indices_cache "
            "(admin_level, admin_name, week_start, ndvi_mean, ndvi_std, valid_pixels) "
            "VALUES ($1, $2, $3, $4, $5, $6)",
            "district", admin_name, ws, ndvi, 0.05, 5000,
        )


async def _cleanup(conn, district: str) -> None:
    """Remove test data seeded into cache tables."""
    await conn.execute(
        "DELETE FROM weather_daily_cache WHERE district = $1", district
    )
    await conn.execute(
        "DELETE FROM agri_indices_cache WHERE admin_name = $1", district
    )


# ---------------------------------------------------------------------------
# Unit Tests (no DB)
# ---------------------------------------------------------------------------


class TestBinaryMetrics:
    def test_perfect_detection(self):
        from src.services.weather_accuracy import BinaryMetrics

        m = BinaryMetrics(hits=10, misses=0, false_alarms=0, correct_neg=10, n_total=20)
        assert m.pod == 1.0
        assert m.far == 0.0
        assert m.csi == 1.0
        assert m.hss == 1.0
        assert m.accuracy_pct == 100.0

    def test_no_skill(self):
        from src.services.weather_accuracy import BinaryMetrics

        m = BinaryMetrics(hits=5, misses=5, false_alarms=5, correct_neg=5, n_total=20)
        assert m.pod == 0.5
        assert m.far == 0.5
        assert m.csi is not None
        assert m.hss is not None
        assert abs(m.hss) < 0.1

    def test_empty(self):
        from src.services.weather_accuracy import BinaryMetrics

        m = BinaryMetrics()
        assert m.pod is None
        assert m.far is None
        assert m.hss is None
        assert m.accuracy_pct is None

    def test_serializable(self):
        from src.services.weather_accuracy import BinaryMetrics

        m = BinaryMetrics(hits=3, misses=1, false_alarms=2, correct_neg=4, n_total=10)
        s = json.dumps(m.to_dict())
        assert "pod" in s
        assert "hss" in s


class TestContinuousMetrics:
    def test_basic(self):
        from src.services.weather_accuracy import ContinuousMetrics

        m = ContinuousMetrics(errors=[1.0, -1.0, 2.0, -2.0])
        assert m.n == 4
        assert m.mae == 1.5
        assert m.bias == 0.0
        assert m.rmse is not None

    def test_empty(self):
        from src.services.weather_accuracy import ContinuousMetrics

        m = ContinuousMetrics()
        assert m.mae is None
        assert m.bias is None
        assert m.rmse is None


class TestSeasonDates:
    def test_season_a(self):
        from src.services.weather_accuracy import _season_dates

        d_from, d_to = _season_dates("A", 2025)
        assert d_from == "2025-09-01"
        assert d_to == "2026-01-31"

    def test_season_b(self):
        from src.services.weather_accuracy import _season_dates

        d_from, d_to = _season_dates("B", 2025)
        assert d_from == "2025-02-01"
        assert d_to == "2025-06-30"

    def test_season_default(self):
        from src.services.weather_accuracy import _season_dates

        d_from, d_to = _season_dates("X")
        assert d_from is not None
        assert d_to is not None


# ---------------------------------------------------------------------------
# DB Integration Tests
# ---------------------------------------------------------------------------


class TestDrySpellDetector:
    async def test_detects_synthetic_dry_spell(self):
        """Seed 20 consecutive dry days, verify detection."""
        district = "_test_dry_spell_detect"
        conn = await _get_conn()
        try:
            await _cleanup(conn, district)
            today = date.today()

            # 5 wet days, then 20 consecutive dry days
            for i in range(5):
                d = today - timedelta(days=30 - i)
                await conn.execute(
                    "INSERT INTO weather_daily_cache "
                    "(district, observation_date, precipitation, temperature_mean, "
                    "temperature_max, temperature_min, solar_radiation) "
                    "VALUES ($1, $2, $3, 20.0, 25.0, 15.0, 18.0)",
                    district, d, 12.0,
                )
            for i in range(20):
                d = today - timedelta(days=25 - i)
                await conn.execute(
                    "INSERT INTO weather_daily_cache "
                    "(district, observation_date, precipitation, temperature_mean, "
                    "temperature_max, temperature_min, solar_radiation) "
                    "VALUES ($1, $2, $3, 20.0, 25.0, 15.0, 18.0)",
                    district, d, 0.3,
                )

            from src.services.weather_accuracy import detect_dry_spells

            result = await detect_dry_spells(
                conn,
                district=district,
                threshold_mm=2.0,
                min_duration_days=10,
            )

            assert result["status"] == "success"
            assert result["total_dry_spells"] >= 1
            spells = result["dry_spells"]
            assert len(spells) >= 1
            assert spells[0]["duration_days"] >= 10
            assert spells[0]["district"] == district
            # JSON-serializable
            json.dumps(result)
        finally:
            await _cleanup(conn, district)
            await conn.close()

    async def test_no_data(self):
        conn = await _get_conn()
        try:
            from src.services.weather_accuracy import detect_dry_spells

            result = await detect_dry_spells(
                conn,
                district="NonexistentDistrict999",
            )
            assert result["status"] == "no_data"
        finally:
            await conn.close()


class TestNDVIConcordance:
    async def test_concordance_with_synthetic_data(self):
        """Seed matching weather + NDVI data and verify concordance."""
        district = "_test_ndvi_concordance"
        conn = await _get_conn()
        try:
            await _cleanup(conn, district)
            await _seed_weather(conn, district, days=60)
            await _seed_ndvi(conn, district, weeks=8)

            from src.services.weather_accuracy import compute_ndvi_concordance

            result = await compute_ndvi_concordance(
                conn,
                district=district,
            )

            assert result["status"] in ("success", "insufficient_data")

            if result["status"] == "success":
                assert result["concordance_score"] is not None
                assert 0.0 <= result["concordance_score"] <= 1.0
                assert "interpretation" in result

            json.dumps(result)
        finally:
            await _cleanup(conn, district)
            await conn.close()


class TestComputeBinaryAccuracy:
    async def test_with_seeded_data(self):
        """Seed weather data and test binary accuracy computation."""
        district = "_test_binary_accuracy"
        conn = await _get_conn()
        try:
            await _cleanup(conn, district)
            await _seed_weather(conn, district, days=30)

            from src.services.weather_accuracy import compute_binary_accuracy

            result = await compute_binary_accuracy(
                conn,
                district=district,
                threshold_mm=5.0,
            )

            assert result["status"] in ("success", "no_data")
            assert "n_observations" in result
            json.dumps(result)
        finally:
            await _cleanup(conn, district)
            await conn.close()


class TestInsuranceAccuracy:
    async def test_full_pipeline(self):
        """Test compute_insurance_accuracy runs without error."""
        district = "_test_insurance_accuracy"
        conn = await _get_conn()
        try:
            await _cleanup(conn, district)
            await _seed_weather(conn, district, days=30)
            await _seed_ndvi(conn, district, weeks=8)

            from src.services.weather_accuracy import compute_insurance_accuracy

            result = await compute_insurance_accuracy(
                conn,
                district=district,
                threshold_mm=5.0,
            )

            assert result["status"] == "success"
            assert "confidence_rating" in result
            assert 0 <= result["confidence_rating"] <= 100
            assert "recommendation" in result
            assert "components" in result

            components = result["components"]
            assert "binary_accuracy" in components
            assert "dry_spells" in components
            assert "ndvi_concordance" in components

            s = json.dumps(result)
            assert len(s) > 0
        finally:
            await _cleanup(conn, district)
            await conn.close()


class TestToolsJsonValid:
    def test_tools_json_valid(self):
        import pathlib

        tools_path = pathlib.Path("src/geoprocessing/tools.json")
        data = json.loads(tools_path.read_text())
        assert isinstance(data, list)

        names = {t["function"]["name"] for t in data}
        assert "detect_dry_spells" in names
        assert "get_insurance_accuracy" in names
        assert "get_forecast_accuracy" in names

        # Verify get_forecast_accuracy has lookback_days param
        for t in data:
            if t["function"]["name"] == "get_forecast_accuracy":
                props = t["function"]["parameters"]["properties"]
                assert "lookback_days" in props
                break
