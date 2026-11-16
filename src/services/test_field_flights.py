"""One field across the flights of its place (src/services/field_flights.py): the same outline on every other
picture of the ground, each flight said by its own day and by what its own picture can bear."""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from src.services import field_facts, field_flights, field_health, field_taps, plant_finder


def _square(west, size=0.001):
    return {"type": "Polygon", "coordinates": [[[west, -2], [west + size, -2], [west + size, -2 + size],
                                                [west, -2 + size], [west, -2]]]}


def _photo(name, bands, uploaded, flown=None, bounds=(29.9, -2.1, 30.1, -1.9)):
    metadata = {"cog_key": f"cogs/{name}.tif", "band_count": bands}
    if flown:
        metadata["flown_on"] = flown
    return {"name": name, "metadata": metadata, "bounds": list(bounds), "bands": bands,
            "uploaded": datetime(2026, 4, uploaded, 9, tzinfo=timezone.utc)}


async def _link(metadata):
    return f"https://store/{metadata['cog_key']}"


def test_a_flight_is_said_by_the_day_given_and_an_upload_day_is_never_passed_off_as_one():
    assert field_flights.day_said(date(2026, 3, 4), True) == "4 March 2026"
    assert field_flights.day_said(date(2026, 3, 4), False) == "added 4 March 2026"
    added = datetime(2026, 4, 2, 23, tzinfo=timezone.utc)
    assert field_flights.when({"flown_on": "2026-03-14"}, added) == (date(2026, 3, 14), True)
    assert field_flights.when({}, added) == (date(2026, 4, 2), False)
    assert field_flights.when({"flown_on": "last week"}, added) == (date(2026, 4, 2), False)  # not a day: not used
    assert field_flights.when({}, None) is None and field_flights.line("a.tif", {}, None, ["x"]) is None
    with pytest.raises(ValueError, match="days to come"):
        field_flights.check_day(date(2026, 5, 2), date(2026, 5, 1))
    with pytest.raises(ValueError, match="too long ago"):
        field_flights.check_day(date(1999, 12, 31), date(2026, 5, 1))
    field_flights.check_day(date(2026, 5, 1), date(2026, 5, 1))


@pytest.mark.anyio
async def test_the_same_outline_is_measured_on_each_other_flight_that_holds_all_of_it(monkeypatch):
    inside, outside = {"geometry": _square(30.0)}, {"geometry": _square(30.0995)}  # the second crosses the edge
    asked: list[tuple[str, int]] = []

    async def health(s3, bucket, photo_key, cog_url, features):
        asked.append((photo_key, len(features)))
        assert cog_url == f"https://store/{photo_key}"
        if photo_key == "cogs/march.tif":
            return {field_health.key_of(f["geometry"]): field_health.FieldHealth(
                said="health index (NDVI) 0.45", ndvi=0.45, temperature_said="surface 31.0 °C") for f in features}, True
        return {}, photo_key != "cogs/may.tif"  # may: not measured yet; rgba: nothing to measure

    monkeypatch.setattr(field_health, "asked", health)
    photos = [_photo("may", 5, 20), _photo("rgba", 4, 12), _photo("colour", 3, 10), _photo("march", 5, 9, "2026-03-14"),
              _photo("elsewhere", 5, 8, bounds=(31, -2.1, 31.2, -1.9)),
              _photo("bands_NIR", 5, 7)]
    flights = await field_flights.of_features(None, "b", [inside, outside], photos, _link, leave_out="bands_NIR")
    assert list(flights) == [field_health.key_of(inside["geometry"])]  # a field half off the pictures is not followed
    # a colour picture is never opened; a photo of another place and this flight's own band files are not asked
    assert asked == [("cogs/may.tif", 1), ("cogs/rgba.tif", 1), ("cogs/march.tif", 1)]
    lines = {entry["photo"]: entry for entry in flights[field_health.key_of(inside["geometry"])]}
    assert lines["march"]["said"] == "health index (NDVI) 0.45 · surface 31.0 °C"
    assert (lines["march"]["day_said"], lines["march"]["flown"], lines["march"]["measured"]) == ("14 March 2026", True, True)
    assert lines["may"]["said"] == "being measured" and lines["may"]["day_said"] == "added 20 April 2026"
    assert lines["colour"]["said"] == lines["rgba"]["said"] == "no health index from this picture"
    assert not lines["colour"]["measured"]


