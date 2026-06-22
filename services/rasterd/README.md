# mundi-rasterd

`mundi-rasterd` is the first Rust sidecar for fast uploaded raster tile serving.
It is intentionally small:

- HTTP endpoint for raster XYZ tiles
- GDAL-backed RGB/RGBA reads from WebMercator COGs
- Raw uploaded GeoTIFF fallback through per-tile GDAL warp
- Dataset-handle cache so repeated raw drone tile requests avoid reopening the file
- In-memory LRU tile cache
- Optional `forge3d-cog` compile marker for the upstream Forge3D raster
  direction; current raster tiles are still GDAL-backed

The goal is to move Ingabe's heaviest drone orthophoto path away from per-tile
Python `rio-tiler` rendering. The sidecar handles optimized WebMercator COGs
directly and can also render raw uploaded GeoTIFFs immediately by warping only
the requested 256px tile. COG generation is still valuable, but it is no longer
the gate before a drone raster can appear on the map.
Active Forge3D use currently lives in the Python impact-map adapter, where it
constructs Forge3D scene/layer objects for Sphere flood results.

## API

```text
GET /healthz
GET /tiles/{z}/{x}/{y}.png?url=<signed-raster-url-or-vsis3-path>&layer_id=<id>&bands=1,2,3
GET /debug/cache
```

The FastAPI app delegates uploaded raster XYZ tiles to this service when
`RASTER_TILE_ENGINE_URL` is set. With `RASTER_TILE_ENGINE_DIRECT_S3=1`, the app
passes `/vsis3/<bucket>/<key>` directly; otherwise it passes a short-lived
presigned URL.

## Local Run

```bash
docker compose --profile rasterd up -d rasterd
RASTER_TILE_ENGINE_URL=http://rasterd:8877 docker compose up -d --force-recreate app
```

This does not require a server GPU. Browser MapLibre still uses the user's GPU
for drawing; this sidecar focuses on faster CPU tile extraction and caching.
