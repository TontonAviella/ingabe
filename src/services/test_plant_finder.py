"""The plant finder on a made-up photo. The two models are replaced by stand-ins that answer by position, so the
tests are about what the finder does with their answers: which squares are read, refused and counted, which boxes
belong to the plot, and which green is flagged as other green.

One test runs the real models on the 57 reference squares when PLANT_FINDER_TEST_BUNDLE names their folder (the
models are 98 MB, so they are not in the repo); without it that test is skipped and says so.
"""

from __future__ import annotations

import json
import os

import numpy as np
import pytest
import rasterio
from PIL import Image
from pyproj import Transformer
from rasterio.transform import from_origin
from shapely.geometry import box, mapping
from shapely.ops import transform as reproject

from src.services import plant_finder

X0, Y0 = 830000.0, 9810000.0  # UTM 35S, near Kigali
PIXEL_M = 0.0325
N = plant_finder.SQUARE_PX
SOIL, GREEN = (196, 158, 118), (70, 150, 55)


def _photo(tmp_path, pixel_m=PIXEL_M, squares=(3, 2)):
    """A photo of bare soil, `squares` grid squares wide and tall, with a round green tuft every 2 m."""
    width, height = squares[0] * N, squares[1] * N
    rgb = np.empty((3, height, width), dtype="uint8")
    for band, value in enumerate(SOIL):
        rgb[band] = value
    yy, xx = np.mgrid[0:height, 0:width]
    for cy in range(30, height, 62):  # 62 px is about 2 m
        for cx in range(30, width, 62):
            tuft = (yy - cy) ** 2 + (xx - cx) ** 2 <= 6 ** 2
            for band, value in enumerate(GREEN):
                rgb[band][tuft] = value
    path = str(tmp_path / "photo.tif")
    with rasterio.open(path, "w", driver="GTiff", width=width, height=height, count=3, dtype="uint8", crs="EPSG:32735",
                       transform=from_origin(X0, Y0, pixel_m, pixel_m)) as ds:
        ds.write(rgb)
    return path


def _plot(col0, row0, col1, row1, pixel_m=PIXEL_M, number=7):
    """A plot given by its corners in photo pixels, as a WGS84 feature."""
    to_wgs84 = Transformer.from_crs("EPSG:32735", "EPSG:4326", always_xy=True).transform
    shape_ = reproject(to_wgs84, box(X0 + col0 * pixel_m, Y0 - row1 * pixel_m, X0 + col1 * pixel_m, Y0 - row0 * pixel_m))
    area = (col1 - col0) * (row1 - row0) * pixel_m ** 2 / 10_000
    return {"type": "Feature", "geometry": mapping(shape_), "properties": {"number": number, "area_ha": area}}


@pytest.fixture
def stand_ins(monkeypatch):
    """Every square scores `scores[(j, i)]` (default 0.9) and holds one plant box around each tuft whose column
    index is even, so half the tufts are plants and half are other green."""
    scores: dict[tuple[int, int], float] = {}
    seen: list[tuple[int, int]] = []

    def where(rgb):
        return int(rgb[0, 0, 0]), int(rgb[0, 0, 1])  # the square's (j, i), written into its corner by _mark

    def score(models, rgb):
        seen.append(where(rgb))
        return scores.get(where(rgb), 0.9)

    def boxes(models, rgb):
        j, i = where(rgb)
        found = []
        for cy in range(30, 100 * N, 62):
            for k, cx in enumerate(range(30, 100 * N, 62)):
                x, y = cx - j * N, cy - i * N
                if k % 2 == 0 and 0 <= x < N and 0 <= y < N:
                    found.append((x - 8.0, y - 8.0, x + 8.0, y + 8.0))
        return found

    monkeypatch.setattr(plant_finder, "picture_score", score)
    monkeypatch.setattr(plant_finder, "plant_boxes", boxes)
    return scores, seen


