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
    return drone_plots.measure_own_plots(str(photo), _outlines_wgs84(), "Test plots")


def test_plots_are_numbered_from_the_north_west_with_true_areas(plots):
    props = plots.plots()
    assert [p["number"] for p in props] == [1, 2, 3, 4, 5, 6]
    assert all(p["area_ha"] == pytest.approx(0.64, rel=0.02) for p in props)
    # first row (north) west to east, then the second row
    assert props[0]["lon"] < props[1]["lon"] < props[2]["lon"]
    assert props[0]["lat"] > props[3]["lat"]
    assert plots.total_ha == pytest.approx(6 * 0.64, rel=0.02)


def test_the_bare_plot_is_the_least_green_and_mostly_bare(plots):
    by_number = {p["number"]: p for p in plots.plots()}
    bare = by_number[BARE_PLOT + 1]
    assert bare["group"] == drone_plots.LEAST_GREEN
    assert bare["bare_share"] > 0.9
    assert by_number[6]["group"] == drone_plots.GREENEST
    assert min(plots.plots(), key=lambda p: p["greenness"])["number"] == BARE_PLOT + 1


def test_groups_need_five_measured_plots_and_keep_unknown_apart():
    assert drone_plots._groups([0.1, 0.2, None]) == [drone_plots.BETWEEN, drone_plots.BETWEEN, drone_plots.UNKNOWN]
    groups = drone_plots._groups([0.05, 0.1, 0.15, 0.2, 0.25, None])
    assert groups[0] == drone_plots.LEAST_GREEN and groups[4] == drone_plots.GREENEST
    assert groups[5] == drone_plots.UNKNOWN


def test_a_plot_off_the_photo_is_not_measured(photo):
    to_wgs84 = Transformer.from_crs("EPSG:32735", "EPSG:4326", always_xy=True).transform
    outside = reproject(to_wgs84, box(X0 + 400, Y0 - 100, X0 + 480, Y0 - 20))  # east of the 300 m photo
    result = drone_plots.measure_own_plots(str(photo), [outside], "Test plots")
    assert result.plots()[0]["greenness"] is None and result.plots()[0]["group"] == drone_plots.UNKNOWN


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
