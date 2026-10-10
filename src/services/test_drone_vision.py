"""The vision model's look at each plot, with the model replaced by fixed answers (no paid calls)."""

from __future__ import annotations

import asyncio
import io
import time

import numpy as np
import pytest
import rasterio
from PIL import Image
from pyproj import Transformer
from rasterio.transform import from_origin
from shapely.geometry import box
from shapely.ops import transform as reproject

from src.services import drone_plots, drone_vision

X0, Y0, PIXEL_M = 830000, 9810000, 0.5


@pytest.fixture
def photo(tmp_path):
    path = tmp_path / "field.tif"
    with rasterio.open(path, "w", driver="GTiff", width=400, height=400, count=3, dtype="uint8",
                       crs="EPSG:32735", transform=from_origin(X0, Y0, PIXEL_M, PIXEL_M)) as ds:
        ds.write(np.stack([np.full((400, 400), v, dtype="uint8") for v in (90, 140, 60)]))
    return path


@pytest.fixture
def plots(photo):
    to_wgs84 = Transformer.from_crs("EPSG:32735", "EPSG:4326", always_xy=True).transform
    own = [drone_plots.MapPlot(reproject(to_wgs84, box(X0 + 10 + 50 * i, Y0 - 60, X0 + 50 + 50 * i, Y0 - 20)), f"P{i}")
           for i in range(3)]
    return drone_plots.measure_own_plots(str(photo), own, "Test plots")


def test_a_plot_picture_is_a_jpeg_of_the_plot_and_its_margin(photo):
    with rasterio.open(photo) as ds:
        picture, width_m, cm = drone_vision.plot_picture(ds, box(X0 + 10, Y0 - 60, X0 + 50, Y0 - 20))
    image = Image.open(io.BytesIO(picture))
    assert image.format == "JPEG"
    assert width_m == pytest.approx(40 + 2 * drone_vision.CONTEXT_M)
    assert cm == pytest.approx(PIXEL_M * 100, rel=0.05)  # small plot: shown at the photo's own detail


def test_answers_outside_the_lists_are_dropped():
    look = drone_vision._look(4, {"main_crop": "tomatoes", "other_crops": ["beans", "weeds", "beans"],
                                  "stage": "ripe", "crop_cover_percent": 140, "weeds": "lots",
                                  "problems": ["gaps", "aliens"], "note": "x" * 500, "confidence": "certain"})
    assert (look.main_crop, look.stage, look.weeds, look.confidence) == ("unsure", "unsure", "unsure", "low")
    assert look.other_crops == ["beans", "beans"] and look.problems == ["gaps"]
    assert look.crop_cover_percent == 100 and len(look.note) == 300


def _answer(crop):
    return {"evidence": "Stars of long blades in rows.", "main_crop": crop, "other_crops": [], "stage": "young",
            "crop_cover_percent": 40, "weeds": "few", "problems": [], "note": "Young plants in rows.",
            "confidence": "medium"}


def _fake_asks(monkeypatch, first, second):
    """The two looks answer `first` and `second` (crop names) for every plot; the calls are recorded."""
    calls = []

    async def fake_ask(client, model, system, content, schema, name):
        calls.append((name, sum(1 for part in content if part["type"] == "image_url")))
        return _answer(first if name == "plot_look" else second), 0.0002

    monkeypatch.setattr(drone_vision, "_ask", fake_ask)
    monkeypatch.setattr(drone_vision, "_client", lambda: (None, "openai/gpt-6-luna"))
    return calls


def test_the_crop_is_named_when_both_looks_agree(photo, plots, monkeypatch):
    calls = _fake_asks(monkeypatch, "maize", "maize")
    seen = []
    refs = [drone_vision.Reference("maize", b"jpeg", "checked by eye"), drone_vision.Reference("cassava", b"jpeg", "x")]
    result = asyncio.run(drone_vision.survey(str(photo), plots, "Test cell", lambda d, n: seen.append((d, n)), refs))
    assert sorted(result.looks) == [1, 2, 3] and result.plots == 3
    assert {look.main_crop for look in result.looks.values()} == {"maize"}
    assert result.cost_usd == pytest.approx(0.0012)  # two looks a plot
    # the first look sees the plot and 2 squares; the second also sees the 2 reference squares
    assert sorted(set(calls)) == [("plot_check", 5), ("plot_look", 3)] and seen[-1] == (3, 3)


def test_when_the_looks_disagree_the_plot_is_not_sure_with_both_crops_kept(photo, plots, monkeypatch):
    _fake_asks(monkeypatch, "cassava", "maize")
    result = asyncio.run(drone_vision.survey(str(photo), plots, None, lambda d, n: None))
    look = result.looks[1]
    assert (look.main_crop, look.candidates, look.confidence) == ("unsure", ("cassava", "maize"), "low")
    assert look.stage == "young" and look.evidence  # the rest of the first look is kept


def test_a_survey_with_too_many_failed_looks_fails(photo, plots, monkeypatch):
    async def failing_ask(*args):
        raise RuntimeError("provider down")

    monkeypatch.setattr(drone_vision, "_ask", failing_ask)
    monkeypatch.setattr(drone_vision, "_client", lambda: (None, "openai/gpt-6-luna"))
    with pytest.raises(RuntimeError, match="could not look at 3 of 3"):
        asyncio.run(drone_vision.survey(str(photo), plots, None, lambda d, n: None))


