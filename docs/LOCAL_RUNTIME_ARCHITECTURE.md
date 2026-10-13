# Local Runtime Architecture Decision

## Decision

Ingabe uses a hybrid architecture:

1. Deterministic fast routes handle known admin, raster fact, and FastSAM mask
   requests before any general agent is constructed.
2. FastSAM is the selected local orthophoto segmentation engine. Building
   candidates must have confidence strictly greater than `0.65`.
3. GeoLibre Rust/WASM remains the deterministic geoprocessing toolbox for
   raster/vector conversion, spectral indices, terrain, hydrology,
   GeoParquet, and PMTiles work.
4. Life-Harness remains the lightweight procedural guard around tool use.
5. Sage plans every turn the fast routes do not answer on the app's own chat
   loop. The Hermes Agent runtime was removed on 2026-10-12: it was off in
   production and on the Mac at removal (switched on only for local validation
   runs); cold agent construction took about `8.8s`, and its callback route was
   disabled (HTTP 503) in normal runs.
6. HarnessX, the davidondrej skills snapshot, and segment-geospatial/SamGeo are
   not added to the application runtime. They duplicate existing layers or do
   not contain Ingabe-specific GIS procedures.
7. forge3d is not a direct dependency. It may return later behind a dedicated
   3D viewer/export feature; it is not part of FastSAM accuracy or MapLibre
   mask delivery.

## Measured Evidence

- FastSAM model load: about `0.79s` on the local app container.
- Life-Harness retrieval: about `33us` per call; argument validation: about
  `5us` per call.
- GeoLibre smoke: 747 registered tools; vector-to-GeoParquet and NDVI raster
  workflows both passed. The two-workflow suite took about `17-19s`.
- HarnessX is a beta second agent runtime with its own providers, tools,
  sandbox, memory, UI, and 30+ dependencies. That is replacement-scale, not a
  small sharpening layer.
- SamGeo recommends a capable GPU for large rasters and duplicates the chosen
  FastSAM path.

Run `scripts/runtime-audit.sh` for status.
