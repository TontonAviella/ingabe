# mundi-rasterd

`mundi-rasterd` is the first Rust sidecar for fast uploaded raster tile serving.
It is intentionally small:

- HTTP endpoint for raster XYZ tiles
- GDAL-backed RGB/RGBA reads from WebMercator COGs
- In-memory LRU tile cache
- Optional `forge3d-cog` compile marker for the upstream Forge3D raster
  direction; current raster tiles are still GDAL-backed

The goal is to move Ingabe's heaviest drone orthophoto path away from per-tile
Python `rio-tiler` rendering. The sidecar currently handles COGs already warped
to `EPSG:3857`; unsupported inputs fall back to the existing Python path.
Active Forge3D use currently lives in the Python impact-map adapter, where it
constructs Forge3D scene/layer objects for Sphere flood results.

## API

```text
GET /healthz
GET /tiles/{z}/{x}/{y}.png?url=<signed-cog-url>&layer_id=<id>&bands=1,2,3
GET /debug/cache
```

The FastAPI app generates the signed COG URL and delegates to this service when
`RASTER_TILE_ENGINE_URL` is set.

## Local Run

```bash
docker compose --profile rasterd up -d rasterd
RASTER_TILE_ENGINE_URL=http://rasterd:8877 docker compose up -d --force-recreate app
```

This does not require a server GPU. Browser MapLibre still uses the user's GPU
for drawing; this sidecar focuses on faster CPU tile extraction and caching.
