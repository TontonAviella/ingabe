from src.services.tessera_embeddings import tessera_embedding_status


def test_tessera_embedding_status_is_honest_about_role():
    status = tessera_embedding_status()

    assert status["engine_role"] == "satellite_embedding_memory"
    assert status["map_renderer"] is False
    assert "building footprint extraction" in status["not_active_for"]
    assert "drone-resolution damage segmentation" in status["not_active_for"]
    assert status["recommended_runtime_path"].startswith("Precompute")
