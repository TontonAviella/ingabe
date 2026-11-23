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


def test_squares_sit_on_the_photo_and_say_their_reading_class_day_and_whether_they_are_faded():
    plants = _plants([[0.62, 0.05], [0.31, 0.2]], [[4, 4], [4, 31]], haze=[[False, False], [False, True]])
    drawn = sv.plants_squares(plants)
    features = drawn["geojson"]["features"]
    assert drawn["square_m"] == 10 and drawn["main_day"] == TODAY - timedelta(days=4) and len(features) == 4
    first = features[0]
    # the day carries its year and there is no "days old": the squares are saved on a map and read on later days
    assert first["properties"] == {"plants": "Dense vegetation", "ndvi": 0.62, "seen_on": "19 Nov 2026", "faded": False}
    ring = first["geometry"]["coordinates"][0]
    assert ring[0] == ring[-1] and len(ring) == 5
    # the box's own corner, within the 2 m by which a north-up box and the satellite's grid lines part at this longitude
    assert ring[0][0] == pytest.approx(BOX[0], abs=5e-5) and ring[0][1] == pytest.approx(BOX[3], abs=5e-5)
    assert abs(ring[1][0] - ring[0][0]) == pytest.approx(10 / 111_270, rel=0.02)  # 10 m of longitude at 1.7 degrees S
    assert features[1]["properties"]["plants"] == "Bare soil"  # cloud is already left out: not "Bare soil or cloud"
    last = features[3]["properties"]
    assert last["faded"] is True and last["seen_on"] == "23 Oct 2026"
    assert last["note"] == "An older look than most of the photo; Seen through haze: may read low"


def test_a_large_photo_gets_wider_squares_holding_their_mean_and_their_oldest_day(monkeypatch):
    monkeypatch.setattr(sv, "MAX_SQUARES", 4)
    ndvi = np.full((4, 4), 0.4)
    ndvi[:2, :2] = [[0.2, 0.4], [0.6, 0.8]]
    days = np.full((4, 4), 4)
    days[0, 0] = 31
    drawn = sv.plants_squares(_plants(ndvi, days))
    assert drawn["square_m"] == 20 and len(drawn["geojson"]["features"]) == 4
    first = drawn["geojson"]["features"][0]["properties"]
    assert first["ndvi"] == 0.5 and first["seen_on"] == "23 Oct 2026" and first["faded"] is True


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
        "30% of the photo is drawn faded: clouds hid it on 19 Nov, so its look is older and must not be read against "
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
    assert len(sv.plants_squares(plants)["geojson"]["features"]) == 80


# --- Soil moisture ---------------------------------------------------------------------------------------------


def _soil(percent: Any = ((62.0, 68.3), (76.0, np.nan)), *, first: bool = True, centre: Optional[float] = 69.0,
          low: Optional[float] = 40.0, high: Optional[float] = 75.0) -> sv.Soil:
    return sv.Soil(period="2026-11-D1", percent=np.asarray(percent, dtype=np.float32), west=30.4169922, north=-1.6923828,
                   step=0.0009765625, first_estimate=first, centre=centre, year_low=low, year_high=high)


def test_soil_squares_carry_their_reading_band_period_and_first_estimate_note():
    features = sv.soil_squares(_soil())["geojson"]["features"]
    assert len(features) == 3  # the square with no value is not drawn
    assert features[1]["properties"] == {"moisture": 68.3, "band": "65-75%", "period": "1-10 Nov 2026", "note": "FAO's first estimate"}
    ring = features[0]["geometry"]["coordinates"][0]
    assert ring[0] == [30.4169922, -1.6923828] and ring[2] == [30.4179688, -1.6933594]
    assert "note" not in sv.soil_squares(_soil(first=False))["geojson"]["features"][0]["properties"]


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
