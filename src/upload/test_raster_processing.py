"""Tests for raster preprocessing, handler, and registry dispatch.

These tests pin the current GDAL-based raster processing behavior so that
the Dask swap (Arch #2) can be validated against a known-good baseline.

Sections 1 & 4 (preprocess_raster, edge cases) require only GDAL and run
locally without Docker.  Sections 2 & 3 (handler, registry) import the full
upload stack which uses ``str | None`` syntax, requiring Python ≥3.10 (Docker).
"""

import json
import sys
from pathlib import Path
from typing import Optional
from unittest.mock import AsyncMock, MagicMock

import pytest
from osgeo import gdal

gdal.UseExceptions()

from src.upload.preprocessing import preprocess_raster

# Conditional imports — the handler & registry modules transitively pull in
# database models that use ``str | None`` (Python 3.10+).  Guard them so
# pure-GDAL tests (Sections 1 & 4) still run on the macOS system Python 3.9.
_PY310 = sys.version_info >= (3, 10)

if _PY310:
    from src.upload.handlers.raster_handler import RasterUploadHandler
    from src.upload.base import HandlerResult, UploadContext
    from src.upload.registry import get_handler, get_layer_type, RASTER_EXTS

# Skip decorator for handler/registry tests when running on older Python
_needs_py310 = pytest.mark.skipif(
    not _PY310, reason="Handler/registry imports require Python ≥3.10"
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

FIXTURES = Path(__file__).resolve().parent.parent.parent / "test_fixtures"


@pytest.fixture
def waterboard_tif() -> Path:
    """3-band Web Mercator GeoTIFF (waterboard.tif)."""
    p = FIXTURES / "waterboard.tif"
    if not p.exists():
        pytest.skip(f"Fixture not found: {p}")
    return p


@pytest.fixture
def la_dem_26711() -> Path:
    """1-band NAD27 / UTM zone 11N raster (losangeles-dem_26711.tif)."""
    p = FIXTURES / "losangeles-dem_26711.tif"
    if not p.exists():
        pytest.skip(f"Fixture not found: {p}")
    return p


@pytest.fixture
def frazier_dem() -> Path:
    """1-band NAD27 / UTM DEM (frazier_8928_75m.dem)."""
    p = FIXTURES / "frazier_8928_75m.dem"
    if not p.exists():
        pytest.skip(f"Fixture not found: {p}")
    return p


@pytest.fixture
def la_dem_geographic() -> Path:
    """1-band NAD27 geographic DEM (los_angeles-e.DEM) — coords already ~4326."""
    p = FIXTURES / "los_angeles-e.DEM"
    if not p.exists():
        pytest.skip(f"Fixture not found: {p}")
    return p


def _make_ctx(
    temp_file_path: str,
    file_ext: str = ".tif",
    metadata_dict: Optional[dict] = None,
):
    """Build a minimal UploadContext for handler tests (Python ≥3.10 only)."""
    conn = MagicMock()
    conn.execute = AsyncMock()
    return UploadContext(
        map_id="M_test",
        layer_id="L_test",
        layer_name="Test Raster",
        file_basename="test_raster",
        user_id="U_test",
        project_id="P_test",
        temp_file_path=temp_file_path,
        file_ext=file_ext,
        file_size_bytes=1024,
        s3_key=f"uploads/test/{file_ext.lstrip('.')}",
        metadata_dict=metadata_dict if metadata_dict is not None else {},
        conn=conn,
        bucket_name="test-bucket",
    )


# ===================================================================
# Section 1: preprocess_raster — unit tests (GDAL only, any Python)
# ===================================================================


class TestPreprocessRaster:
    """Pin GDAL-based bounds extraction and metadata mutation."""

    def test_returns_bounds_for_valid_raster(self, waterboard_tif):
        """preprocess_raster returns [xmin, ymin, xmax, ymax] for a valid file."""
        metadata: dict = {}
        bounds = preprocess_raster(str(waterboard_tif), metadata)

        assert bounds is not None
        assert len(bounds) == 4
        xmin, ymin, xmax, ymax = bounds
        assert xmin < xmax, "xmin should be less than xmax"
        assert ymin < ymax, "ymin should be less than ymax"

    def test_transforms_non_4326_to_wgs84(self, waterboard_tif):
        """Web Mercator bounds should be reprojected to ~WGS84 lon/lat range."""
        metadata: dict = {}
        bounds = preprocess_raster(str(waterboard_tif), metadata)

        assert bounds is not None
        xmin, ymin, xmax, ymax = bounds
        # WGS84 longitude: -180..180, latitude: -90..90
        assert -180 <= xmin <= 180, f"xmin {xmin} out of WGS84 range"
        assert -180 <= xmax <= 180, f"xmax {xmax} out of WGS84 range"
        assert -90 <= ymin <= 90, f"ymin {ymin} out of WGS84 range"
        assert -90 <= ymax <= 90, f"ymax {ymax} out of WGS84 range"

    def test_stores_original_srid_in_metadata(self, waterboard_tif):
        """EPSG code should be recorded in metadata['original_srid']."""
        metadata: dict = {}
        preprocess_raster(str(waterboard_tif), metadata)

        assert "original_srid" in metadata
        # waterboard.tif is Web Mercator (3857)
        assert metadata["original_srid"] == 3857

    def test_single_band_stats(self, la_dem_26711):
        """1-band raster should have raster_value_stats_b1 with min/max."""
        metadata: dict = {}
        preprocess_raster(str(la_dem_26711), metadata)

        assert "raster_value_stats_b1" in metadata
        stats = metadata["raster_value_stats_b1"]
        assert "min" in stats and "max" in stats
        assert stats["min"] == pytest.approx(0.0, abs=1e-3)
        assert stats["max"] == pytest.approx(2441.0, abs=1e-3)

    def test_multiband_no_stats(self, waterboard_tif):
        """Multi-band raster should NOT have raster_value_stats_b1."""
        metadata: dict = {}
        preprocess_raster(str(waterboard_tif), metadata)

        assert "raster_value_stats_b1" not in metadata

    def test_frazier_dem_bounds_and_stats(self, frazier_dem):
        """Pin known-good values for frazier_8928_75m.dem.

        This is the fixture used by the existing test_gdal.py integration test.
        Bounds should be reprojected from NAD27/UTM to WGS84.
        """
        metadata: dict = {}
        bounds = preprocess_raster(str(frazier_dem), metadata)

        assert bounds is not None
        xmin, ymin, xmax, ymax = bounds
        # Expected WGS84 bounds from test_gdal.py reference:
        #   -119.000768, 34.747827, -118.875899, 34.877212
        assert xmin == pytest.approx(-119.001, abs=0.01)
        assert ymin == pytest.approx(34.748, abs=0.01)
        assert xmax == pytest.approx(-118.876, abs=0.01)
        assert ymax == pytest.approx(34.877, abs=0.01)

        assert "raster_value_stats_b1" in metadata
        assert metadata["raster_value_stats_b1"]["min"] == pytest.approx(963.0, abs=1)
        assert metadata["raster_value_stats_b1"]["max"] == pytest.approx(2443.0, abs=1)

    def test_geographic_crs_no_transform(self, la_dem_geographic):
        """NAD27 geographic DEM has coords already in ~lon/lat, bounds should be sane."""
        metadata: dict = {}
        bounds = preprocess_raster(str(la_dem_geographic), metadata)

        assert bounds is not None
        xmin, ymin, xmax, ymax = bounds
        # los_angeles-e.DEM covers roughly -119..-118, 34..35
        assert -120 < xmin < -117
        assert -120 < xmax < -117
        assert 33 < ymin < 36
        assert 33 < ymax < 36

    def test_raises_for_nonexistent_file(self, tmp_path):
        """Non-existent file raises RuntimeError (GDAL UseExceptions mode)."""
        missing = tmp_path / "does_not_exist.tif"
        metadata: dict = {}
        with pytest.raises(RuntimeError):
            preprocess_raster(str(missing), metadata)

    def test_raises_for_corrupt_file(self, tmp_path):
        """Corrupt file triggers GDAL RuntimeError (UseExceptions is active)."""
        bad_file = tmp_path / "corrupt.tif"
        bad_file.write_text("this is not a raster")
        metadata: dict = {}
        with pytest.raises(RuntimeError):
            preprocess_raster(str(bad_file), metadata)

    def test_metadata_mutated_in_place(self, frazier_dem):
        """preprocess_raster should mutate the dict passed in, not return a new one."""
        metadata = {"existing_key": "preserved"}
        preprocess_raster(str(frazier_dem), metadata)

        assert metadata["existing_key"] == "preserved"
        assert "raster_value_stats_b1" in metadata


# ===================================================================
# Section 2: RasterUploadHandler — handler contract tests (Py ≥3.10)
# ===================================================================


@_needs_py310
class TestRasterUploadHandler:
    """Verify the RasterUploadHandler implements the BaseUploadHandler contract."""

    @pytest.mark.anyio
    async def test_preprocess_returns_raster_type(self, waterboard_tif):
        """preprocess() should return HandlerResult with layer_type='raster'."""
        ctx = _make_ctx(str(waterboard_tif))
        handler = RasterUploadHandler()
        result = await handler.preprocess(ctx)

        assert isinstance(result, HandlerResult)
        assert result.layer_type == "raster"

    @pytest.mark.anyio
    async def test_preprocess_does_not_modify_file(self, waterboard_tif):
        """Raster preprocess should not convert or create temp files."""
        ctx = _make_ctx(str(waterboard_tif))
        handler = RasterUploadHandler()
        result = await handler.preprocess(ctx)

        assert result.updated_temp_file_path is None
        assert result.updated_s3_key is None
        assert result.updated_file_ext is None
        assert result.temp_dir_to_cleanup is None

    @pytest.mark.anyio
    async def test_create_layers_inserts_db_row(self, frazier_dem):
        """create_layers() should INSERT one row into map_layers."""
        ctx = _make_ctx(str(frazier_dem), file_ext=".dem")
        handler = RasterUploadHandler()
        result = HandlerResult(layer_type="raster")

        result = await handler.create_layers(ctx, result)

        # Should have called conn.execute once (INSERT INTO map_layers)
        ctx.conn.execute.assert_called_once()
        call_args = ctx.conn.execute.call_args

        # First positional arg is the SQL
        sql = call_args[0][0]
        assert "INSERT INTO map_layers" in sql

        # Layer ID matches context
        assert call_args[0][1] == "L_test"
        # Type is "raster"
        assert call_args[0][4] == "raster"

    @pytest.mark.anyio
    async def test_create_layers_populates_result(self, frazier_dem):
        """create_layers() should populate created_layer_ids and first_layer_url."""
        ctx = _make_ctx(str(frazier_dem), file_ext=".dem")
        handler = RasterUploadHandler()
        result = HandlerResult(layer_type="raster")

        result = await handler.create_layers(ctx, result)

        assert "L_test" in result.created_layer_ids
        assert result.first_layer_url == "/api/layer/L_test.cog.tif"
        assert result.first_layer_name == "Test Raster"

    @pytest.mark.anyio
    async def test_create_layers_extracts_bounds(self, frazier_dem):
        """create_layers() should extract bounds and store in result."""
        ctx = _make_ctx(str(frazier_dem), file_ext=".dem")
        handler = RasterUploadHandler()
        result = HandlerResult(layer_type="raster")

        result = await handler.create_layers(ctx, result)

        assert result.bounds is not None
        assert len(result.bounds) == 4
        # Should be WGS84
        xmin, ymin, xmax, ymax = result.bounds
        assert -180 <= xmin <= 180
        assert -90 <= ymin <= 90

    @pytest.mark.anyio
    async def test_create_layers_passes_metadata_as_json(self, frazier_dem):
        """Metadata dict should be JSON-serialized in the INSERT call."""
        ctx = _make_ctx(str(frazier_dem), file_ext=".dem", metadata_dict={"source": "test"})
        handler = RasterUploadHandler()
        result = HandlerResult(layer_type="raster")

        await handler.create_layers(ctx, result)

        call_args = ctx.conn.execute.call_args
        # 6th positional arg (index 5 in args[0]) is json.dumps(metadata_dict)
        metadata_json = call_args[0][5]
        parsed = json.loads(metadata_json)
        assert parsed["source"] == "test"
        # preprocess_raster should have added raster stats
        assert "raster_value_stats_b1" in parsed

    @pytest.mark.anyio
    async def test_handler_full_round_trip(self, la_dem_26711):
        """Full preprocess -> create_layers round trip."""
        ctx = _make_ctx(str(la_dem_26711))
        handler = RasterUploadHandler()

        # Phase 1: preprocess
        result = await handler.preprocess(ctx)
        assert result.layer_type == "raster"

        # Phase 2: create_layers
        result = await handler.create_layers(ctx, result)
        assert len(result.created_layer_ids) == 1
        assert result.bounds is not None
        assert result.first_layer_url is not None

        # Metadata should contain stats from the 1-band DEM
        assert "raster_value_stats_b1" in ctx.metadata_dict


# ===================================================================
# Section 3: Registry dispatch tests (Py >=3.10)
# ===================================================================


@_needs_py310
class TestRegistryRasterDispatch:
    """Verify the registry routes raster extensions to RasterUploadHandler."""

    @pytest.mark.parametrize("ext", [".tif", ".tiff", ".jpg", ".jpeg", ".png", ".dem"])
    def test_raster_extensions_dispatch_to_raster_handler(self, ext):
        """All raster extensions should resolve to RasterUploadHandler."""
        handler = get_handler(ext)
        assert isinstance(handler, RasterUploadHandler)

    @pytest.mark.parametrize("ext", [".tif", ".tiff", ".jpg", ".jpeg", ".png", ".dem"])
    def test_get_layer_type_returns_raster(self, ext):
        """get_layer_type should return 'raster' for raster extensions."""
        assert get_layer_type(ext) == "raster"

    def test_raster_exts_frozenset(self):
        """RASTER_EXTS should contain exactly the expected extensions."""
        expected = {".tif", ".tiff", ".jpg", ".jpeg", ".png", ".dem"}
        assert RASTER_EXTS == expected

    @pytest.mark.parametrize("ext", [".TIF", ".Tiff", ".JPG", ".DEM"])
    def test_case_insensitive_dispatch(self, ext):
        """Handler lookup should be case-insensitive."""
        handler = get_handler(ext)
        assert isinstance(handler, RasterUploadHandler)

    @pytest.mark.parametrize("ext", [".TIF", ".Tiff", ".JPG", ".DEM"])
    def test_case_insensitive_layer_type(self, ext):
        """get_layer_type should be case-insensitive."""
        assert get_layer_type(ext) == "raster"

    def test_non_raster_does_not_dispatch_to_raster(self):
        """Vector extension should NOT resolve to RasterUploadHandler."""
        handler = get_handler(".geojson")
        assert not isinstance(handler, RasterUploadHandler)

    def test_non_raster_layer_type(self):
        """Vector extension should return 'vector', not 'raster'."""
        assert get_layer_type(".geojson") == "vector"


# ===================================================================
# Section 4: Edge cases & regression guards (GDAL only, any Python)
# ===================================================================


class TestRasterEdgeCases:
    """Edge cases that should be pinned before the Dask swap."""

    def test_multiband_bounds_still_extracted(self, waterboard_tif):
        """3-band raster should still have valid bounds even without stats."""
        metadata: dict = {}
        bounds = preprocess_raster(str(waterboard_tif), metadata)
        assert bounds is not None

    def test_empty_metadata_dict_is_safe(self, frazier_dem):
        """Passing an empty dict should not raise."""
        metadata: dict = {}
        bounds = preprocess_raster(str(frazier_dem), metadata)
        assert bounds is not None
        assert len(metadata) > 0  # Should have been mutated

    def test_pre_existing_metadata_preserved(self, frazier_dem):
        """Pre-existing metadata keys should survive preprocessing."""
        metadata = {"user_note": "important", "tags": ["agriculture"]}
        preprocess_raster(str(frazier_dem), metadata)
        assert metadata["user_note"] == "important"
        assert metadata["tags"] == ["agriculture"]

    @_needs_py310
    @pytest.mark.anyio
    async def test_handler_result_serializable(self, frazier_dem):
        """HandlerResult fields should be JSON-serializable for logging."""
        ctx = _make_ctx(str(frazier_dem), file_ext=".dem")
        handler = RasterUploadHandler()
        result = await handler.preprocess(ctx)

        # Ensure all fields can be serialized
        serializable = {
            "layer_type": result.layer_type,
            "bounds": result.bounds,
            "created_layer_ids": result.created_layer_ids,
            "first_layer_url": result.first_layer_url,
            "updated_temp_file_path": result.updated_temp_file_path,
        }
        json_str = json.dumps(serializable)
        assert json_str  # Should not raise


# ===================================================================
# Section 5: Dask raster pipeline tests
# ===================================================================

from src.upload.dask_raster import DASK_AVAILABLE

_needs_dask = pytest.mark.skipif(
    not DASK_AVAILABLE, reason="Dask/rioxarray/rasterio not installed"
)


@_needs_dask
class TestDaskMetadataExtraction:
    """Validate Dask pipeline metadata extraction against GDAL baseline."""

    def test_extract_bounds_matches_gdal(self, frazier_dem):
        """Dask-extracted bounds should match preprocess_raster bounds."""
        from src.upload.dask_raster import extract_raster_metadata

        gdal_meta: dict = {}
        gdal_bounds = preprocess_raster(str(frazier_dem), gdal_meta)

        dask_meta = extract_raster_metadata(str(frazier_dem))
        dask_bounds = dask_meta["bounds"]

        assert gdal_bounds is not None
        assert dask_bounds is not None
        for g, d in zip(gdal_bounds, dask_bounds):
            assert g == pytest.approx(d, abs=0.01), (
                f"Bounds mismatch: GDAL={gdal_bounds}, Dask={dask_bounds}"
            )

    def test_extract_srid_matches_gdal(self, waterboard_tif):
        """Dask-extracted SRID should match GDAL."""
        from src.upload.dask_raster import extract_raster_metadata

        gdal_meta: dict = {}
        preprocess_raster(str(waterboard_tif), gdal_meta)

        dask_meta = extract_raster_metadata(str(waterboard_tif))

        assert dask_meta["original_srid"] == gdal_meta.get("original_srid")

    def test_extract_band_stats_matches_gdal(self, la_dem_26711):
        """Dask-extracted min/max should match GDAL ComputeStatistics."""
        from src.upload.dask_raster import extract_raster_metadata

        gdal_meta: dict = {}
        preprocess_raster(str(la_dem_26711), gdal_meta)

        dask_meta = extract_raster_metadata(str(la_dem_26711))

        gdal_stats = gdal_meta.get("raster_value_stats_b1")
        dask_stats = dask_meta.get("raster_value_stats_b1")

        assert gdal_stats is not None
        assert dask_stats is not None
        assert gdal_stats["min"] == pytest.approx(dask_stats["min"], abs=1e-3)
        assert gdal_stats["max"] == pytest.approx(dask_stats["max"], abs=1e-3)

    def test_multiband_no_stats(self, waterboard_tif):
        """Multi-band raster should not have band statistics."""
        from src.upload.dask_raster import extract_raster_metadata

        meta = extract_raster_metadata(str(waterboard_tif))
        assert meta["raster_value_stats_b1"] is None
        assert meta["band_count"] == 3

    def test_extract_dimensions(self, waterboard_tif):
        """Width and height should be positive."""
        from src.upload.dask_raster import extract_raster_metadata

        meta = extract_raster_metadata(str(waterboard_tif))
        assert meta["width"] > 0
        assert meta["height"] > 0

    def test_frazier_dem_known_values(self, frazier_dem):
        """Pin known-good Dask values against the same frazier DEM fixture."""
        from src.upload.dask_raster import extract_raster_metadata

        meta = extract_raster_metadata(str(frazier_dem))

        assert meta["bounds"] is not None
        xmin, ymin, xmax, ymax = meta["bounds"]
        assert xmin == pytest.approx(-119.001, abs=0.01)
        assert ymin == pytest.approx(34.748, abs=0.01)
        assert xmax == pytest.approx(-118.876, abs=0.01)
        assert ymax == pytest.approx(34.877, abs=0.01)

        assert meta["raster_value_stats_b1"] is not None
        assert meta["raster_value_stats_b1"]["min"] == pytest.approx(963.0, abs=1)
        assert meta["raster_value_stats_b1"]["max"] == pytest.approx(2443.0, abs=1)


@_needs_dask
class TestDaskCogGeneration:
    """Validate Dask-based COG generation."""

    def test_generate_cog_creates_valid_file(self, frazier_dem, tmp_path):
        """generate_cog should create a valid COG file."""
        from src.upload.dask_raster import generate_cog

        output = tmp_path / "test.cog.tif"
        result = generate_cog(str(frazier_dem), str(output))

        assert result == str(output)
        assert output.exists()
        assert output.stat().st_size > 0

    def test_cog_is_epsg_3857(self, frazier_dem, tmp_path):
        """Generated COG should be in EPSG:3857."""
        import rasterio
        from src.upload.dask_raster import generate_cog

        output = tmp_path / "test.cog.tif"
        generate_cog(str(frazier_dem), str(output))

        with rasterio.open(str(output)) as src:
            assert src.crs is not None
            assert src.crs.to_epsg() == 3857

    def test_cog_is_tiled(self, frazier_dem, tmp_path):
        """Generated COG should have internal tiling."""
        import rasterio
        from src.upload.dask_raster import generate_cog

        output = tmp_path / "test.cog.tif"
        generate_cog(str(frazier_dem), str(output))

        with rasterio.open(str(output)) as src:
            assert src.profile.get("tiled") or src.is_tiled

    def test_multiband_cog(self, waterboard_tif, tmp_path):
        """3-band raster should produce a valid 3-band COG."""
        import rasterio
        from src.upload.dask_raster import generate_cog

        output = tmp_path / "test_rgb.cog.tif"
        generate_cog(str(waterboard_tif), str(output))

        assert output.exists()
        with rasterio.open(str(output)) as src:
            assert src.count >= 3
            assert src.crs.to_epsg() == 3857


@_needs_dask
class TestRasterPipelineOrchestrator:
    """Validate the RasterPipeline orchestrator class."""

    def test_is_available(self):
        """Pipeline should report as available when deps are installed."""
        from src.upload.dask_raster import RasterPipeline

        assert RasterPipeline.is_available() is True

    def test_extract_metadata_returns_dict(self, frazier_dem):
        """extract_metadata should return a complete dict."""
        from src.upload.dask_raster import RasterPipeline

        meta = RasterPipeline.extract_metadata(str(frazier_dem))
        assert isinstance(meta, dict)
        assert "bounds" in meta
        assert "original_srid" in meta
        assert "raster_value_stats_b1" in meta

    def test_apply_metadata_to_dict(self, frazier_dem):
        """apply_metadata_to_dict should mutate the dict and return bounds."""
        from src.upload.dask_raster import RasterPipeline

        extracted = RasterPipeline.extract_metadata(str(frazier_dem))
        target = {"existing": "value"}
        bounds = RasterPipeline.apply_metadata_to_dict(target, extracted)

        assert bounds is not None
        assert target["existing"] == "value"
        assert "raster_value_stats_b1" in target

    def test_create_cog_with_auto_path(self, frazier_dem):
        """create_cog with no output path should create a temp file."""
        import os
        from src.upload.dask_raster import RasterPipeline

        result = RasterPipeline.create_cog(str(frazier_dem))
        assert os.path.exists(result)
        assert os.path.getsize(result) > 0
        # Clean up
        os.unlink(result)

    def test_graceful_failure_on_bad_file(self, tmp_path):
        """extract_metadata should return defaults for an invalid file."""
        from src.upload.dask_raster import RasterPipeline

        bad_file = tmp_path / "bad.tif"
        bad_file.write_text("not a raster")

        meta = RasterPipeline.extract_metadata(str(bad_file))
        assert meta["bounds"] is None
        assert meta["band_count"] == 0
