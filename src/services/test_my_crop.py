"""Only my crop on a made-up photo: tufts in rows, even rows one colour (the reader's crop), odd rows another. The
models are replaced by stand-ins (the plant finder boxes every tuft in an even column; a patch's look is its mean
colour), so the tests are about what the service does with their answers: which boxes become the reader's crop,
what is kept per square, what a tap may be, and the background job.
"""

from __future__ import annotations

import asyncio

import numpy as np
import pytest
import rasterio
from botocore.exceptions import ClientError
from PIL import Image
from pyproj import Transformer
from rasterio.transform import from_origin

from src.services import my_crop, plant_finder
from src.services.test_plant_finder import PIXEL_M, SOIL, X0, Y0, N, _plot

MINE, THEIRS = (70, 150, 55), (40, 60, 160)
STEP = 62  # about 2 m between tufts


def _photo(tmp_path, squares=(2, 1)):
    width, height = squares[0] * N, squares[1] * N
    rgb = np.empty((3, height, width), dtype="uint8")
    for band, value in enumerate(SOIL):
        rgb[band] = value
    yy, xx = np.mgrid[0:height, 0:width]
    for r, cy in enumerate(range(30, height, STEP)):
        for cx in range(30, width, STEP):
            tuft = (yy - cy) ** 2 + (xx - cx) ** 2 <= 8 ** 2
            for band, value in enumerate(MINE if r % 2 == 0 else THEIRS):
                rgb[band][tuft] = value
    for i in range(squares[1]):  # each square's (j, i) in its corner, for the stand-ins
        for j in range(squares[0]):
            rgb[0, i * N, j * N], rgb[1, i * N, j * N] = j, i
    path = str(tmp_path / "photo.tif")
    with rasterio.open(path, "w", driver="GTiff", width=width, height=height, count=3, dtype="uint8", crs="EPSG:32735",
                       transform=from_origin(X0, Y0, PIXEL_M, PIXEL_M)) as ds:
        ds.write(rgb)
    return path


def _tap(col, row):
    lon, lat = Transformer.from_crs("EPSG:32735", "EPSG:4326", always_xy=True).transform(
        X0 + col * PIXEL_M, Y0 - row * PIXEL_M)
    return (lon, lat)


class Store:
    """Object storage in memory, with the two calls the service makes."""

    class exceptions:  # noqa: N801 - boto3's name
        class NoSuchKey(Exception):
            pass

    def __init__(self):
        self.kept: dict[str, bytes] = {}

    def get_object(self, Bucket, Key):  # noqa: N803 - boto3's names
        if Key not in self.kept:
            raise self.exceptions.NoSuchKey()
        body = self.kept[Key]
        return {"Body": type("Body", (), {"read": lambda self: body})()}

    def put_object(self, Bucket, Key, Body, **_):  # noqa: N803
        self.kept[Key] = Body

    def head_object(self, Bucket, Key):  # noqa: N803
        if Key not in self.kept:
            raise ClientError({"Error": {"Code": "404"}}, "HeadObject")
        return {}


@pytest.fixture
def stand_ins(monkeypatch):
    scores: dict[tuple[int, int], float] = {}
    read: list[tuple[int, int]] = []

    def where(rgb):
        return int(rgb[0, 0, 0]), int(rgb[0, 0, 1])

    def boxes(models, rgb):
        j, i = where(rgb)
        found = []
        for cy in range(30, 100 * N, STEP):
            for k, cx in enumerate(range(30, 100 * N, STEP)):
                x, y = cx - j * N, cy - i * N
                if k % 2 == 0 and 0 <= x < N and 0 <= y < N:
                    found.append((x - 9.0, y - 9.0, x + 9.0, y + 9.0))
        return found

    def looks(session, rgb):
        read.append(where(rgb))
        small = np.asarray(Image.fromarray(rgb).resize((my_crop.GRID, my_crop.GRID), Image.BOX), np.float32)
        out = np.zeros((my_crop.GRID, my_crop.GRID, 384), np.float32)
        out[..., :3] = small - np.array(SOIL, np.float32)  # what differs from bare soil
        return out

    monkeypatch.setattr(plant_finder, "picture_score", lambda models, rgb: scores.get(where(rgb), 0.9))
    monkeypatch.setattr(plant_finder, "plant_boxes", boxes)
    monkeypatch.setattr(plant_finder, "load_models", lambda s3, bucket: None)
    monkeypatch.setattr(my_crop, "_look_session", lambda s3, bucket: None)
    monkeypatch.setattr(my_crop, "looks_of", looks)
    monkeypatch.setattr(my_crop, "_jobs", {})
    return scores, read


