"""Generate cases/multi_turn.jsonl and cases/chains.jsonl.

Run: python evals/sage_routing/build_conversations.py

multi_turn: a follow-up whose meaning depends on earlier turns ("do the same
for Huye"). The history is real chat-completion structure: user, assistant
tool call, tool result, assistant reply.

chains: requests that need several tools. The runner executes the loop with
the stub results below instead of real tools (no side effects, no data
access) and checks that every ``must_call`` tool is called within
``max_steps`` model calls.
"""
from __future__ import annotations

import json
from pathlib import Path

CASES = Path(__file__).resolve().parent / "cases"
RASTER_MAP = {"layers": [{"name": "Farm_A_Orthophoto", "type": "raster"},
                         {"name": "Farm_B_Orthophoto", "type": "raster"}]}


def turn(n: int, user: str, tool: str, args: dict, result: dict, reply: str) -> list[dict]:
    call_id = f"call_eval_{n}"
    return [
        {"role": "user", "content": user},
        {"role": "assistant", "content": None, "tool_calls": [{
            "id": call_id, "type": "function",
            "function": {"name": tool, "arguments": json.dumps(args)},
        }]},
        {"role": "tool", "tool_call_id": call_id, "content": json.dumps(result)},
        {"role": "assistant", "content": reply},
    ]


def say(user: str, reply: str) -> list[dict]:
    return [{"role": "user", "content": user}, {"role": "assistant", "content": reply}]


NDVI_HUYE = turn(1, "what is the NDVI in Huye district?", "get_ndvi_stats", {"district": "Huye"},
                 {"status": "success", "district": "Huye", "mean_ndvi": 0.58, "week": "2026-W39"},
                 "Huye's mean NDVI this week is 0.58, healthy for the season.")
DROUGHT_BUG = turn(2, "is Bugesera in drought?", "get_drought_status", {"district": "Bugesera"},
                   {"status": "success", "district": "Bugesera", "drought_status": "moderate", "vci": 31},
                   "Bugesera shows moderate drought (VCI 31).")
FORECAST_MUS = turn(3, "will it rain in Musanze next week?", "get_forecast", {"district": "Musanze"},
                    {"status": "success", "district": "Musanze", "total_precip_mm_7d": 42.0},
                    "About 42 mm of rain is expected in Musanze over the next 7 days.")
SECTORS_NYZ = turn(4, "show me the sectors of Nyanza", "show_admin_boundary",
                   {"admin_level": "sector", "name": "*", "district": "Nyanza"},
                   {"status": "success", "layer_id": "Lsect0nyanza", "features": 10},
                   "I added Nyanza's 10 sectors to the map.")
HOUSES_A = turn(5, "count the houses in Farm_A_Orthophoto", "analyze_raster_object_candidates",
                {"layer_id": "Lfarmaortho1", "target_classes": ["building"]},
                {"status": "success", "summary": {"candidate_count": 214}},
                "I found 214 likely buildings in Farm_A_Orthophoto and drew them on the map.")
SITUATION_NYG = turn(6, "agricultural situation report for Nyagatare", "get_insurance_intelligence",
                     {"district": "Nyagatare"},
                     {"status": "success", "district": "Nyagatare", "overall_status": "watch"},
                     "Nyagatare is on watch: rainfall is below normal, vegetation is still near average.")
BRAIN_KABEZA = turn(7, "what do we know about the Kabeza cooperative?", "search_brain", {"query": "Kabeza cooperative"},
                    {"status": "success", "results": [{"slug": "coop-kabeza", "title": "Kabeza cooperative"}]},
                    "Kabeza is a maize cooperative in Kayonza with 120 members.")

