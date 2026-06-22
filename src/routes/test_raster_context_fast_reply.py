from __future__ import annotations

from src.routes.message_routes import _raster_context_fast_reply


def _result(domain: str) -> dict:
    return {
        "status": "success",
        "geojson_feature_count": 4221,
        "summary": {
            "domain": domain,
            "cell_count": 4221,
            "high_or_severe_cell_count": 1586,
            "max_score": 100.0,
            "evidence_basis": "uploaded raster pixels grouped into internal cells",
        },
    }


def test_building_count_reply_refuses_to_count_from_raster_proxy() -> None:
    reply = _raster_context_fast_reply(
        _result("housing"),
        "Cyampirita_Orthophoto",
        requested_building_count=True,
    )

    assert "highlights built-up-looking areas" in reply
    assert "does not mark individual houses yet" in reply
    assert "small roof/house shapes outlined" in reply
    assert "screening cells" in reply
    assert "marks to review" in reply
    assert "likely house/settlement attention cells" not in reply


def test_generic_raster_context_reply_stays_proxy_only() -> None:
    reply = _raster_context_fast_reply(
        _result("mixed"),
        "Cyampirita_Orthophoto",
    )

    assert "screened Cyampirita_Orthophoto" in reply
    assert "visual proxy cells" in reply
    assert "screening cells" in reply
    assert "house" not in reply.lower()


def test_housing_proxy_reply_does_not_claim_exact_assets() -> None:
    reply = _raster_context_fast_reply(
        _result("housing"),
        "Cyampirita_Orthophoto",
    )

    assert "settlement-looking visual patterns" in reply
    assert "find where to look first" in reply
    assert "not an exact count" in reply
    assert "likely house/settlement attention cells" not in reply
