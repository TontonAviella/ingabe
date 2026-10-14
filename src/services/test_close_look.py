"""The close look (close_look) with the vision model replaced by canned answers: close-ups asked in the sent
picture's pixels and kept in the photo's own, checks matched to the suggestions they are about, what is flagged for a
person, squares spread over the valid part of an orthomosaic, and the day's spending cap."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import numpy as np
import pytest
from PIL import Image

from src.services import close_look


class _Redis:
    def __init__(self, spent=0.0):
        self.values = {"spent": spent}

    async def get(self, key):
        return self.values["spent"]

    async def incrbyfloat(self, key, amount):
        self.values["spent"] += amount

    async def expire(self, key, seconds):
        return True


def test_close_ups_are_kept_in_the_photos_pixels_and_checks_match_their_suggestions(monkeypatch):
    asked = []

    async def ask(redis, name, prompt, pictures, schema):
        asked.append((name, len(pictures)))
        if name == "tower_whole":
            return {"worries": [{"id": "structure", "can_see": "partly", "answer": "steel in view", "confidence": "medium",
                                 "needs": "close-ups of the joints"}],
                    "regions": [{"name": "a joint", "box": [100, 100, 300, 200], "why": "bolts"},
                                {"name": "a sliver", "box": [5, 5, 6, 6], "why": "too small"}]}
        if name == "tower_region":
            return {"what_is_shown": "a joint", "findings": [{"kind": "loose cable", "where": "left", "can_see": "yes",
                                                              "confidence": "medium", "flag": True}], "cannot_judge": []}
        return {"items": [{"n": 1, "really": "panel antenna", "agrees": "yes", "damage": "none seen", "confidence": "high"},
                          {"n": 2, "really": "fibre splice closure", "agrees": "no", "damage": "none seen", "confidence": "medium"}]}

    monkeypatch.setattr(close_look, "_ask", ask)
    photo = Image.new("RGB", (3136, 2090))  # sent at 1568 px wide: every box doubles
    shown = [close_look.Shown(id="j-1", part="tower_antenna", box=(10, 10, 60, 90)),
             close_look.Shown(id="j-2", part="tower_radio_unit", box=(200, 200, 260, 260))]
    result = asyncio.run(close_look.look_at_photo(_Redis(), photo, "taken 08:16", shown, {}))
    assert [n for n, _ in asked] == ["tower_whole", "tower_region", "tower_check"]
    (region,) = result["regions"]  # the 1 px region is too small to look at
    assert region["box"] == [160, 160, 640, 440]  # [200, 200, 600, 400] in the photo's pixels, plus a tenth around
    assert [(c["suggestion_id"], c["agrees"]) for c in result["checks"]] == [("j-1", "yes"), ("j-2", "no")]
    assert close_look.flags(result) == ["loose cable", "#2: fibre splice closure"]


def test_squares_are_spread_over_the_valid_part_and_the_same_for_the_same_photo():
    valid = np.zeros((100, 100), bool)
    valid[:, :60] = True  # the right part of the photo is empty (no data)
    squares = close_look.squares_in(10_000, 10_000, 1_000, valid, seed="photo-a")
    assert 3 <= len(squares) <= close_look.SQUARES
    assert all(x + 1_000 <= 6_000 for x, _ in squares)
    assert all(abs(a[0] - b[0]) >= 1_500 or abs(a[1] - b[1]) >= 1_500 for i, a in enumerate(squares) for b in squares[i + 1:])
    assert squares == close_look.squares_in(10_000, 10_000, 1_000, valid, seed="photo-a")
    assert close_look.squares_in(500, 500, 1_000, valid, seed="photo-a") == []


def test_a_days_spending_stops_at_the_cap(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    monkeypatch.setenv("CLOSE_LOOK_DAILY_USD", "1.5")

    async def answer(kind, key, compute):
        return await compute(), False

    monkeypatch.setattr(close_look.llm_cache, "answer", answer)
    monkeypatch.setattr(close_look, "_client", lambda: (SimpleNamespace(base_url="https://api.anthropic.com/v1/"), "m"))
    with pytest.raises(close_look.OverBudget, match=r"\$1\.50"):
        asyncio.run(close_look._ask(_Redis(spent=1.6), "tower_whole", "q", [b"x"], close_look.WHOLE_SCHEMA))
    monkeypatch.setenv("CLOSE_LOOK", "0")
    with pytest.raises(close_look.Unavailable):
        asyncio.run(close_look._ask(_Redis(), "tower_whole", "q", [b"x"], close_look.WHOLE_SCHEMA))


def test_a_photos_facts_in_words():
    photo = SimpleNamespace(taken_at="2020-10-06T08:16:38", gimbal_yaw=-2.6, gimbal_pitch=-29.8, relative_altitude_m=64.2)
    assert close_look.facts_of(photo, lambda yaw: "north") == (
        "taken 2020-10-06 08:16 local time, camera heading 357° (north), tilted 30° down, 64 m above take-off")
    none = SimpleNamespace(taken_at=None, gimbal_yaw=None, gimbal_pitch=None, relative_altitude_m=None)
    assert close_look.facts_of(none, str) == "no camera facts recorded"
