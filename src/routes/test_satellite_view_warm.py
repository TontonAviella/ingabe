"""Opening a photo's questions starts its satellite view, so it is ready when the reader asks about their crops."""

from __future__ import annotations

import asyncio
from datetime import date
from types import SimpleNamespace
from typing import Any

from src.routes import drone_card_routes as routes

BOUNDS = [30.41749, -1.7009, 30.43148, -1.69289]


def _layer(bounds: Any = BOUNDS) -> Any:
    return SimpleNamespace(layer_id="Lphoto", bounds=bounds)


def test_a_view_that_is_not_fresh_is_started_once_in_the_background(monkeypatch):
    started, asked = [], []
    monkeypatch.setattr(routes.satellite_view, "fresh", lambda bounds: False)
    monkeypatch.setattr(routes.satellite_view, "view", lambda bounds, today: asked.append((bounds, today)))
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
