# Does the radar-predicted NDVI z-score mean anything? (2026-10-07)

No. The insurance engine's NDVI z-score fell back on it whenever the optical
anomaly cache had nothing for the district, which has been always since the
Dagster NDVI pipeline went stale. Measured against real optical NDVI and a real
NDVI anomaly for 10 Rwanda cells, it agreed with neither, so it has been removed:
without an optical anomaly the NDVI z-score is now missing.

## What it was

`insurance_engine._sar_predicted_ndvi_z` (removed in this change) asked
`sar_ndvi.SARNDVIPredictor` for the NDVI of a box (centre +/- 0.05 degrees) today and
returned `(predicted - 0.45) / 0.15`. The predictor is a GradientBoosting model fitted once
per process, on the first box anybody asks about (an insurance report or Sage's
`predict_ndvi_from_sar`), from 180 days of Sentinel-1 backscatter paired with Sentinel-2
NDVI read by `STACService.compute_admin_ndvi`. The report labelled the result
"Sentinel-2/SAR NDVI", and every crop's `ndvi_z_score < -1.5` trigger (weight 0.8) fired on it.

## How it was measured

`scripts/sar_ndvi_skill.py`; full results in `docs/evidence/sar_ndvi_skill.json`.

- **Places**: Roger's two field cells (Kanyangese/Cyampirita, Nyarubuye/Kabarama) and one
  cell per farming zone: Kamate (Nyagatare), Kayumba (Bugesera), Kabarore (Gatsibo),
  Bukomeye (Huye), Gisesero (Musanze), Pera (Rusizi), Butunzi (Rulindo), Mujuga (Nyamagabe).
  Each box is the engine's.
- **The predictor as the app runs it**: `train_model` and `predict_ndvi`, unchanged code,
  one model per place (each place in turn as "the first box asked"), applied to all ten
  places. Only the remote reads were kept on disk.
- **Truth NDVI**: Sentinel-2 L2A over the same box, clouds masked with the scene
  classification band, from Digital Earth Africa; a date counts when >= 80% of the box is
  clear (8 dates per place, Apr-Oct 2026).
- **Truth z-score**: Digital Earth Africa `ndvi_anomaly` (monthly NDVI standardised
  anomaly, Landsat + Sentinel-2, against the 1984-2020 Landsat climatology
  `ndvi_climatology_ls`), averaged over the box, April-September 2026.

## Results

**Predicted NDVI against clear-sky optical NDVI**

| Prediction | Pairs | Mean error | Bias | Correlation |
|---|---|---|---|---|
| Model trained at another place (what every report after the first got) | 711 | 0.120 | -0.053 | 0.23 |
| Model trained at the same place (in-sample) | 79 | 0.061 | -0.045 | 0.66 |
| VH/VV mapping used when training fails | 79 | 0.109 | -0.063 | 0.32 |
| The constant 0.45 | 79 | 0.107 | +0.014 | 0.00 |

For the same place and date, the ten possible "first" models disagree by a median of
0.315 NDVI. A GradientBoosting model never predicts outside the range of its training
labels, so the first box decides what every report can say: trained at Kanyangese
(labels 0.28-0.41) every z-score lies between -1.10 and -0.30, the trigger never fires and
vegetation always reads below normal; trained at Kamate (labels 0.20-0.25) every z-score
lies between -1.68 and -1.30.

**The engine's z-score against the real anomaly** (other-place models, 6 months x 10 places)

| | Value |
|---|---|
| Pairs | 531 |
| Mean error / bias | 0.66 / +0.20 |
| Correlation | -0.01 |
| Trigger (< -1.5): both / radar only / real only / neither | 0 / 12 / 18 / 501 |

The real climatology is not 0.45 +/- 0.15: its monthly means over these boxes run from
0.31 to 0.72 and its per-pixel standard deviations from 0.06 to 0.12. In September 2026
the real anomaly was between -0.45 and -1.32 at all ten cells (none below -1.5).

**Could better training fix it?** The same radar features with clean labels (the
cloud-masked NDVI of each clear date), pooled across places:

| Model | Mean error | Correlation | z-score vs optical z-score (same climatology) |
|---|---|---|---|
| Pooled, tested on a place it never saw | 0.058 | 0.81 | r 0.01, mean error 0.74; trigger: 2 both, 11 radar only, 5 optical only |
| Per place, tested on a date it never saw | 0.049 | 0.84 | r -0.03, mean error 0.62; trigger: 3 both, 12 radar only, 4 optical only |

Clean labels make the radar follow the NDVI level (the dry-season decline from about 0.6
to 0.3), but not the departure from normal for the month, which is what a z-score and its
trigger need. One season of ten places cannot show an anomaly skill that is not there;
proving one would need several seasons, including a real drought.

## Faults found in the path

1. **Labels are not cloud-masked** (not "from the tile centre": `compute_admin_ndvi` reads
   the box). On clear dates they match the masked NDVI within 0.01 (154 labels); on partly
   cloudy dates they read 0.11 low (35 labels, 18%).
2. **One model for the whole country**, trained on whichever box is asked first, shared
   with Sage's `predict_ndvi_from_sar`.
3. **Invented baseline**: z = (NDVI - 0.45) / 0.15.
4. **Training reads the 50 oldest radar scenes** of the 180 days: for 4 of the 10 boxes they
   end on 19-29 July while the labels run to 8 September, so August labels were paired with
   July radar or dropped.
5. **Training labels cover a few weeks**: `max_scenes=20` scenes with <= 30% cloud gave
   Jul 30 - Sep 8 on boxes two tiles cover, each date counted twice (one per tile), so the
   cross-validation scores (RMSE 0.02-0.14) compare a date with itself.
6. **Prediction reads the 10 oldest radar scenes** of the last 30 days (a box has 6-18).
7. **The report date was ignored**: the prediction was always for today.

## What changed

`compute_insurance_intelligence` no longer reads the radar predictor. The NDVI z-score is
the district's optical anomaly (`anomaly_alerts_cache`) or missing; a missing signal's
trigger is left out and the report's confidence counts it as missing, as for the other
signals. Before/after on fixed inputs: `docs/evidence/insurance_ndvi_z_before_after.json`.

## Still open

- `anomaly_alerts_cache` itself is not a climatology z-score: `weekly_anomaly_scan` writes
  only alerts (z < -2 against the mean of the last 8 weeks), so its 30-day average is
  always below the trigger once any alert exists, and the Dagster pipeline is stale.
- Digital Earth Africa's monthly `ndvi_anomaly` is a real, published NDVI z-score (about a
  week after each month ends) and is the obvious candidate source.
- Sage's `predict_ndvi_from_sar` still answers from the same predictor.
