from src.services.whitebox_engine import whitebox_engine_status


def test_whitebox_engine_status_has_spatial_intelligence_contract():
    status = whitebox_engine_status()

    assert status["engine_role"] == "analysis_backend"
    assert status["map_renderer"] is False
    assert "terrain" in status["curated_domains"]
    assert "hydrology" in status["curated_domains"]
    assert "urban_environment" in status["curated_domains"]
    assert "drone_lidar" in status["curated_domains"]
