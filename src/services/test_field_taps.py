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


def test_a_choice_that_runs_along_the_window_border_is_left_out_unless_it_is_the_only_one(monkeypatch):
    n = field_taps.WINDOW_PX
    small = np.zeros((n, n), bool)
    small[400:600, 400:600] = True
    touching = np.zeros((n, n), bool)
    touching[0:300, 100:900] = True  # 800 px of the top row: 94 m, under the rule
    cut = np.zeros((n, n), bool)
    cut[0:600, :] = True  # the whole top row and 600 px down each side: 261 m on the border
    assert round(field_taps.on_the_border_m(touching)) == 94 and round(field_taps.on_the_border_m(cut)) == 261
    assert field_taps.without_edge_choices([(0, small), (1, touching), (2, cut)]) == ([(0, small), (1, touching)], 1)
    # a field larger than the window: every choice is cut, so none is left out
    assert field_taps.without_edge_choices([(2, cut)]) == ([(2, cut)], 0)

    class WholeWindowThird(RecordedShapeModel):
        def ask(self, window_array, taps, before):
            masks, low = super().ask(window_array, taps, before)
            masks[3] = True  # choice 2 is everything the model saw
            return masks, low

    s3, _ = _prepared_store(monkeypatch)
    monkeypatch.setattr(field_taps, "_tap_model", lambda s3_, bucket: _ready(WholeWindowThird()))
    found, left_out = asyncio.run(field_taps.outlines_and_left_out(s3, "b", "photo.tif", [_tap(60, 10)], None))
    assert [o["choice"] for o in found] == [0, 1] and left_out == 1
    # the choice that was not offered cannot be kept, nor corrected and then kept
    with pytest.raises(TapError):
        asyncio.run(field_taps.keep(s3, "b", "photo.tif", "layer-1", [_tap(60, 10)], 2, "user-1"))
    with pytest.raises(TapError, match="not offered"):
        asyncio.run(field_taps.keep(s3, "b", "photo.tif", "layer-1", [_tap(60, 10), _tap(70, 10)], 2, "user-1"))
    assert asyncio.run(field_taps.keep(s3, "b", "photo.tif", "layer-1", [_tap(60, 10), _tap(70, 10)], 1, "user-1"))["pick"] == 1


async def _ready(model):
    return model


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
    state = asyncio.run(field_taps.state(s3, "b", "other.tif", "layer-1"))
    assert state["prepared"] is False and state["kept"] == [] and state["measured"] == field_taps.MEASURED
    with pytest.raises(TapError, match="not prepared"):
        asyncio.run(field_taps.outlines(s3, "b", "other.tif", [_tap(60, 10)], None))


def test_kept_outlines_are_computed_here_and_can_be_removed(monkeypatch):
    s3, model = _prepared_store(monkeypatch)
    three = asyncio.run(field_taps.outlines(s3, "b", "photo.tif", [_tap(60, 10)], None))
    assert [o["choice"] for o in three] == [0, 1, 2]
    assert three[0]["area_ha"] < three[1]["area_ha"] < three[2]["area_ha"]

    row = asyncio.run(field_taps.keep(s3, "b", "photo.tif", "layer-1", [_tap(60, 10), _tap(70, 10)], 0, "user-1"))
    assert row["corrections"] == 1 and row["kept_by"] == "user-1"
    # choice 0 (half side 50 px) grown to 100 px: a 200 px square of a 120 m / 1024 px window
    assert row["area_ha"] == pytest.approx((200 * 120 / 1024) ** 2 / 10_000, rel=0.02)
    state = asyncio.run(field_taps.state(s3, "b", "photo.tif", "layer-1"))
    assert state["prepared"] and state["windows"] == 4 and [k["id"] for k in state["kept"]] == [row["id"]]

    assert asyncio.run(field_taps.remove(s3, "b", "photo.tif", "layer-1", "nope")) is False
    assert asyncio.run(field_taps.remove(s3, "b", "photo.tif", "layer-1", row["id"])) is True
    assert asyncio.run(field_taps.kept(s3, "b", "photo.tif", "layer-1")) == []