ROWS = list(range(30, N, STEP))  # tuft rows in one square
CROP_TAPS = [_tap(30, ROWS[0]), _tap(30 + 2 * STEP, ROWS[0]), _tap(30, ROWS[2])]
OTHER_TAPS = [_tap(30, ROWS[1]), _tap(30 + 2 * STEP, ROWS[1]), _tap(30, ROWS[3])]


def test_boxes_that_look_like_the_crop_taps_are_my_crop_and_the_rest_are_drawn_apart(tmp_path, stand_ins):
    path, store, plot = _photo(tmp_path), Store(), _plot(0, 0, 2 * N, N)
    with pytest.raises(my_crop.NotReady):
        my_crop.answer(path, plot, store, "b", "photo", CROP_TAPS, OTHER_TAPS)
    ticks: list[tuple[int, int]] = []
    my_crop.prepare(path, plot, store, "b", "photo", lambda done, total: ticks.append((done, total)))
    assert ticks == [(0, 2), (1, 2), (2, 2)] and len(store.kept) == 2
    found = my_crop.answer(path, plot, store, "b", "photo", CROP_TAPS, OTHER_TAPS)
    boxed_columns = len([k for k, cx in enumerate(range(30, 2 * N, STEP)) if k % 2 == 0])
    all_columns = len(range(30, 2 * N, STEP))
    even_rows, odd_rows = len(ROWS[0::2]), len(ROWS[1::2])
    assert found["my_crop"] == boxed_columns * even_rows
    assert found["other_plants"] == boxed_columns * odd_rows
    # green the finder did not box (the other rows are blue: not green, so no peak)
    assert found["other_green"] == (all_columns - boxed_columns) * even_rows
    assert found["counted_share"] == pytest.approx(1.0, abs=0.01) and found["squares_counted"] == 2
    assert found["per_ha"] == pytest.approx(found["my_crop"] / found["counted_area_m2"] * 10_000, rel=0.01)
    kinds = [(f["properties"]["kind"], f["properties"]["kind_code"]) for f in found["points"]["features"]]
    assert kinds.count(("my_crop", 0)) == found["my_crop"] and kinds.count(("other_plant", 1)) == found["other_plants"]
    assert kinds.count(("other_green", 2)) == found["other_green"]
    # the taps swapped: the other rows become the crop. The taps decide, not the finder.
    swapped = my_crop.answer(path, plot, store, "b", "photo", OTHER_TAPS, CROP_TAPS)
    assert (swapped["my_crop"], swapped["other_plants"]) == (found["other_plants"], found["my_crop"])


def test_squares_are_read_once_and_a_refused_square_gives_nothing_and_takes_no_tap(tmp_path, stand_ins):
    scores, read = stand_ins
    scores[(1, 0)] = 0.5  # the second square: single plants cannot be told apart
    path, store, plot = _photo(tmp_path), Store(), _plot(0, 0, 2 * N, N)
    my_crop.prepare(path, plot, store, "b", "photo", lambda done, total: None)
    assert read == [(0, 0)]  # no looks are made for a refused square
    my_crop.prepare(path, plot, store, "b", "photo", lambda done, total: None)
    assert read == [(0, 0)]  # and nothing is read twice
    found = my_crop.answer(path, plot, store, "b", "photo", CROP_TAPS, OTHER_TAPS)
    assert (found["squares_counted"], found["squares_refused"]) == (1, 1)
    assert found["counted_share"] == pytest.approx(0.5, abs=0.01)
    east_of_first = _tap(N, 0)[0]
    assert all(f["geometry"]["coordinates"][0] < east_of_first for f in found["points"]["features"])
    with pytest.raises(my_crop.TapProblem, match="cannot be told apart"):
        my_crop.answer(path, plot, store, "b", "photo", [*CROP_TAPS[:2], _tap(N + 30, ROWS[0])], OTHER_TAPS)
    with pytest.raises(my_crop.TapProblem, match="outside the field"):
        my_crop.answer(path, _plot(0, 0, N, N), store, "b", "photo", [*CROP_TAPS[:2], _tap(N + 30, ROWS[0])], OTHER_TAPS)
    scores[(0, 0)] = 0.1  # every square refused, on another photo key so nothing kept is reused
    my_crop.prepare(path, plot, store, "b", "photo2", lambda done, total: None)
    with pytest.raises(plant_finder.CannotCount, match="cannot be told apart"):
        my_crop.answer(path, plot, store, "b", "photo2", CROP_TAPS, OTHER_TAPS)


