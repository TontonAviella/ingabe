"""Plots on a synthetic photo: six plots of known size and colour, one of them bare."""

from __future__ import annotations

import io
import json
import zipfile

import numpy as np
import openpyxl
import pytest
import rasterio
from pyproj import Transformer
from rasterio.transform import from_origin
from shapely.geometry import box
from shapely.ops import transform as reproject

from src.services import drone_plots

PIXEL_M = 0.5
SIZE = 600  # 300 m x 300 m
X0, Y0 = 830000, 9810000  # UTM 35S, Rwanda
PLOT_M = 80  # each plot 80 m x 80 m = 0.64 ha
# Plot corners (column, row) in metres from the north-west corner; the third is bare, the sixth greenest.
PLOTS = [(10, 10), (110, 10), (210, 10), (10, 110), (110, 110), (210, 110)]
GREEN = [140, 140, 100, 140, 140, 175]  # green band value per plot; red is 90 everywhere on plots
BARE_PLOT = 2  # index: red above green, so bare soil


@pytest.fixture
def photo(tmp_path):
    red = np.full((SIZE, SIZE), 90, dtype="uint8")
    green = np.full((SIZE, SIZE), 120, dtype="uint8")
    blue = np.full((SIZE, SIZE), 60, dtype="uint8")
    for (cx, cy), g in zip(PLOTS, GREEN):
        rows, cols = slice(int(cy / PIXEL_M), int((cy + PLOT_M) / PIXEL_M)), slice(int(cx / PIXEL_M), int((cx + PLOT_M) / PIXEL_M))
        green[rows, cols] = g
    red[int(10 / PIXEL_M):int(90 / PIXEL_M), int(210 / PIXEL_M):int(290 / PIXEL_M)] = 150  # the bare plot
    path = tmp_path / "plots.tif"
    with rasterio.open(path, "w", driver="GTiff", width=SIZE, height=SIZE, count=3, dtype="uint8",
                       crs="EPSG:32735", transform=from_origin(X0, Y0, PIXEL_M, PIXEL_M)) as ds:
        ds.write(np.stack([red, green, blue]))
    return path


def _outlines_wgs84():
    to_wgs84 = Transformer.from_crs("EPSG:32735", "EPSG:4326", always_xy=True).transform
    return [reproject(to_wgs84, box(X0 + cx, Y0 - cy - PLOT_M, X0 + cx + PLOT_M, Y0 - cy)) for cx, cy in PLOTS]


@pytest.fixture
def plots(photo):
    names = ["A-01", "A-02", "A-03", "B-01", "B-02", "Plot with a very long name"]
    own = [drone_plots.MapPlot(g, name, {"Farmer": f"Farmer {i}"}) for i, (g, name) in enumerate(zip(_outlines_wgs84(), names))]
    return drone_plots.measure_own_plots(str(photo), own, "Test plots")


def test_plots_are_numbered_from_the_north_west_with_true_areas(plots):
    props = plots.plots()
    assert [p["number"] for p in props] == [1, 2, 3, 4, 5, 6]
    assert all(p["area_ha"] == pytest.approx(0.64, rel=0.02) for p in props)
    # first row (north) west to east, then the second row
    assert props[0]["lon"] < props[1]["lon"] < props[2]["lon"]
    assert props[0]["lat"] > props[3]["lat"]
    assert plots.total_ha == pytest.approx(6 * 0.64, rel=0.02)


def test_the_bare_plot_is_set_apart_and_the_others_compared(plots):
    by_number = {p["number"]: p for p in plots.plots()}
    bare = by_number[BARE_PLOT + 1]
    assert bare["bare_share"] > 0.9 and bare["group"] == drone_plots.MOSTLY_SOIL
    assert by_number[6]["group"] == drone_plots.GREENEST
    assert {by_number[n]["group"] for n in (1, 2, 4, 5)} == {drone_plots.LEAST_GREEN}  # tied, all at the cut


