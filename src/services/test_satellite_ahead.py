"""The night's run over the drone photos of farm projects: which photos, and what one night does."""

from __future__ import annotations

import json
from datetime import date
from typing import Any

from src.services import satellite_ahead as ahead
from src.services import satellite_view as sv

TODAY = date(2026, 11, 23)
BOX = [30.41749, -1.7009, 30.43148, -1.69289]
READY = {"cog_status": "ready", "cog_key": "cog/x.tif"}


def test_only_finished_photos_with_a_place_on_the_map_are_taken_and_a_shared_box_once():
    rows: list[tuple[Any, ...]] = [
        ("Lphoto000001", BOX, READY),
        ("Lphoto000002", BOX, json.dumps(READY)),  # the same photo on another map; psycopg2 may hand metadata as text
        ("Lphoto000003", [30.5, -1.8, 30.51, -1.79], READY),
        ("Lmaking00001", [30.6, -1.8, 30.61, -1.79], {"cog_status": "processing"}),
        ("Lmast0000001", [0, 0, 4000, 3000], {**READY, "crs_missing": True}),  # a phone picture: no place on the map
        ("Lnobounds001", None, READY),
        ("Lnometa00001", [30.7, -1.8, 30.71, -1.79], None),
    ]
    assert ahead.photo_boxes(rows) == [("Lphoto000001", BOX), ("Lphoto000003", [30.5, -1.8, 30.51, -1.79])]


def test_the_photos_come_from_maps_of_farm_projects_that_are_not_deleted():
    sql = " ".join(ahead.PHOTOS_SQL.split())
    assert "l.type = 'raster'" in sql and "l.layer_id = ANY(m.layers)" in sql
    assert "m.soft_deleted_at IS NULL" in sql and "p.soft_deleted_at IS NULL" in sql
    assert "p.industry = ANY(%s)" in sql and ahead.FARM_INDUSTRIES == ["agriculture"]
    assert "m.id = p.maps[array_upper(p.maps, 1)]" in sql  # the project's newest map, not its older versions


def test_a_night_refreshes_each_photo_counts_what_happened_and_goes_on_past_a_failure():
    photos = [("L1", [30.0, -2.0, 30.01, -1.99]), ("L2", [30.1, -2.0, 30.11, -1.99]),
              ("L3", [30.2, -2.0, 30.21, -1.99]), ("L4", [30.3, -2.0, 30.31, -1.99])]
    asked = []

    def refresh(box, today):
        asked.append((box, today))
        if box[0] == 30.1:
            raise OSError("cache volume full")
        return sv.MADE if box[0] == 30.0 else sv.NOTHING_NEW

    night = ahead.make_ahead(photos, TODAY, refresh=refresh, clock=lambda: 0.0)
    assert [box for box, _ in asked] == [box for _, box in photos] and {day for _, day in asked} == {TODAY}
    assert night == {"photos": 4, "fresh": 3, "done": {"made": 1, "nothing new": 2}, "failed": ["L2"], "not_reached": 0,
                     "seconds": 0.0}


def test_a_night_stops_at_its_time_budget_and_says_how_many_photos_it_did_not_reach():
    photos = [(f"L{i}", [30.0 + i / 10, -2.0, 30.01 + i / 10, -1.99]) for i in range(5)]
    ticks = iter(range(0, 10_000, 40))  # each look at the clock is 40 seconds on

    night = ahead.make_ahead(photos, TODAY, refresh=lambda box, today: sv.MADE, clock=lambda: float(next(ticks)), budget_s=100)
    # the clock reads 0 at the start, then 40, 80, 120 before each photo: two photos are made, three are left
    assert night["done"] == {"made": 2} and night["not_reached"] == 3 and night["photos"] == 5 and night["fresh"] == 2


def test_no_photos_is_a_quiet_night():
    assert ahead.make_ahead([], TODAY, refresh=lambda box, today: sv.MADE, clock=lambda: 0.0) == {
        "photos": 0, "fresh": 0, "done": {}, "failed": [], "not_reached": 0, "seconds": 0.0}


def test_a_night_that_left_no_photo_fresh_says_so_even_when_nothing_raised():
    photos = [("L1", [30.0, -2.0, 30.01, -1.99]), ("L2", [30.1, -2.0, 30.11, -1.99])]
    # the search is down, or the cache volume cannot be written: refresh raises nothing, and nothing is fresh
    for outcome in (sv.NOT_CHECKED, sv.NOT_KEPT, sv.NOT_WHOLE, sv.TOO_WIDE):
        night = ahead.make_ahead(photos, TODAY, refresh=lambda box, today, o=outcome: o, clock=lambda: 0.0)
        assert night["fresh"] == 0 and night["failed"] == [] and night["done"] == {outcome: 2}
    # a new pass that could not be read leaves the older view fresh: that photo is served
    kept = ahead.make_ahead(photos, TODAY, refresh=lambda box, today: sv.KEPT_OLDER, clock=lambda: 0.0)
    assert kept["fresh"] == 2