@pytest.mark.anyio
async def test_one_flight_that_cannot_be_read_does_not_hide_the_others_and_only_the_newest_few_are_looked_at(monkeypatch):
    async def health(s3, bucket, photo_key, cog_url, features):
        if photo_key == "cogs/broken.tif":
            raise OSError("the store did not answer")
        return {}, True

    monkeypatch.setattr(field_health, "asked", health)
    field = {"geometry": _square(30.0)}
    photos = [_photo("broken", 5, 20)] + [_photo(f"f{n}", 5, 19 - n) for n in range(field_flights.MAX_FLIGHTS + 2)]
    flights = await field_flights.of_features(None, "b", [field], photos, _link)
    names = [entry["photo"] for entry in flights[field_health.key_of(field["geometry"])]]
    assert names == [f"f{n}" for n in range(field_flights.MAX_FLIGHTS - 1)]  # the broken one took a place, and said nothing


def test_flights_are_put_oldest_first_and_a_field_on_one_photo_says_nothing_about_flights():
    own = field_flights.line("today.tif", {"flown_on": "2026-04-02"}, None, ["health index (NDVI) 0.71"], this=True)
    earlier = field_flights.line("march.tif", {"flown_on": "2026-03-14"}, None, ["health index (NDVI) 0.45"])
    same_day = field_flights.line("other.tif", {}, datetime(2026, 4, 2, tzinfo=timezone.utc), [])
    assert field_flights.with_this(own, []) == () and field_flights.with_this(None, [earlier]) == ()
    flights = field_flights.with_this(own, [same_day, earlier])
    assert [entry["photo"] for entry in flights] == ["march.tif", "today.tif", "other.tif"]
    assert field_flights.told(flights) == ["14 March 2026: health index (NDVI) 0.45",
                                           "2 April 2026 (this photo): health index (NDVI) 0.71",
                                           "added 2 April 2026: no health index from this picture"]
    assert field_flights.told(()) is None


@pytest.mark.anyio
async def test_a_kept_field_carries_its_flights_with_this_photos_own_facts_among_them(monkeypatch):
    geometry = _square(30.0)
    rows = [{"id": "a", "number": 3, "geometry": geometry, "area_ha": 0.18,
             "green_cover": {"share": 0.62, "threshold": field_taps.GREEN_THRESHOLD}}]

    async def kept(s3, bucket, key, layer_id):
        return rows

    async def yes(s3, bucket, key):
        return True

    async def none(s3, bucket, key):
        return {}

    monkeypatch.setattr(field_taps, "enabled", lambda: True)
    monkeypatch.setattr(field_taps, "kept", kept)
    monkeypatch.setattr(field_taps, "cover_checked", yes)
    monkeypatch.setattr(plant_finder, "counted", none)
    key = field_health.key_of(geometry)
    health = {key: field_health.FieldHealth(said="health index (NDVI) 0.71", ndvi=0.71)}
    other = field_flights.line("march.tif", {"flown_on": "2026-03-14"}, None, ["health index (NDVI) 0.45"])
    this_photo = {"name": "april.tif", "metadata": {}, "uploaded": datetime(2026, 4, 2, tzinfo=timezone.utc)}
    facts = (await field_facts.of_kept(None, "b", "photo", "L1", None, health, {key: [other]}, this_photo))["a"]
    assert [(entry["day_said"], entry["this"], entry["said"]) for entry in facts.flights] == [
        ("14 March 2026", False, "health index (NDVI) 0.45"),
        ("added 2 April 2026", True, "green cover 62% · health index (NDVI) 0.71")]
    alone = (await field_facts.of_kept(None, "b", "photo", "L1", None, health, {}, this_photo))["a"]
    assert alone.flights == () and alone.health_said == "health index (NDVI) 0.71"
