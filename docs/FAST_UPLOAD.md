# Fast upload workflow for drone orthophotos

Your residential uplink is ~9 Mbps. A raw 3 GB GeoTIFF takes 40+ minutes.
A WebP-compressed COG of the same orthophoto is ~600 MB and takes ~9 minutes
on the same link. Same 5 cm pixels. Same map. ¼ the bytes.

## One command before upload

```bash
gdal_translate -of COG \
  -co COMPRESS=WEBP \
  -co QUALITY=90 \
  -co BIGTIFF=YES \
  -co BLOCKSIZE=512 \
  -co OVERVIEWS=AUTO \
  Cyampirita_Orthophoto.tif \
  Cyampirita_Orthophoto.cog.tif
```

Then drag `Cyampirita_Orthophoto.cog.tif` into mundi.ai.

## What happens server-side

`src/routes/postgres_routes.py::_background_generate_cog` calls
`_is_already_cog()` on every uploaded raster. If the file is a tiled GeoTIFF
with overviews (i.e. it came out of `gdal_translate -of COG`), the server
copies it server-side to the canonical `cog/layer/{layer_id}.cog.tif` path
without re-encoding. Layer metadata gets `cog_source: client_provided`.
gdalwarp is skipped entirely.

If you upload a raw GeoTIFF, server still works — it falls back to the
gdalwarp path and produces a (less compressed) COG itself. The fast path is
purely opt-in: convert locally and you save your own upload time.

## Why WebP not DEFLATE

Visual orthophotos compress 4-5× better with WebP at quality 90 than with
DEFLATE, and the visual difference is invisible at any zoom. For analytical
rasters where you need exact pixel values (NDVI, NDRE, classification),
use `COMPRESS=DEFLATE` instead — lossless, smaller win (~2× compression).

## Compression cheat sheet

| File type | Suggested |
|---|---|
| Drone visual orthophoto | `-co COMPRESS=WEBP -co QUALITY=90` |
| Multispectral / NDVI / NDRE rasters | `-co COMPRESS=DEFLATE -co PREDICTOR=2` |
| LiDAR (.las) | `laszip input.las output.laz` (separate tool) |
| Vector | already FlatGeoBuf in mundi pipeline |

## Verify it's a COG

```bash
gdalinfo Cyampirita_Orthophoto.cog.tif | grep -iE 'block|overview'
```

You should see `Block=512x512` lines and a Image Structure metadata block
mentioning overviews.

## Why we can't ship this in the browser yet

WebAssembly GDAL exists but is too slow for 3 GB files. A future
`mundi-up.py` desktop CLI can hide this command from the user — drop, walk
away, file lands as a COG with no flags to remember. Until that ships, this
one-line is the workflow.
