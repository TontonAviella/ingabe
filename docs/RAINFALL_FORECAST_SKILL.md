# How accurate are our rainfall forecasts? (2026-10-04)

Measured with `scripts/forecast_skill.py`: the forecasts each model actually
issued 1–7 days ahead (Open-Meteo Previous Runs API, at each district's
centre), against what fell (CHIRPS v2.0 daily, averaged over the district).
Baseline: the district's normal rainfall for that day
(`src/services/monthly_rainfall_normals.json`, CHIRPS 2000–2023).

- Period: 2025-03-01 to 2026-06-30, the last CHIRPS day (426 days, three rainy seasons).
- First run: all 30 districts. Second run, which adds the fairer baselines: every second district (15, covering every zone).
- Days 8–16 cannot be measured: no archive keeps those forecasts.

## Results (15 districts, 2025-03-01 → 2026-06-30)

**Will it rain (≥ 1 mm)?** Real but moderate skill, best in the first 3 days.

| Model | Lead | Days right | Heidke skill (0 = chance) |
|---|---|---|---|
| ICON | 1 / 3 / 5 days | 74% / 73% / 71% | 0.48 / 0.45 / 0.43 |
| ECMWF IFS | 1 / 3 / 5 / 7 | 70% / 68% / 68% / 67% | 0.42 / 0.38 / 0.37 / 0.35 |
| GFS | 1 / 3 / 5 / 7 | 65% / 66% / 64% / 66% | 0.32 / 0.33 / 0.29 / 0.31 |
| Mean of the 4 models | 1 / 3 / 5 | 68% / 68% / 69% | 0.37 / 0.38 / 0.38 |

Between 28% and 43% of forecast rain days stayed dry (false alarm ratio, all-district run).

**How much rain?** No better than the normal for the time of year.

| Measure | Forecast | Normal for that time of year |
|---|---|---|
| Daily correlation with what fell (day 1, best model) | 0.36 | 0.30 |
| Daily correlation (day 5–7) | 0.19–0.26 | 0.27–0.31 |
| 7-day total, raw ECMWF: mean error / bias | 17.3 mm / **+9.7 mm (too wet)** | 11.4 mm / — |
| 7-day total, ECMWF after removing its bias (cross-validated) | 12.9 mm | 11.4 mm |
| 7-day totals within 25% (or 5 mm) | 27% | 43% |

## What this means
- Payouts must be judged on observed rainfall (satellite estimates now, gauges later), never on forecasts.
- Forecasts are useful as an early warning of rain or no rain in the next 1–3 days. ICON is the strongest model here, and the 4-model mean is not better than ICON alone.
- Forecast rainfall amounts should be shown as a range around the normal, not as a precise number. The insurance projection already uses the normal beyond day 16 (#99). Inside the 16 days, raw amounts should be bias-corrected or blended toward the normal.
- BK's "> 90% accuracy" is not achievable for forecast rainfall amounts with these models at district scale.

## Caveats
- CHIRPS is itself an estimate, and weak at daily timing (it is partly derived from infrared satellite data), which pulls every score down. A comparison with rain gauges (TAHMO, Rwanda Meteorology Agency) is the next step and would give firmer numbers.
- The forecasts are point forecasts at the district's centre, compared with district averages.
- The live app's own bias correction (`forecast_fusion.py`) is not replayed here. The cross-validated linear correction above stands in for it.
- 16 months is a short record. Results by season (A vs B) need a longer one.

## Seasonal outlook: Copernicus SEAS5, October–December (measured 2026-10-04)

Measured with `scripts/seasonal_skill.py`.
- **Forecast:** ECMWF SEAS5 total precipitation as issued on 1 October, for lead months 1–3 (October–December). It is averaged over the 16 points of the 1° grid around Rwanda.
- **Model versions:** SEAS5.1 for the 1993–2016 hindcasts and from 2023; SEAS5 for the October 2017–2022 runs, which that version issued.
- **Compared with:** CHIRPS v2.0 October–December totals, averaged over Rwanda's 30 districts, for 1993–2025 (33 seasons).

| Measure | Result |
|---|---|
| Correlation of the ensemble mean with what fell | **0.20** |
| Tercile hit rate (below / near / above normal, leave-one-out) | **30%**; chance is 33% |
| "Dry season" calls that were right | 5 of 11 |
| "Wet season" calls that were right | 4 of 11 |
| Mean seasonal total, model vs CHIRPS | ~640 mm vs ~390 mm (the model is too wet; terciles remove this) |

**What this means:**
- At Rwanda's scale, the 1 October seasonal forecast shows **no usable skill** for October–December rainfall. Its tercile calls do no better than chance.
- It must not drive payouts or trigger probabilities. At most, it can be mentioned as the large-scale outlook (for example, the ENSO/IOD state), with this skill stated alongside it.
- The insurance projection beyond day 16 already uses the CHIRPS normal (#99). These results support keeping it that way.
- Caveats:
  - 33 seasons is a small sample.
  - The model's 1° box includes parts of Lake Kivu and neighbouring countries.
  - A calibrated, downscaled product (for example ICPAC's regional outlook) could do better and is worth checking separately.
- The 2026 forecast could not be retrieved on 4 October: ECMWF publishes the October run on the 5th.
