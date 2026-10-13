# FastSAM Evaluation

Date: 2026-07-12

## Decision

- FastSAM remains the local orthophoto mask engine. Building requests use a
  4-million-pixel sample and direct FastSAM object masks only.
- The displayed `confidence` value is a geometry and image-evidence screening
  score. It is not a learned FastSAM building probability. Building candidates
  must remain strictly above `0.65`.
- Deterministic Rwanda boundary and FastSAM routes run before the model plans
  a turn, because a planner cannot improve segmentation pixels and should not
  add latency to known procedures.
- SAMGeo, Clay, TerraMind, and HarnessX are not part of this runtime.

## Full Orthophoto Benchmark

Cyampirita was evaluated against 381 OpenStreetMap building footprints inside
the image bounds. OSM is incomplete and can be stale, so this is a repeatable
proxy benchmark, not survey ground truth.

| Pipeline | Candidates | Precision | Recall | F1 | Native elapsed |
| --- | ---: | ---: | ---: | ---: | ---: |
| Previous 2M direct plus color supplement | 326 | 0.626 | 0.609 | 0.617 | 48.1 s in emulated container |
| Previous direct masks only | 231 | 0.792 | 0.546 | 0.646 | included above |
| Color supplement alone | 95 | 0.221 | 0.073 | 0.110 | included above |
| New 4M direct masks only | 354 | 0.757 | 0.703 | 0.729 | 39.1 s on native Apple Silicon |

The color supplement was removed because its precision and recall were both
poor. Increasing detail recovered more direct masks and improved the balanced
F1 score. This is still candidate screening, not a final building inventory.

## Next Accuracy Step

FastSAM is class-agnostic. Further material improvement requires either local
reference footprints or a compact aerial-building classifier/segmenter trained
for Rwandan roof appearance. Adding another general agent does not solve this
vision limitation.
