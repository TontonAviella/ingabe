"""Field outlines from taps (src/services/field_taps.py): the grid, which window answers a tap, the order the model
is asked in, outlines as shapes with their area, and the kept outlines in storage.

The model is replaced by RecordedShapeModel, which answers with arrays of the shapes the real export returns
(four 1024 px outlines, four 256 px low-resolution forms: read from onnxruntime on 2026-11-12). One test runs the
real export when FIELD_TAPS_TEST_MODEL and FIELD_TAPS_TEST_WINDOW name the files (they are 16 MB and 2 MB, so they
are not in the repo); without them it is skipped and says so.
"""

from __future__ import annotations

import asyncio
import io
import json
import os

import numpy as np
import pytest
from pyproj import Transformer
from shapely.geometry import Point, shape

from src.services import field_taps
from src.services.field_taps import Grid, Tap, TapError
from src.services.test_farm_records import _FakeS3

CRS = "EPSG:32735"
WEST, NORTH = 800_000.0, 9_790_000.0
GRID = Grid(crs=CRS, origin_x=WEST, origin_y=NORTH, windows=frozenset({(0, 0), (1, 0), (1, 1), (2, 1)}))
_TO_LONLAT = Transformer.from_crs(CRS, "EPSG:4326", always_xy=True).transform


def _tap(east_m: float, south_m: float, add: bool = True) -> Tap:
    """A tap this many metres east and south of the photo's top-left corner."""
    lon, lat = _TO_LONLAT(WEST + east_m, NORTH - south_m)
    return Tap(lon=lon, lat=lat, add=add)


class RecordedShapeModel:
    """Answers like TapModel.ask: choice n is a square of side 100 * (n + 1) px around the first tap; the single
    answer (index 0) is the square of the outline handed in as `before`, grown or shrunk by 50 px per extra tap."""

    def __init__(self) -> None:
        self.asked: list[tuple[int, bool]] = []

    @staticmethod
    def _square(col: float, row: float, half: int) -> np.ndarray:
        mask = np.zeros((field_taps.WINDOW_PX, field_taps.WINDOW_PX), bool)
        mask[max(int(row) - half, 0):int(row) + half, max(int(col) - half, 0):int(col) + half] = True
        return mask

    def ask(self, window_array, taps, before):
        assert window_array.shape == (1, 256, 64, 64)
        self.asked.append((len(taps), before is not None))
        col, row = taps[0][0], taps[0][1]
        low = np.zeros((4, 256, 256), np.float32)
        for n in range(4):
            low[n, 0, 0] = 50 * n  # the low-resolution form carries the square's half side
        if before is None:
            return np.stack([self._square(col, row, 50 * n) for n in (1, 1, 2, 3)]), low
        half = int(before[0, 0]) + (50 if taps[-1][2] == 1 else -50)
        low[0, 0, 0] = half
        return np.stack([self._square(col, row, half)] * 4), low


WINDOW_ARRAY = np.zeros((1, 256, 64, 64), np.float16)


def test_switch_is_off_unless_set(monkeypatch):
    monkeypatch.delenv("FIELD_TAPS", raising=False)
    assert not field_taps.enabled()
    monkeypatch.setenv("FIELD_TAPS", "1")
    assert field_taps.enabled()


def test_every_window_touching_the_photo_is_a_candidate():
    # a photo 130 m wide and 70 m tall: centres at 0, 60, 120 m east and 0, 60 m south
    assert field_taps.candidate_windows(0, -70, 130, 0) == [(0, 0), (1, 0), (2, 0), (0, 1), (1, 1), (2, 1)]


def test_the_first_tap_takes_the_nearest_prepared_window():
    window, placed = field_taps.place(GRID, [_tap(75, 20)])
    assert window == (1, 0)
    col, row, label = placed[0]
    # 15 m east and 20 m south of the window centre, 1024 px for 120 m
    assert (round(col), round(row), label) == (640, 683, 1)


def test_a_tap_where_nothing_is_prepared_is_refused():
    with pytest.raises(TapError, match="not prepared"):
        field_taps.place(GRID, [_tap(10, 70)])  # nearest centre is (0, 1), which was not prepared


def test_a_correction_outside_the_first_taps_window_is_refused():
    with pytest.raises(TapError, match="too far"):
        field_taps.place(GRID, [_tap(60, 0), _tap(125, 0, add=False)])


def test_only_the_measured_number_of_corrections_is_answered():
    taps = [_tap(60, 10)] + [_tap(62, 10)] * (field_taps.MAX_CORRECTIONS + 1)
    with pytest.raises(TapError, match="corrections were measured"):
        field_taps.place(GRID, taps)
    with pytest.raises(TapError, match="first tap"):
        field_taps.place(GRID, [_tap(60, 10, add=False)])


