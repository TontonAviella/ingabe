from src.tools.display_layer import DisplayLayerArgs


PROVENANCE_FIELDS = {
    "source_catalog",
    "source_collection",
    "scene_id",
    "scene_date",
    "cloud_cover",
    "platform",
}


def test_provenance_fields_are_required_but_accept_null():
    schema = DisplayLayerArgs.model_json_schema()

    assert PROVENANCE_FIELDS <= set(schema["required"])
    args = DisplayLayerArgs(
        asset_url="https://example.com/sample.tif",
        layer_name="Sample",
        style_hint="visual",
        bbox="29,-3,31,-1",
        band_index=1,
        source_catalog=None,
        source_collection=None,
        scene_id=None,
        scene_date=None,
        cloud_cover=None,
        platform=None,
    )
    assert args.cloud_cover is None
