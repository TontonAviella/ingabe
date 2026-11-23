"""The satellite views of the drone photos in farm projects, made ahead each night.

A first view of a photo waits 30 to 60 seconds on files held far away (measured 2026-11-23: the search for
passes 8 s, their cloud maps 5 to 10 s, their bands 17 s, soil moisture 22 s; our own maths under 0.3 s). So
nobody should be the first: each night the pipelines' nightly_satellite_views goes through the photos and has
satellite_view.refresh keep a fresh view of each. On most nights that is one search a photo (about 4 s), because
a new pass comes every five days and FAO's soil moisture every ten.

Nothing is drawn here and no map is touched: the layers go on a map when its reader asks
(src/tools/satellite_view.py). The night only spares them the wait.
"""

from __future__ import annotations

import json
import logging
import time
from collections import Counter
from datetime import date
from typing import Any, Callable, Iterable, Sequence

from src.services import industry, photo_context, satellite_view

logger = logging.getLogger(__name__)

# The night's run shares the pipelines' one run slot with the 02:00 UTC jobs. Photos it does not reach in this time
# keep the view they have, or get one on the first ask, and are first in line the next night only by being stale.
BUDGET_MINUTES = 90

# Drone photos that are on the newest map of a farm project, neither deleted (a project keeps its older map
# versions; a photo taken off the map since is not made ahead). Read with no user set, so every owner's photos are
# seen (the tables' row rules let a connection with no app.user_id read all rows).
PHOTOS_SQL = """
    SELECT DISTINCT l.layer_id, l.bounds, l.metadata
    FROM map_layers l
    JOIN user_mundiai_maps m ON l.layer_id = ANY(m.layers)
    JOIN user_mundiai_projects p ON p.id = m.project_id
    WHERE l.type = 'raster'
      AND l.bounds IS NOT NULL
      AND m.soft_deleted_at IS NULL
      AND p.soft_deleted_at IS NULL
      AND m.id = p.maps[array_upper(p.maps, 1)]
      AND p.industry = ANY(%s)
    ORDER BY l.layer_id
"""
FARM_INDUSTRIES = sorted(industry.AGRICULTURE)


def photo_boxes(rows: Iterable[Sequence[Any]]) -> list[tuple[str, list[float]]]:
    """(layer id, box) for each row of PHOTOS_SQL that is a finished photo with a place on the map; a box shared by
    several layers (the same photo on two maps, or uploaded twice) comes once: views are kept by box."""
    seen: set[tuple[float, ...]] = set()
    photos: list[tuple[str, list[float]]] = []
    for layer_id, bounds, metadata in rows:
        if isinstance(metadata, str):
            metadata = json.loads(metadata)
        metadata = metadata or {}
        if not bounds or not photo_context.is_photo_ready(metadata) or not photo_context.is_georeferenced(metadata, bounds):
            continue
        box = [float(v) for v in bounds]
        key = tuple(round(v, 5) for v in box)
        if key in seen:
            continue
        seen.add(key)
        photos.append((str(layer_id), box))
    return photos


def make_ahead(photos: Sequence[tuple[str, list[float]]], today: date, *,
               refresh: Callable[[Sequence[float], date], str] = satellite_view.refresh,
               clock: Callable[[], float] = time.monotonic, budget_s: float = BUDGET_MINUTES * 60) -> dict[str, Any]:
    """Have a fresh view kept for each photo, one after another, within the time budget.

    Returns what the night did: {"photos", "fresh", "done": {outcome: count}, "failed": [layer ids], "not_reached",
    "seconds"}; "fresh" counts the photos left with a fresh view on the cache volume. One photo's failure is logged
    and the night goes on to the next."""
    started = clock()
    done: Counter[str] = Counter()
    failed: list[str] = []
    reached = 0
    for layer_id, box in photos:
        if clock() - started > budget_s:
            break
        reached += 1
        try:
            done[refresh(box, today)] += 1
        except Exception:  # noqa: BLE001 - one photo must not end the night for the others
            logger.exception("satellite views ahead: photo %s could not be refreshed", layer_id)
            failed.append(layer_id)
    fresh = sum(count for outcome, count in done.items() if outcome in satellite_view.LEFT_FRESH)
    return {"photos": len(photos), "fresh": fresh, "done": dict(done), "failed": failed,
            "not_reached": len(photos) - reached, "seconds": round(clock() - started, 1)}