def test_a_manifest_from_other_settings_is_refused():
    record = field_taps.manifest(CRS, WEST, NORTH, [(0, 0)])
    assert field_taps.grid_of(json.loads(json.dumps(record))).windows == frozenset({(0, 0)})
    with pytest.raises(ValueError, match="step_m"):
        field_taps.grid_of({**record, "step_m": 30.0})
    with pytest.raises(ValueError, match="model"):
        field_taps.grid_of({**record, "model": "mobile_sam"})


def test_one_tap_gives_three_choices_and_a_choice_not_under_the_tap_is_dropped():
    model = RecordedShapeModel()
    found = field_taps.outline_masks(model, WINDOW_ARRAY, [(512.0, 512.0, 1)], None)
    assert [choice for choice, _ in found] == [0, 1, 2]
    assert [int(mask.sum()) for _, mask in found] == [100 * 100, 200 * 200, 300 * 300]
    assert model.asked == [(1, False)]

    class HoleUnderTap(RecordedShapeModel):
        def ask(self, window_array, taps, before):
            masks, low = super().ask(window_array, taps, before)
            masks[1, 512, 512] = False  # choice 0 has a hole exactly under the tap
            masks[2] = False  # choice 1 is empty
            return masks, low

    found = field_taps.outline_masks(HoleUnderTap(), WINDOW_ARRAY, [(512.0, 512.0, 1)], None)
    assert [choice for choice, _ in found] == [2]


def test_a_choice_that_repeats_another_is_dropped_but_a_small_one_is_kept():
    class SameTwice(RecordedShapeModel):
        def ask(self, window_array, taps, before):
            masks, low = super().ask(window_array, taps, before)
            masks[2] = masks[1]  # choice 1 is choice 0 again
            masks[2, 462, 462] = False  # bar one pixel
            return masks, low

    found = field_taps.outline_masks(SameTwice(), WINDOW_ARRAY, [(512.0, 512.0, 1)], None)
    # the repeat goes; the small first choice stays next to the large third one, with their own numbers
    assert [(choice, int(mask.sum())) for choice, mask in found] == [(0, 100 * 100), (2, 300 * 300)]


def test_the_part_under_a_tap_is_read_at_the_nearest_pixel():
    # Cyampirita field s05: the tap fell at (429.32, 635.77) and the field's outline began at row 636
    mask = np.zeros((field_taps.WINDOW_PX, field_taps.WINDOW_PX), bool)
    mask[636:700, 429:500] = True
    assert field_taps._part_under(mask, 429.32, 635.77) is not None
    assert field_taps._part_under(mask, 428.4, 635.77) is None


def test_a_correction_asks_again_with_every_tap_and_the_outline_before():
    model = RecordedShapeModel()
    taps = [(512.0, 512.0, 1), (700.0, 512.0, 1), (520.0, 300.0, 0)]
    found = field_taps.outline_masks(model, WINDOW_ARRAY, taps, pick=1)
    assert model.asked == [(1, False), (2, True), (3, True)]
    # choice 1 (half side 100) grown by the "add" tap, then shrunk by the "remove" tap
    assert [(choice, int(mask.sum())) for choice, mask in found] == [(1, 200 * 200)]
    with pytest.raises(TapError, match="Pick one"):
        field_taps.outline_masks(model, WINDOW_ARRAY, taps, pick=None)


def test_an_outline_is_a_lonlat_shape_with_its_area_in_hectares():
    mask = RecordedShapeModel._square(512, 512, 256)  # 512 px of a 1024 px, 120 m window: 60 m square
    outline = field_taps.outline_shape(GRID, (1, 1), mask)
    assert outline["area_ha"] == pytest.approx(0.36, rel=0.01)
    centre = shape(outline["geometry"]).centroid
    lon, lat = _TO_LONLAT(WEST + 60, NORTH - 60)
    assert (centre.x, centre.y) == pytest.approx((lon, lat), abs=1e-6)
    assert field_taps.outline_shape(GRID, (1, 1), np.zeros_like(mask)) is None


def _prepared_store(monkeypatch) -> tuple[_FakeS3, RecordedShapeModel]:
    s3, model = _FakeS3(), RecordedShapeModel()
    s3.objects[field_taps.manifest_key("photo.tif")] = json.dumps(
        field_taps.manifest(CRS, WEST, NORTH, sorted(GRID.windows))).encode()
    raw = io.BytesIO()
    np.save(raw, WINDOW_ARRAY)
    s3.objects[field_taps.window_key("photo.tif", (1, 0))] = raw.getvalue()

    async def recorded_model(s3_, bucket):
        return model

    monkeypatch.setattr(field_taps, "_tap_model", recorded_model)
    monkeypatch.setattr(field_taps, "_windows", type(field_taps._windows)())
    return s3, model