# (id, intent, category, history, text, expect, map_state)
MULTI_TURN = [
    ("mt-ndvi-same-for", "mt-same-for-place", "ndvi", NDVI_HUYE, "do the same for Nyanza",
     {"any_of": ["get_ndvi_stats", "get_agri_indices"], "args": {"get_ndvi_stats": {"district": "nyanza"}}}, None),
    ("mt-ndvi-what-about", "mt-same-for-place", "ndvi", NDVI_HUYE, "and Gisagara?",
     {"any_of": ["get_ndvi_stats", "get_agri_indices"], "args": {"get_ndvi_stats": {"district": "gisagara"}}}, None),
    ("mt-drought-same-for", "mt-same-for-place", "drought", DROUGHT_BUG, "what about Kirehe?",
     {"any_of": ["get_drought_status", "get_insurance_intelligence"], "args": {"get_drought_status": {"district": "kirehe"}}}, None),
    ("mt-forecast-same-for", "mt-same-for-place", "weather", FORECAST_MUS, "same for Rubavu please",
     {"any_of": ["get_forecast"]}, None),
    ("mt-sectors-same-for", "mt-same-for-place", "admin_display", SECTORS_NYZ, "now do Huye",
     {"any_of": ["show_admin_boundary", "new_layer_from_postgis"]}, None),
    ("mt-houses-other-raster", "mt-same-for-place", "ortho_objects", HOUSES_A, "now the other orthophoto",
     {"any_of": ["analyze_raster_object_candidates", "create_raster_h3_context_layer"]}, RASTER_MAP),
    ("mt-situation-same-for", "mt-same-for-place", "insurance", SITUATION_NYG, "do Gatsibo too",
     {"any_of": ["get_insurance_intelligence"], "args": {"get_insurance_intelligence": {"district": "gatsibo"}}}, None),
    ("mt-ndvi-then-sectors", "mt-drill-down", "ndvi", NDVI_HUYE, "break that down by sector",
     {"any_of": ["get_cell_ndvi_stats", "get_agri_indices"], "args": {"get_cell_ndvi_stats": {"district": "huye"}}}, None),
    ("mt-drought-then-forecast", "mt-follow-on-question", "weather", DROUGHT_BUG, "will rain come soon there?",
     {"any_of": ["get_forecast"]}, None),
    ("mt-drought-then-payout", "mt-follow-on-question", "insurance", DROUGHT_BUG, "does that trigger the maize insurance?",
     {"any_of": ["get_insurance_intelligence", "evaluate_insurance_trigger"]}, None),
    ("mt-forecast-then-impact", "mt-follow-on-question", "weather", FORECAST_MUS, "what will that rain do to crops there?",
     {"any_of": ["analyze_expected_rain_impact", "get_insurance_intelligence"]}, None),
    ("mt-houses-then-area", "mt-follow-on-question", "ortho_facts", HOUSES_A, "and how many hectares is that image?",
     {"any_of": ["describe_user_raster"]}, RASTER_MAP),
    ("mt-sectors-then-ndvi", "mt-follow-on-question", "ndvi", SECTORS_NYZ, "which of those sectors has the lowest NDVI?",
     {"any_of": ["get_cell_ndvi_stats", "get_agri_indices", "query_postgis_database"]}, None),
    ("mt-brain-then-add", "mt-follow-on-question", "brain", BRAIN_KABEZA, "add a note that they finished harvesting today",
     {"any_of": ["add_observation"], "args": {"add_observation": {"slug": "kabeza"}}}, None),
    ("mt-correction-place", "mt-correction", "ndvi", NDVI_HUYE, "sorry, I meant Nyaruguru",
     {"any_of": ["get_ndvi_stats", "get_agri_indices"], "args": {"get_ndvi_stats": {"district": "nyaruguru"}}}, None),
    ("mt-correction-raster", "mt-correction", "ortho_objects", HOUSES_A, "no, I meant Farm_B_Orthophoto",
     {"any_of": ["analyze_raster_object_candidates", "create_raster_h3_context_layer"]}, RASTER_MAP),
    ("mt-thanks-after-tool", "mt-text-reply", "small_talk", NDVI_HUYE, "great, thanks!", {"no_tool": True}, None),
    ("mt-explain-after-tool", "mt-text-reply", "explain", DROUGHT_BUG, "what does VCI 31 mean?", {"no_tool": True}, None),
    ("mt-greeting-then-ask", "mt-after-small-talk", "weather",
     say("hi Sage", "Hello! How can I help with your fields today?"),
     "is it going to rain in Ngoma this weekend?", {"any_of": ["get_forecast"]}, None),
    ("mt-capabilities-then-ask", "mt-after-small-talk", "ndvi",
     say("what can you do?", "I can show places on the map, analyse weather, vegetation, drought and drone images."),
     "ok, show me NDVI for Kayonza", {"any_of": ["get_ndvi_stats", "get_agri_indices", "compute_spectral_index"]}, None),
]

# Reasonable opening moves beyond the chain's own tools and stubbed tools.
EXTRA_FIRST_STEPS = {
    "ch-sectors-ndvi": ["new_layer_from_postgis", "get_agri_indices"],
    "ch-forecast-two-places": ["search_location"],
    "ch-flood-and-note": ["search_location"],
}