def test_a_field_keeps_its_number_and_a_number_is_never_used_twice(monkeypatch):
    s3, _ = _prepared_store(monkeypatch)
    one = asyncio.run(field_taps.keep(s3, "b", "photo.tif", "layer-1", [_tap(60, 10)], 0, "user-1"))
    two = asyncio.run(field_taps.keep(s3, "b", "photo.tif", "layer-1", [_tap(60, 10)], 1, "user-1"))
    assert (one["number"], two["number"]) == (1, 2)
    # the centre is a point inside the outline: where the number is written
    assert shape(one["geometry"]).contains(Point(one["centre"]))
    assert asyncio.run(field_taps.remove(s3, "b", "photo.tif", "layer-1", two["id"])) is True
    three = asyncio.run(field_taps.keep(s3, "b", "photo.tif", "layer-1", [_tap(60, 10)], 2, "user-1"))
    assert three["number"] == 3  # 2 is gone for good
    assert [k["number"] for k in asyncio.run(field_taps.kept(s3, "b", "photo.tif", "layer-1"))] == [1, 3]

    # outlines kept before fields had numbers are numbered in the order they were kept, and stay so
    old = [{k: v for k, v in row.items() if k not in ("number", "centre")} for row in (one, three)]
    s3.objects[field_taps._kept_key("photo.tif", "layer-1")] = json.dumps({"outlines": old}).encode()
    assert [(k["number"], k["centre"]) for k in asyncio.run(field_taps.kept(s3, "b", "photo.tif", "layer-1"))] == [
        (1, one["centre"]), (2, three["centre"])]
    assert asyncio.run(field_taps.keep(s3, "b", "photo.tif", "layer-1", [_tap(60, 10)], 1, "user-1"))["number"] == 3


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
    plain = asyncio.run(field_taps.keep(s3, "b", "photo.tif", "layer-1", [_tap(60, 10)], 0, "user-1"))
    assert plain["by_hand"] is False and "model_geometry" not in plain

    wider = [_corner(50, 0), _corner(72, 0), _corner(72, 20), _corner(50, 20)]
    row = asyncio.run(field_taps.keep(s3, "b", "photo.tif", "layer-1", [_tap(60, 10)], 0, "user-1", moved=wider))
    assert row["by_hand"] is True and row["area_ha"] == pytest.approx(0.044, rel=0.03)
    # the model's own outline and area stay on the record, with how much the two overlap
    assert row["model_geometry"] == plain["geometry"] and row["model_area_ha"] == plain["area_ha"]
    assert 0 < row["model_overlap"] < 1
    with pytest.raises(field_taps.TapError, match="spot you tapped"):
        asyncio.run(field_taps.keep(s3, "b", "photo.tif", "layer-1", [_tap(60, 10)], 0, "user-1",
                                    moved=[_corner(80, 0), _corner(100, 0), _corner(100, 20)]))
    assert len(asyncio.run(field_taps.kept(s3, "b", "photo.tif", "layer-1"))) == 2


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
            if Key == field_taps._kept_key("photo.tif", "layer-1"):
                await asyncio.sleep(0.05)
        return found_

    monkeypatch.setattr(field_taps, "outlines", ready_outlines)
    monkeypatch.setattr(s3, "get_object", slow_read)

    async def both():
        return await asyncio.gather(*[field_taps.keep(s3, "b", "photo.tif", "layer-1", [_tap(60, 10)], n % 3, f"user-{n}")
                                      for n in range(6)])

    rows = asyncio.run(both())
    kept = asyncio.run(field_taps.kept(s3, "b", "photo.tif", "layer-1"))
    assert len(kept) == 6 and sorted(r["number"] for r in rows) == [1, 2, 3, 4, 5, 6]
    assert sorted(k["id"] for k in kept) == sorted(r["id"] for r in rows)


def test_a_photo_holds_a_limited_number_of_kept_fields(monkeypatch):
    s3, _ = _prepared_store(monkeypatch)
    monkeypatch.setattr(field_taps, "MAX_KEPT_FIELDS", 2)
    for _ in range(2):
        asyncio.run(field_taps.keep(s3, "b", "photo.tif", "layer-1", [_tap(60, 10)], 0, "user-1"))
    with pytest.raises(field_taps.TapError, match="already has 2 outlined fields"):
        asyncio.run(field_taps.keep(s3, "b", "photo.tif", "layer-1", [_tap(60, 10)], 0, "user-1"))
    assert len(asyncio.run(field_taps.kept(s3, "b", "photo.tif", "layer-1"))) == 2


def test_who_kept_a_field_is_recorded_but_not_shown_on_the_screen(monkeypatch):
    s3, _ = _prepared_store(monkeypatch)
    asyncio.run(field_taps.keep(s3, "b", "photo.tif", "layer-1", [_tap(60, 10)], 0, "user-1"))
    assert asyncio.run(field_taps.kept(s3, "b", "photo.tif", "layer-1"))[0]["kept_by"] == "user-1"
    assert "kept_by" not in asyncio.run(field_taps.state(s3, "b", "photo.tif", "layer-1"))["kept"][0]