def test_a_photo_that_was_not_prepared_says_so(monkeypatch):
    s3 = _FakeS3()
    state = asyncio.run(field_taps.state(s3, "b", "other.tif"))
    assert state["prepared"] is False and state["kept"] == [] and state["measured"] == field_taps.MEASURED
    with pytest.raises(TapError, match="not prepared"):
        asyncio.run(field_taps.outlines(s3, "b", "other.tif", [_tap(60, 10)], None))


def test_kept_outlines_are_computed_here_and_can_be_removed(monkeypatch):
    s3, model = _prepared_store(monkeypatch)
    three = asyncio.run(field_taps.outlines(s3, "b", "photo.tif", [_tap(60, 10)], None))
    assert [o["choice"] for o in three] == [0, 1, 2]
    assert three[0]["area_ha"] < three[1]["area_ha"] < three[2]["area_ha"]

    row = asyncio.run(field_taps.keep(s3, "b", "photo.tif", [_tap(60, 10), _tap(70, 10)], 0, "user-1"))
    assert row["corrections"] == 1 and row["kept_by"] == "user-1"
    # choice 0 (half side 50 px) grown to 100 px: a 200 px square of a 120 m / 1024 px window
    assert row["area_ha"] == pytest.approx((200 * 120 / 1024) ** 2 / 10_000, rel=0.02)
    state = asyncio.run(field_taps.state(s3, "b", "photo.tif"))
    assert state["prepared"] and state["windows"] == 4 and [k["id"] for k in state["kept"]] == [row["id"]]

    assert asyncio.run(field_taps.remove(s3, "b", "photo.tif", "nope")) is False
    assert asyncio.run(field_taps.remove(s3, "b", "photo.tif", row["id"])) is True
    assert asyncio.run(field_taps.kept(s3, "b", "photo.tif")) == []


def test_a_field_keeps_its_number_and_a_number_is_never_used_twice(monkeypatch):
    s3, _ = _prepared_store(monkeypatch)
    one = asyncio.run(field_taps.keep(s3, "b", "photo.tif", [_tap(60, 10)], 0, "user-1"))
    two = asyncio.run(field_taps.keep(s3, "b", "photo.tif", [_tap(60, 10)], 1, "user-1"))
    assert (one["number"], two["number"]) == (1, 2)
    # the centre is a point inside the outline: where the number is written
    assert shape(one["geometry"]).contains(Point(one["centre"]))
    assert asyncio.run(field_taps.remove(s3, "b", "photo.tif", two["id"])) is True
    three = asyncio.run(field_taps.keep(s3, "b", "photo.tif", [_tap(60, 10)], 2, "user-1"))
    assert three["number"] == 3  # 2 is gone for good
    assert [k["number"] for k in asyncio.run(field_taps.kept(s3, "b", "photo.tif"))] == [1, 3]

    # outlines kept before fields had numbers are numbered in the order they were kept, and stay so
    old = [{k: v for k, v in row.items() if k not in ("number", "centre")} for row in (one, three)]
    s3.objects[field_taps._kept_key("photo.tif")] = json.dumps({"outlines": old}).encode()
    assert [(k["number"], k["centre"]) for k in asyncio.run(field_taps.kept(s3, "b", "photo.tif"))] == [
        (1, one["centre"]), (2, three["centre"])]
    assert asyncio.run(field_taps.keep(s3, "b", "photo.tif", [_tap(60, 10)], 1, "user-1"))["number"] == 3


def _corner(east_m: float, south_m: float) -> tuple[float, float]:
    tap = _tap(east_m, south_m)
    return (tap.lon, tap.lat)


def test_a_line_moved_by_hand_is_measured_here_and_must_stay_one_shape_around_the_tap():
    square = [_corner(50, 0), _corner(70, 0), _corner(70, 20), _corner(50, 20)]
    own = field_taps.moved_line(square, _tap(60, 10))
    assert own["area_ha"] == pytest.approx(0.04, rel=0.02)  # 20 m by 20 m
    assert field_taps.moved_line(square + [square[0]], _tap(60, 10)) == own  # closed or not, the same outline
    with pytest.raises(field_taps.TapError, match="crosses itself"):
        field_taps.moved_line([square[0], square[2], square[1], square[3]], _tap(60, 10))
    with pytest.raises(field_taps.TapError, match="spot you tapped"):
        field_taps.moved_line(square, _tap(90, 10))
    with pytest.raises(field_taps.TapError, match="corners"):
        field_taps.moved_line(square[:2], _tap(60, 10))


