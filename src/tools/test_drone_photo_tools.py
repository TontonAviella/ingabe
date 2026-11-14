"""How a plant count is said and drawn (src/tools/drone_photo_tools.py): to two figures, and plants alone unless
the person asked for the other green."""

from __future__ import annotations

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
