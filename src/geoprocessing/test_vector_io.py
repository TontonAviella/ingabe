from pathlib import Path

import pytest

from src.geoprocessing.vector_io import (
    compute_vector_bounds,
    list_renderable_vector_layers,
    read_vector_feature_records,
    write_vector_enrichment,
)


def test_read_vector_feature_records_uses_geojson_shape(tmp_path) -> None:
    import geopandas as gpd
    from shapely.geometry import Point

    path = tmp_path / "sample.geojson"
    gdf = gpd.GeoDataFrame(
        {"name": ["alpha", "beta"]},
        geometry=[Point(30.0, -2.0), Point(31.0, -1.0)],
        crs="EPSG:4326",
    )
    gdf.to_file(path, engine="pyogrio")

    records = read_vector_feature_records(str(path))

    assert [record.feature_id for record in records] == [1, 2]
    assert records[0].properties == {"name": "alpha"}
    assert records[0].geometry == {"type": "Point", "coordinates": (30.0, -2.0)}


def test_compute_vector_bounds_reprojects_to_wgs84(tmp_path) -> None:
    import geopandas as gpd
    from shapely.geometry import Point

    path = tmp_path / "sample_3857.geojson"
    gdf = gpd.GeoDataFrame(
        {"name": ["alpha", "beta"]},
        geometry=[Point(30.0, -2.0), Point(31.0, -1.0)],
        crs="EPSG:4326",
    ).to_crs("EPSG:3857")
    gdf.to_file(path, engine="pyogrio")

    bounds = compute_vector_bounds(str(path))

    assert bounds is not None
    assert bounds == pytest.approx([30.0, -2.0, 31.0, -1.0], abs=1e-6)


def test_write_vector_enrichment_preserves_features(tmp_path) -> None:
    import geopandas as gpd
    import pyogrio
    from shapely.geometry import Point

    input_path = tmp_path / "input.fgb"
    output_path = tmp_path / "output.fgb"
    gdf = gpd.GeoDataFrame(
        {"name": ["alpha", "beta"]},
        geometry=[Point(30.0, -2.0), Point(31.0, -1.0)],
        crs="EPSG:4326",
    )
    gdf.to_file(input_path, engine="pyogrio")
    input_records = read_vector_feature_records(str(input_path))

    write_vector_enrichment(
        str(input_path),
        str(output_path),
        "risk_score",
        {1: 12.5, 2: 44.0},
    )

    enriched = pyogrio.read_dataframe(output_path)
    expected = {
        record.properties["name"]: {1: 12.5, 2: 44.0}[record.feature_id]
        for record in input_records
    }
    actual = dict(zip(enriched["name"], enriched["risk_score"]))
    assert actual == expected


def test_list_renderable_vector_layers_filters_kml_overlays() -> None:
    path = Path(__file__).parent.parent.parent / "test_fixtures" / "KML_Samples.kml"

    layers = list_renderable_vector_layers(str(path))

    assert set(layers) == {
        "Absolute and Relative",
        "Highlighted Icon",
        "Google Campus",
        "Extruded Polygon",
        "Placemarks",
        "Paths",
    }