def test_a_kept_hand_moved_line_is_marked_and_keeps_the_models_outline_beside_it(monkeypatch):
    s3, _ = _prepared_store(monkeypatch)
    plain = asyncio.run(field_taps.keep(s3, "b", "photo.tif", [_tap(60, 10)], 0, "user-1"))
    assert plain["by_hand"] is False and "model_geometry" not in plain

    wider = [_corner(50, 0), _corner(72, 0), _corner(72, 20), _corner(50, 20)]
    row = asyncio.run(field_taps.keep(s3, "b", "photo.tif", [_tap(60, 10)], 0, "user-1", moved=wider))
    assert row["by_hand"] is True and row["area_ha"] == pytest.approx(0.044, rel=0.03)
    # the model's own outline and area stay on the record, with how much the two overlap
    assert row["model_geometry"] == plain["geometry"] and row["model_area_ha"] == plain["area_ha"]
    assert 0 < row["model_overlap"] < 1
    with pytest.raises(field_taps.TapError, match="spot you tapped"):
        asyncio.run(field_taps.keep(s3, "b", "photo.tif", [_tap(60, 10)], 0, "user-1",
                                    moved=[_corner(80, 0), _corner(100, 0), _corner(100, 20)]))
    assert len(asyncio.run(field_taps.kept(s3, "b", "photo.tif"))) == 2


def test_a_hand_moved_corner_must_be_a_real_place_near_the_tap():
    square = [_corner(50, 0), _corner(70, 0), _corner(70, 20), _corner(50, 20)]
    for bad in (float("nan"), float("inf"), 400.0):
        with pytest.raises(field_taps.TapError, match="not a place on the map"):
            field_taps.moved_line([(bad, square[0][1]), *square[1:]], _tap(60, 10))
    far = _corner(50, -(field_taps.MAX_LINE_REACH_M + 50))  # a corner over 500 m north of the tap
    with pytest.raises(field_taps.TapError, match="from the spot you tapped"):
        field_taps.moved_line([far, *square[1:]], _tap(60, 10))


def test_keeps_at_the_same_moment_are_all_kept_with_their_own_numbers(monkeypatch):
    s3, _ = _prepared_store(monkeypatch)
    square = {"type": "Polygon", "coordinates": [[list(_corner(50, 0)), list(_corner(70, 0)), list(_corner(70, 20)),
                                                   list(_corner(50, 20)), list(_corner(50, 0))]]}

    async def ready_outlines(s3_, bucket, photo_key, taps, pick):  # no model work: all six reach the record at once
        return [{"choice": choice, "geometry": square, "area_ha": 0.04} for choice in range(3)]

    read = s3.get_object

    async def slow_read(Bucket, Key):
        # the kept record comes back after a pause, so every keep has read it before any of them writes. Without
        # the per-photo guard they overwrite each other (checked on a copy with the guard taken out: the test fails)
        found_ = None
        try:
            found_ = await read(Bucket, Key)
        finally:
            if Key == field_taps._kept_key("photo.tif"):
                await asyncio.sleep(0.05)
        return found_

    monkeypatch.setattr(field_taps, "outlines", ready_outlines)
    monkeypatch.setattr(s3, "get_object", slow_read)

    async def both():
        return await asyncio.gather(*[field_taps.keep(s3, "b", "photo.tif", [_tap(60, 10)], n % 3, f"user-{n}")
                                      for n in range(6)])

    rows = asyncio.run(both())
    kept = asyncio.run(field_taps.kept(s3, "b", "photo.tif"))
    assert len(kept) == 6 and sorted(r["number"] for r in rows) == [1, 2, 3, 4, 5, 6]
    assert sorted(k["id"] for k in kept) == sorted(r["id"] for r in rows)


def test_a_photo_holds_a_limited_number_of_kept_fields(monkeypatch):
    s3, _ = _prepared_store(monkeypatch)
    monkeypatch.setattr(field_taps, "MAX_KEPT_FIELDS", 2)
    for _ in range(2):
        asyncio.run(field_taps.keep(s3, "b", "photo.tif", [_tap(60, 10)], 0, "user-1"))
    with pytest.raises(field_taps.TapError, match="already has 2 outlined fields"):
        asyncio.run(field_taps.keep(s3, "b", "photo.tif", [_tap(60, 10)], 0, "user-1"))
    assert len(asyncio.run(field_taps.kept(s3, "b", "photo.tif"))) == 2


