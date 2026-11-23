"""The satellite view of a drone photo: the newest clear look at each square, and FAO's soil moisture.

The band files here are real GeoTIFFs on the grid Sentinel-2 uses east of 30 degrees E in Rwanda (EPSG:32736,
10 m bands and a 20 m class map sharing one corner), written around the Cyampirita photo's box; the soil file
is on FAO WaPOR's grid. Nothing is read from the network: the scene search and the raster workers are stood in.
"""

from __future__ import annotations

import os
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Optional

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin
from rasterio.warp import transform_bounds
from shapely.geometry import Point, Polygon, box

from src.services import satellite_view as sv
from src.services import wapor_service
from src.services.stac_service import STACService

BOX = [30.41749, -1.7009, 30.43148, -1.69289]  # west, south, east, north: the Cyampirita photo
TODAY = date(2026, 11, 23)
CLOUD, VEGETATION = 9, 4  # Sentinel-2 scene classes
NIR = 3000


def _red_for(ndvi: float) -> int:
    return int(round(NIR * (1 - ndvi) / (1 + ndvi)))


def _write(path: Path, pixel_m: int, west_value: int, east_value: int, split: float, dtype: str, shift_m: float = 0.0) -> str:
    """A file over BOX with 300 m to spare, its corner on a whole 20 m; west of `split` (0-1 across the box) it
    holds west_value, east of it east_value. shift_m moves the whole file east, off the box."""
    west, south, east, north = transform_bounds("EPSG:4326", "EPSG:32736", *BOX)
    left = np.floor((west - 300) / 20) * 20 + shift_m
    top = np.ceil((north + 300) / 20) * 20
    width = int(np.ceil((east + 300 - left) / 20) * 20 / pixel_m)
    height = int(np.ceil((top - (south - 300)) / 20) * 20 / pixel_m)
    data = np.full((height, width), west_value, dtype=dtype)
    split_m = np.round((west + split * (east - west) - left) / 20) * 20
    data[:, min(width, max(0, int(split_m // pixel_m))):] = east_value
    with rasterio.open(path, "w", driver="GTiff", height=height, width=width, count=1, dtype=dtype, crs="EPSG:32736",
                       transform=from_origin(left, top, pixel_m, pixel_m)) as dst:
        dst.write(data, 1)
    return str(path)


def _pass(folder: Path, day: date, *, ndvi: float, cloud_east_of: float = 9.0, haze_west_of: float = -9.0,
          tile: str = "36MTD", shift_m: float = 0.0) -> dict[str, Any]:
    """One pass as the scene search lists it: NDVI `ndvi` everywhere, cloud east of one line, haze west of another."""
    stem = f"{tile}_{day:%Y%m%d}"
    red = _red_for(ndvi)
    assets = {
        "red": _write(folder / f"{stem}_red.tif", 10, red, red, 1.0, "uint16", shift_m),
        "nir": _write(folder / f"{stem}_nir.tif", 10, NIR, NIR, 1.0, "uint16", shift_m),
        "blue": _write(folder / f"{stem}_blue.tif", 10, 1500, 500, haze_west_of, "uint16", shift_m),
        "scl": _write(folder / f"{stem}_scl.tif", 20, VEGETATION, CLOUD, cloud_east_of, "uint8", shift_m),
    }
    return {"id": f"S2A_{tile}_{day:%Y%m%d}_0_L2A", "datetime": f"{day.isoformat()}T08:21:00Z", "cloud_cover": 50.0,
            "assets": {name: {"href": href} for name, href in assets.items()}}


@pytest.fixture
def in_process(monkeypatch):
    """Raster workers run in this process, and the scene search answers from a list of passes."""
    monkeypatch.setattr(sv.raster_process, "run", lambda fn, *args: fn(*args))

    def use(passes: list[dict[str, Any]], error: Optional[str] = None) -> list[str]:
        asked: list[str] = []

        def search(self, bbox=None, datetime_range: str = "", collections=None, max_cloud_cover=20.0, limit=10):
            asked.append(datetime_range)
            if error:
                return {"error": error}
            start, end = (date.fromisoformat(part) for part in datetime_range.split("/"))
            return {"items": [p for p in passes if start <= date.fromisoformat(p["datetime"][:10]) <= end]}

        monkeypatch.setattr(STACService, "search_imagery", search)
        return asked

    return use


def _look(day: date, ndvi: Any, clear: Any, sharp: Any = None) -> sv.Look:
    ndvi = np.asarray(ndvi, dtype=np.float32)
    clear = np.asarray(clear, dtype=bool)
    return sv.Look(day, ndvi, clear, clear if sharp is None else np.asarray(sharp, dtype=bool))


# --- The newest clear look at each square ------------------------------------------------------------------


def test_each_square_takes_its_newest_sharp_look():
    new = _look(TODAY, [[0.6, 0.6]], [[True, False]])
    old = _look(TODAY - timedelta(days=20), [[0.3, 0.3]], [[True, True]])
    ndvi, seen, from_haze = sv.newest_clear([old, new])
    assert ndvi.ravel().tolist() == pytest.approx([0.6, 0.3])
    assert seen.tolist() == [[TODAY.toordinal(), (TODAY - timedelta(days=20)).toordinal()]]
    assert not from_haze.any()


def test_a_hazy_look_is_passed_over_for_an_older_sharp_one_and_used_only_when_it_is_all_there_is():
    hazy_new = _look(TODAY, [[0.2, 0.2]], [[True, True]], sharp=[[False, False]])
    sharp_old = _look(TODAY - timedelta(days=30), [[0.5, np.nan]], [[True, False]])
    ndvi, seen, from_haze = sv.newest_clear([hazy_new, sharp_old])
    assert ndvi.ravel().tolist() == pytest.approx([0.5, 0.2])
    assert from_haze.tolist() == [[False, True]]
    assert seen.tolist() == [[(TODAY - timedelta(days=30)).toordinal(), TODAY.toordinal()]]


def test_haze_is_wide_bright_blue_not_one_bright_roof():
    blue = np.full((30, 30), 500.0)
    blue[10, 10] = 4000.0  # one roof
    assert not sv.hazy(blue).any()
    blue[:, :15] = 1500.0  # haze over the western half
    flagged = sv.hazy(blue)
    assert flagged[:, :11].all() and not flagged[:, 19:].any()


def test_change_is_measured_on_the_main_looks_squares_against_a_look_two_weeks_older_or_more():
    main = _look(TODAY, np.full((10, 10), 0.55), np.ones((10, 10), bool))
    week_ago = _look(TODAY - timedelta(days=7), np.full((10, 10), 0.50), np.ones((10, 10), bool))
    six_weeks = _look(TODAY - timedelta(days=42), np.full((10, 10), 0.31), np.ones((10, 10), bool))
    ndvi, seen, from_haze = sv.newest_clear([main, week_ago])
    assert sv.change_since([main, week_ago], ndvi, seen, from_haze) is None  # a week is too close to compare
    change = sv.change_since([main, week_ago, six_weeks], ndvi, seen, from_haze)
    assert change is not None
    assert (change.now, change.before, change.squares) == (TODAY, TODAY - timedelta(days=42), 100)
    assert change.median == pytest.approx(0.24, abs=0.005)


def test_no_change_is_stated_when_the_older_look_covers_under_half_of_the_main_squares():
    main = _look(TODAY, np.full((10, 10), 0.55), np.ones((10, 10), bool))
    partial = np.zeros((10, 10), bool)
    partial[:, :4] = True
    old = _look(TODAY - timedelta(days=30), np.full((10, 10), 0.3), partial)
    ndvi, seen, from_haze = sv.newest_clear([main, old])
    assert sv.change_since([main, old], ndvi, seen, from_haze) is None


# --- Reading passes ----------------------------------------------------------------------------------------------


def test_the_whole_photo_is_covered_from_the_newest_clear_look_at_each_square(tmp_path, in_process):
    asked = in_process([
        _pass(tmp_path, TODAY - timedelta(days=1), ndvi=0.9, cloud_east_of=0.0),  # all cloud: read no further than its class map
        _pass(tmp_path, TODAY - timedelta(days=4), ndvi=0.6, cloud_east_of=0.7),
        _pass(tmp_path, TODAY - timedelta(days=12), ndvi=0.4),
        _pass(tmp_path, TODAY - timedelta(days=40), ndvi=0.3),
    ])
    plants = sv.plants_view(BOX, TODAY)
    assert plants.covered == 1.0 and not plants.hazy.any()
    assert plants.grid.crs == "EPSG:32736" and plants.grid.shape == (89, 156)  # the photo's 13,884 squares of 10 m
    assert plants.main_day == TODAY - timedelta(days=4)
    fresh = plants.seen == (TODAY - timedelta(days=4)).toordinal()
    assert 0.65 < fresh.mean() < 0.75
    assert np.nanmedian(plants.ndvi[fresh]) == pytest.approx(0.6, abs=0.01)
    assert set(np.unique(plants.seen[~fresh])) == {(TODAY - timedelta(days=12)).toordinal()}
    assert np.nanmedian(plants.ndvi[~fresh]) == pytest.approx(0.4, abs=0.01)
    assert plants.change is not None and plants.change.before == TODAY - timedelta(days=40)
    assert plants.change.median == pytest.approx(0.3, abs=0.01)
    assert plants.passes_read == 4
    assert asked == [f"{(TODAY - timedelta(days=45)).isoformat()}/{TODAY.isoformat()}"]  # one step back was enough


def test_ground_under_haze_takes_an_older_sharp_look(tmp_path, in_process):
    in_process([
        _pass(tmp_path, TODAY - timedelta(days=2), ndvi=0.25, haze_west_of=0.6),  # the class map calls it all clear
        _pass(tmp_path, TODAY - timedelta(days=30), ndvi=0.5),
    ])
    plants = sv.plants_view(BOX, TODAY)
    newest = plants.seen == (TODAY - timedelta(days=2)).toordinal()
    assert 0.3 < newest.mean() < 0.45  # only the sharp eastern part comes from the hazy pass
    assert newest[:, -30:].all() and not newest[:, :60].any()
    assert not plants.hazy.any() and plants.covered == 1.0


def test_a_square_only_ever_seen_through_haze_is_filled_and_marked(tmp_path, in_process):
    in_process([_pass(tmp_path, TODAY - timedelta(days=2), ndvi=0.25, haze_west_of=0.6)])
    plants = sv.plants_view(BOX, TODAY)
    assert plants.covered == 1.0
    assert plants.hazy[:, :60].all() and not plants.hazy[:, -30:].any()


def test_the_search_goes_further_back_only_while_squares_lack_a_sharp_look(tmp_path, in_process):
    asked = in_process([
        _pass(tmp_path, TODAY - timedelta(days=3), ndvi=0.6, cloud_east_of=0.5),
        _pass(tmp_path, TODAY - timedelta(days=70), ndvi=0.3),
    ])
    plants = sv.plants_view(BOX, TODAY)
    assert plants.covered == 1.0
    assert len(asked) == 2 and asked[1] == f"{(TODAY - timedelta(days=100)).isoformat()}/{(TODAY - timedelta(days=46)).isoformat()}"


def test_a_grid_whose_scenes_stop_short_of_the_photo_gives_way_to_the_other_grid(tmp_path, in_process):
    short = tmp_path / "short"
    short.mkdir()
    in_process([
        _pass(short, TODAY - timedelta(days=2), ndvi=0.9, tile="35MRU", shift_m=1200),
        _pass(short, TODAY - timedelta(days=7), ndvi=0.9, tile="35MRU", shift_m=1200),
        _pass(tmp_path, TODAY - timedelta(days=2), ndvi=0.5),
    ])
    plants = sv.plants_view(BOX, TODAY)
    assert plants.covered == 1.0 and np.nanmedian(plants.ndvi) == pytest.approx(0.5, abs=0.01)


def test_no_clear_pass_and_a_failed_search_each_say_so_in_plain_words(tmp_path, in_process):
    in_process([_pass(tmp_path, TODAY - timedelta(days=2), ndvi=0.5, cloud_east_of=0.0)])
    with pytest.raises(sv.NoPicture, match="No satellite pass saw this ground clearly in the last 180 days"):
        sv.plants_view(BOX, TODAY)
    in_process([], error="HTTPSConnectionPool: timed out")
    with pytest.raises(sv.NoPicture) as said:
        sv.plants_view(BOX, TODAY)
    assert str(said.value) == "The satellite's pictures could not be searched just now."  # no raw error text


# --- Squares and sentences ----------------------------------------------------------------------------------


def _plants(ndvi: Any, days_ago: Any, change: Optional[sv.Change] = None, haze: Any = None) -> sv.Plants:
    ndvi = np.asarray(ndvi, dtype=np.float32)
    seen = np.vectorize(lambda d: 0 if d < 0 else (TODAY - timedelta(days=int(d))).toordinal())(np.asarray(days_ago)).astype(np.int32)
    west, south, east, north = transform_bounds("EPSG:4326", "EPSG:32736", *BOX)
    grid = sv.Grid("EPSG:32736", (10.0, 0.0, west, 0.0, -10.0, north), ndvi.shape)
    return sv.Plants(grid, ndvi, seen, np.zeros(ndvi.shape, bool) if haze is None else np.asarray(haze, bool), change, 3)


def _by_size(features: list[dict[str, Any]], size: int, kind: str = "Polygon") -> list[dict[str, Any]]:
    return [f for f in features if f["properties"]["h3_resolution"] == size and f["geometry"]["type"] == kind]


def _area(features: list[dict[str, Any]]) -> float:
    return sum(Polygon(f["geometry"]["coordinates"][0]).area for f in features)


def test_hexagon_sizes_are_never_finer_than_the_data_and_widen_while_the_photo_holds_enough_of_them():
    assert sv.hexagon_sizes(1_240_000, 10) == [10, 11, 12]  # Cyampirita, 124 ha, 10 m squares: 300 m2 at the smallest
    assert sv.hexagon_sizes(1_240_000, 100) == [9, 10]  # its soil moisture: 1.5 ha at the smallest; 74 ha would be 2 hexagons
    assert sv.hexagon_sizes(1_240_000, 20) == [9, 10, 11]  # wider squares, wider hexagons
    assert sv.hexagon_sizes(3_000, 10) == [12]  # a small plot: one size
    assert sv.hexagon_sizes(100_000_000, 100) == [8, 9, 10]


def test_plants_hexagons_cover_the_whole_photo_at_every_size_and_stay_inside_its_frame():
    ndvi = np.full((89, 156), 0.55)  # the Cyampirita photo's own grid
    drawn = sv.plants_hexagons(_plants(ndvi, np.full(ndvi.shape, 4)), BOX)
    features = drawn["geojson"]["features"]
    assert drawn["sizes"] == [10, 11, 12] and drawn["square_m"] == 10 and drawn["main_day"] == TODAY - timedelta(days=4)
    assert drawn["latitude"] == pytest.approx(-1.697, abs=0.001)
    frame = box(*BOX)
    counts = [len(_by_size(features, size)) for size in drawn["sizes"]]
    assert counts[0] < counts[1] < counts[2] and 3_500 < counts[2] < 5_000
    for size in drawn["sizes"]:
        mine = _by_size(features, size)
        # the hexagons of one size tile the frame: no hole and no overlap. The grid of squares leaves at most a few
        # metres of the frame's edge unseen, where a north-up box and the satellite's grid lines part.
        assert _area(mine) == pytest.approx(frame.area, rel=0.02)
        assert all(frame.buffer(1e-9).contains(Polygon(f["geometry"]["coordinates"][0])) for f in mine)
        assert all(f["geometry"]["coordinates"][0][0] == f["geometry"]["coordinates"][0][-1] for f in mine)


def test_a_hexagon_says_its_mean_its_class_its_day_and_on_wider_sizes_its_weakest_square():
    ndvi = np.full((89, 156), 0.62)
    ndvi[40:45, 70:75] = 0.05  # a bare patch 50 m across in the middle of dense plants
    features = sv.plants_hexagons(_plants(ndvi, np.full(ndvi.shape, 4)), BOX)["geojson"]["features"]
    wide, small = _by_size(features, 10), _by_size(features, 12)
    plain = next(f["properties"] for f in small if f["properties"]["ndvi"] == 0.62)
    # the day carries its year and there is no "days old": the layer is saved on a map and read on later days
    assert plain == {"plants": "Dense vegetation", "ndvi": 0.62, "seen_on": "19 Nov 2026", "older": False, "h3_resolution": 12}
    # from afar a 2,500 m2 patch only lowers the mean of a 1.5 ha hexagon a little, so the hexagon also names its
    # weakest square: the reason to zoom in
    holding = [f["properties"] for f in wide if f["properties"]["weakest"] == 0.05]
    assert 1 <= len(holding) <= 3 and all(0.5 < p["ndvi"] <= 0.62 and p["plants"] != "Bare soil" for p in holding)
    assert all(f["properties"]["weakest"] == 0.62 for f in wide if f["properties"] not in holding)
    # zoomed in, the patch is its own hexagons. A hexagon is 19 m across and holds about three squares, so a patch
    # narrower than about 40 m is drawn weaker, not bare: the hexagons along its rim mix it with its neighbours.
    bare = [f["properties"] for f in small if f["properties"]["plants"] == "Bare soil"]
    assert 1 <= len(bare) <= 9 and all("weakest" not in p for p in bare)  # cloud is already left out: not "Bare soil or cloud"


def test_an_older_look_and_haze_are_said_on_the_hexagon_and_a_tie_of_days_goes_to_the_older():
    days = np.full((89, 156), 4)
    days[:, 100:] = 31
    haze = np.zeros(days.shape, bool)
    haze[:, 150:] = True
    features = sv.plants_hexagons(_plants(np.full(days.shape, 0.4), days, haze=haze), BOX)["geojson"]["features"]
    small = [f["properties"] for f in _by_size(features, 12)]
    assert {p["seen_on"] for p in small} == {"19 Nov 2026", "23 Oct 2026"}
    assert all(p["older"] == (p["seen_on"] == "23 Oct 2026") for p in small)
    notes = {p.get("note") for p in small}
    assert {None, "An older look than most of the photo", "An older look than most of the photo; Seen through haze: may read low"} <= notes
    # a hexagon lying across the line between the two looks says so
    assert any("Parts of it were seen on other days" in (p.get("note") or "") for p in small)
    assert 0.3 < sum(p["older"] for p in small) / len(small) < 0.42  # 36% of the photo's columns


def test_a_large_photo_is_first_joined_into_wider_squares_holding_their_mean_and_their_oldest_day(monkeypatch):
    monkeypatch.setattr(sv, "MAX_SQUARES", 4)
    ndvi = np.full((4, 4), 0.4)
    ndvi[:2, :2] = [[0.2, 0.4], [0.6, 0.8]]
    days = np.full((4, 4), 4)
    days[0, 0] = 31
    drawn = sv.plants_hexagons(_plants(ndvi, days), BOX)
    assert drawn["square_m"] == 20 and drawn["sizes"] == [11]  # 20 m squares: 2,100 m2 hexagons, not 300 m2 ones
    readings = [f["properties"]["ndvi"] for f in drawn["geojson"]["features"]]
    # the first four squares became one of 0.5 (their mean), the rest stay 0.4; a hexagon holds a mix of the two
    assert readings and all(0.4 <= value <= 0.5 for value in readings) and max(readings) > 0.4


def test_the_plants_sentences_say_cover_days_classes_and_direction():
    ndvi = np.full((10, 10), 0.55)
    ndvi[:, 7:] = 0.25
    days = np.full((10, 10), 4)
    days[:, 7:] = 31
    change = sv.Change(TODAY - timedelta(days=4), date(2026, 10, 8), 0.24, 70)
    facts = sv.plants_facts(_plants(ndvi, days, change), TODAY)
    assert facts["sentences"] == [
        "Every 10 m square of this photo has a clear satellite look (Sentinel-2).",
        "Seen: 70% on 19 Nov and 30% on 23 Oct.",
        "30% of the photo holds an older look, drawn in grey: clouds hid it on 19 Nov, so it must not be read against "
        "the fresh part.",
        "By greenness (NDVI): 70% cropland or moderate vegetation and 30% sparse vegetation. The middle square reads 0.55.",
        "On the squares seen 19 Nov, NDVI rose by 0.24 since 8 Oct.",
    ]
    assert facts["direction"] == "rose" and facts["median"] == 0.55
    steady = sv.plants_facts(_plants(ndvi, days, sv.Change(TODAY, date(2026, 10, 8), -0.02, 70)), TODAY)
    assert steady["direction"] == "steady" and "stayed about the same (-0.02)" in steady["sentences"][-1]
    fell = sv.plants_facts(_plants(ndvi, days, sv.Change(TODAY, date(2026, 10, 8), -0.07, 70)), TODAY)
    assert fell["direction"] == "fell" and "NDVI fell by 0.07" in fell["sentences"][-1]


def test_ground_no_pass_saw_is_said_and_not_counted_in_the_shares():
    ndvi = np.full((10, 10), 0.5)
    ndvi[:, :2] = np.nan
    days = np.full((10, 10), 4)
    days[:, :2] = -1
    plants = _plants(ndvi, days)
    assert plants.covered == pytest.approx(0.8)
    facts = sv.plants_facts(plants, TODAY)
    assert facts["sentences"][0] == ("80% of this photo has a clear satellite look (Sentinel-2); no pass saw the rest in "
                                     "the last 180 days.")
    assert facts["classes"][2]["share"] == 1.0  # of the squares that were seen
    drawn = sv.plants_hexagons(plants, BOX)["geojson"]["features"]
    assert drawn and all(f["properties"]["ndvi"] == 0.5 for f in drawn)  # no hexagon is drawn from ground no pass saw


# --- Soil moisture ---------------------------------------------------------------------------------------------


def _soil(percent: Any = ((62.0, 68.3), (76.0, np.nan)), *, first: bool = True, centre: Optional[float] = 69.0,
          low: Optional[float] = 40.0, high: Optional[float] = 75.0) -> sv.Soil:
    return sv.Soil(period="2026-11-D1", percent=np.asarray(percent, dtype=np.float32), west=30.4169922, north=-1.6923828,
                   step=0.0009765625, first_estimate=first, centre=centre, year_low=low, year_high=high)


def _soil_over_the_photo(first: bool = True) -> sv.Soil:
    """FAO's own grid over the Cyampirita photo: 9 rows of 15 squares, wetter to the east."""
    percent = np.tile(np.linspace(62.0, 76.0, 15, dtype=np.float32), (9, 1))
    return sv.Soil(period="2026-11-D1", percent=percent, west=30.4169922, north=-1.6923828, step=0.0009765625,
                   first_estimate=first, centre=69.0, year_low=40.0, year_high=75.0)


def test_soil_hexagons_cover_the_photo_inside_its_frame_with_their_reading_band_and_ten_days():
    drawn = sv.soil_hexagons(_soil_over_the_photo(), BOX)
    features = drawn["geojson"]["features"]
    assert drawn["sizes"] == [9, 10]
    frame = box(*BOX)
    for size in drawn["sizes"]:
        mine = _by_size(features, size)
        assert _area(mine) == pytest.approx(frame.area, rel=1e-6)  # the soil squares reach past the frame: all of it is covered
        assert all(frame.buffer(1e-9).contains(Polygon(f["geometry"]["coordinates"][0])) for f in mine)
    assert len(_by_size(features, 9)) < 25 < len(_by_size(features, 10))
    one = _by_size(features, 10)[0]["properties"]
    assert set(one) == {"moisture", "band", "period", "h3_resolution", "note"}
    assert one["period"] == "1-10 Nov 2026" and one["note"] == "FAO's first estimate" and one["band"] == sv.soil_band(one["moisture"])[0]
    west_most = min(_by_size(features, 10), key=lambda f: f["geometry"]["coordinates"][0][0][0])["properties"]["moisture"]
    east_most = max(_by_size(features, 10), key=lambda f: f["geometry"]["coordinates"][0][0][0])["properties"]["moisture"]
    assert west_most < 65 < 73 < east_most  # a hexagon holds the ground under it, not the photo's mean
    assert "note" not in sv.soil_hexagons(_soil_over_the_photo(first=False), BOX)["geojson"]["features"][0]["properties"]


def test_each_soil_hexagon_with_room_inside_the_frame_gets_one_point_that_writes_its_reading():
    features = sv.soil_hexagons(_soil_over_the_photo(), BOX)["geojson"]["features"]
    frame = box(*BOX)
    for size in (9, 10):
        outlines, points = _by_size(features, size), _by_size(features, size, "Point")
        assert 0 < len(points) <= len(outlines)
        assert all(frame.contains(Point(p["geometry"]["coordinates"])) for p in points)
        assert all(p["properties"]["reading"] == f"{p['properties']['moisture']:.0f}%" for p in points)
        # a sliver of a hexagon left inside the frame has no room for a number
        slivers = [f for f in outlines if Polygon(f["geometry"]["coordinates"][0]).area < 0.1 * max(
            Polygon(o["geometry"]["coordinates"][0]).area for o in outlines)]
        assert len(points) <= len(outlines) - len(slivers) + 1
    assert all("reading" not in f["properties"] for f in features if f["geometry"]["type"] == "Polygon")


def test_a_soil_square_with_no_value_is_not_drawn_and_no_squares_give_no_hexagons():
    none = sv.soil_hexagons(_soil(((np.nan, np.nan), (np.nan, np.nan))), BOX)
    assert none["geojson"]["features"] == [] and none["sizes"] == []
    # one square of FAO's file holds a value: only the hexagons over it are drawn, each with that value
    one = sv.soil_hexagons(_soil(((62.0, np.nan), (np.nan, np.nan))), BOX)["geojson"]["features"]
    assert one and {f["properties"]["moisture"] for f in one} == {62.0}
    assert sum(Polygon(f["geometry"]["coordinates"][0]).area for f in one if f["geometry"]["type"] == "Polygon") < 0.1 * box(*BOX).area


def test_passes_whose_bands_hold_no_value_give_no_picture_not_a_crash(tmp_path, in_process):
    empty = _pass(tmp_path, TODAY - timedelta(days=2), ndvi=0.5)
    for band in ("red", "nir"):
        with rasterio.open(empty["assets"][band]["href"], "r+") as dst:
            dst.write(np.zeros((dst.height, dst.width), dtype="uint16"), 1)
    in_process([empty])
    with pytest.raises(sv.NoPicture, match="No satellite pass saw this ground clearly"):
        sv.plants_view(BOX, TODAY)


def test_a_kept_file_that_is_not_a_view_is_made_again(tmp_path, monkeypatch):
    monkeypatch.setattr(sv, "_KEEP_DIR", tmp_path)
    sv._views.clear()
    sv._keep_path(sv._key(BOX)).write_bytes(b"not a zip file")
    monkeypatch.setattr(sv, "plants_view", lambda bbox, today: _plants(np.full((4, 4), 0.5), np.full((4, 4), 4)))
    monkeypatch.setattr(sv, "soil_view", lambda bbox, today: _soil())
    assert sv.view(BOX, TODAY).plants is not None
    sv._views.clear()


def test_soil_bands_are_number_ranges_with_no_dry_or_wet_words():
    assert sv.soil_band(-0.4)[0] == "Below 45%"  # a reading just under zero still has a band
    assert [sv.soil_band(v)[0] for v in (10, 45, 54.9, 65, 75, 100)] == [
        "Below 45%", "45-55%", "45-55%", "65-75%", "75% and above", "75% and above"]
    assert sv.period_words("2026-11-D1", TODAY) == "1-10 Nov"
    assert sv.period_words("2026-02-D3", TODAY) == "21-28 Feb"
    assert sv.period_words("2025-12-D3", TODAY) == "21-31 Dec 2025"
    assert sv.period_words("2026-11-D1") == "1-10 Nov 2026"  # no "today" to lean on: a saved layer is read later


def test_the_soil_sentences_place_the_reading_in_its_own_year_and_say_how_far_to_trust_it():
    facts = sv.soil_facts(_soil(), TODAY)
    assert facts["sentences"] == [
        "Soil moisture for 1-10 Nov (FAO WaPOR estimate, 100 m squares): the middle square reads 68%, from 62% to 76% "
        "across the photo.",
        "At the photo's centre it reads 69%; over the year before it ran from 40% to 75%, so it is near the top of its "
        "own year.",
        "This is FAO's first estimate; FAO replaces it with a steadier one about two months later.",
        "It follows Rwanda's wet and dry seasons but was not confirmed against rain week by week (tested 2026-11-23): "
        "read it as the season's level, not a precise reading.",
    ]
    assert facts["position"] == "high"
    assert sv.soil_facts(_soil(centre=45.0), TODAY)["position"] == "low"
    assert sv.soil_facts(_soil(centre=57.0), TODAY)["position"] == "middle"
    unplaced = sv.soil_facts(_soil(low=None, high=None, first=False), TODAY)
    assert unplaced["position"] is None and len(unplaced["sentences"]) == 2  # no year to place it in: not guessed


def test_soil_is_read_from_the_newest_published_ten_days_over_the_whole_photo(tmp_path, monkeypatch):
    step = 0.0009765625
    left, top = np.floor(30.40 / step) * step, np.ceil(-1.68 / step) * step
    values = np.full((40, 50), 650, dtype="int16")
    values[:, 30:] = 700
    values[0, 0] = wapor_service.NODATA
    path = tmp_path / "rsm.tif"
    with rasterio.open(path, "w", driver="GTiff", height=40, width=50, count=1, dtype="int16", crs="EPSG:4326",
                       transform=from_origin(left, top, step, step), nodata=wapor_service.NODATA) as dst:
        dst.write(values, 1)
    tried: list[str] = []

    def run(fn, url, *args):
        tried.append(url)
        return ("unpublished", None) if "2026-11-D3" in url or "2026-11-D2" in url else fn(str(path), *args)

    monkeypatch.setattr(sv.raster_process, "run", run)
    year = {"2026-11-D1": 0.66, **{f"2026-{m:02d}-D1": 0.40 + m * 0.03 for m in range(1, 11)},
            **{f"2026-{m:02d}-D2": 0.5 for m in range(1, 11)}, **{f"2026-{m:02d}-D3": 0.5 for m in range(1, 11)}}
    asked: dict[str, Any] = {}

    def series(layer, periods, lat, lon):
        asked.update(layer=layer, periods=periods, lat=lat, lon=lon)
        return {p: year.get(p) for p in periods}

    monkeypatch.setattr(wapor_service, "point_series", series)
    soil = sv.soil_view(BOX, TODAY)
    assert [u.rsplit(".", 2)[-2] for u in tried] == ["2026-11-D3", "2026-11-D2", "2026-11-D1"]
    assert soil.period == "2026-11-D1" and soil.first_estimate is True
    assert soil.percent.shape == (9, 15)  # every 100 m square that touches the photo
    assert np.nanmin(soil.percent) == pytest.approx(65.0) and np.nanmax(soil.percent) == pytest.approx(70.0)
    assert soil.west <= BOX[0] and soil.north >= BOX[3] and soil.west + 15 * soil.step >= BOX[2]
    assert asked["layer"] == "L2-RSM-D" and len(asked["periods"]) == 37 and asked["periods"][1] == "2026-10-D3"
    assert asked["lat"] == pytest.approx(-1.696895) and asked["lon"] == pytest.approx(30.424485)
    assert (soil.centre, soil.year_low, soil.year_high) == (pytest.approx(66.0), pytest.approx(43.0), pytest.approx(70.0))


def test_soil_that_cannot_be_read_or_is_not_published_says_so_in_plain_words(monkeypatch):
    monkeypatch.setattr(sv.raster_process, "run", lambda fn, *args: ("unpublished", None))
    with pytest.raises(sv.NoPicture, match="FAO has not published soil moisture for the last weeks"):
        sv.soil_view(BOX, TODAY)
    assert sv._read_soil("/no/such/file-404.tif", BOX, {})[0] == "error"  # "404" in a name is not "not published"
    monkeypatch.setattr(sv.raster_process, "run", lambda fn, *args: ("error", "CPLE_HttpResponseError: HTTP response code: 503"))
    with pytest.raises(sv.NoPicture) as said:
        sv.soil_view(BOX, TODAY)
    assert str(said.value) == "FAO's soil moisture file could not be read just now."


# --- The view, and what the two show together -----------------------------------------------------------------


def _view(plants: Optional[sv.Plants], soil: Optional[sv.Soil], soil_missing: Optional[str] = None) -> sv.View:
    return sv.View(bbox=tuple(BOX), today=TODAY, plants=plants, plants_missing=None, soil=soil,  # type: ignore[arg-type]
                   soil_missing=soil_missing)


@pytest.mark.parametrize("moved, centre, said", [
    (0.24, 69.0, "Plants are filling in and the soil reads near the top of its year: the rains are carrying the crop."),
    (-0.10, 42.0, "Plants are thinning and the soil reads near the bottom of its year, as in a dry spell or the dry season. "
                  "A harvest lowers greenness too."),
    (-0.10, 69.0, "Plants are thinning though the soil reads near the top of its year. A harvest, pests, disease or too "
                  "much water are worth checking on the ground."),
    (0.01, 57.0, "Greenness has held steady and the soil reads in the middle of its year."),
])
def test_together_is_one_sentence_from_the_direction_of_greenness_and_the_soils_place_in_its_year(moved, centre, said):
    plants = _plants(np.full((4, 4), 0.5), np.full((4, 4), 4), sv.Change(TODAY, date(2026, 10, 8), moved, 16))
    assert sv.words(_view(plants, _soil(centre=centre)))["together"] == said


def test_together_is_left_unsaid_without_a_direction_or_a_place_in_the_year():
    plants = _plants(np.full((4, 4), 0.5), np.full((4, 4), 4))
    assert sv.words(_view(plants, _soil()))["together"] is None  # no earlier look to compare with
    moved = _plants(np.full((4, 4), 0.5), np.full((4, 4), 4), sv.Change(TODAY, date(2026, 10, 8), 0.2, 16))
    assert sv.words(_view(moved, _soil(low=None, high=None)))["together"] is None
    said = sv.words(_view(moved, None, "FAO's soil moisture file could not be read just now."))
    assert said["soil"] is None and said["together"] is None and said["missing"] == ["FAO's soil moisture file could not be read just now."]


def test_a_view_is_used_while_fresh_kept_on_disk_and_a_failed_part_is_asked_for_again(tmp_path, monkeypatch):
    monkeypatch.setattr(sv, "_KEEP_DIR", tmp_path / "kept")
    sv._views.clear()
    plants = _plants(np.full((4, 4), 0.5), np.full((4, 4), 4), sv.Change(TODAY, date(2026, 10, 8), 0.2, 16))
    calls = {"plants": 0, "soil": 0}

    def make_plants(bbox, today):
        calls["plants"] += 1
        return plants

    def no_soil(bbox, today):
        calls["soil"] += 1
        raise sv.NoPicture("FAO's soil moisture file could not be read just now.")

    monkeypatch.setattr(sv, "plants_view", make_plants)
    monkeypatch.setattr(sv, "soil_view", no_soil)
    half = sv.view(BOX, TODAY)
    assert half.plants is plants and half.soil is None
    assert half.soil_missing is not None and half.soil_missing.startswith("FAO's soil moisture file")
    monkeypatch.setattr(sv, "soil_view", lambda bbox, today: calls.__setitem__("soil", calls["soil"] + 1) or _soil())
    whole = sv.view(BOX, TODAY)
    assert whole.soil is not None and calls == {"plants": 2, "soil": 2}  # the half view was not kept
    assert sv.view(BOX, TODAY) is whole and calls == {"plants": 2, "soil": 2}

    sv._views.clear()  # as after a restart: the day's view comes back from the cache volume
    again = sv.view(BOX, TODAY)
    assert calls == {"plants": 2, "soil": 2}
    assert again.plants is not None and again.soil is not None
    assert again.plants.change == plants.change and again.plants.grid == plants.grid
    assert np.array_equal(again.plants.ndvi, plants.ndvi) and np.array_equal(again.plants.seen, plants.seen)
    assert again.soil.period == "2026-11-D1" and np.allclose(again.soil.percent, _soil().percent, equal_nan=True)
    assert sv.words(again) == sv.words(whole) and again.today == TODAY
    # the calendar day does not matter (a view kept by its day was made again for everyone at midnight UTC) ...
    assert sv.view(BOX, TODAY + timedelta(days=1)) is again and calls == {"plants": 2, "soil": 2}
    # ... its age does: after FRESH_HOURS it is made again
    sv._views.clear()
    (kept_file,) = (tmp_path / "kept").glob("*.npz")
    stale = kept_file.stat().st_mtime - (sv.FRESH_HOURS + 1) * 3600
    os.utime(kept_file, (stale, stale))
    sv.view(BOX, TODAY + timedelta(days=1))
    assert calls == {"plants": 3, "soil": 3}
    sv._views.clear()


def test_a_box_too_wide_for_one_farm_is_refused_in_plain_words_and_a_box_that_is_not_one_raises(monkeypatch):
    monkeypatch.setattr(sv, "plants_view", lambda *a: pytest.fail("read the satellite for a box too wide"))
    wide = sv.view([29.0, -2.5, 30.5, -1.5], TODAY)
    assert wide.plants is None and wide.soil is None and wide.plants_missing is not None
    assert wide.plants_missing.startswith("This picture covers about 18,") and "too wide" in wide.plants_missing
    with pytest.raises(ValueError):
        sv.view([30.43, -1.70, 30.41, -1.69], TODAY)


def test_two_askers_at_once_get_one_making_and_fresh_tells_when_asking_costs_nothing(tmp_path, monkeypatch):
    import threading
    import time

    monkeypatch.setattr(sv, "_KEEP_DIR", tmp_path / "kept")
    sv._views.clear()
    plants = _plants(np.full((4, 4), 0.5), np.full((4, 4), 4))
    made = []

    def slow_plants(bbox, today):
        made.append(1)
        time.sleep(0.2)
        return plants

    monkeypatch.setattr(sv, "plants_view", slow_plants)
    monkeypatch.setattr(sv, "soil_view", lambda bbox, today: _soil())
    assert sv.fresh(BOX) is False
    got: list[sv.View] = []
    askers = [threading.Thread(target=lambda: got.append(sv.view(BOX, TODAY))) for _ in range(3)]
    for asker in askers:
        asker.start()
    for asker in askers:
        asker.join()
    assert len(made) == 1 and got[0] is got[1] is got[2]
    assert sv.fresh(BOX) is True
    sv._views.clear()
    assert sv.fresh(BOX) is True  # after a restart it is still on the cache volume
    sv._views.clear()


# --- Made ahead, each night --------------------------------------------------------------------------------------


KNOWN_PASS = date(2026, 11, 14)  # the newest pass the search holds on the first night
MID_NOV = date(2026, 11, 18)  # the ten days after 1-10 Nov have not ended: FAO can hold nothing newer


@pytest.fixture
def night(tmp_path, monkeypatch):
    """A cache volume of its own, and counters of every read of the satellites."""
    monkeypatch.setattr(sv, "_KEEP_DIR", tmp_path / "kept")
    sv._views.clear()
    state: dict[str, Any] = {"plants": 0, "soil": 0, "searched": [], "newest": KNOWN_PASS, "soil_asked": 0,
                             "soil_period": "2026-11-D1", "soil_fails": False}

    def plants_view(bbox, today):
        state["plants"] += 1
        made = _plants(np.full((4, 4), 0.5), np.full((4, 4), 4))
        return sv.Plants(made.grid, made.ndvi, made.seen, made.hazy, None, 3, newest_pass=state["newest"])

    def newest_soil(bbox, today):
        state["soil_asked"] += 1
        if state["soil_fails"]:
            raise sv.NoPicture("FAO's soil moisture file could not be read just now.")
        return state["soil_period"], None

    def soil_view(bbox, today, newest=None):
        state["soil"] += 1
        if newest is None:
            newest_soil(bbox, today)
        soil = _soil()
        return sv.Soil(state["soil_period"], soil.percent, soil.west, soil.north, soil.step, soil.first_estimate,
                       soil.centre, soil.year_low, soil.year_high)

    def newest_pass_since(bbox, since, today):
        state["searched"].append((since, today))
        return state["newest"]

    monkeypatch.setattr(sv, "plants_view", plants_view)
    monkeypatch.setattr(sv, "soil_view", soil_view)
    monkeypatch.setattr(sv, "_newest_soil", newest_soil)
    monkeypatch.setattr(sv, "_newest_pass_since", newest_pass_since)
    yield state
    sv._views.clear()


def _kept_file(tmp_path: Path) -> Path:
    (kept_file,) = (tmp_path / "kept").glob("*.npz")
    return kept_file


def _age(kept_file: Path, hours: float) -> float:
    then = kept_file.stat().st_mtime - hours * 3600
    os.utime(kept_file, (then, then))
    return then




def test_the_first_night_makes_the_view_and_the_next_ask_costs_nothing(night, tmp_path):
    assert sv.refresh(BOX, MID_NOV) == sv.MADE
    assert (night["plants"], night["soil"], night["searched"]) == (1, 1, [])
    sv._views.clear()  # the app is another process: it finds the view on the cache volume
    assert sv.fresh(BOX) is True
    seen = sv.view(BOX, MID_NOV)
    assert (night["plants"], night["soil"]) == (1, 1)
    assert seen.plants is not None and seen.plants.newest_pass == KNOWN_PASS


def test_a_night_with_no_new_pass_is_one_search_and_the_kept_view_is_fresh_again(night, tmp_path):
    sv.refresh(BOX, MID_NOV)
    made_at = _age(_kept_file(tmp_path), sv.FRESH_HOURS + 20)  # two nights on: past its freshness
    sv._views.clear()
    assert sv.fresh(BOX) is False
    assert sv.refresh(BOX, MID_NOV + timedelta(days=2)) == sv.NOTHING_NEW
    # the search asked only about the days since the newest pass the kept view knew of
    assert night["searched"] == [(KNOWN_PASS, MID_NOV + timedelta(days=2))]
    assert (night["plants"], night["soil"]) == (1, 1)  # no pass and no soil file was read again
    assert _kept_file(tmp_path).stat().st_mtime > made_at and sv.fresh(BOX) is True
    sv._views.clear()
    again = sv.view(BOX, MID_NOV + timedelta(days=2))
    assert again.today == MID_NOV + timedelta(days=2) and again.plants is not None and again.plants.ndvi.shape == (4, 4)
    assert (night["plants"], night["soil"]) == (1, 1)


def test_a_new_pass_cloudy_or_not_has_the_view_made_again(night, tmp_path):
    sv.refresh(BOX, MID_NOV)
    night["newest"] = MID_NOV
    assert sv.refresh(BOX, MID_NOV + timedelta(days=1)) == sv.MADE
    assert (night["plants"], night["soil"]) == (2, 2)
    # the pass is now known: the night after reads nothing again, even if that pass was all cloud
    assert sv.refresh(BOX, MID_NOV + timedelta(days=2)) == sv.NOTHING_NEW
    assert night["plants"] == 2 and night["searched"][-1] == (MID_NOV, MID_NOV + timedelta(days=2))


def test_soil_moisture_is_read_again_only_when_fao_has_published_something_newer(night, tmp_path):
    sv.refresh(BOX, MID_NOV)
    assert (night["soil"], night["soil_asked"]) == (1, 1)
    # 18 to 20 Nov: the ten days after 1-10 Nov have not ended, so FAO is not even asked
    assert sv.refresh(BOX, date(2026, 11, 20)) == sv.NOTHING_NEW and (night["soil"], night["soil_asked"]) == (1, 1)
    # 21 Nov: 11-20 Nov has ended and its file may be up. FAO is asked which period is its newest; it is still
    # 1-10 Nov, so the reading and the centre's year are not read again.
    assert sv.refresh(BOX, date(2026, 11, 21)) == sv.NOTHING_NEW
    assert (night["plants"], night["soil"], night["soil_asked"]) == (1, 1, 2)
    night["soil_period"] = "2026-11-D2"
    assert sv.refresh(BOX, date(2026, 11, 24)) == sv.SOIL_AGAIN
    assert (night["plants"], night["soil"], night["soil_asked"]) == (1, 2, 3)
    sv._views.clear()
    kept = sv.view(BOX, date(2026, 11, 24))
    assert kept.soil is not None and kept.soil.period == "2026-11-D2" and night["plants"] == 1
    # 25 Nov: 21-30 Nov has not ended, nothing newer can exist
    assert sv.refresh(BOX, date(2026, 11, 25)) == sv.NOTHING_NEW and night["soil_asked"] == 3
    # a soil file that cannot be read leaves the kept reading in place, and the view fresh
    night["soil_fails"] = True
    assert sv.refresh(BOX, date(2026, 12, 2)) == sv.NOTHING_NEW and (night["soil"], night["soil_asked"]) == (2, 4)
    sv._views.clear()
    still = sv.view(BOX, date(2026, 12, 2))
    assert still.soil is not None and still.soil.period == "2026-11-D2"


def test_an_older_period_from_fao_is_one_file_failing_to_open_not_news(night, tmp_path):
    night["soil_period"] = "2026-11-D2"
    sv.refresh(BOX, date(2026, 11, 24))
    night["soil_period"] = "2026-11-D1"  # tonight the newer file did not open and the walk back found the older one
    assert sv.refresh(BOX, date(2026, 12, 2)) == sv.NOTHING_NEW and night["soil"] == 1
    sv._views.clear()
    kept = sv.view(BOX, date(2026, 12, 2))
    assert kept.soil is not None and kept.soil.period == "2026-11-D2"


def test_a_new_pass_that_cannot_be_read_leaves_the_older_view_fresh_and_is_tried_again(night, tmp_path):
    sv.refresh(BOX, MID_NOV)
    made_at = _age(_kept_file(tmp_path), sv.FRESH_HOURS + 2)
    night["newest"] = MID_NOV
    night["soil_fails"] = True  # FAO's file cannot be read tonight
    assert sv.refresh(BOX, MID_NOV + timedelta(days=1)) == sv.KEPT_OLDER and night["plants"] == 2
    assert _kept_file(tmp_path).stat().st_mtime > made_at and sv.fresh(BOX) is True
    sv._views.clear()
    served = sv.view(BOX, MID_NOV + timedelta(days=1))
    assert served.plants is not None and served.plants.newest_pass == KNOWN_PASS and night["plants"] == 2
    night["soil_fails"] = False
    assert sv.refresh(BOX, MID_NOV + timedelta(days=2)) == sv.MADE and night["plants"] == 3


def test_a_cache_volume_that_cannot_be_written_is_owned_up_to(night, tmp_path, monkeypatch):
    blocked = tmp_path / "a file where the folder should be"
    blocked.write_text("x")
    monkeypatch.setattr(sv, "_KEEP_DIR", blocked / "kept")
    assert sv.refresh(BOX, MID_NOV) == sv.NOT_KEPT and night["plants"] == 1
    sv._views.clear()
    assert sv.fresh(BOX) is False


def test_a_copy_in_memory_lives_no_longer_than_its_files_freshness(night, tmp_path):
    held = sv.view(BOX, MID_NOV)
    _age(_kept_file(tmp_path), sv.FRESH_HOURS + 1)  # the night was missed: the file is past its freshness
    sv._views[sv._key(BOX)] = (held, _kept_file(tmp_path).stat().st_mtime)
    assert sv.fresh(BOX) is False
    again = sv.view(BOX, MID_NOV + timedelta(days=2))
    assert again is not held and night["plants"] == 2


def test_a_first_estimate_that_fao_has_since_replaced_is_read_again(night, tmp_path):
    sv.refresh(BOX, MID_NOV)
    # 80 days after 10 Nov the file is FAO's final one: the same period, read again
    assert sv.refresh(BOX, date(2027, 1, 29)) == sv.SOIL_AGAIN and night["soil"] == 2


def test_a_first_estimate_old_enough_to_have_been_replaced_is_read_again():
    first = _soil()  # 1-10 Nov, a first estimate
    assert sv._soil_may_be_newer(first, date(2026, 11, 20)) is False
    assert sv._soil_may_be_newer(first, date(2026, 11, 21)) is True
    final = _soil(first=False)
    kept_newest = sv.Soil("2026-11-D3", final.percent, final.west, final.north, final.step, True, 69.0, 40.0, 75.0)
    assert sv._soil_may_be_newer(kept_newest, date(2026, 12, 10)) is False  # 1-10 Dec ends that day
    assert sv._soil_may_be_newer(kept_newest, date(2026, 12, 11)) is True
    old_first = sv.Soil("2026-08-D3", final.percent, final.west, final.north, final.step, True, 69.0, 40.0, 75.0)
    assert sv._soil_may_be_newer(old_first, date(2026, 11, 23)) is True


def test_a_search_that_fails_leaves_the_kept_view_as_it_is(night, tmp_path):
    sv.refresh(BOX, MID_NOV)
    made_at = _age(_kept_file(tmp_path), 30)
    night["newest"] = None
    assert sv.refresh(BOX, MID_NOV + timedelta(days=1)) == sv.NOT_CHECKED
    assert _kept_file(tmp_path).stat().st_mtime == made_at and night["plants"] == 1


def test_a_view_kept_before_passes_were_remembered_a_half_view_and_a_wide_box(night, tmp_path):
    night["newest"] = None  # as plants_view gave before it remembered the newest pass
    assert sv.refresh(BOX, MID_NOV) == sv.MADE
    night["newest"] = KNOWN_PASS
    assert sv.refresh(BOX, MID_NOV) == sv.MADE and night["plants"] == 2 and night["searched"] == []
    night["soil_fails"] = True
    other = [BOX[0] + 0.1, BOX[1], BOX[2] + 0.1, BOX[3]]
    assert sv.refresh(other, MID_NOV) == sv.NOT_WHOLE
    assert sv.fresh(other) is False  # nothing is kept: the first ask tries again
    assert sv.refresh([29.0, -2.5, 30.5, -1.5], MID_NOV) == sv.TOO_WIDE and night["plants"] == 3


def test_the_app_gives_up_its_copy_in_memory_when_the_night_has_written_a_newer_view(night, tmp_path):
    held = sv.view(BOX, MID_NOV)  # the app made it on an afternoon ask and holds it in memory
    assert sv.view(BOX, MID_NOV) is held
    _age(_kept_file(tmp_path), 10)
    sv._views[sv._key(BOX)] = (held, _kept_file(tmp_path).stat().st_mtime)
    # the night, in the pipelines' process, makes a newer one: here, the same call with the memory out of the way
    in_memory = dict(sv._views)
    sv._views.clear()
    night["newest"] = MID_NOV
    assert sv.refresh(BOX, MID_NOV + timedelta(days=1)) == sv.MADE
    sv._views.clear()
    sv._views.update(in_memory)
    newer = sv.view(BOX, MID_NOV + timedelta(days=1))
    assert newer is not held and newer.plants is not None and newer.plants.newest_pass == MID_NOV
    assert night["plants"] == 2  # read from the cache volume, not from the satellites


def test_the_view_remembers_the_newest_pass_the_search_held_even_an_all_cloud_one(tmp_path, in_process):
    asked = in_process([
        _pass(tmp_path, TODAY - timedelta(days=1), ndvi=0.5, cloud_east_of=0.0),
        _pass(tmp_path, TODAY - timedelta(days=6), ndvi=0.5),
        _pass(tmp_path, TODAY - timedelta(days=21), ndvi=0.3),
    ])
    plants = sv.plants_view(BOX, TODAY)
    assert plants.covered == 1.0 and plants.newest_pass == TODAY - timedelta(days=1)
    assert plants.main_day == TODAY - timedelta(days=6)
    # the night's question: anything since? One search over those days only.
    del asked[:]
    assert sv._newest_pass_since(BOX, plants.newest_pass, TODAY) == TODAY - timedelta(days=1)
    assert asked == [f"{(TODAY - timedelta(days=1)).isoformat()}/{TODAY.isoformat()}"]
    assert sv._newest_pass_since(BOX, TODAY - timedelta(days=30), TODAY) == TODAY - timedelta(days=1)
    in_process([], error="HTTPSConnectionPool: timed out")
    assert sv._newest_pass_since(BOX, plants.newest_pass, TODAY) is None


def test_a_pass_on_the_photos_other_grid_counts_as_a_new_pass(tmp_path, in_process):
    # a photo on the seam of two grids: the view is made from one grid, and the newest pass shows up on the other
    in_process([
        _pass(tmp_path, TODAY - timedelta(days=6), ndvi=0.5),
        _pass(tmp_path, TODAY - timedelta(days=11), ndvi=0.5),
        _pass(tmp_path, TODAY - timedelta(days=1), ndvi=0.5, tile="35MRU"),
    ])
    assert sv.plants_view(BOX, TODAY).newest_pass == TODAY - timedelta(days=1)
    assert sv._newest_pass_since(BOX, TODAY - timedelta(days=6), TODAY) == TODAY - timedelta(days=1)


# --- One field of the photo, and the view a card or a field panel may read without a satellite -------------------


def _field(left_m: float, top_m: float, wide_m: float, tall_m: float) -> dict[str, Any]:
    """A field's outline in latitude and longitude, placed from the top-left corner of the photo's grid of squares."""
    from pyproj import Transformer

    west, _, _, north = transform_bounds("EPSG:4326", "EPSG:32736", *BOX)
    to_lonlat = Transformer.from_crs("EPSG:32736", "EPSG:4326", always_xy=True).transform
    corners = [(west + left_m, north - top_m), (west + left_m + wide_m, north - top_m),
               (west + left_m + wide_m, north - top_m - tall_m), (west + left_m, north - top_m - tall_m)]
    ring = [list(to_lonlat(x, y)) for x, y in corners]
    return {"type": "Polygon", "coordinates": [ring + ring[:1]]}


def _two_looks() -> sv.Plants:
    """89 rows of 156 squares over the photo: the west 100 seen 4 days ago at 0.60, the east 56 seen 40 days ago at
    0.30, with one square of the west reading far off."""
    ndvi = np.full((89, 156), 0.60, dtype=np.float32)
    ndvi[:, 100:] = 0.30
    ndvi[2, 2] = 0.05
    days = np.full((89, 156), 4)
    days[:, 100:] = 40
    return _plants(ndvi, days)


def test_a_field_takes_the_middle_of_the_whole_squares_inside_it_and_says_their_day():
    seen = _view(_two_looks(), _soil_over_the_photo())
    field = sv.field_seen(seen, _field(3, 3, 54, 54))
    # the field's line runs 3 m inside the first squares and 7 m into the sixth: 6 x 6 squares touch it, and the
    # 4 x 4 between lie wholly inside it
    assert field is not None and (field.whole, field.squares) == (16, 16)
    assert field.ndvi == pytest.approx(0.60)  # the middle, so the one square reading 0.05 does not move it
    assert field.day == TODAY - timedelta(days=4) and field.older is False
    short, sentences = sv.field_words(field, TODAY)
    assert short == "satellite NDVI 0.60 on 19 Nov"
    assert sentences is not None and sentences.startswith(
        "From the satellite (Sentinel-2): NDVI 0.60 on 19 Nov, the middle of the 16 whole 10 m squares inside this field.")


def test_a_field_too_small_for_the_squares_gets_no_ndvi_and_is_told_why():
    seen = _view(_two_looks(), _soil_over_the_photo())
    field = sv.field_seen(seen, _field(103, 103, 25, 25))  # one square lies wholly inside
    assert field is not None and field.whole == 1 and field.ndvi is None
    short, sentences = sv.field_words(field, TODAY)
    assert short is None
    assert sentences is not None and "holds 1 of the satellite's 10 m squares whole, too few for an NDVI of its own (it takes 4)" in sentences
    nothing = sv.field_seen(seen, _field(303, 303, 12, 12))
    assert nothing is not None and nothing.whole == 0
    assert "holds none of the satellite's 10 m squares whole" in (sv.field_words(nothing, TODAY)[1] or "")


def test_a_field_under_an_older_look_says_so_and_one_across_two_looks_uses_one_day_only():
    seen = _view(_two_looks(), _soil_over_the_photo())
    old = sv.field_seen(seen, _field(1203, 103, 60, 60))
    assert old is not None and old.ndvi == pytest.approx(0.30) and old.older is True
    short, sentences = sv.field_words(old, TODAY)
    assert short == "satellite NDVI 0.30 on 14 Oct, an older look"
    assert "That is weeks older than the look most of the photo has, so do not read it against fields seen later." in (sentences or "")
    # 7 columns of fresh look and 4 of old inside it: the figure is the fresh squares alone, never a mix of two
    # crop moments
    across = sv.field_seen(seen, _field(923, 103, 120, 60))
    assert across is not None and across.ndvi == pytest.approx(0.60) and across.day == TODAY - timedelta(days=4)
    assert (across.squares, across.whole) == (35, 55)
    # and it says how much of the field it rests on
    assert "the middle of 35 of the 55 whole 10 m squares inside this field (the others had no clear look that day)" in (
        sv.field_words(across, TODAY)[1] or "")


def test_squares_seen_only_through_haze_are_left_out_of_a_fields_figure():
    plants = _two_looks()
    haze = np.zeros(plants.ndvi.shape, bool)
    haze[:10, :10] = True
    hazy = sv.Plants(plants.grid, plants.ndvi, plants.seen, haze, None, 3)
    field = sv.field_seen(_view(hazy, _soil_over_the_photo()), _field(3, 3, 64, 64))
    assert field is not None and field.whole == 25 and field.squares == 0 and field.ndvi is None
    assert "no NDVI for this field yet; too few of its 10 m squares had a clear look on one day" in (
        sv.field_words(field, TODAY)[1] or "")


def test_a_field_reads_the_soil_square_under_its_middle_as_the_seasons_level_not_its_own():
    seen = _view(_two_looks(), _soil_over_the_photo())
    west = sv.field_seen(seen, _field(0, 0, 50, 50))
    east = sv.field_seen(seen, _field(1400, 0, 50, 50))
    assert west is not None and east is not None
    assert west.soil is not None and east.soil is not None and west.soil < east.soil  # wetter to the east
    assert west.period == "2026-11-D1"
    sentences = sv.field_words(west, TODAY)[1] or ""
    assert f"Soil moisture {west.soil:.0f}% for 1-10 Nov in the 100 m square under its middle (FAO estimate): the "\
           "season's level, not this field's own reading." in sentences
    off_the_grid = {"type": "Polygon", "coordinates": [[[31.0, -1.0], [31.001, -1.0], [31.001, -1.001], [31.0, -1.001], [31.0, -1.0]]]}
    away = sv.field_seen(seen, off_the_grid)
    assert away is not None and away.soil is None and away.whole == 0


def test_a_broken_outline_has_no_satellite_figure_and_does_not_raise():
    seen = _view(_two_looks(), _soil_over_the_photo())
    assert sv.field_seen(seen, {"type": "Polygon", "coordinates": "not a ring"}) is None
    assert sv.field_seen(seen, {"type": "Polygon", "coordinates": []}) is None


def test_the_named_sentences_are_the_sentences_sage_says():
    plants = sv.plants_facts(_two_looks(), TODAY)
    assert plants["sentences"] == list(plants["parts"].values())
    assert set(plants["parts"]) == {"covered", "seen", "older", "greenness"}  # no haze, no change: neither is said
    soil = sv.soil_facts(_soil_over_the_photo(), TODAY)
    assert soil["sentences"] == list(soil["parts"].values()) and list(soil["parts"]) == ["reading", "year", "first", "limits"]


def test_ready_gives_the_kept_view_and_reads_no_satellite(night, tmp_path):
    assert sv.ready(BOX) is None and sv.of_fields(BOX, {"a": _field(0, 0, 50, 50)}) == {}
    assert night["plants"] == 0 and night["searched"] == []  # asking read nothing
    made = sv.view(BOX, MID_NOV)
    assert sv.ready(BOX) is made
    sv._views.clear()  # another process, or after a restart: read from the cache volume
    kept = sv.ready(BOX)
    assert kept is not None and kept.plants is not None and np.array_equal(kept.plants.ndvi, made.plants.ndvi)
    assert sv.ready(BOX) is kept  # and held from then on
    said = sv.of_fields(BOX, {"a": _field(-3, -3, 46, 46), "broken": {"type": "Polygon", "coordinates": []}})
    assert set(said) == {"a"} and said["a"][0] is not None and said["a"][0].startswith("satellite NDVI 0.50 on ")
    assert night["plants"] == 1
    sv._views.clear()
    _age(_kept_file(tmp_path), sv.FRESH_HOURS + 1)
    assert sv.ready(BOX) is None and night["plants"] == 1  # past its freshness it is not said, and nothing is read


def test_too_wide_says_why_without_reading_anything_and_never_raises():
    assert sv.too_wide(BOX) is None
    assert "too wide for a square-by-square satellite view" in (sv.too_wide([29.0, -3.0, 31.0, -1.0]) or "")
    # a photo's cards ask this while they are listed: a box that is not one is said, not raised
    for not_a_box in ([30.0, -2.0, 30.0, -2.0], [30.43, -1.70, 30.41, -1.69], [30.0, -95.0, 30.1, -94.0], [30.0, -2.0]):
        assert sv.too_wide(not_a_box) == sv.NOT_A_BOX


def test_a_newer_look_beside_an_older_main_one_is_said_since_both_are_drawn_in_colour():
    # the main look is the older one here: 100 columns seen 23 Oct, 56 seen 19 Nov
    ndvi = np.full((89, 156), 0.35, dtype=np.float32)
    ndvi[:, 100:] = 0.60
    days = np.full((89, 156), 31)
    days[:, 100:] = 4
    said = sv.plants_facts(_plants(ndvi, days), TODAY)
    assert "older" not in said["parts"]
    assert said["parts"]["newer"] == ("36% of the photo holds a newer look (up to 19 Nov) than the main one of 23 Oct: a "
                                      "difference between the two parts can be the weeks between them, not the crop.")
    assert list(said["parts"]) == ["covered", "seen", "newer", "greenness"]
    assert "newer" not in sv.plants_facts(_two_looks(), TODAY)["parts"]  # there the main look is the fresh one


def test_whole_squares_are_found_by_exact_geometry_also_along_a_straight_edge_and_in_a_field_that_bends():
    plants = _two_looks()
    west, _, _, north = transform_bounds("EPSG:4326", "EPSG:32736", *BOX)
    # a straight-edged field 3 m wider than 3 x 2 squares on each side: all 6 lie wholly inside, though the middle of
    # each is only 8 m from the line
    straight = box(west + 97, north - 123, west + 133, north - 97)
    inside = sv._whole_squares(straight, plants.grid)
    assert inside.sum() == 6 and inside[10:12, 10:13].all()
    # an L-shaped field: the squares in its notch are not inside it
    bent = Polygon([(west + 197, north - 197), (west + 253, north - 197), (west + 253, north - 223), (west + 223, north - 223),
                    (west + 223, north - 253), (west + 197, north - 253)])
    inside = sv._whole_squares(bent, plants.grid)
    # two rows of 5 across the top, three rows of 2 down the leg, and nothing in the notch
    assert inside.sum() == 10 + 6 and inside[20:22, 20:25].all() and inside[22:25, 20:22].all()
    assert not inside[22:25, 22:25].any()
    # a field off the grid, and one that is a sliver, hold none
    assert not sv._whole_squares(box(west - 500, north + 100, west - 400, north + 200), plants.grid).any()
    assert not sv._whole_squares(box(west + 300, north - 304, west + 400, north - 301), plants.grid).any()


def test_what_was_said_of_an_outline_is_remembered_while_the_same_view_is_held(night, tmp_path, monkeypatch):
    sv.view(BOX, MID_NOV)
    worked: list[int] = []
    real = sv.field_seen
    monkeypatch.setattr(sv, "field_seen", lambda *a, **k: worked.append(1) or real(*a, **k))
    first = sv.of_fields(BOX, {"a": _field(-3, -3, 46, 46), "broken": {"type": "Polygon", "coordinates": []}})
    assert set(first) == {"a"} and len(worked) == 2
    again = sv.of_fields(BOX, {"a": _field(-3, -3, 46, 46), "broken": {"type": "Polygon", "coordinates": []},
                               "b": _field(3, 3, 14, 14)})
    assert again["a"] == first["a"] and set(again) == {"a", "b"} and len(worked) == 3  # only the outline not yet asked about
    # the night writes a newer view: what was said of the old one is not said of the new
    night["newest"] = MID_NOV + timedelta(days=1)
    _age(_kept_file(tmp_path), 1)
    assert sv.refresh(BOX, MID_NOV + timedelta(days=2)) == sv.MADE
    sv.of_fields(BOX, {"a": _field(-3, -3, 46, 46)})
    assert len(worked) == 4


def test_one_outline_that_cannot_be_worked_out_loses_its_own_words_not_every_fields(night):
    sv.view(BOX, MID_NOV)
    off_the_globe = {"type": "Polygon", "coordinates": [[[30.42, float("nan")], [30.43, -1.7], [30.43, -1.69], [30.42, float("nan")]]]}
    far_off = {"type": "Polygon", "coordinates": [[[1e308, 1e308], [1e308, -1e308], [-1e308, 1e308], [1e308, 1e308]]]}
    said = sv.of_fields(BOX, {"good": _field(-3, -3, 46, 46), "nan": off_the_globe, "far": far_off})
    assert said["good"][0] is not None and said["good"][0].startswith("satellite NDVI 0.50")
    assert said.get("nan", (None, None))[0] is None and said.get("far", (None, None))[0] is None


def test_what_is_worked_out_is_filed_under_the_stamp_of_the_view_it_came_from(night, tmp_path):
    made = sv.view(BOX, MID_NOV)
    key = sv._key(BOX)
    seen, stamp = sv._ready(key)
    assert seen is made and stamp == sv._views[key][1]
    sv._views.clear()  # after a restart: read from the cache volume, held under the file's own time
    seen, stamp = sv._ready(key)
    assert seen is not None and stamp == _kept_file(tmp_path).stat().st_mtime == sv._views[key][1]
    assert sv._ready(sv._key([29.0, -2.0, 29.01, -1.99])) == (None, 0.0)


def test_the_one_line_forms_say_the_sentences_own_numbers_and_care_keeps_the_cautions():
    """A short answer (Sage's) says "short" and "care"; a full one (the card) says every sentence. The numbers are the
    same ones."""
    # a third of the photo last seen a month ago, a corner through haze, and greener than six weeks before
    ndvi = [[0.2, 0.5, 0.5], [0.65, 0.5, 0.5]]
    days = [[4, 4, 35], [4, 4, 35]]
    haze = [[True, False, False], [False, False, False]]
    plants = sv.plants_facts(_plants(ndvi, days, sv.Change(TODAY, date(2026, 10, 8), 0.2, 70), haze), TODAY)
    assert plants["short"] == ("NDVI 0.50 in the middle square; it rose by 0.20 since 8 Oct. Across the photo: 17% dense "
                               "vegetation, 67% cropland or moderate vegetation and 17% sparse vegetation.")
    # with no earlier look to compare, the figure and the shares alone
    assert sv.plants_facts(_plants(ndvi, days, None, haze), TODAY)["short"].startswith("NDVI 0.50 in the middle square. Across the photo: ")
    assert "The middle square reads 0.50." in plants["parts"]["greenness"]
    assert "NDVI rose by 0.20 since 8 Oct." in plants["parts"]["change"]
    assert plants["care"] == [plants["parts"]["older"], plants["parts"]["haze"]]
    # ground no pass saw is a caution too, said first
    patchy = sv.plants_facts(_plants(ndvi, [[4, 4, -1], [4, 4, -1]]), TODAY)
    assert patchy["care"] == [patchy["parts"]["covered"]] and patchy["care"][0].startswith("67% of this photo has a clear")
    assert sv.plants_facts(_plants(ndvi, [[4] * 3] * 2), TODAY)["care"] == []  # all seen, one day, no haze: nothing to add
    soil = sv.soil_facts(_soil(), TODAY)
    assert soil["short"] == "68% for 1-10 Nov, from 62% to 76% across the photo; near the top of its own year."
    assert "reads 68%, from 62% to 76% across the photo" in soil["parts"]["reading"]
    assert "near the top of its own year" in soil["parts"]["year"]
    assert sv.soil_facts(_soil(centre=None), TODAY)["short"] == "68% for 1-10 Nov, from 62% to 76% across the photo."
    assert sv.soil_facts(_soil(first=False), TODAY)["care"] == "Read soil moisture as the season's level, not a precise reading."
    assert "season's level, not a precise reading" in soil["care"] and "season's level, not a precise reading" in soil["parts"]["limits"]
