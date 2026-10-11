# Finder evaluation (YOLOE visual prompts)

The scripts that produced the numbers in `src/services/answer_levels_evidence.json` and
`src/services/answer_levels_targets.json` (measured 2026-10-11 on a Mac M1 CPU). They are kept so the
numbers can be re-run; they are not part of the app.

Score: AP50 (boxes matched to human boxes at IoU 0.5; 1.0 is perfect). P/R are reported at the best-F1 score
cut chosen on the test pictures, which flatters every method equally.

## Data (none of it is in the repo)
| Task | Source | Licence |
|---|---|---|
| tower_antenna / tower_mw / tower_rru | Roboflow "Cell Tower Detection v1" COCO, resized to 1333 px (`TOWERS_COCO_DIR`) | MIT (per the Roboflow export) |
| kenol_building | Kenya Space Agency "Kenol section" drone tiles, 8.8 cm (source.coop/ksa/kenol-section) | CC BY-SA 4.0 |
| field_weed / field_cotton | weed/cotton UAV tiles (YOLO labels; many weeds unlabelled, so precision reads low) | as supplied by Roger |
| maize_nerac / maize_selommes | INRAe UAVmulticrop (zenodo.org/records/4890370), 640 px tiles | CC BY 4.0 |
| trees_savanna / trees_forest | NeonTreeEvaluation test split (huggingface.co/datasets/CanopyRS/NeonTreeEvaluation) | CC BY 4.0 |

## Layout and order
Set `FINDER_EVAL_DIR` to a work folder holding `raw/` (the downloads), `w/` (weights: `yoloe-11s-seg.pt`,
`yoloe-26s-seg.pt`, `yoloe-26n-seg.pt`) and receiving `tasks/` and `results/`.

1. `prep.py`, `prep_weeds.py`, `prep_trees.py`, `prep_maize.py` build `tasks/<task>.json` (pool and test pictures with
   human boxes; pool and test come from different photos, tiles or fields).
2. `run_yoloe.py` — marks from OTHER pictures (1/5/10 marks, 3 random draws) -> `results/scores.jsonl`.
3. `run_sameimage.py` — 3 marks on the SAME picture, the finder finds the rest -> `results/sameimage.jsonl`.
4. `run_scale.py` — the same at a larger finder input size (zoom) -> `results/scale.jsonl`.
5. `run_lookalike_towers.py`, `run_contrast.py` — the target's marks plus marks of its look-alike -> `results/*.jsonl`.
6. `run_yoloe26.py` — YOLOE-11s vs YOLOE-26s/26n on the same pictures and marks -> `results/yoloe26.jsonl`.
7. `record_test_result.py` — not a measurement: records one real result on the committed tower photo for the app's
   example finder tests (`test_fixtures/example_finder/recorded_yoloe_result.json`); runs inside the app image.

Run them one at a time: each loads a model on the CPU; parallel runs on a 16 GB Mac swapped and skewed timings.
