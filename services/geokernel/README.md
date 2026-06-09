# mundi-geokernel

`mundi-geokernel` is the Rust sidecar for geospatial operations that should not
sit in the Python request path. Its first production-shaped endpoint accelerates
admin-boundary to H3 overlap generation with `h3o` and `geo`.

The app uses it opportunistically:

1. If `GEOKERNEL_URL` is set, `/api/rwanda/grid/h3/admin-polyfill` calls
   `POST /admin/h3-overlap`.
2. If the sidecar is unavailable or rejects a geometry, the app falls back to the
   existing Python/Shapely/H3 implementation.

## Endpoints

- `GET /healthz`
- `POST /admin/h3-overlap`

The overlap endpoint accepts the same core fields as the Python
`AdminH3Options` path:

```json
{
  "geojson": { "type": "FeatureCollection", "features": [] },
  "resolution": 9,
  "admin_level": "district",
  "id_property": "district_id",
  "name_property": "district_name",
  "max_hexes": 50000,
  "min_overlap_ratio": 0.0,
  "include_geometry": true,
  "containment_mode": "centroid"
}
```

`containment_mode` can be `centroid` for Python-compatible candidate selection
or `intersects` for edge-complete coverage.
