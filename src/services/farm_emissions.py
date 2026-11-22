"""Farm emissions for a district, a province or Rwanda, read from emissions_annual_cache.

The rows are EDGAR's estimates (European Commission, Joint Research Centre) for four farming sources, shared
out from squares about 11 km wide by each district's outline (migration d4a9e2b6c1f3). They are not measured in
Rwanda and they are farming only, and every answer says so: ``NOTE`` goes out with each result.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Optional

# Only rows rebuilt by district outline are read; the older rows summed the rectangle around a district.
METHOD_MARK = "+outline_share"

# Tonnes of CO2 that warm as much as one tonne of the gas over 100 years (IPCC AR6, 2021; methane from
# living sources). Ammonia is an air pollutant, not a greenhouse gas, so it has no factor.
# Carbon dioxide is not read: EDGAR's own country table has no farm CO2 row for Rwanda, so the few hundred
# tonnes its grid puts inside the outline have no national figure behind them (checked 2026-11-20; migration
# e7b3c5d9a2f4 removes those rows, and a CO2 row that turned up again would still not be counted).
GWP100: dict[str, float] = {"CH4": 27.0, "N2O": 273.0}
GAS_NAMES: dict[str, str] = {"CH4": "methane", "N2O": "nitrous oxide", "NH3": "ammonia"}
READ_GASES = frozenset(GAS_NAMES)

# Plain names for EDGAR's four farming sources.
SOURCE_NAMES: dict[str, str] = {
    "Enteric fermentation": "Livestock digestion (enteric fermentation)",
    "Manure management": "Manure",
    "Agricultural soils": "Farm soils",
    "Agricultural waste burning": "Burning of crop waste",
}
# Districts of 132 and 167 km2: little more than one EDGAR square (about 123 km2), so most of their figure
# is one square's value shared by area.
ROUGH_DISTRICTS = frozenset({"Nyarugenge", "Kicukiro"})

NOTE = (
    "These are EDGAR estimates from the European Commission, not measurements taken in Rwanda. They cover "
    "farming only (livestock digestion, manure, farm soils, burning of crop waste): no transport, energy, "
    "industry, waste or forests, so they are NOT a place's total greenhouse gas emissions. Say both things "
    "plainly, and say 'farm emissions', never just 'emissions'. EDGAR works on squares about 11 km wide, "
    "shared out here by district outline. CO2-equivalent uses IPCC AR6 100-year factors (methane 27, nitrous "
    "oxide 273). Quote the figures as given: do NOT add, subtract or combine them yourself; every source's "
    "total and share is already here. One farm source is left out: indirect nitrous oxide (from nitrogen "
    "that washes or drifts off the land), so the farm figure here is a little low."
)
# How much is left out is known from EDGAR's country table only (2022: 351.6 t of nitrous oxide, about 96,000
# t CO2-equivalent against 2.82 million), so the size is given for Rwanda and for no smaller place.
LEFT_OUT_RWANDA = " For Rwanda as a whole EDGAR's own table puts the left-out part at about 3% more in 2022."
LEFT_OUT_SMALLER = (
    " How much is left out is NOT known for a district or a province: give no percent or figure for it."
)
ROUGH_NOTE = (
    " This district is little more than one EDGAR square, so its figure is rough: most of it is one square's "
    "value shared by area. Say so."
)


def _round(tonnes: float) -> float:
    return float(round(tonnes)) if abs(tonnes) >= 100 else round(tonnes, 1)


def summarise(rows: Iterable[Mapping[str, Any]], place: str, level: str, year: Optional[int]) -> dict[str, Any]:
    """One answer from the cache rows of the districts that make up ``place``.

    ``rows`` hold district, year, emission_type, sector_label and total_tonnes. ``year`` None means the latest
    year present. Rows are summed over the districts, so a province or Rwanda is the sum of its districts.
    """
    rows = [r for r in rows if r["total_tonnes"] is not None and r["emission_type"] in READ_GASES]
    years = sorted({int(r["year"]) for r in rows})
    if not years:
        return {
            "status": "no_data",
            "place": place,
            "note": f"No farm emission estimates are stored for {place}. Do NOT give a figure; say there is none.",
        }
    chosen = years[-1] if year is None else int(year)
    if chosen not in years:
        return {
            "status": "no_data",
            "place": place,
            "years_available": years,
            "note": (f"No farm emission estimate is stored for {place} in {chosen}; the years held are "
                     f"{years[0]} to {years[-1]}. Do NOT give a figure for {chosen}."),
        }

    def co2e_of(year_rows: list[Mapping[str, Any]]) -> float:
        return sum(float(r["total_tonnes"]) * GWP100[r["emission_type"]] for r in year_rows
                   if r["emission_type"] in GWP100)

    this_year = [r for r in rows if int(r["year"]) == chosen]
    total = co2e_of(this_year)
    tonnes: dict[tuple[str, str], float] = {}
    for r in this_year:
        key = (str(r["sector_label"]), str(r["emission_type"]))
        tonnes[key] = tonnes.get(key, 0.0) + float(r["total_tonnes"])
    sources = []
    for label in sorted({label for label, _gas in tonnes}):
        gases = [
            {"gas": GAS_NAMES[gas], "tonnes_of_gas": _round(t), "tonnes_co2e": _round(t * GWP100[gas])}
            for (lab, gas), t in tonnes.items() if lab == label and gas in GWP100
        ]
        source_co2e = sum(t * GWP100[gas] for (lab, gas), t in tonnes.items() if lab == label and gas in GWP100)
        sources.append({
            "source": SOURCE_NAMES.get(label, label),
            "tonnes_co2e": _round(source_co2e),
            "share_percent": round(100 * source_co2e / total) if total else 0,
            "gases": sorted(gases, key=lambda g: -g["tonnes_co2e"]),
        })
    sources.sort(key=lambda s: -s["tonnes_co2e"])
    districts = {str(r["district"]) for r in this_year}
    rough = level == "district" and bool(districts & ROUGH_DISTRICTS)
    return {
        "status": "success",
        # Rides ahead of every figure: one answer in twelve once dropped "estimate" when it sat only in the note.
        "what_these_figures_are": "EDGAR estimates, not measurements; farming only. Say both with the figure.",
        "place": place,
        "level": level,
        "year": chosen,
        "years_available": years,
        "districts_summed": len(districts),
        "farm_greenhouse_gases_tonnes_co2e": _round(total),
        "by_source": sources,
        "ammonia_tonnes_not_a_greenhouse_gas": _round(sum(t for (_lab, gas), t in tonnes.items() if gas == "NH3")),
        "farm_greenhouse_gases_by_year_tonnes_co2e": {
            y: _round(co2e_of([r for r in rows if int(r["year"]) == y])) for y in years
        },
        "source": "EDGAR v8.0 FT2022 (greenhouse gases) and v8.1 FT2022 (ammonia), European Commission JRC",
        "note": NOTE + (LEFT_OUT_RWANDA if len(districts) == 30 else LEFT_OUT_SMALLER) + (ROUGH_NOTE if rough else ""),
    }
