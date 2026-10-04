"""data_coverage: every data result says, in plain words, what one value covers."""

from __future__ import annotations

import json

import pytest

from src.services import data_coverage
from src.services.data_coverage import annotate, describe, point_sample_note


def test_district_value_says_every_finer_unit_shares_it():
    note = describe("agera5", "district")["note"]
    assert "whole district" in note
    assert "Every sector, cell and village in the district gets this same value." in note


def test_coarse_source_at_a_fine_level_says_neighbours_share_it():
    note = describe("chirps", "village")["note"]
    # ~30 km2 rainfall squares over ~1.64 km2 villages
    assert "about 18 neighbouring villages can share it" in note


def test_fine_source_does_not_claim_sharing():
    note = describe("sentinel2", "cell")["note"]
    assert "neighbouring" not in note


def test_forecast_is_a_grid_square_not_an_admin_unit():
    block = describe("forecast", "point")
    assert block["value_covers"] == "point"
    assert "grid square" in block["note"]


@pytest.mark.parametrize("result_form", ["dict", "json"])
def test_annotate_keeps_the_result_form(result_form):
    result = {"status": "success", "district": "Huye", "rows": [1, 2]}
    given = json.dumps(result) if result_form == "json" else result
    out = annotate("get_weather_stats", {"district": "Huye"}, given)
    payload = json.loads(out) if result_form == "json" else out
    assert isinstance(out, str if result_form == "json" else dict)
    assert payload["rows"] == [1, 2]
    assert payload["data_coverage"]["value_covers"] == "district"


def test_level_comes_from_the_arguments():
    sector = annotate("get_cell_ndvi_stats", {"district": "Huye", "sector": "Tumba"}, {"rows": []})
    cell = annotate("get_cell_ndvi_stats", {"district": "Huye", "cell_name": "Cyarwa"}, {"rows": []})
    indices = annotate("get_agri_indices", {"admin_level": "sector", "name": "Tumba"}, {"rows": []})
    assert sector["data_coverage"]["value_covers"] == "sector"
    assert cell["data_coverage"]["value_covers"] == "cell"
    assert indices["data_coverage"]["value_covers"] == "sector"


@pytest.mark.parametrize("result", [
    {"status": "error", "error": "no data"},
    {"error": "boom"},
    "not json",
    ["a", "list"],
    {"data_coverage": {"note": "already there"}},
])
def test_failures_and_odd_results_are_left_alone(result):
    assert annotate("get_weather_stats", {}, result) is None


def test_unknown_tools_are_left_alone():
    assert annotate("zoom_to_location", {}, {"status": "ok"}) is None


def test_every_mapped_tool_names_a_known_source():
    for name, spec in data_coverage.TOOLS.items():
        assert spec.source in data_coverage.SOURCES, name


def test_point_sample_says_where_the_value_comes_from():
    assert point_sample_note("chirps", "Huye", "district") == (
        "From CHIRPS satellite rainfall estimates: the 5 km square at the centre of Huye, "
        "not an average over the whole district."
    )
    village = point_sample_note("chirps", "Gasharu", "village")
    assert "about 18 neighbouring villages share these figures" in village


def test_map_levels_point_finer_levels_at_the_finest_with_values():
    levels = {x["level"]: x for x in data_coverage.map_levels(("district",))}
    assert levels["district"] == {"level": "district", "has_values": True, "values_from": "district"}
    assert levels["village"] == {"level": "village", "has_values": False, "values_from": "district"}


def test_map_levels_use_their_own_values_where_they_have_them():
    levels = {x["level"]: x for x in data_coverage.map_levels(("district", "cell"))}
    assert levels["sector"]["values_from"] == "district"
    assert levels["village"]["values_from"] == "cell"


def test_shared_value_note_names_the_unit_and_count():
    note = data_coverage.shared_value_note("district", "Huye", "village", 509)
    assert note == "One value for the whole Huye district: all 509 villages in it share it."
