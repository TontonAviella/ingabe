"""How a plant count is said and drawn (src/tools/drone_photo_tools.py): to two figures, and plants alone unless
the person asked for the other green."""

from __future__ import annotations

import pytest

from src.services import count_check, field_taps
from src.tools import drone_photo_tools as tools


def test_a_count_is_said_to_two_figures_because_the_test_put_it_within_18_percent():
    said = {count: tools.plants_about(count) for count in (0, 7, 95, 99, 100, 163, 702, 745, 4987, 12345)}
    assert said == {0: 0, 7: 7, 95: 95, 99: 99, 100: 100, 163: 160, 702: 700, 745: 740, 4987: 5000, 12345: 12000}


def test_the_map_gets_the_plants_alone_unless_the_other_green_was_asked_for():
    def point(code):
        return {"type": "Feature", "geometry": {"type": "Point", "coordinates": [30.0, -2.0]},
                "properties": {"kind": "plant" if code == 0 else "other_green", "kind_code": code}}

    found = {"type": "FeatureCollection", "features": [point(0), point(1), point(0), point(1), point(1)]}
    alone = tools.points_drawn(found, show_other_green=False)
    assert [f["properties"]["kind_code"] for f in alone["features"]] == [0, 0]
    assert tools.points_drawn(found, show_other_green=True) is found
    assert len(found["features"]) == 5  # the kept answer itself is not changed
    # the choice is always stated (every tool argument is required), and its description says when to say yes
    assert "ONLY when the person asks" in tools.CountPlantsArgs.model_fields["show_other_green"].description


@pytest.mark.anyio
async def test_a_count_carries_the_persons_own_check_only_for_the_field_they_checked(monkeypatch):
    square = {"type": "Polygon", "coordinates": [[[30, -2], [30.001, -2], [30.001, -1.999], [30, -1.999], [30, -2]]]}
    other = {"type": "Polygon", "coordinates": [[[31, -2], [31.001, -2], [31.001, -1.999], [31, -1.999], [31, -2]]]}
    rows = [{"id": "a", "number": 3, "geometry": square}]

    async def kept(s3, bucket, key, layer_id):
        return rows

    async def summary(s3, bucket, key, layer_id, row):
        return {"said": "In the 1 part you checked, the app counted 38 plants and you counted 41."} if row["id"] == "a" else None

    monkeypatch.setattr(field_taps, "enabled", lambda: True)
    monkeypatch.setattr(field_taps, "kept", kept)
    monkeypatch.setattr(count_check, "summary", summary)

    def plot(number, geometry):
        return {"type": "Feature", "geometry": geometry, "properties": {"number": number}}

    assert "you counted 41" in await tools._checked_by_a_person(None, "b", "photo", "L1", plot(3, square))
    # a plot with the same number but another shape is not the field that was checked (Ingabe's own plots)
    assert await tools._checked_by_a_person(None, "b", "photo", "L1", plot(3, other)) is None
    assert await tools._checked_by_a_person(None, "b", "photo", "L1", plot(4, square)) is None
    monkeypatch.setattr(field_taps, "enabled", lambda: False)
    assert await tools._checked_by_a_person(None, "b", "photo", "L1", plot(3, square)) is None


def test_a_count_must_say_whether_the_number_is_a_field_or_a_plot():
    """Field 7 (outlined by the person) and Plot 7 (found by Ingabe) are different pieces of land: the tool cannot be
    called without saying which, so a number is never looked up in the wrong set by default."""
    import pydantic
    import pytest

    from src.tools.drone_photo_tools import CountPlantsArgs

    assert CountPlantsArgs.model_fields["outlined_field"].is_required()
    with pytest.raises(pydantic.ValidationError):
        CountPlantsArgs(layer_id="L1", plot_number=7, show_other_green=False)  # type: ignore[call-arg]
    assert CountPlantsArgs(layer_id="L1", plot_number=7, show_other_green=False, outlined_field=True).outlined_field is True
