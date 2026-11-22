"""Farm emissions: the reader's sums, its honest "no data", and the seed's own arithmetic."""

import csv
from collections import defaultdict
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.services import farm_emissions
from src.services.legacy_tool_shim import _RWANDA_DISTRICT_TO_PROVINCE, LEGACY_HANDLERS

SEED = Path(__file__).resolve().parents[2] / "alembic" / "seeds" / "edgar_farm_emissions_rwanda_2018_2022.csv"


def _seed_rows() -> list[dict]:
    with SEED.open(newline="") as fh:
        return [{**r, "year": int(r["year"]), "total_tonnes": float(r["total_tonnes"])} for r in csv.DictReader(fh)]


def test_the_seed_holds_every_district_gas_source_and_year_once():
    rows = _seed_rows()
    keys = {(r["district"], r["year"], r["emission_type"], r["sector"]) for r in rows}
    assert len(rows) == len(keys) == 30 * 11 * 5
    assert {r["district"] for r in rows} == set(_RWANDA_DISTRICT_TO_PROVINCE)
    assert all(r["total_tonnes"] >= 0 for r in rows)


def test_rwanda_livestock_methane_is_the_rebuilt_figure_not_the_double_counted_one():
    # The rectangle rows summed to 109,195 t for 2022; the same grids shared by outline give 61,338 t.
    total = sum(r["total_tonnes"] for r in _seed_rows()
                if r["year"] == 2022 and r["emission_type"] == "CH4" and r["sector"] == "ENF")
    assert total == pytest.approx(61338.1, abs=1.0)


def test_a_place_is_the_sum_of_its_districts_and_co2e_uses_the_stated_factors():
    rows = [
        {"district": "A", "year": 2022, "emission_type": "CH4", "sector_label": "Enteric fermentation", "total_tonnes": 100.0},
        {"district": "B", "year": 2022, "emission_type": "CH4", "sector_label": "Enteric fermentation", "total_tonnes": 50.0},
        {"district": "A", "year": 2022, "emission_type": "N2O", "sector_label": "Agricultural soils", "total_tonnes": 2.0},
        {"district": "A", "year": 2022, "emission_type": "NH3", "sector_label": "Agricultural soils", "total_tonnes": 7.0},
        {"district": "A", "year": 2021, "emission_type": "CO2", "sector_label": "Agricultural soils", "total_tonnes": 5.0},
    ]
    out = farm_emissions.summarise(rows, "Somewhere", "province", None)
    assert out["status"] == "success" and out["year"] == 2022 and out["districts_summed"] == 2
    assert out["farm_greenhouse_gases_tonnes_co2e"] == 150 * 27 + 2 * 273
    assert out["by_source"] == [
        {"source": "Livestock digestion (enteric fermentation)", "tonnes_co2e": 4050.0, "share_percent": 88,
         "gases": [{"gas": "methane", "tonnes_of_gas": 150.0, "tonnes_co2e": 4050.0}]},
        {"source": "Farm soils", "tonnes_co2e": 546.0, "share_percent": 12,
         "gases": [{"gas": "nitrous oxide", "tonnes_of_gas": 2.0, "tonnes_co2e": 546.0}]},
    ]
    # The sources add up to the total, so Sage never has to add them itself.
    assert sum(s["tonnes_co2e"] for s in out["by_source"]) == out["farm_greenhouse_gases_tonnes_co2e"]
    # Ammonia is reported, and never counted as a greenhouse gas.
    assert out["ammonia_tonnes_not_a_greenhouse_gas"] == 7.0
    # Farm carbon dioxide has no national figure behind it, so a CO2 row is never read: 2021 holds only one.
    assert out["years_available"] == [2022]
    assert out["farm_greenhouse_gases_by_year_tonnes_co2e"] == {2022: 4596.0}
    assert "indirect nitrous oxide" in out["note"]
    assert "farming only" in out["note"] and "not measurements" in out["note"]
    assert "estimates, not measurements; farming only" in out["what_these_figures_are"]


def test_a_year_or_place_without_rows_gets_no_figure():
    rows = [{"district": "A", "year": 2022, "emission_type": "CH4", "sector_label": "x", "total_tonnes": 1.0}]
    missing_year = farm_emissions.summarise(rows, "A district", "district", 2015)
    assert missing_year["status"] == "no_data" and "farm_greenhouse_gases_tonnes_co2e" not in missing_year
    assert farm_emissions.summarise([], "A district", "district", None)["status"] == "no_data"


class _Conn:
    def __init__(self):
        self.calls = []

    async def fetch(self, sql, *params):
        self.calls.append((sql, params))
        return [r for r in _seed_rows() if r["district"] in params[0]]


async def _ask(**arguments):
    conn = _Conn()
    out = await LEGACY_HANDLERS["get_farm_emissions"](SimpleNamespace(arguments=arguments, conn=conn))
    return out, conn


@pytest.mark.asyncio
async def test_kigali_means_its_three_districts_and_only_outline_rows_are_read():
    out, conn = await _ask(province="Kigali")
    assert out["place"] == "Kigali City" and out["districts_summed"] == 3
    assert conn.calls[0][1] == (["Gasabo", "Kicukiro", "Nyarugenge"], "%+outline_share")
    by_district = defaultdict(float)
    for r in _seed_rows():
        if r["year"] == 2022 and r["emission_type"] in farm_emissions.GWP100:
            by_district[r["district"]] += r["total_tonnes"] * farm_emissions.GWP100[r["emission_type"]]
    expected = sum(by_district[d] for d in ("Gasabo", "Kicukiro", "Nyarugenge"))
    assert out["farm_greenhouse_gases_tonnes_co2e"] == pytest.approx(expected, abs=1.0)


@pytest.mark.asyncio
async def test_district_rwanda_and_an_unknown_place():
    district, _ = await _ask(district="nyagatare district", year=2020)
    assert district["place"] == "Nyagatare district" and district["year"] == 2020
    rwanda, _ = await _ask()
    assert rwanda["place"] == "Rwanda" and rwanda["districts_summed"] == 30
    assert "rough" not in district["note"]
    # The size of the left-out indirect nitrous oxide is known for Rwanda only, so only Rwanda's note gives it.
    assert "about 3%" in rwanda["note"] and "about 3%" not in district["note"]
    assert "NOT known for a district or a province" in district["note"]
    assert "rough" in (await _ask(district="Kicukiro"))[0]["note"]
    unknown, conn = await _ask(district="Kampala")
    assert unknown["status"] == "no_data" and not conn.calls
