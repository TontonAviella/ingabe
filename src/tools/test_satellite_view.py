"""Sage's satellite view tool: whose photo it may read, what it puts on the map, and the words it answers with."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import date
from typing import Any, Optional

import numpy as np
import pytest

from src.services import satellite_layers, satellite_view as sv
from src.tools import satellite_view as tool
from src.tools.pyd import IngabeToolCallMetaArgs

META = IngabeToolCallMetaArgs(user_uuid="user-1", conversation_id=1, map_id="M1", project_id="P1", session=None)
BOX = [30.41749, -1.7009, 30.43148, -1.69289]
TODAY = date(2026, 11, 23)


def _view(plants: bool = True, soil: bool = True, soil_missing: Optional[str] = None) -> sv.View:
    seen = np.full((4, 4), date(2026, 11, 19).toordinal(), dtype=np.int32)
    the_plants = sv.Plants(sv.Grid("EPSG:32736", (10.0, 0.0, 212650.0, 0.0, -10.0, 9812700.0), (4, 4)),
                           np.full((4, 4), 0.55, dtype=np.float32), seen, np.zeros((4, 4), bool),
                           sv.Change(date(2026, 11, 19), date(2026, 10, 8), 0.24, 16), 5)
    the_soil = sv.Soil(period="2026-11-D1", percent=np.array([[62.0, 68.0]], dtype=np.float32), west=30.417, north=-1.692,
                       step=0.0009765625, first_estimate=True, centre=69.0, year_low=40.0, year_high=75.0)
    return sv.View(bbox=tuple(BOX), today=TODAY, plants=the_plants if plants else None, plants_missing=None,  # type: ignore[arg-type]
                   soil=the_soil if soil else None, soil_missing=soil_missing)


@pytest.fixture
def world(monkeypatch):
    """The photo's row, the view the satellites give, the layers put on the map and what the map was told."""
    state: dict[str, Any] = {
        "row": {"layer_id": "Lphoto", "name": "Cyampirita_Orthophoto", "type": "raster", "bounds": BOX, "owner_uuid": "user-1"},
        "view": _view(), "asked": [], "told": [],
        "layers": [satellite_layers.OnMap("Lplants", "Plants (NDVI) - Cyampirita_Orthophoto",
                                          satellite_layers.PLANTS_KIND, False, BOX),
                   satellite_layers.OnMap("Lsoil", "Soil moisture - Cyampirita_Orthophoto", satellite_layers.SOIL_KIND, False, BOX)],
    }

    class Conn:
        async def fetchrow(self, sql, layer_id):
            return state["row"]

    @asynccontextmanager
    async def read_connection():
        yield Conn()

    def view(bounds, today):
        state["asked"].append(bounds)
        if isinstance(state["view"], Exception):
            raise state["view"]
        return state["view"]

    async def put_on_map(seen, **kwargs):
        state["put"] = kwargs
        return state["layers"]

    @asynccontextmanager
    async def action(conversation_id, description, layer_id=None, update_style_json=False, bounds=None):
        state["told"].append((description, layer_id, update_style_json))
        yield None

    monkeypatch.setattr(tool, "get_async_read_connection", read_connection)
    monkeypatch.setattr(tool.satellite_view, "view", view)
    monkeypatch.setattr(tool.satellite_layers, "put_on_map", put_on_map)
    monkeypatch.setattr(tool, "kue_ephemeral_action", action)
    return state


async def _ask() -> dict[str, Any]:
    return await tool.show_plants_and_soil_from_satellite(tool.ShowPlantsAndSoilFromSatelliteArgs(layer_id="Lphoto"), META)