def test_groups_compare_only_plots_with_a_crop():
    crop = [(0.05, 0.1), (0.1, 0.0), (0.15, 0.2), (0.2, 0.0), (0.25, 0.1)]
    groups = drone_plots.plot_groups(crop + [(None, None), (-0.1, 0.9)])
    assert groups[0] == drone_plots.LEAST_GREEN and groups[4] == drone_plots.GREENEST
    assert groups[5] == drone_plots.UNKNOWN and groups[6] == drone_plots.MOSTLY_SOIL
    few = drone_plots.plot_groups([(0.1, 0.0), (0.2, 0.0), (-0.2, 0.95)])
    assert few == [drone_plots.BETWEEN, drone_plots.BETWEEN, drone_plots.MOSTLY_SOIL]


def test_plots_off_the_photo_are_left_out_and_half_off_is_not_measured(photo):
    to_wgs84 = Transformer.from_crs("EPSG:32735", "EPSG:4326", always_xy=True).transform
    outside = reproject(to_wgs84, box(X0 + 400, Y0 - 100, X0 + 480, Y0 - 20))  # east of the 300 m photo
    half_off = reproject(to_wgs84, box(X0 + 260, Y0 - 200, X0 + 340, Y0 - 120))  # 40 of its 80 m on the photo
    result = drone_plots.measure_own_plots(
        str(photo), [drone_plots.MapPlot(outside, "out"), drone_plots.MapPlot(half_off, "half")], "Test plots")
    assert [p["name"] for p in result.plots()] == ["half"]
    assert result.plots()[0]["greenness"] is not None  # exactly half on the photo is still measured


def test_own_plot_names_and_columns_are_kept(plots):
    by_number = {p["number"]: p for p in plots.plots()}
    assert by_number[1]["name"] == "A-01" and by_number[1]["tag"] == "A-01"
    assert by_number[6]["tag"] == "6"  # a long name shows as the plot's number on the photo
    assert by_number[1]["attributes"] == {"Farmer": "Farmer 0"}
    rows = list(openpyxl.load_workbook(io.BytesIO(drone_plots.to_xlsx(plots, "Test photo")))["Plots"].values)
    assert rows[0][1] == "Name on your map" and rows[0][-1] == "Farmer"
    assert rows[1][1] == "A-01" and rows[1][-1] == "Farmer 0"


def test_a_plot_map_file_is_read_with_its_names(tmp_path):
    import geopandas as gpd

    to_wgs84 = Transformer.from_crs("EPSG:32735", "EPSG:4326", always_xy=True).transform
    frame = gpd.GeoDataFrame(
        {"UPI": ["1/02/03/04/1", "1/02/03/04/2", None], "Name": ["north", "south", "well"], "Crop": ["maize", None, None]},
        geometry=[reproject(to_wgs84, box(X0, Y0 - 50, X0 + 50, Y0)), reproject(to_wgs84, box(X0, Y0 - 100, X0 + 50, Y0 - 50)),
                  reproject(to_wgs84, box(X0, Y0, X0 + 1, Y0 + 1)).centroid],
        crs="EPSG:4326",
    ).to_crs("EPSG:32735")
    path = tmp_path / "plots.gpkg"
    frame.to_file(path, driver="GPKG")
    plots = drone_plots.read_plot_map(str(path))
    assert [p.name for p in plots] == ["1/02/03/04/1", "1/02/03/04/2"]  # UPI wins over Name; the point is not a plot
    assert plots[0].attributes == {"UPI": "1/02/03/04/1", "Name": "north", "Crop": "maize"}
    assert plots[1].attributes == {"UPI": "1/02/03/04/2", "Name": "south"}
    assert -2 < plots[0].geometry.centroid.y < -1  # back in WGS84 over Rwanda


def test_the_plot_name_column_is_found_whatever_its_case():
    assert drone_plots._name_column(["geometry", "Farmer", "PLOT_ID", "name"]) == "PLOT_ID"
    assert drone_plots._name_column(["Farmer", "Crop"]) is None


def test_duplicates_from_overlapping_tiles_are_dropped_and_small_overlaps_cut():
    a = box(0, 0, 10, 10)
    duplicate = box(1, 0, 11, 10)  # 90% shared with a
    neighbour = box(9, 0, 19, 10)  # 10% shared with a
    kept = drone_plots._keep_distinct([(0.9, a), (0.5, duplicate), (0.6, neighbour)])
    assert len(kept) == 2
    assert kept[1][1].intersection(a).area == pytest.approx(0, abs=1e-9)


