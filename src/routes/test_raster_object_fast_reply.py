from __future__ import annotations

from src.routes.message_routes import _raster_object_fast_reply


def test_raster_object_reply_does_not_confirm_house_count() -> None:
    reply = _raster_object_fast_reply(
        {
            "status": "success",
            "summary": {
                "candidate_count": 14,
                "candidate_building_count": 11,
                "class_counts": {"building": 11, "road": 3},
                "confirmed_count_available": False,
                "count_semantics": "candidate_screening",
                "honesty_note": "These are candidate polygons from the uploaded image, not confirmed houses.",
            },
        },
        "Cyampirita_Orthophoto",
        requested_building_count=True,
    )

    assert "11 likely building/roof candidate polygons" in reply
    assert "did not produce a confirmed house count" in reply
    assert "confirmed house count needs footprint evidence" in reply
    assert "I counted 11 houses" not in reply


def test_raster_object_reply_keeps_generic_candidate_language() -> None:
    reply = _raster_object_fast_reply(
        {
            "status": "success",
            "summary": {
                "candidate_count": 4,
                "class_counts": {"road": 2, "water": 2},
                "honesty_note": "These are candidate polygons from the uploaded image, not confirmed assets.",
            },
        },
        "Cyampirita_Orthophoto",
    )

    assert "4 object candidates" in reply
    assert "road: 2" in reply
    assert "water: 2" in reply
    assert "not confirmed assets" in reply