@pytest.mark.anyio
async def test_the_first_ask_starts_the_reading_and_the_next_ask_after_it_answers(tmp_path, stand_ins):
    path, store, plot = _photo(tmp_path), Store(), _plot(0, 0, 2 * N, N)
    with pytest.raises(my_crop.TapProblem, match="Tap 3 to 10"):
        await my_crop.count(path, plot, store, "b", "photo", CROP_TAPS[:2], OTHER_TAPS)
    first = await my_crop.count(path, plot, store, "b", "photo", CROP_TAPS, OTHER_TAPS)
    assert first["working"] is True
    job = next(iter(my_crop._jobs.values()))
    assert (await my_crop.count(path, plot, store, "b", "photo", CROP_TAPS, OTHER_TAPS)).get("working") is True
    await asyncio.wait_for(job["task"], 20)
    assert (job["done"], job["total"]) == (2, 2)
    found = await my_crop.count(path, plot, store, "b", "photo", CROP_TAPS, OTHER_TAPS)
    assert "working" not in found and found["my_crop"] > 0 and my_crop._jobs == {}
    # a plot too large to count: the job fails with the reason, and the next ask says it
    big = {**plot, "properties": {**plot["properties"], "area_ha": 3.0}}
    with pytest.raises(plant_finder.CannotCount, match="up to 0.5 ha"):
        await my_crop.count(path, big, store, "b", "photo", CROP_TAPS, OTHER_TAPS)


@pytest.mark.anyio
async def test_a_reading_that_fails_says_so_once_keeps_no_error_object_and_can_be_asked_again(tmp_path, stand_ins,
                                                                                               monkeypatch):
    path, store, plot = _photo(tmp_path), Store(), _plot(0, 0, 2 * N, N)
    real = my_crop.prepare

    def broken(*args):
        raise RuntimeError("storage is down")

    monkeypatch.setattr(my_crop, "prepare", broken)
    assert (await my_crop.count(path, plot, store, "b", "photo", CROP_TAPS, OTHER_TAPS))["working"] is True
    job = next(iter(my_crop._jobs.values()))
    await asyncio.wait_for(job["task"], 20)  # the job itself ends without raising: nothing holds the model
    assert job["task"].exception() is None and isinstance(job["error"], str)
    with pytest.raises(plant_finder.CannotCount, match="could not be read"):
        await my_crop.count(path, plot, store, "b", "photo", CROP_TAPS, OTHER_TAPS)
    assert my_crop._jobs == {}
    monkeypatch.setattr(my_crop, "prepare", real)  # storage is back: the next ask starts again and answers
    assert (await my_crop.count(path, plot, store, "b", "photo", CROP_TAPS, OTHER_TAPS))["working"] is True
    await asyncio.wait_for(next(iter(my_crop._jobs.values()))["task"], 20)
    assert (await my_crop.count(path, plot, store, "b", "photo", CROP_TAPS, OTHER_TAPS))["my_crop"] > 0


@pytest.mark.anyio
async def test_only_so_many_fields_wait_to_be_read_and_a_square_without_plants_counts_none(tmp_path, stand_ins,
                                                                                         monkeypatch):
    path, store, plot = _photo(tmp_path), Store(), _plot(0, 0, 2 * N, N)
    monkeypatch.setattr(my_crop, "_jobs", {f"other{n}": {"done": 0, "total": 0} for n in range(my_crop.MAX_WAITING)})
    with pytest.raises(my_crop.Busy, match="Try this one again"):
        await my_crop.count(path, plot, store, "b", "photo", CROP_TAPS, OTHER_TAPS)
    # a countable square in which the finder boxes nothing: no plant of any kind, and no error
    monkeypatch.setattr(plant_finder, "plant_boxes", lambda models, rgb: [])
    my_crop.prepare(path, plot, store, "b", "bare", lambda done, total: None)
    found = my_crop.answer(path, plot, store, "b", "bare", CROP_TAPS, OTHER_TAPS)
    assert (found["my_crop"], found["other_plants"], found["per_ha"]) == (0, 0, 0)
    assert found["other_green"] > 0  # the green is still flagged
