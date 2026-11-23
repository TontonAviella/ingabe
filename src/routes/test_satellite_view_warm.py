"""Opening a photo's questions starts its satellite view, so it is ready when the reader asks about their crops."""

from __future__ import annotations

import asyncio
import time
import types
from datetime import date
from types import SimpleNamespace
from typing import Any

import pytest

from src.routes import drone_card_routes as routes
from src.services import background_jobs, photo_context, satellite_view

BOX = [30.41749, -1.7009, 30.43148, -1.69289]

BOUNDS = [30.41749, -1.7009, 30.43148, -1.69289]


def _layer(bounds: Any = BOUNDS) -> Any:
    return SimpleNamespace(layer_id="Lphoto", bounds=bounds)


def test_a_view_that_is_not_fresh_is_started_once_in_the_background(monkeypatch):
    started, asked = [], []
    monkeypatch.setattr(routes.satellite_view, "fresh", lambda bounds: False)
    whole = SimpleNamespace(plants=object(), soil=object(), plants_missing=None, soil_missing=None)
    monkeypatch.setattr(routes.satellite_view, "view", lambda bounds, today: asked.append((bounds, today)) or whole)
    monkeypatch.setattr(routes.background_jobs, "start", lambda key, work: started.append((key, work)))
    routes._warm_satellite_view(_layer())
    ((key, work),) = started
    assert key == "satellite-view:Lphoto"
    asyncio.run(work(lambda done, parts: None))
    assert asked == [(BOUNDS, date.today())]


def test_a_fresh_view_or_a_photo_with_no_place_starts_nothing(monkeypatch):
    started = []
    monkeypatch.setattr(routes.background_jobs, "start", lambda key, work: started.append(key))
    monkeypatch.setattr(routes.satellite_view, "fresh", lambda bounds: True)
    routes._warm_satellite_view(_layer())
    monkeypatch.setattr(routes.satellite_view, "fresh", lambda bounds: False)
    routes._warm_satellite_view(_layer(bounds=None))
    assert started == []


# --- With the real background jobs: once, never while fresh, and not over and over when it cannot be made -----------


@pytest.fixture
def warm(monkeypatch):
    """A photo layer, and a count of every making of its view; `state` says what the satellites give."""
    layer = types.SimpleNamespace(layer_id="Lwarm" + str(time.time_ns()), bounds=BOX)
    state: dict[str, Any] = {"made": 0, "fresh": False, "whole": True}

    def view(bounds, today):
        state["made"] += 1
        part = object() if state["whole"] else None
        return satellite_view.View(bbox=tuple(bounds), today=today, plants=part, plants_missing=None, soil=part,  # type: ignore[arg-type]
                                   soil_missing=None if state["whole"] else "FAO's soil moisture file could not be read just now.")

    monkeypatch.setattr(satellite_view, "view", view)
    monkeypatch.setattr(satellite_view, "fresh", lambda bounds: state["fresh"])
    yield layer, state
    background_jobs._jobs.pop(photo_context.satellite_job_key(layer.layer_id), None)
    routes._failure_told.discard(photo_context.satellite_job_key(layer.layer_id))


async def _settled(layer) -> None:
    for _ in range(200):
        job = background_jobs.status(photo_context.satellite_job_key(layer.layer_id))
        if job is None or job.state != "running":
            return
        await asyncio.sleep(0.01)


@pytest.mark.anyio
async def test_opening_the_cards_makes_the_view_once_and_nothing_while_a_fresh_one_is_kept(warm):
    layer, state = warm
    routes._warm_satellite_view(layer)
    routes._warm_satellite_view(layer)  # asked again while it is being made: the same making
    await _settled(layer)
    assert state["made"] == 1 and background_jobs.status(photo_context.satellite_job_key(layer.layer_id)) is None
    state["fresh"] = True
    routes._warm_satellite_view(layer)
    await _settled(layer)
    assert state["made"] == 1


@pytest.mark.anyio
async def test_a_view_that_is_not_whole_fails_the_job_and_is_not_made_again_by_every_refresh_of_the_deck(warm):
    layer, state = warm
    state["whole"] = False
    routes._warm_satellite_view(layer)
    await _settled(layer)
    job = background_jobs.status(photo_context.satellite_job_key(layer.layer_id))
    assert job is not None and job.state == "failed" and state["made"] == 1
    # the deck is asked again every 15 seconds while any card works: it leaves the failed making alone
    routes._warm_satellite_view(layer)
    await _settled(layer)
    assert state["made"] == 1
    # the open satellite card asks again by itself while it works: that ask is told of the failure and reads nothing,
    # or a view that keeps failing would be read again and again for as long as the card stays open
    state["whole"] = True
    routes._warm_satellite_view(layer, asked=True)
    await _settled(layer)
    still = background_jobs.status(photo_context.satellite_job_key(layer.layer_id))
    assert state["made"] == 1 and still is not None and still.state == "failed"
    # opening the card after that tries again
    routes._warm_satellite_view(layer, asked=True)
    await _settled(layer)
    assert state["made"] == 2 and background_jobs.status(photo_context.satellite_job_key(layer.layer_id)) is None


@pytest.mark.anyio
async def test_a_card_left_open_on_a_view_that_keeps_failing_reads_the_satellites_once_per_opening(warm):
    layer, state = warm
    state["whole"] = False
    for opening in (1, 2, 3):
        routes._warm_satellite_view(layer, asked=True)  # the opening: tries
        await _settled(layer)
        assert state["made"] == opening
        routes._warm_satellite_view(layer, asked=True)  # the card's own ask 15 seconds on: told of the failure
        await _settled(layer)
        assert state["made"] == opening


@pytest.mark.anyio
async def test_after_a_while_the_deck_tries_a_failed_making_again(warm, monkeypatch):
    layer, state = warm
    state["whole"] = False
    routes._warm_satellite_view(layer)
    await _settled(layer)
    key = photo_context.satellite_job_key(layer.layer_id)
    failed = background_jobs.status(key)
    assert failed is not None
    background_jobs._jobs[key] = background_jobs.Job(
        state="failed", parts_done=0, parts=1, started=failed.started - routes.SATELLITE_RETRY_MINUTES * 60 - 1)
    routes._warm_satellite_view(layer)
    await _settled(layer)
    assert state["made"] == 2


@pytest.mark.anyio
async def test_a_photo_with_no_place_or_wider_than_one_farm_starts_nothing(warm):
    layer, state = warm
    routes._warm_satellite_view(types.SimpleNamespace(layer_id=layer.layer_id, bounds=None))
    routes._warm_satellite_view(types.SimpleNamespace(layer_id=layer.layer_id, bounds=[29.0, -3.0, 31.0, -1.0]))
    await _settled(layer)
    assert state["made"] == 0 and date.today()  # nothing was read