def test_an_outline_around_kept_plots_is_a_block_not_a_plot():
    plots = [(0.6, box(10, 10, 20, 20)), (0.5, box(30, 10, 40, 20))]
    block = (0.15, box(0, 0, 100, 50))  # the plots cover 8% of it: a small overlap, but it surrounds them
    kept = drone_plots._keep_distinct(plots + [block])
    assert [conf for conf, _ in kept] == [0.6, 0.5]


def test_a_plot_with_a_tree_inside_keeps_its_outline():
    tree = (0.6, box(40, 20, 45, 25))
    plot = (0.3, box(0, 0, 100, 50))
    kept = drone_plots._keep_distinct([tree, plot])
    assert len(kept) == 2 and len(kept[1][1].interiors) == 1


def test_outlines_cut_by_an_inner_tile_edge_are_left_to_the_next_tile():
    width = height = 3000
    inner = (0, 100, 1021, 300)  # touches the east edge of the first tile, which has a neighbour
    assert drone_plots._touches_inner_edge(inner, 0, 0, width, height)
    photo_edge = (0, 100, 300, 300)  # touches the photo's own west edge only
    assert not drone_plots._touches_inner_edge(photo_edge, 0, 0, width, height)


def test_tiles_cover_the_whole_read_with_overlap():
    starts = drone_plots._tile_starts(3118)
    assert starts[0] == 0 and starts[-1] + drone_plots.TILE_PX == 3118
    assert all(b - a <= drone_plots.TILE_PX - drone_plots.TILE_OVERLAP_PX for a, b in zip(starts, starts[1:]))


def test_excel_has_one_row_per_plot_and_says_how_it_was_made(plots):
    book = openpyxl.load_workbook(io.BytesIO(drone_plots.to_xlsx(plots, "Test photo")))
    rows = list(book["Plots"].values)
    assert rows[0][0] == "Plot" and len(rows) == 1 + plots.count
    assert "How it was made" in book.sheetnames


def test_shapefile_zip_opens_with_every_plot(plots, tmp_path):
    raw = drone_plots.to_shapefile_zip(plots, "test_plots")
    names = zipfile.ZipFile(io.BytesIO(raw)).namelist()
    assert {"test_plots.shp", "test_plots.dbf", "test_plots.prj"} <= set(names)
    import geopandas as gpd

    path = tmp_path / "plots.zip"
    path.write_bytes(raw)
    frame = gpd.read_file(f"zip://{path}")
    assert len(frame) == plots.count and "plot" in frame.columns


def test_kept_plots_round_trip_through_json(plots):
    again = drone_plots._from_json(drone_plots._to_json(plots))
    assert again.geojson == json.loads(json.dumps(plots.geojson))  # tuples come back as lists
    assert (again.found_at, again.source, again.count) == (plots.found_at, plots.source, plots.count)
    assert json.loads(drone_plots.to_geojson(plots))["type"] == "FeatureCollection"


def test_a_kml_plot_map_keeps_its_names_and_drops_google_earth_fields(tmp_path):
    ring = "30.4270,-1.6950,0 30.4275,-1.6950,0 30.4275,-1.6955,0 30.4270,-1.6955,0 30.4270,-1.6950,0"
    path = tmp_path / "blocks.kml"
    path.write_text(f"""<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2"><Document><Placemark>
  <name>Block A-01</name>
  <ExtendedData><Data name="Farmer"><value>Test farmer 1</value></Data></ExtendedData>
  <Polygon><tessellate>1</tessellate><outerBoundaryIs><LinearRing><coordinates>{ring}</coordinates></LinearRing></outerBoundaryIs></Polygon>
</Placemark></Document></kml>""")
    plots = drone_plots.read_plot_map(str(path))
    assert [p.name for p in plots] == ["Block A-01"]
    assert plots[0].attributes == {"Name": "Block A-01", "Farmer": "Test farmer 1"}