def test_turned_down_taps_are_counted_for_good_and_kept_in_full_up_to_a_limit(monkeypatch):
    s3, _ = _prepared_store(monkeypatch)
    monkeypatch.setattr(field_taps, "MAX_TURNED_DOWN_ROWS", 3)
    for n in range(5):
        asyncio.run(field_taps.turn_down(s3, "b", "photo.tif", "layer-1", [_tap(60, 10)], None, "closed", f"user-{n}"))
    rows = asyncio.run(field_taps.turned_down(s3, "b", "photo.tif", "layer-1"))
    assert [r["turned_down_by"] for r in rows] == ["user-2", "user-3", "user-4"]  # the latest three, in full
    assert asyncio.run(field_taps.state(s3, "b", "photo.tif", "layer-1"))["turned_down"] == 5  # all five still counted


def test_a_refused_manifest_or_a_missing_window_reads_as_not_prepared(monkeypatch):
    s3, _ = _prepared_store(monkeypatch)
    # a window named in the manifest but gone from storage: said in plain words, not a server error
    with pytest.raises(field_taps.TapError, match="not prepared"):
        asyncio.run(field_taps.outlines(s3, "b", "photo.tif", [_tap(0, 0)], None))
    record = json.loads(s3.objects[field_taps.manifest_key("photo.tif")])
    record["step_m"] = 30
    s3.objects[field_taps.manifest_key("photo.tif")] = json.dumps(record).encode()
    assert asyncio.run(field_taps.state(s3, "b", "photo.tif", "layer-1"))["prepared"] is False


def test_a_tap_that_was_not_kept_is_recorded_with_its_reason(monkeypatch):
    s3, _ = _prepared_store(monkeypatch)
    assert asyncio.run(field_taps.state(s3, "b", "photo.tif", "layer-1"))["turned_down"] == 0
    first = asyncio.run(field_taps.turn_down(s3, "b", "photo.tif", "layer-1", [_tap(60, 10)], None, "none_of_these", "user-1"))
    asyncio.run(field_taps.turn_down(s3, "b", "photo.tif", "layer-1", [_tap(60, 10), _tap(70, 10)], 1, "closed", "user-2"))
    rows = asyncio.run(field_taps.turned_down(s3, "b", "photo.tif", "layer-1"))
    assert [(r["reason"], r["pick"], r["corrections"], r["turned_down_by"]) for r in rows] == [
        ("none_of_these", None, 0, "user-1"), ("closed", 1, 1, "user-2")]
    assert rows[0]["id"] == first["id"] and rows[0]["taps"] == [{"lon": _tap(60, 10).lon, "lat": _tap(60, 10).lat, "add": True}]
    # kept outlines are a separate record: turning taps down never touches them
    state = asyncio.run(field_taps.state(s3, "b", "photo.tif", "layer-1"))
    assert state["turned_down"] == 2 and state["kept"] == []
    with pytest.raises(field_taps.TapError, match="reason"):
        asyncio.run(field_taps.turn_down(s3, "b", "photo.tif", "layer-1", [_tap(60, 10)], None, "bored", "user-1"))


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


# Green cover (Tests 77 and 78): the rule as measured, its limits, and where it is kept.

GREEN, SOIL, PALE_GREEN, PALE_SOIL = (60, 140, 40), (180, 120, 90), (150, 160, 120), (170, 170, 140)


def test_green_cover_counts_the_pixels_green_by_the_measured_rule():
    rgb = np.zeros((3, 40, 40), np.uint8)
    rgb[:, :, :20] = np.array(GREEN, np.uint8)[:, None, None]
    rgb[:, :, 20:] = np.array(SOIL, np.uint8)[:, None, None]
    everywhere = np.ones((40, 40), bool)
    # 21 of 40 columns: at the border the 3 x 3 mean of one green and two soil columns still reads green (excess
    # green 0.158), the spread from leaf into soil that makes the rule read high on sparse ground (Test 77)
    assert field_taps._cover_from_pixels(rgb, everywhere) == pytest.approx(21 / 40)
    # only pixels of the photo inside the field count
    left = np.zeros((40, 40), bool)
    left[:, :10] = True
    assert field_taps._cover_from_pixels(rgb, left) == 1.0
    assert field_taps._cover_from_pixels(rgb, np.zeros((40, 40), bool)) is None
    # either side of the measured threshold 0.15: excess green 0.196 counts, 0.118 does not
    for colour, share in ((PALE_GREEN, 1.0), (PALE_SOIL, 0.0)):
        flat = np.broadcast_to(np.array(colour, np.uint8)[:, None, None], (3, 10, 10)).copy()
        assert field_taps._cover_from_pixels(flat, np.ones((10, 10), bool)) == share


