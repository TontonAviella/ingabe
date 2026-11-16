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
    assert field_flights.when({}, added) == (date(2026, 4, 3), False)  # 23:00 UTC is one in the morning in Kigali
    assert field_flights.when({"flown_on": "last week"}, added) == (date(2026, 4, 3), False)  # not a day: not used
    assert field_flights.line("***", {}, added, [])["photo"] == "a photo"  # never "a thermal photo"
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
                said="health index (NDVI) 0.45, 2nd lowest of 9 fields", ndvi=0.45, share=0.99,
                temperature_said="surface 31.0 °C, 2.0 °C warmer than the middle of 9 fields", temp_c=31.0)
                for f in features}, True
        return {}, photo_key != "cogs/may.tif"  # may: not measured yet; rgba: nothing to measure

    monkeypatch.setattr(field_health, "asked", health)
    photos = [_photo("may", 5, 20), _photo("rgba", 4, 12), _photo("colour", 3, 10), _photo("march", 5, 9, "2026-03-14"),
              _photo("elsewhere", 5, 8, bounds=(31, -2.1, 31.2, -1.9)),
              _photo("bands_NIR", 5, 7)]
    this = {"metadata": {}, "uploaded": datetime(2026, 4, 25, 9, tzinfo=timezone.utc)}
    apart, unsure = field_flights.other_flights(this, photos + [_photo("same day", 5, 25)])
    assert (len(apart), unsure) == (len(photos), 1)  # a file added with this photo is not known to be another flight
    photos = [p for p in apart if p["name"] != "bands_NIR"]
    flights = await field_flights.of_features(None, "b", [inside, outside], photos, _link)
    assert list(flights) == [field_health.key_of(inside["geometry"])]  # a field half off the pictures is not followed
    # a colour picture is never opened; a photo of another place and this flight's own band files are not asked
    assert asked == [("cogs/may.tif", 1), ("cogs/rgba.tif", 1), ("cogs/march.tif", 1)]
    lines = {entry["photo"]: entry for entry in flights[field_health.key_of(inside["geometry"])]}
    # the number alone: a place among that photo's fields is not set beside a place among this photo's
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
    apart = {"geometry": _square(40.0)}  # a field only one old photo holds
    photos = ([_photo("broken", 5, 20)] + [_photo(f"f{n}", 5, 19 - n) for n in range(field_flights.MAX_FLIGHTS + 2)]
              + [_photo("old", 5, 1, bounds=(39.9, -2.1, 40.1, -1.9))])
    flights = await field_flights.of_features(None, "b", [field, apart], photos, _link)
    names = [entry["photo"] for entry in flights[field_health.key_of(field["geometry"])]]
    assert names == [f"f{n}" for n in range(field_flights.MAX_FLIGHTS)]  # the broken one said nothing
    # the newest flights of one field do not use up the other field's
    assert [entry["photo"] for entry in flights[field_health.key_of(apart["geometry"])]] == ["old"]


@pytest.mark.anyio
async def test_a_flight_that_could_not_be_read_is_not_said_to_be_being_measured(monkeypatch):
    async def health(s3, bucket, photo_key, cog_url, features):
        return {}, False

    monkeypatch.setattr(field_health, "asked", health)
    monkeypatch.setattr(field_health, "failed_lately", lambda photo_key: photo_key == "cogs/broken.tif")
    field = {"geometry": _square(30.0)}
    flights = await field_flights.of_features(None, "b", [field], [_photo("broken", 5, 20), _photo("slow", 5, 19)], _link)
    said = {entry["photo"]: entry["said"] for entry in flights[field_health.key_of(field["geometry"])]}
    assert said == {"broken": field_flights.NOT_READ, "slow": field_flights.NOT_YET}


def test_flights_are_put_oldest_first_and_a_field_on_one_photo_says_nothing_about_flights():
    own = field_flights.line("today.tif", {"flown_on": "2026-04-02"}, None, ["health index (NDVI) 0.71"], this=True)
    earlier = field_flights.line("march.tif", {"flown_on": "2026-03-14"}, None, ["health index (NDVI) 0.45"])
    same_day = field_flights.line("other.tif", {}, datetime(2026, 4, 2, tzinfo=timezone.utc), [])
    assert field_flights.with_this(own, []) == () and field_flights.with_this(None, [earlier]) == ()
    flights = field_flights.with_this(own, [same_day, earlier])
    assert [entry["photo"] for entry in flights] == ["march.tif", "today.tif", "other.tif"]
    # a photo only known by its upload day is never slotted between flights whose day was given
    early_upload = field_flights.line("jan.tif", {}, datetime(2026, 1, 5, tzinfo=timezone.utc), [])
    assert [e["photo"] for e in field_flights.in_order([early_upload, own, earlier])] == ["march.tif", "today.tif", "jan.tif"]
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
    from_bands = {key: field_health.FieldHealth(said="health index (NDVI) 0.71", ndvi=0.71, index_from="result, added 9 Jul 2026")}
    borrowed = (await field_facts.of_kept(None, "b", "photo", "L1", None, from_bands, {key: [other]}, this_photo))["a"]
    assert borrowed.flights[-1]["said"].endswith("health index (NDVI) 0.71, measured on result, added 9 Jul 2026")
    alone = (await field_facts.of_kept(None, "b", "photo", "L1", None, health, {}, this_photo))["a"]
    assert alone.flights == () and alone.health_said == "health index (NDVI) 0.71"


def test_two_photos_are_two_flights_only_when_their_days_say_so_or_they_were_added_apart():
    def at(day, flown=None):
        return {"name": f"p{day}", "metadata": {"flown_on": flown} if flown else {},
                "uploaded": datetime(2026, 4, day, 9, tzinfo=timezone.utc)}

    this = at(10, "2026-04-02")
    apart, unsure = field_flights.other_flights(this, [at(10, "2026-03-14"), at(10, "2026-04-02"), at(10), at(20)])
    # another day given: a flight; the same day given: this flight's own file, said nowhere; no day, added with it:
    # not known; no day, added ten days later: a flight
    assert [p["name"] for p in apart] == ["p10", "p20"] and apart[0]["metadata"]["flown_on"] == "2026-03-14" and unsure == 1
    assert field_flights.other_flights(None, [at(20)]) == ([], 0)


def test_one_flight_says_its_number_alone_and_where_it_came_from():
    health = field_health.FieldHealth(said="health index (NDVI) 0.71, 2nd lowest of 9 fields", ndvi=0.71, share=1.0,
                                      temperature_said="surface 30.0 °C, level with the middle of 9 fields", temp_c=30.0)
    assert field_flights.facts_of(health) == ["health index (NDVI) 0.71", "surface 30.0 °C"]
    # measured on another file than the photo: said, so its day is not taken for the photo's day
    assert field_flights.facts_of(health, "result, added 14 Jul 2026", "thermal.tif") == [
        "health index (NDVI) 0.71, measured on result, added 14 Jul 2026", "surface 30.0 °C, measured on thermal.tif"]
    # values of no known scale: no number, and no place either
    raw = field_health.FieldHealth(said="health index (NDVI): lowest of 9 fields", raw_index=True, share=1.0)
    assert field_flights.facts_of(raw) == [field_flights.RAW, None]
    # a picture that holds only part of the field says nothing of it
    assert field_flights.facts_of(field_health.FieldHealth(said="x", ndvi=0.5, share=0.6)) == []
    assert field_flights.facts_of(None) == []