# (id, intent, category, text, must_call, max_steps, stubs, map_state)
CHAINS = [
    ("ch-rain-impact", "ch-forecast-then-impact", "weather",
     "Will this week's rain hurt crops in Bugesera? Map the risk.",
     ["get_forecast", "analyze_expected_rain_impact"], 5,
     {"get_forecast": {"status": "success", "district": "Bugesera", "total_precip_mm_24h": 38, "total_precip_mm_72h": 95,
                       "bbox": "30.0,-2.4,30.4,-2.1"}}, None),
    ("ch-drought-and-rain", "ch-drought-and-forecast", "drought",
     "Is Kayonza in drought, and will it rain there next week?",
     ["get_drought_status", "get_forecast"], 4,
     {"get_drought_status": {"status": "success", "district": "Kayonza", "drought_status": "severe", "vci": 18}}, None),
    ("ch-yield-and-stage", "ch-two-district-reads", "crops",
     "For Gatsibo, what's the maize yield risk and what growth stage are crops at?",
     ["get_yield_risk", "get_crop_growth_stage"], 4, {}, None),
    ("ch-insurance-and-accuracy", "ch-situation-and-accuracy", "insurance",
     "Give me the maize insurance situation in Nyagatare and how reliable the rainfall data is there.",
     ["get_insurance_intelligence", "get_insurance_accuracy"], 4, {}, None),
    ("ch-point-soil", "ch-geocode-then-soil", "soil",
     "Which sector is -2.60, 29.74 in, and what is the soil like there?",
     ["reverse_geocode_coordinates", "get_soil_properties"], 4,
     {"reverse_geocode_coordinates": {"status": "success", "district": "Huye", "sector": "Tumba"}}, None),
    ("ch-sat-find-display", "ch-search-then-display", "satellite",
     "Find the clearest recent Sentinel-2 scene for Musanze and display it.",
     ["search_satellite_imagery", "display_satellite_layer"], 4,
     {"search_satellite_imagery": {"status": "success", "scenes": [
         {"id": "S2B_35MRU_20260921", "date": "2026-09-21", "cloud_cover": 4.1, "bbox": "29.4,-1.6,29.8,-1.3"}]}}, None),
    ("ch-weather-and-accuracy", "ch-weather-and-accuracy", "weather",
     "How much did it rain in Huye last month, and how accurate were the forecasts?",
     ["get_weather_stats", "get_forecast_accuracy"], 4, {}, None),
    ("ch-drought-dry-spells", "ch-drought-and-dry-spells", "drought",
     "Check drought in Bugesera and list this season's dry spells.",
     ["get_drought_status", "detect_dry_spells"], 4, {}, None),
    ("ch-anomaly-then-ndvi", "ch-anomaly-then-detail", "ndvi",
     "Show the latest vegetation anomaly alerts, then the NDVI for the worst-hit district.",
     ["get_anomaly_alerts", "get_ndvi_stats"], 4,
     {"get_anomaly_alerts": {"status": "success", "alerts": [
         {"district": "Kirehe", "severity": "high", "ndvi_drop": 0.21},
         {"district": "Ngoma", "severity": "medium", "ndvi_drop": 0.09}]}}, None),
    ("ch-brain-then-add", "ch-brain-lookup-then-note", "brain",
     "Look up our notes on the Kabeza cooperative and record that they harvested beans today.",
     ["search_brain", "add_observation"], 4,
     {"search_brain": {"status": "success", "results": [{"slug": "coop-kabeza", "title": "Kabeza cooperative"}]}}, None),
    ("ch-houses-and-area", "ch-ortho-count-and-area", "ortho_objects",
     "Count the houses in Farm_A_Orthophoto and tell me how many hectares it covers.",
     ["analyze_raster_object_candidates", "describe_user_raster"], 4, {}, RASTER_MAP),
    ("ch-flood-and-note", "ch-flood-then-note", "satellite",
     "Map the flood extent in Nyabihu after the May rains and log it in our notes for Nyabihu.",
     ["detect_flood_extent", "add_observation"], 4, {}, None),
    ("ch-locate-then-ndvi-map", "ch-locate-then-index", "ndvi",
     "Find Rugezi wetland and put an NDWI water map over it.",
     ["compute_spectral_index"], 4,
     {"search_location": {"status": "success", "name": "Rugezi", "bbox": [29.80, -1.42, 29.92, -1.30]}}, None),
    ("ch-sectors-ndvi", "ch-boundary-then-index", "ndvi",
     "Show Huye's sectors on the map and tell me which has the lowest NDVI.",
     ["get_cell_ndvi_stats"], 5, {}, None),
    ("ch-forecast-two-places", "ch-two-place-forecast", "weather",
     "Compare next week's rain forecast for Musanze and Rubavu.",
     ["get_forecast"], 4, {}, None),
]


def main() -> None:
    CASES.mkdir(exist_ok=True)
    mt_rows = []
    for cid, intent, category, history, text, expect, map_state in MULTI_TURN:
        row = {"id": cid, "intent": intent, "text": text, "category": category,
               "stratum": "coverage", "lang": "en", "history": history}
        if map_state:
            row["map_state"] = map_state
        row["expect"] = expect
        mt_rows.append(row)
    ch_rows = []
    for cid, intent, category, text, must_call, max_steps, stubs, map_state in CHAINS:
        row = {"id": cid, "intent": intent, "text": text, "category": category,
               "stratum": "coverage", "lang": "en"}
        if map_state:
            row["map_state"] = map_state
        # The first step must be one of the chain's tools; the chain check
        # then requires all of them.
        first = set(must_call) | set(stubs) | set(EXTRA_FIRST_STEPS.get(cid, ()))
        row["expect"] = {"any_of": sorted(first)}
        row["chain"] = {"must_call": must_call, "max_steps": max_steps, "stubs": stubs}
        ch_rows.append(row)
    for name, rows in (("multi_turn", mt_rows), ("chains", ch_rows)):
        path = CASES / f"{name}.jsonl"
        path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n")
        print(f"wrote {path} ({len(rows)} cases)")


if __name__ == "__main__":
    main()