def test_closeups_are_squares_at_full_detail_with_a_metre_bar(photo):
    with rasterio.open(photo) as ds:
        big = drone_vision.closeups(ds, box(X0 + 5, Y0 - 195, X0 + 195, Y0 - 5), 1, 2)
        small = drone_vision.closeups(ds, box(X0 + 10, Y0 - 20, X0 + 20, Y0 - 10), 2, 2)
        again = drone_vision.closeups(ds, box(X0 + 5, Y0 - 195, X0 + 195, Y0 - 5), 1, 2)
    assert len(big) == 2 and big == again  # the same plot always gets the same squares
    assert len(small) == 1  # too small for two squares: shown once
    image = Image.open(io.BytesIO(big[0]))
    assert image.size == (drone_vision.CLOSE_PX, drone_vision.CLOSE_PX)
    assert image.getpixel((14, drone_vision.CLOSE_PX - 18))[0] > 240  # the white metre bar


def test_a_kept_survey_round_trips():
    look = drone_vision._look(1, _answer("beans"), _answer("maize"))
    survey = drone_vision.Survey(looks={1: look}, plots=1, model="openai/gpt-6-luna",
                                 done_at="2026-10-06T17:00:00+00:00", cost_usd=0.0002)
    assert drone_vision._from_json(drone_vision._to_json(survey)) == survey


def test_references_and_surveys_never_cross_partners():
    """A partner's checked squares steer only that partner's surveys (audit R1-8)."""
    import asyncio

    from src.services.test_farm_records import _FakeS3

    s3 = _FakeS3()
    a, b = drone_vision.reference_scope("org-a-id", "u1"), drone_vision.reference_scope("org-b-id", "u2")
    assert a != b and drone_vision.reference_scope(None, "u3") == "user-u3"
    asyncio.run(drone_vision.add_reference(s3, "bkt", a, "maize", b"\xff\xd8 a's square", "checked in the field"))
    assert [r.crop for r in asyncio.run(drone_vision.load_references(s3, "bkt", a))] == ["maize"]
    assert asyncio.run(drone_vision.load_references(s3, "bkt", b)) == []

    class Plots:
        source, found_at = "found", "t"

    assert drone_vision.survey_key("cog/x.tif", Plots(), a) != drone_vision.survey_key("cog/x.tif", Plots(), b)


async def test_vision_calls_are_spaced_under_the_free_tier_rate(monkeypatch):
    monkeypatch.setenv("DRONE_VISION_REQUESTS_PER_MINUTE", "600")  # one call every 0.1 s
    monkeypatch.setattr(drone_vision, "_next_call_at", {})
    starts: list[float] = []

    async def call() -> None:
        await drone_vision.pace("gemini-3.5-flash-lite")
        starts.append(time.monotonic())

    await asyncio.gather(*(call() for _ in range(4)))
    gaps = [b - a for a, b in zip(starts, starts[1:])]
    assert all(g >= 0.09 for g in gaps), gaps


async def test_vision_calls_are_not_paced_when_no_rate_is_set(monkeypatch):
    monkeypatch.delenv("DRONE_VISION_REQUESTS_PER_MINUTE", raising=False)
    t = time.monotonic()
    for _ in range(20):
        await drone_vision.pace("openai/gpt-6-luna")
    assert time.monotonic() - t < 0.05


def test_kept_surveys_from_before_still_match_and_anthropic_thinking_is_in_the_key(monkeypatch):
    """OpenAI-style providers keep the old survey key (no survey is redone); Claude's thinking budget is part of
    its key, so looks made without thinking are not taken for looks made with it."""
    class Plots:
        source, found_at = "found", "t"

    monkeypatch.setenv("DRONE_VISION_MODEL", "openai/gpt-6-luna")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://openrouter.ai/api/v1")
    assert drone_vision.survey_key("cog/x.tif", Plots(), "org-a") == \
        "drone_vision/v2|cog/x.tif|found|t|openai/gpt-6-luna|org-a"
    monkeypatch.setenv("DRONE_VISION_MODEL", "claude-haiku-5-5")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://api.anthropic.com/v1/")
    assert drone_vision.survey_key("cog/x.tif", Plots(), "org-a") == (
        'drone_vision/v2|cog/x.tif|found|t|claude-haiku-5-5|{"budget_tokens": 8000, "type": "enabled"}|org-a')


async def test_a_look_through_anthropic_asks_for_a_thinking_budget(monkeypatch):
    sent = {}

    class Completions:
        async def create(self, **kwargs):
            sent.update(kwargs)
            message = type("M", (), {"content": '{"main_crop": "maize"}'})
            return type("R", (), {"choices": [type("C", (), {"message": message})], "usage": None})

    client = type("Client", (), {"base_url": "https://api.anthropic.com/v1/",
                                 "chat": type("Chat", (), {"completions": Completions()})})

    async def no_cache(kind, key, compute):
        return await compute(), False

    monkeypatch.delenv("DRONE_VISION_REQUESTS_PER_MINUTE", raising=False)
    monkeypatch.setattr(drone_vision.llm_cache, "answer", no_cache)
    monkeypatch.setattr(drone_vision.llm_cache, "record", lambda kind, usage: 0.0)
    answer, _ = await drone_vision._ask(client, "claude-haiku-5-5", "system", [], {}, "plot_look")
    assert answer == {"main_crop": "maize"}
    assert sent["extra_body"]["thinking"] == {"type": "enabled", "budget_tokens": 8000}
    assert sent["max_tokens"] > 8000 and "reasoning_effort" not in sent
