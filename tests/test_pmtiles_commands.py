from src.postgis_tiles import MVT_LAYER_NAME
from src.upload.pmtiles import (
    _build_flatgeobuf_reproject_command,
    _build_ogr_pmtiles_fallback_command,
    _build_tippecanoe_pmtiles_command,
)


def test_uploaded_vector_pmtiles_commands_force_expected_mvt_layer_name():
    reproject_cmd = _build_flatgeobuf_reproject_command(
        output_path="/tmp/reprojected.fgb",
        ogr_source="/tmp/input.geojson",
        dataset_layer=None,
    )
    tippecanoe_cmd = _build_tippecanoe_pmtiles_command(
        output_path="/tmp/output.pmtiles",
        input_path="/tmp/reprojected.fgb",
    )
    fallback_cmd = _build_ogr_pmtiles_fallback_command(
        output_path="/tmp/output.pmtiles",
        input_path="/tmp/reprojected.fgb",
    )

    assert reproject_cmd[reproject_cmd.index("-nln") + 1] == MVT_LAYER_NAME
    assert tippecanoe_cmd[tippecanoe_cmd.index("-l") + 1] == MVT_LAYER_NAME
    assert fallback_cmd[fallback_cmd.index("-nln") + 1] == MVT_LAYER_NAME
