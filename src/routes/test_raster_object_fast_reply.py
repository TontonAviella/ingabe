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

    assert "found 11 possible roof/house shapes" in reply
    assert "colored mask layer" in reply
    assert "map overlay to review" in reply
    assert "Spot-check the important areas" in reply
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
            "delivery": {"status": "verified", "verified": True},
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

    assert "found 500 possible roof/house shapes" in reply
    assert "colored mask layer" in reply
    assert "capped at 500" in reply
    assert "not the final house count" in reply
    assert "layer `House/Roof Masks - Cyampirita_Orthophoto`" in reply
    assert "layer Lobjects123" in reply
    assert "Object Candidates" not in reply
    assert "500 houses" not in reply
    assert "top 500 likely matches" not in reply


def test_raster_object_reply_does_not_claim_unverified_layer_is_visible() -> None:
    reply = _raster_object_fast_reply(
        {
            "status": "success",
            "layer_id": "Lobjects123",
            "delivery": {
                "status": "unverified",
                "verified": False,
                "error": "PMTiles object was not readable",
            },
            "summary": {
                "candidate_count": 4,
                "candidate_building_count": 4,
                "class_counts": {"building": 4},
            },
        },
        "Cyampirita_Orthophoto",
        requested_building_count=True,
    )

    assert "could not verify" in reply
    assert "not claiming that it is visible" in reply
    assert "Turn on the layer" not in reply
    assert "I added them" not in reply


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

    assert "found 4 possible features" in reply
    assert "road: 2" in reply
    assert "water: 2" in reply
    assert "colored mask layer" in reply


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


def test_raster_object_reply_discloses_fastsam_fallback() -> None:
    reply = _raster_object_fast_reply(
        {
            "status": "success",
            "summary": {
                "candidate_count": 8,
                "candidate_building_count": 8,
                "class_counts": {"building": 8},
                "screening_model": "rasterio_numpy_candidate_extractor_v2",
            },
            "engines": {
                "selection": {
                    "requested": "fastsam",
                    "used": "rasterio_numpy_candidate_extractor_v2",
                }
            },
        },
        "Cyampirita_Orthophoto",
        requested_building_count=True,
    )

    assert "FastSAM was not available for this live run" in reply
    assert "quick image screener" in reply
    assert "rough overlay" in reply