def test_who_kept_a_field_is_recorded_but_not_shown_on_the_screen(monkeypatch):
    s3, _ = _prepared_store(monkeypatch)
    asyncio.run(field_taps.keep(s3, "b", "photo.tif", [_tap(60, 10)], 0, "user-1"))
    assert asyncio.run(field_taps.kept(s3, "b", "photo.tif"))[0]["kept_by"] == "user-1"
    assert "kept_by" not in asyncio.run(field_taps.state(s3, "b", "photo.tif"))["kept"][0]


def test_turned_down_taps_are_counted_for_good_and_kept_in_full_up_to_a_limit(monkeypatch):
    s3, _ = _prepared_store(monkeypatch)
    monkeypatch.setattr(field_taps, "MAX_TURNED_DOWN_ROWS", 3)
    for n in range(5):
        asyncio.run(field_taps.turn_down(s3, "b", "photo.tif", [_tap(60, 10)], None, "closed", f"user-{n}"))
    rows = asyncio.run(field_taps.turned_down(s3, "b", "photo.tif"))
    assert [r["turned_down_by"] for r in rows] == ["user-2", "user-3", "user-4"]  # the latest three, in full
    assert asyncio.run(field_taps.state(s3, "b", "photo.tif"))["turned_down"] == 5  # all five still counted


def test_a_refused_manifest_or_a_missing_window_reads_as_not_prepared(monkeypatch):
    s3, _ = _prepared_store(monkeypatch)
    # a window named in the manifest but gone from storage: said in plain words, not a server error
    with pytest.raises(field_taps.TapError, match="not prepared"):
        asyncio.run(field_taps.outlines(s3, "b", "photo.tif", [_tap(0, 0)], None))
    record = json.loads(s3.objects[field_taps.manifest_key("photo.tif")])
    record["step_m"] = 30
    s3.objects[field_taps.manifest_key("photo.tif")] = json.dumps(record).encode()
    assert asyncio.run(field_taps.state(s3, "b", "photo.tif"))["prepared"] is False


def test_a_tap_that_was_not_kept_is_recorded_with_its_reason(monkeypatch):
    s3, _ = _prepared_store(monkeypatch)
    assert asyncio.run(field_taps.state(s3, "b", "photo.tif"))["turned_down"] == 0
    first = asyncio.run(field_taps.turn_down(s3, "b", "photo.tif", [_tap(60, 10)], None, "none_of_these", "user-1"))
    asyncio.run(field_taps.turn_down(s3, "b", "photo.tif", [_tap(60, 10), _tap(70, 10)], 1, "closed", "user-2"))
    rows = asyncio.run(field_taps.turned_down(s3, "b", "photo.tif"))
    assert [(r["reason"], r["pick"], r["corrections"], r["turned_down_by"]) for r in rows] == [
        ("none_of_these", None, 0, "user-1"), ("closed", 1, 1, "user-2")]
    assert rows[0]["id"] == first["id"] and rows[0]["taps"] == [{"lon": _tap(60, 10).lon, "lat": _tap(60, 10).lat, "add": True}]
    # kept outlines are a separate record: turning taps down never touches them
    state = asyncio.run(field_taps.state(s3, "b", "photo.tif"))
    assert state["turned_down"] == 2 and state["kept"] == []
    with pytest.raises(field_taps.TapError, match="reason"):
        asyncio.run(field_taps.turn_down(s3, "b", "photo.tif", [_tap(60, 10)], None, "bored", "user-1"))


def test_the_real_export_answers_in_the_recorded_shapes():
    model_path, window_path = os.environ.get("FIELD_TAPS_TEST_MODEL"), os.environ.get("FIELD_TAPS_TEST_WINDOW")
    if not (model_path and window_path):
        pytest.skip("set FIELD_TAPS_TEST_MODEL (.onnx) and FIELD_TAPS_TEST_WINDOW (.npy) to run the real tap model")
    model, array = field_taps.TapModel(model_path), np.load(window_path)
    masks, low = model.ask(array, [(512.0, 512.0, 1)], None)
    assert masks.shape == (4, 1024, 1024) and masks.dtype == bool and low.shape == (4, 256, 256)
    three = field_taps.outline_masks(model, array, [(512.0, 512.0, 1)], None)
    assert three and all(mask[512, 512] for _, mask in three)
    corrected = field_taps.outline_masks(model, array, [(512.0, 512.0, 1), (700.0, 512.0, 0)], three[0][0])
    assert len(corrected) <= 1