@pytest.mark.anyio
async def test_both_layers_go_on_the_map_and_the_answer_says_plants_soil_and_the_two_together(world):
    result = await _ask()
    assert result["status"] == "success" and world["asked"] == [BOX]
    assert world["put"] == {"photo_layer_id": "Lphoto", "photo_name": "Cyampirita_Orthophoto", "user_uuid": "user-1",
                            "map_id": "M1", "project_id": "P1"}
    assert world["told"] == [("Adding Plants (NDVI) - Cyampirita_Orthophoto", "Lplants", True),
                             ("Adding Soil moisture - Cyampirita_Orthophoto", "Lsoil", True)]
    assert result["together"] == "Plants are filling in and the soil reads near the top of its year: the rains are carrying the crop."
    assert [layer["kind"] for layer in result["layers"]] == [satellite_layers.PLANTS_KIND, satellite_layers.SOIL_KIND]
    said = tool.reply(result)
    assert said.startswith("Here is what the satellites show over Cyampirita_Orthophoto.\n\n**Plants.** Every 10 m square")
    assert "**Soil moisture.** Soil moisture for 1-10 Nov (FAO WaPOR estimate, 100 m squares)" in said
    assert "**Together.** Plants are filling in" in said
    assert said.endswith('Two layers are on the map, each with its own switch: "Plants (NDVI) - Cyampirita_Orthophoto" '
                         'and "Soil moisture - Cyampirita_Orthophoto". Zoom in and the hexagons get smaller, to show where a '
                         'weak patch sits; tap a hexagon for its NDVI and its date and tap a soil moisture number for its reading.')


@pytest.mark.anyio
async def test_layers_already_on_the_map_are_not_announced_again(world):
    world["layers"] = [satellite_layers.OnMap(layer.layer_id, layer.name, layer.kind, True, layer.bounds) for layer in world["layers"]]
    result = await _ask()
    assert world["told"] == [] and all(layer["already_on_map"] for layer in result["layers"])


@pytest.mark.anyio
async def test_one_missing_part_is_said_in_plain_words_and_the_other_still_comes(world):
    world["view"] = _view(soil=False, soil_missing="FAO's soil moisture file could not be read just now.")
    world["layers"] = world["layers"][:1]
    result = await _ask()
    assert result["status"] == "success" and result["soil"] is None and result["together"] is None
    said = tool.reply(result)
    assert "FAO's soil moisture file could not be read just now." in said and "**Soil moisture.**" not in said
    assert said.endswith('The layer "Plants (NDVI) - Cyampirita_Orthophoto" is on the map. Zoom in and the hexagons get '
                         'smaller; tap a hexagon for its NDVI and its date.')


@pytest.mark.anyio
async def test_a_layer_that_could_not_be_drawn_is_owned_up_to_and_the_numbers_still_told(world):
    world["layers"] = world["layers"][:1]  # the soil layer could not be saved
    result = await _ask()
    assert result["not_drawn"] == ["the soil moisture layer"]
    assert "I could not draw the soil moisture layer on the map just now; the numbers above still hold." in tool.reply(result)


@pytest.mark.anyio
@pytest.mark.parametrize("change, error", [
    ({"owner_uuid": "someone-else"}, "Layer Lphoto is not owned by you."),
    ({"type": "vector"}, "Layer Lphoto is type 'vector', not a drone photo."),
    ({"bounds": None}, "This photo has no place on the map yet, so the satellite cannot be matched to it."),
])
async def test_only_the_askers_own_placed_drone_photo_is_read(world, change, error):
    world["row"] = {**world["row"], **change}
    assert await _ask() == {"status": "error", "error": error}
    assert world["asked"] == []


@pytest.mark.anyio
async def test_a_failure_is_told_in_plain_words_never_as_raw_error_text(world):
    world["view"] = RuntimeError("CPLE_OpenFailedError: '/vsicurl/https://...' not recognized")
    result = await _ask()
    assert result == {"status": "error", "error": "The satellite view could not be made just now. Nothing was added to the map."}
    assert tool.reply(result) == "The satellite view could not be made just now. Nothing was added to the map."
    world["view"] = sv.View(bbox=tuple(BOX), today=TODAY, plants=None, plants_missing="No satellite pass saw this ground clearly "  # type: ignore[arg-type]
                            "in the last 180 days.", soil=None, soil_missing="FAO has not published soil moisture for the last weeks.")
    assert (await _ask())["error"] == ("No satellite pass saw this ground clearly in the last 180 days. "
                                       "FAO has not published soil moisture for the last weeks.")
