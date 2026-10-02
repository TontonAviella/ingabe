"""Generate cases/paraphrases.jsonl: several wordings per request type.

Run: python evals/sage_routing/build_paraphrases.py

Each request type is one ``intent`` (cluster). Its wordings are filled with
different districts, chosen with a fixed seed so the file is reproducible.
compare() resamples whole intents, so these wordings add coverage without
being counted as independent evidence.
"""
from __future__ import annotations

import json
import random
from pathlib import Path

OUT = Path(__file__).resolve().parent / "cases" / "paraphrases.jsonl"

DISTRICTS = [
    "Bugesera", "Burera", "Gakenke", "Gasabo", "Gatsibo", "Gicumbi", "Gisagara",
    "Huye", "Kamonyi", "Karongi", "Kayonza", "Kicukiro", "Kirehe", "Muhanga",
    "Musanze", "Ngoma", "Ngororero", "Nyabihu", "Nyagatare", "Nyamagabe",
    "Nyamasheke", "Nyanza", "Nyarugenge", "Nyaruguru", "Rubavu", "Ruhango",
    "Rulindo", "Rusizi", "Rutsiro", "Rwamagana",
]
RASTER_MAP = {"layers": [
    {"name": "Farm_A_Orthophoto", "type": "raster"},
    {"name": "Farm_B_Orthophoto", "type": "raster"},
]}
ONE_RASTER_MAP = {"layers": [{"name": "Farm_D_Orthophoto", "type": "raster"}]}
NDVI_MAP = {"layers": [{"name": "Plot9_NDVI", "type": "raster"}, {"name": "Plot9_NDVI_June", "type": "raster"}]}
SHOW = ["show_admin_boundary", "search_location", "new_layer_from_postgis", "zoom_to_bounds"]
LAYER = ["show_admin_boundary", "new_layer_from_postgis"]
HOUSES = ["analyze_raster_object_candidates", "create_raster_h3_context_layer"]

