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
                "honesty_note": "These are likely marks from the uploaded image.",
            },
        },
        "Cyampirita_Orthophoto",
        requested_building_count=True,
    )

    assert "marked 11 possible roof/house shapes" in reply
    assert "marks to review from the image" in reply
    assert "Spot-check the important marks" in reply
    assert "I counted 11 houses" not in reply
    assert "Open Buildings" not in reply
    assert "OSM" not in reply
    assert "SAM" not in reply
    assert "YOLO" not in reply


def test_raster_object_reply_discloses_candidate_cap() -> None:
    reply = _raster_object_fast_reply(
        {
            "status": "success",
            "layer_id": "Lobjects123",
            "summary": {
                "candidate_count": 500,
                "candidate_building_count": 500,
                "class_counts": {"building": 500},
                "candidate_count_capped": True,
                "max_candidates": 500,
                "honesty_note": "These are likely marks from the uploaded image.",
            },
        },
        "Cyampirita_Orthophoto",
        requested_building_count=True,
    )

    assert "displayed 500 roof/house review marks" in reply
    assert "capped at 500 marks" in reply
    assert "marks shown for review" in reply
    assert "not as the number of houses" in reply
    assert "outlined layer `House/Roof Review Marks - Cyampirita_Orthophoto`" in reply
    assert "layer Lobjects123" in reply
    assert "Object Candidates" not in reply
    assert "500 houses" not in reply
    assert "top 500 likely matches" not in reply


def test_raster_object_reply_keeps_generic_candidate_language() -> None:
    reply = _raster_object_fast_reply(
        {
            "status": "success",
            "summary": {
                "candidate_count": 4,
                "class_counts": {"road": 2, "water": 2},
                "honesty_note": "These are likely marks from the uploaded image.",
            },
        },
        "Cyampirita_Orthophoto",
    )

    assert "marked 4 possible objects" in reply
    assert "road: 2" in reply
    assert "water: 2" in reply
    assert "review marks" in reply


def test_raster_object_reply_timeout_is_plain_language() -> None:
    reply = _raster_object_fast_reply(
        {
            "status": "timeout",
            "error": "Raster object marking exceeded the live chat timeout.",
        },
        "Cyampirita_Orthophoto",
        requested_building_count=True,
    )

    assert "could not finish marking the houses" in reply
    assert "does not keep thinking forever" in reply
    assert "object candidates" not in reply
    assert "SamGeo" not in reply
    assert "GeoLibre" not in reply


def test_raster_object_error_is_plain_language() -> None:
    reply = _raster_object_fast_reply(
        {
            "status": "error",
            "error": "backend tool unavailable",
        },
        "Cyampirita_Orthophoto",
    )

    assert "could not mark the requested features" in reply
    assert "backend tool unavailable" in reply
    assert "object candidates" not in reply
