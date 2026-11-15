"""One field, one set of facts (src/services/field_facts.py): the same name, size and count on every surface, and a
kept field's cover and checks carried by the plot that is that field."""

from __future__ import annotations

import types

import pytest

from src.services import count_check, drone_plots, field_facts, field_taps, plant_finder


def _square(west, size=0.001):
    return {"type": "Polygon", "coordinates": [[[west, -2], [west + size, -2], [west + size, -2 + size],
                                                [west, -2 + size], [west, -2]]]}


def _plots(source, *features):
    return types.SimpleNamespace(source=source, geojson={"type": "FeatureCollection", "features": [
        {"type": "Feature", "geometry": geometry, "properties": {"number": number, "area_ha": area, **extra}}
        for number, geometry, area, extra in features]})


def test_a_field_is_named_and_sized_one_way():
    assert field_facts.name(7, True) == "Field 7" and field_facts.name(7, False) == "Plot 7"
    assert field_facts.name(3, False, "Maize A") == "Maize A · plot 3"  # the reader's own name, with the number to ask by
    said = {area: field_facts.area_said(area) for area in (132.4, 2.34, 0.184, 0.05, 0.039, 0.0052, 0.0004)}
    assert said == {132.4: "132 ha", 2.34: "2.3 ha", 0.184: "0.18 ha", 0.05: "0.05 ha", 0.039: "390 m²", 0.0052: "52 m²",
                    0.0004: "under 10 m²"}
    assert [field_facts.about(n) for n in (0, 95, 163, 702, 4987)] == [0, 95, 160, 700, 5000]
    assert field_facts.plants_said(702) == "about 700 plants" and field_facts.plants_said(1) == "about 1 plant"
    # a count of part of a field never goes without that part, on any surface
    assert field_facts.plants_said(280, 0.4) == "about 280 plants in the 40% of it that can be counted"
    assert field_facts.plants_said(280, 0.999) == "about 280 plants"


def test_two_outlines_are_one_field_when_they_differ_only_in_the_last_decimals():
    a = _square(30.0)
    nudged = {"type": "Polygon", "coordinates": [[[x + 1e-10, y - 1e-10] for x, y in a["coordinates"][0]]]}
    assert field_facts.same_field(a, nudged)  # a trip to the photo's coordinates and back
    assert not field_facts.same_field(a, _square(30.0005))  # half of it elsewhere
    assert not field_facts.same_field(a, _square(31.0))
    bowtie = {"type": "Polygon", "coordinates": [[[30, -2], [30.001, -1.999], [30.001, -2], [30, -1.999], [30, -2]]]}
    assert not field_facts.same_field(a, bowtie)  # a line that crosses itself is compared, not a crash
    assert not field_facts.same_field(a, {"type": "Polygon"})  # nor is a broken record


@pytest.mark.anyio
async def test_a_plot_carries_the_kept_fields_cover_count_and_check(monkeypatch):
    rows = [{"id": "a", "number": 3, "geometry": _square(30.0), "green_cover": {"share": 0.624, "threshold": field_taps.GREEN_THRESHOLD}},
            {"id": "b", "number": 4, "geometry": _square(30.01), "green_cover": {"share": 0.5, "threshold": 0.05}}]
    counts = {"k3": {"number": 3, "plants": 702, "squares_counted": 5, "counted_share": 0.86, "geometry": _square(30.0)},
              "k5": {"number": 5, "plants": 0, "squares_counted": 0, "counted_share": 0.0, "geometry": _square(30.02)}}
    reads: list[str] = []

    async def kept(s3, bucket, key, layer_id):
        return rows

    async def cover_checked(s3, bucket, key):
        return checked["on"]

    async def counted(s3, bucket, key):
        reads.append("counts")
        return counts

    async def summary(s3, bucket, key, layer_id, row):
        return {"said": "In the 1 part of this field checked by hand, the app counted 38 plants."} if row["id"] == "a" else None

    checked = {"on": True}
    monkeypatch.setattr(field_taps, "enabled", lambda: True)
    monkeypatch.setattr(field_taps, "kept", kept)
    monkeypatch.setattr(field_taps, "cover_checked", cover_checked)
    monkeypatch.setattr(plant_finder, "counted", counted)
    monkeypatch.setattr(plant_finder, "answer_key", lambda key, feature: f"k{feature['properties']['number']}")
    monkeypatch.setattr(count_check, "summary", summary)
    plots = _plots(drone_plots.OUTLINED_FIELDS_NAME, (3, _square(30.0), 0.184, {}), (4, _square(30.01), 0.02, {}),
                   (5, _square(30.02), 0.3, {}))
    three, four, five = await field_facts.of_plots(None, "b", "photo", "L1", plots)
    assert reads == ["counts"]  # one read for all the counts, whatever the number of plots
    assert field_facts.said(three) == "Field 3 · 0.18 ha · green cover 62% · about 700 plants in the 86% of it that can be counted"
    assert three.kept_id == "a" and three.counted_share == 0.86 and "checked by hand" in three.checked_said
    # a figure from another rule than today's is not a figure; a field nobody counted has no count
    assert field_facts.said(four) == "Field 4 · 200 m²" and four.kept_id == "b" and four.checked_said is None
    # a plot where nothing could be counted has no count, and a plot that is no kept field has no cover
    assert field_facts.said(five) == "Field 5 · 0.30 ha" and five.kept_id is None
    # on a photo the green rule was not checked on, no cover is said anywhere
    checked["on"] = False
    assert (await field_facts.of_plots(None, "b", "photo", "L1", plots))[0].cover_said is None
    # Ingabe's own plots with the same numbers are not the reader's fields: no cover, no check, and "Plot"
    own = _plots("found", (3, _square(31.0), 0.184, {}), (9, _square(32.0), 1.2, {"name": "Block A"}))
    checked["on"] = True
    plot, named = await field_facts.of_plots(None, "b", "photo", "L1", own)
    assert plot.name == "Plot 3" and plot.kept_id is None and plot.cover_said is None and plot.checked_said is None
    assert plot.plants_said.startswith("about 700 plants")  # the count belongs to the plot's number in its own set
    assert named.name == "Block A · plot 9" and named.area_said == "1.2 ha"
    # the field panel reads the same facts from the kept fields themselves: no plot set is built for it
    kept_facts = await field_facts.of_kept(None, "b", "photo", "L1")
    assert field_facts.said(kept_facts["a"]) == ("Field 3 · under 10 m² · green cover 62% · about 700 plants in the 86% of it that can be counted")
    assert kept_facts["a"].checked_said and kept_facts["b"].plants_said is None

    # one check that cannot be read loses that check, not the field; a counts record that cannot be read, the counts
    async def broken(*_):
        raise RuntimeError("storage is down")

    monkeypatch.setattr(count_check, "summary", broken)
    assert (await field_facts.of_plots(None, "b", "photo", "L1", plots))[0].plants_said.startswith("about 700 plants")
    monkeypatch.setattr(plant_finder, "counted", broken)
    assert field_facts.said((await field_facts.of_plots(None, "b", "photo", "L1", plots))[0]) == "Field 3 · 0.18 ha · green cover 62%"