def _mark(path):
    """Write each square's (j, i) into its top-left pixel, so the stand-ins know which square they were given."""
    with rasterio.open(path, "r+") as ds:
        rgb = ds.read()
        for i in range(ds.height // N):
            for j in range(ds.width // N):
                rgb[0, i * N, j * N], rgb[1, i * N, j * N] = j, i
        ds.write(rgb)
    return path


def test_plants_are_the_boxes_in_the_plot_and_the_other_green_is_what_is_left(tmp_path, stand_ins):
    path = _mark(_photo(tmp_path))
    found = plant_finder.find_plants(path, _plot(0, 0, 2 * N, N), models=None)  # the two left squares of the top row
    assert (found.squares, found.squares_counted, found.squares_refused) == (2, 2, 0)
    assert found.counted_share == pytest.approx(1.0, abs=0.01)
    tufts = len(range(30, N, 62)) * len(range(30, 2 * N, 62))  # tufts inside the plot
    assert found.plants + found.other_green == tufts
    assert found.plants == len(range(30, N, 62)) * len([k for k, cx in enumerate(range(30, 100 * N, 62)) if k % 2 == 0 and cx < 2 * N])
    kinds = [(f["properties"]["kind"], f["properties"]["kind_code"]) for f in found.points["features"]]
    assert kinds.count(("plant", 0)) == found.plants and kinds.count(("other_green", 1)) == found.other_green
    assert found.per_ha == pytest.approx(found.plants / found.counted_area_m2 * 10_000, rel=0.01)
    # every point lies in the plot
    west, south, east, north = box(*[c for c in _bounds(_plot(0, 0, 2 * N, N))]).bounds
    assert all(west <= f["geometry"]["coordinates"][0] <= east and south <= f["geometry"]["coordinates"][1] <= north
               for f in found.points["features"])


def _bounds(feature):
    xs = [c[0] for c in feature["geometry"]["coordinates"][0]]
    ys = [c[1] for c in feature["geometry"]["coordinates"][0]]
    return min(xs), min(ys), max(xs), max(ys)


def test_a_square_the_picture_check_refuses_is_not_counted_and_the_answer_says_how_much_was(tmp_path, stand_ins):
    scores, seen = stand_ins
    scores[(1, 0)] = 0.5  # the second square looks like ground where plants cannot be told apart
    path = _mark(_photo(tmp_path))
    whole = plant_finder.find_plants(path, _plot(0, 0, 2 * N, N), models=None)
    assert (whole.squares_counted, whole.squares_refused) == (1, 1)
    assert whole.counted_share == pytest.approx(0.5, abs=0.01)
    # nothing from the refused square: no plant, and no other green either (unknown there, not "no weeds")
    east_of_first = _bounds(_plot(N, 0, 2 * N, N))[0]
    assert all(f["geometry"]["coordinates"][0] < east_of_first for f in whole.points["features"])
    # a sliver of a square (under a tenth of it) is left out without being read
    seen.clear()
    sliver = plant_finder.find_plants(path, _plot(0, 0, N + 20, N), models=None)
    assert seen == [(0, 0)] and sliver.squares == 1 and sliver.counted_share < 1.0
    # every square refused: nothing counted, and no density is made up
    scores[(0, 0)] = 0.1
    none = plant_finder.find_plants(path, _plot(0, 0, 2 * N, N), models=None)
    assert (none.plants, none.other_green, none.squares_counted, none.per_ha) == (0, 0, 0, None)


def test_a_photo_or_plot_outside_what_was_measured_is_refused(tmp_path, stand_ins):
    coarse = _photo(tmp_path, pixel_m=0.05)
    with pytest.raises(plant_finder.CannotCount, match="5.0 cm per pixel"):
        plant_finder.find_plants(coarse, _plot(0, 0, N, N, pixel_m=0.05), models=None)
    path = _photo(tmp_path)
    big = _plot(0, 0, 2 * N, N)
    big["properties"]["area_ha"] = 0.8
    with pytest.raises(plant_finder.CannotCount, match="up to 0.5 ha"):
        plant_finder.find_plants(path, big, models=None)


def test_of_two_boxes_on_one_plant_the_surer_one_stays():
    boxes = [[0, 0, 10, 10], [1, 1, 11, 11], [50, 50, 60, 60], [200, 200, 210, 210]]
    assert plant_finder.kept([0.5, 0.9, 0.2, 0.1], boxes) == [1, 2]  # the second wins the pair; the last is under the cut
    assert plant_finder.kept([0.5, 0.9], [[0, 0, 10, 10], [8, 8, 18, 18]]) == [1, 0]  # a small overlap: two plants


@pytest.mark.skipif(not os.environ.get("PLANT_FINDER_TEST_BUNDLE"),
                    reason="set PLANT_FINDER_TEST_BUNDLE to the folder with student.onnx, dino_small.onnx, rule.npz, "
                           "expected.json and tiles/ to run the real models")
def test_the_real_models_give_the_tested_answers_on_the_reference_squares():
    bundle = os.environ["PLANT_FINDER_TEST_BUNDLE"]
    models = plant_finder.Models(f"{bundle}/student.onnx", f"{bundle}/dino_small.onnx", f"{bundle}/rule.npz")
    expected = json.load(open(f"{bundle}/expected.json"))
    assert (expected["student_cut"], expected["student_iou"], expected["picture_cut"]) == (
        plant_finder.STUDENT_CUT, plant_finder.STUDENT_OVERLAP, plant_finder.PICTURE_CUT)
    off, flips = [], 0
    for square in expected["squares"]:
        rgb = np.asarray(Image.open(f"{bundle}/tiles/{square}.png").convert("RGB"))
        if len(plant_finder.plant_boxes(models, rgb)) != expected["student_count"][square]:
            off.append(square)
            assert abs(len(plant_finder.plant_boxes(models, rgb)) - expected["student_count"][square]) <= 1
        score = plant_finder.picture_score(models, rgb)
        assert score == pytest.approx(expected["picture_score"][square], abs=0.001)
        flips += (score >= plant_finder.PICTURE_CUT) != (expected["picture_score"][square] >= plant_finder.PICTURE_CUT)
    assert len(off) <= 1 and flips == 0  # measured 2026-11-14: e172 gives 156 for 155; no square changes sides