# (intent, category, any_of or None for a text answer, args or None, map_state or None, wordings)
# Wordings may use {d} for a district; args may use {d} too (lower-cased).
INTENTS: list[tuple] = [
    ("p-show-district", "admin_display", SHOW, {"search_location": {"query": "{d}"}}, None,
     ["show me {d} on the map", "where is {d} district?", "take me to {d}"]),
    ("p-show-sectors", "admin_display", LAYER, None, None,
     ["show the sectors of {d}", "display {d} sectors on the map", "I want to see all sectors in {d} district"]),
    ("p-show-cells", "admin_display", LAYER, None, None,
     ["show me the cells in {d}", "map the cells of {d} district", "display cell boundaries for {d}"]),
    ("p-list-sectors", "admin_display", LAYER + ["query_postgis_database"], None, None,
     ["list the sectors in {d}", "how many sectors does {d} have?", "which sectors are in {d} district?"]),
    ("p-forecast", "weather", ["get_forecast"], None, None,
     ["what's the forecast for {d} this week?", "will it rain in {d} tomorrow?", "rain outlook for {d} for the next 5 days"]),
    ("p-weather-history", "weather", ["get_weather_stats"], {"get_weather_stats": {"district": "{d}"}}, None,
     ["how much rain fell in {d} last month?", "temperature records for {d} in May 2026", "show observed weather for {d} this season"]),
    ("p-dry-spells", "weather", ["detect_dry_spells"], {"detect_dry_spells": {"district": "{d}"}}, None,
     ["any dry spells in {d} this season?", "find periods without rain in {d} since February", "how long was the longest dry spell in {d}?"]),
    ("p-forecast-accuracy", "weather", ["get_forecast_accuracy"], None, None,
     ["how good have forecasts been for {d}?", "forecast error for {d} over the last 30 days", "check forecast accuracy in {d}"]),
    ("p-evapotranspiration", "weather", ["get_evapotranspiration"], None, None,
     ["evapotranspiration trend in {d}", "how much water are crops using in {d}? give me ET", "actual ET for {d} last 3 months"]),
    ("p-drought", "drought", ["get_drought_status", "get_insurance_intelligence"], None, None,
     ["is {d} in drought right now?", "drought conditions in {d}", "how dry is {d} this season?"]),
    ("p-food-security", "drought", ["get_food_security_alerts"], None, None,
     ["food security phase in Rwanda now", "any IPC food crisis areas in Rwanda?", "which areas face food insecurity?"]),
    ("p-ndvi-district", "ndvi", ["get_ndvi_stats", "get_agri_indices", "query_rwanda_zonal_stats"],
     {"get_ndvi_stats": {"district": "{d}"}}, None,
     ["NDVI for {d} district", "how green is {d} right now?", "latest vegetation index in {d}"]),
    ("p-ndvi-cells", "ndvi", ["get_cell_ndvi_stats", "get_agri_indices"], {"get_cell_ndvi_stats": {"district": "{d}"}}, None,
     ["NDVI per cell in {d}", "which sectors of {d} have the lowest NDVI?", "break down {d} vegetation by sector"]),
    ("p-anomalies", "ndvi", ["get_anomaly_alerts"], None, None,
     ["any vegetation anomaly alerts?", "where did NDVI drop below normal recently?", "show me current crop stress alerts"]),
    ("p-spectral-map", "ndvi", ["compute_spectral_index"], None, None,
     ["map NDVI over {d}", "put an NDVI layer for {d} on the map", "show vegetation index colours over {d}"]),
    ("p-growth-stage", "crops", ["get_crop_growth_stage"], {"get_crop_growth_stage": {"district": "{d}"}}, None,
     ["what stage are crops at in {d}?", "is maize flowering in {d} yet?", "crop phenology in {d}"]),
    ("p-yield-risk", "crops", ["get_yield_risk"], {"get_yield_risk": {"district": "{d}"}}, None,
     ["yield risk in {d}", "is the harvest at risk in {d}?", "which way is the yield trend going in {d}?"]),
    ("p-cropland", "crops", ["query_worldcover_stats"], None, None,
     ["how much cropland is in {d}?", "land cover breakdown for {d}", "hectares of trees vs crops in {d}"]),
    ("p-situation", "insurance", ["get_insurance_intelligence"], {"get_insurance_intelligence": {"district": "{d}"}}, None,
     ["agricultural situation report for {d}", "give me the full farming picture in {d}", "how are farms doing in {d} this season?"]),
    ("p-payout", "insurance", ["get_insurance_intelligence", "evaluate_insurance_trigger"], None, None,
     ["will the drought insurance pay out in {d}?", "is the parametric trigger hit for maize in {d}?", "insurance trigger status for beans in {d}"]),
    ("p-soil-point", "soil", ["get_soil_properties"], None, None,
     ["soil properties at -1.70, 29.90", "what's the soil like at latitude -2.05 longitude 30.40?", "clay and pH at -2.48, 29.57"]),
    ("p-soil-moisture", "soil", ["get_soil_moisture", "get_cygnss_soil_moisture"], None, None,
     ["soil moisture at -1.95, 30.10 this month", "how wet is the soil at -2.20, 30.15?", "soil water trend at -1.50, 30.30"]),
    ("p-sat-search", "satellite", ["search_satellite_imagery", "display_satellite_layer"], None, None,
     ["find cloud-free Sentinel-2 scenes over {d} this month", "latest satellite images for {d}", "which satellite passes covered {d} recently?"]),
    ("p-sat-display", "satellite", ["display_satellite_layer"], None, None,
     ["show a true colour satellite image of {d}", "put recent satellite imagery of {d} on the map", "display the latest optical image for {d}"]),
    ("p-flood", "satellite", ["detect_flood_extent"], None, None,
     ["how much of {d} flooded last month?", "flood extent in {d} after the rains", "map flooded areas in {d}"]),
    ("p-water-bodies", "satellite", ["detect_water_bodies"], None, None,
     ["find ponds in {d} with radar", "detect water bodies in {d}", "map small reservoirs in {d}"]),
    ("p-houses", "ortho_objects", HOUSES, None, RASTER_MAP,
     ["how many houses are in Farm_A_Orthophoto?", "find the buildings in Farm_B_Orthophoto", "mark every roof in Farm_A_Orthophoto"]),
    ("p-houses-this", "ortho_objects", HOUSES, None, ONE_RASTER_MAP,
     ["count the houses in this orthophoto", "where are the buildings in this drone image?", "detect houses in the uploaded image"]),
    ("p-raster-area", "ortho_facts", ["describe_user_raster"], None, RASTER_MAP,
     ["how many hectares does Farm_B_Orthophoto cover?", "area of Farm_A_Orthophoto in hectares", "what is the footprint of Farm_B_Orthophoto?"]),
    ("p-grvi", "ortho_facts", ["analyze_rgb_field"], None, ONE_RASTER_MAP,
     ["how green is this drone image? use GRVI", "compute greenness for Farm_D_Orthophoto", "field coverage and greenness in this orthophoto"]),
    ("p-ndvi-stress", "ndvi", ["find_stress_zones"], None, NDVI_MAP,
     ["where are the stressed patches in Plot9_NDVI?", "find low-NDVI clusters in Plot9_NDVI", "show me the problem zones in Plot9_NDVI"]),
    ("p-ndvi-change", "ndvi", ["compare_rasters"], None, NDVI_MAP,
     ["what changed between Plot9_NDVI and Plot9_NDVI_June?", "compare the two NDVI flights of plot 9", "NDVI difference for plot 9 between the flights"]),
    ("p-buffer", "map_ops", ["native_buffer"], None, None,
     ["buffer the wells layer by 2 km", "draw a 500 m zone around the schools layer", "create a 1 km buffer around the roads layer"]),
    ("p-clip", "map_ops", ["qgis_clip"], None, None,
     ["clip the rivers layer to {d}", "cut the roads layer to the {d} boundary", "keep only the parts of the farms layer inside {d}"]),
    ("p-style", "map_ops", ["set_layer_style"], None, None,
     ["colour the sectors layer by NDVI", "make the farms layer red", "style the districts by rainfall"]),
    ("p-zoom-place", "map_ops", ["search_location", "zoom_to_bounds"], None, None,
     ["zoom to Volcanoes National Park", "fly to Nyungwe forest", "go to Akagera park"]),
    ("p-snapshot", "map_ops", ["render_map_snapshot"], None, None,
     ["send me a picture of this map", "export the current map as an image", "share a snapshot of the map"]),
    ("p-brain-search", "brain", ["search_brain"], None, None,
     ["what do we know about farmer cooperatives in {d}?", "search our notes on {d} maize growers", "any records about {d} irrigation?"]),
    ("p-brain-add", "brain", ["add_observation"], None, None,
     ["note that the {d} coop reported pests today", "log a field visit in {d}: beans look healthy", "record that it hailed in {d} yesterday"]),
    ("p-capabilities", "capabilities", None, None, None,
     ["what can Sage do for farmers?", "what kinds of analysis do you support?", "how can you help with insurance?"]),
    ("p-explain", "explain", None, None, None,
     ["what does SPI mean?", "explain the difference between NDVI and EVI", "why does cloud cover matter for satellite data?"]),
    ("p-thanks", "small_talk", None, None, None,
     ["thank you Sage", "ok great, thanks", "murakoze"]),
]


def build(seed: int = 7) -> list[dict]:
    rng = random.Random(seed)
    rows = []
    for intent, category, any_of, args, map_state, wordings in INTENTS:
        districts = rng.sample(DISTRICTS, len(wordings))
        for i, (wording, district) in enumerate(zip(wordings, districts), 1):
            fill = lambda s: s.replace("{d}", district)  # noqa: E731
            expect: dict = {"any_of": any_of} if any_of else {"no_tool": True}
            if args and "{d}" in wording:
                expect["args"] = {
                    tool: {param: fill(text).lower() for param, text in params.items()}
                    for tool, params in args.items()
                }
            row = {"id": f"{intent}-{i}", "intent": intent, "text": fill(wording),
                   "category": category, "stratum": "coverage", "lang": "en"}
            if map_state:
                row["map_state"] = map_state
            row["expect"] = expect
            rows.append(row)
    return rows


def main() -> None:
    rows = build()
    OUT.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n")
    print(f"wrote {OUT} ({len(rows)} cases, {len(INTENTS)} intents)")


if __name__ == "__main__":
    main()