def _photo_file(tmp_path, pixel_m: float = 0.0325, side_px: int = 400) -> str:
    """A small photo in the test grid's projection: left half green, right half soil, at WEST + 100 m."""
    import rasterio
    from rasterio.transform import from_origin

    rgb = np.zeros((3, side_px, side_px), np.uint8)
    rgb[:, :, :side_px // 2] = np.array(GREEN, np.uint8)[:, None, None]
    rgb[:, :, side_px // 2:] = np.array(SOIL, np.uint8)[:, None, None]
    path = str(tmp_path / "photo.tif")
    with rasterio.open(path, "w", driver="GTiff", width=side_px, height=side_px, count=3, dtype="uint8", crs=CRS,
                       transform=from_origin(WEST + 100, NORTH - 100, pixel_m, pixel_m)) as out:
        out.write(rgb)
    return path


def _field_on_photo(pixel_m: float = 0.0325, side_px: int = 400) -> tuple[dict, float]:
    """The whole photo of _photo_file as a field outline in longitude and latitude, with its area."""
    from shapely.geometry import box, mapping
    from shapely.ops import transform as reproject

    from src.services import ground_area

    side = pixel_m * side_px
    lonlat = reproject(_TO_LONLAT, box(WEST + 100, NORTH - 100 - side, WEST + 100 + side, NORTH - 100))
    return mapping(lonlat), ground_area.area_ha(lonlat)


def test_green_cover_is_read_inside_the_field_and_only_where_it_was_measured(tmp_path):
    path = _photo_file(tmp_path)
    geometry, area = _field_on_photo()
    cover = field_taps.green_cover(path, geometry, area)
    assert cover["why_not"] is None and cover["share"] == pytest.approx(0.5, abs=0.02)
    assert field_taps.green_cover(path, geometry, 0.9)["why_not"].startswith("Not measured for fields over 0.7 ha")
    assert field_taps.green_cover(path, geometry, 0.001)["why_not"] == "Not measured for a field this small."
    coarse = _photo_file(tmp_path, pixel_m=0.10, side_px=200)
    assert "3 cm pixels" in field_taps.green_cover(coarse, *_field_on_photo(0.10, 200))["why_not"]


def _mark_checked(s3) -> None:
    s3.objects[field_taps.cover_rule_key("photo.tif")] = json.dumps(field_taps.cover_rule_record("Tests 77 and 78")).encode()


def _fresh_cover_jobs(monkeypatch) -> None:
    monkeypatch.setattr(field_taps, "_fills", {})
    monkeypatch.setattr(field_taps, "_fill_retry_at", {})


async def _state_after_filling(s3, path, layer="layer-1"):
    """The photo's state once the background job that state() started has finished."""
    first = await field_taps.state(s3, "b", "photo.tif", layer, path)
    job = field_taps._fills.get(f"photo.tif|{layer}")
    if job is not None:
        await job
    return first, await field_taps.state(s3, "b", "photo.tif", layer, path)


MEASURED_42 = {"share": 0.42, "why_not": None, "threshold": field_taps.GREEN_THRESHOLD}


def test_a_kept_field_gets_its_green_cover_and_older_fields_get_it_in_the_background(monkeypatch, tmp_path):
    s3, _ = _prepared_store(monkeypatch)
    _fresh_cover_jobs(monkeypatch)
    _mark_checked(s3)
    path = _photo_file(tmp_path)
    opened = []
    monkeypatch.setattr(field_taps, "green_cover", lambda url, geometry, area: {"share": 0.42, "why_not": None})

    def fake_covers(cog_url, fields):
        opened.append([field_id for field_id, _, _ in fields])
        return {field_id: {"share": 0.42, "why_not": None} for field_id, _, _ in fields}

    monkeypatch.setattr(field_taps, "green_covers", fake_covers)
    row = asyncio.run(field_taps.keep(s3, "b", "photo.tif", "layer-1", [_tap(60, 10)], 0, "user-1", cog_url=path))
    assert row["green_cover"] == MEASURED_42
    # a field kept before green cover existed has none; opening the photo does not wait for it and writes nothing
    old = asyncio.run(field_taps.keep(s3, "b", "photo.tif", "layer-1", [_tap(60, 10)], 1, "user-1"))
    assert "green_cover" not in old
    stored = s3.objects[field_taps._kept_key("photo.tif", "layer-1")]
    first, later = asyncio.run(_state_after_filling(s3, path))
    assert first["cover_filling"] is True and [k.get("green_cover") for k in first["kept"]] == [MEASURED_42, None]
    assert later["cover_filling"] is False and [k["green_cover"] for k in later["kept"]] == [MEASURED_42] * 2
    assert opened == [[old["id"]]]  # only the field without a figure, the photo opened once
    assert s3.objects[field_taps._kept_key("photo.tif", "layer-1")] != stored
    assert later["cover_measured"] == field_taps.COVER_MEASURED

    def broken(cog_url, geometry, area_ha):
        raise OSError("photo unreachable")

    monkeypatch.setattr(field_taps, "green_cover", broken)
    kept_anyway = asyncio.run(field_taps.keep(s3, "b", "photo.tif", "layer-1", [_tap(60, 10)], 2, "user-1", cog_url=path))
    assert "green_cover" not in kept_anyway  # kept all the same; measured later, in the background


def test_missing_green_cover_is_measured_in_batches_and_a_photo_that_cannot_be_read_is_left_alone_for_a_while(
        monkeypatch, tmp_path):
    s3, _ = _prepared_store(monkeypatch)
    _fresh_cover_jobs(monkeypatch)
    _mark_checked(s3)
    monkeypatch.setattr(field_taps, "COVER_BATCH", 2)
    for pick in (0, 1, 2, 0, 1):
        asyncio.run(field_taps.keep(s3, "b", "photo.tif", "layer-1", [_tap(60, 10)], pick, "user-1"))
    batches = []

    def fake_covers(cog_url, fields):
        batches.append(len(fields))
        return {field_id: {"share": None, "why_not": "Not measured for a field this small."} for field_id, _, _ in fields}

    monkeypatch.setattr(field_taps, "green_covers", fake_covers)
    _, later = asyncio.run(_state_after_filling(s3, "photo-url"))
    assert batches == [2, 2, 1] and all(k["green_cover"]["why_not"] for k in later["kept"])
    # a figure, or a reason, that is kept is not measured again
    asyncio.run(_state_after_filling(s3, "photo-url"))
    assert batches == [2, 2, 1]

    # a photo that cannot be read: one try, then nothing until COVER_RETRY_S has passed
    asyncio.run(field_taps.keep(s3, "b", "photo.tif", "layer-1", [_tap(60, 10)], 2, "user-1"))
    tries = []

    def unreadable(cog_url, fields):
        tries.append(len(fields))
        raise OSError("photo unreachable")

    monkeypatch.setattr(field_taps, "green_covers", unreadable)
    first, later = asyncio.run(_state_after_filling(s3, "photo-url"))
    assert first["cover_filling"] is True and later["cover_filling"] is False and tries == [1]
    assert "green_cover" not in later["kept"][-1]
    monkeypatch.setattr(field_taps, "_fill_retry_at", {})  # the wait is over
    asyncio.run(_state_after_filling(s3, "photo-url"))
    assert tries == [1, 1]


def test_green_cover_is_only_given_on_a_photo_the_rule_was_checked_on_and_nothing_is_written_to_say_so(monkeypatch):
    s3, _ = _prepared_store(monkeypatch)
    _fresh_cover_jobs(monkeypatch)
    read = []

    def fake_cover(cog_url, geometry, area_ha):
        read.append(area_ha)
        return {"share": 0.42, "why_not": None}

    monkeypatch.setattr(field_taps, "green_cover", fake_cover)
    monkeypatch.setattr(field_taps, "green_covers", lambda url, fields: {i: fake_cover(url, g, a) for i, g, a in fields})
    not_checked = {"share": None, "why_not": field_taps.COVER_NOT_CHECKED}
    row = asyncio.run(field_taps.keep(s3, "b", "photo.tif", "layer-1", [_tap(60, 10)], 0, "user-1", cog_url="photo-url"))
    assert "green_cover" not in row and read == []  # the photo is not even read
    stored = dict(s3.objects)
    first, later = asyncio.run(_state_after_filling(s3, "photo-url"))
    assert [k["green_cover"] for k in later["kept"]] == [not_checked] and first["cover_filling"] is False
    assert s3.objects == stored and read == []  # opening an unchecked photo reads no photo and writes nothing
    # once the photo is marked as checked, the next visit measures it
    _mark_checked(s3)
    _, later = asyncio.run(_state_after_filling(s3, "photo-url"))
    assert [k["green_cover"] for k in later["kept"]] == [MEASURED_42] and len(read) == 1
    # a mark that is not this rule's is no mark: a figure already kept is no longer shown
    for record in ({**field_taps.cover_rule_record("x"), "threshold": 0.05}, {**field_taps.cover_rule_record("x"), "version": 2},
                   {}, ["not a record"]):
        s3.objects[field_taps.cover_rule_key("photo.tif")] = json.dumps(record).encode()
        _, later = asyncio.run(_state_after_filling(s3, "photo-url"))
        assert [k["green_cover"] for k in later["kept"]] == [not_checked] and len(read) == 1
    # a figure from another threshold than today's is measured again, not shown
    _mark_checked(s3)
    monkeypatch.setattr(field_taps, "GREEN_THRESHOLD", 0.2)
    _mark_checked(s3)
    first, later = asyncio.run(_state_after_filling(s3, "photo-url"))
    assert "green_cover" not in first["kept"][0] and later["kept"][0]["green_cover"]["threshold"] == 0.2 and len(read) == 2


def test_fields_kept_on_one_copy_of_a_photo_do_not_appear_on_another(monkeypatch):
    s3, _ = _prepared_store(monkeypatch)
    mine = asyncio.run(field_taps.keep(s3, "b", "photo.tif", "layer-1", [_tap(60, 10)], 0, "user-1"))
    asyncio.run(field_taps.turn_down(s3, "b", "photo.tif", "layer-1", [_tap(60, 10)], None, "closed", "user-1"))
    # another layer showing the same stored image: prepared just the same, with no fields and no turned-down taps
    other = asyncio.run(field_taps.state(s3, "b", "photo.tif", "layer-2"))
    assert other["prepared"] and other["kept"] == [] and other["turned_down"] == 0
    theirs = asyncio.run(field_taps.keep(s3, "b", "photo.tif", "layer-2", [_tap(60, 10)], 1, "user-2"))
    assert theirs["number"] == 1  # their own numbering
    assert asyncio.run(field_taps.remove(s3, "b", "photo.tif", "layer-2", mine["id"])) is False
    assert [k["id"] for k in asyncio.run(field_taps.kept(s3, "b", "photo.tif", "layer-1"))] == [mine["id"]]


def test_green_cover_never_reads_more_than_the_box_it_was_measured_on(tmp_path):
    """A thin outline can have a small area and a box a kilometre wide: the box decides how much is read."""
    from shapely.geometry import Polygon, mapping
    from shapely.ops import transform as reproject

    from src.services import ground_area

    path = _photo_file(tmp_path)
    x, y = WEST + 100, NORTH - 100
    comb = reproject(_TO_LONLAT, Polygon([(x, y), (x + 400, y), (x + 400, y - 3), (x + 3, y - 3), (x + 3, y - 400),
                                         (x, y - 400)]))
    area = ground_area.area_ha(comb)
    assert field_taps.COVER_MIN_HA < area < field_taps.COVER_MAX_HA
    cover = field_taps.green_cover(path, mapping(comb), area)
    assert cover["share"] is None and "150 m across" in cover["why_not"]
    # the other reasons a field gets no figure
    sliver = reproject(_TO_LONLAT, Polygon([(x, y), (x + 100, y), (x + 100, y - 0.8), (x, y - 0.8)]))
    assert field_taps.green_cover(path, mapping(sliver), 0.01)["why_not"] == "Not measured for a field this narrow."
    geometry, _ = _field_on_photo()
    away = reproject(_TO_LONLAT, Polygon([(x + 500, y), (x + 520, y), (x + 520, y - 20), (x + 500, y - 20)]))
    assert field_taps.green_cover(path, mapping(away), 0.04)["why_not"] == "The photo has no picture inside this field."


def test_a_hand_moved_outline_larger_than_two_hectares_is_refused():
    big = [_corner(60, 10), _corner(260, 10), _corner(260, 210), _corner(60, 210)]  # 200 m x 200 m = 4 ha
    with pytest.raises(TapError, match="at most 2 ha"):
        field_taps.moved_line([(c[0], c[1]) for c in big], _tap(70, 20))
