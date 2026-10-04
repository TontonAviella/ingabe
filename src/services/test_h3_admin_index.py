"""h3_admin_index: hexagon <-> admin unit overlaps (pure parts, no database)."""

from __future__ import annotations

import h3
import pytest
from pyproj import Geod
from shapely.geometry import box

from src.services.h3_admin_index import OverlapRow, hexagon, level_rows, main_units, unit_overlaps

GEOD = Geod(ellps="WGS84")


def _km2(geom) -> float:
    return abs(GEOD.geometry_area_perimeter(geom)[0]) / 1e6


def test_overlaps_add_up_to_the_unit_area():
    unit = box(30.00, -2.00, 30.05, -1.95)  # ~30 km2 near Kigali
    overlaps = unit_overlaps(unit)
    assert sum(o.overlap_km2 for o in overlaps) == pytest.approx(_km2(unit), rel=0.01)


def test_hexagons_inside_count_whole_and_edges_count_part():
    unit = box(30.00, -2.00, 30.05, -1.95)
    fractions = [o.hex_fraction for o in unit_overlaps(unit)]
    assert max(fractions) == pytest.approx(1.0)
    assert 0 < min(fractions) < 1


def test_a_unit_smaller_than_one_hexagon_still_gets_hexagons():
    tiny = box(30.0000, -2.0000, 30.0005, -1.9995)  # ~0.003 km2, a hexagon is ~0.1 km2
    overlaps = unit_overlaps(tiny)
    assert overlaps
    assert sum(o.overlap_km2 for o in overlaps) == pytest.approx(_km2(tiny), rel=0.02)


def test_hexagon_outline_round_trips_to_its_cell():
    cell = h3.latlng_to_cell(-1.95, 30.06, 9)
    centre = hexagon(cell).centroid
    assert h3.latlng_to_cell(centre.y, centre.x, 9) == cell


def test_unit_fraction_sums_to_one_per_unit():
    rows = level_rows("village", [("1", "A", box(30.00, -2.00, 30.01, -1.99))])
    assert sum(r.unit_fraction for r in rows) == pytest.approx(1.0)


def test_main_unit_is_the_one_sharing_most_area():
    rows = [
        OverlapRow("h1", "village", "7", "Small", 0.01, 0.1, 0.5),
        OverlapRow("h1", "village", "3", "Big", 0.09, 0.9, 0.2),
        OverlapRow("h2", "village", "9", "Tie", 0.05, 0.5, 0.1),
        OverlapRow("h2", "village", "4", "TieLowId", 0.05, 0.5, 0.1),
    ]
    assert main_units(rows) == {"h1": ("3", "Big"), "h2": ("4", "TieLowId")}


def test_coarser_hexagons_take_the_unit_most_children_belong_to():
    from src.services.h3_admin_index import coarser_main_units
    parent = h3.latlng_to_cell(-1.95, 30.06, 8)
    kids = sorted(h3.cell_to_children(parent, 9))
    cells = [(k, "Gasabo" if i < 5 else "Kicukiro") for i, k in enumerate(kids)] + [(kids[0], None)]
    assert coarser_main_units(cells, 8) == {parent: "Gasabo"}  # 5 of 7 children
    assert coarser_main_units([(kids[0], None)], 8) == {}


def test_outline_is_a_closed_lon_lat_ring():
    from src.services.h3_admin_index import outline
    cell = h3.latlng_to_cell(-1.95, 30.06, 9)
    ring = outline(cell)["coordinates"][0]
    assert ring[0] == ring[-1] and len(ring) == 7
    assert 29 < ring[0][0] < 31 and -3 < ring[0][1] < -1  # lon first
