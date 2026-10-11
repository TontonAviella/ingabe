# Photo inspections: rules

Owner: `src/services/photo_inspections.py` (CODING_STANDARDS.md, H1). Upload wiring: `src/upload/registry.py`
(`handler_for`) and `src/upload/handlers/photo_set_handler.py`. Viewing a photo: `GET /api/layer/{layer_id}/photos/{sha256}`
in `src/routes/layer_router.py`. Switch: `PHOTO_INSPECTIONS` (off by default; read only by `photo_inspections.enabled()`).

## What becomes a photo inspection

- A ZIP with at least one photo (`.jpg`, `.jpeg`, `.png`, any case) and no `.shp` file. A zipped shapefile keeps the
  vector path exactly. Other files in the ZIP (flight logs, videos) are not unpacked and not part of the inspection.
  macOS metadata (`__MACOSX/`, `._name`) is never a photo.
- A single JPG/PNG that GDAL cannot place on the map (`gdalinfo` gives no `wgs84Extent` and no coordinate system)
  becomes a 1-photo inspection.
- With the switch off: a ZIP without a shapefile is refused as before (400), and a JPG/PNG with no map position is
  refused with 400 "This photo has no map position..." (it used to become a raster whose bounds were its pixel
  corners, e.g. `[0, 3648, 5472, 0]`, which broke the map style). Georeferenced rasters are unchanged either way.
- Out of scope for now: a dropped folder of photos (each photo becomes its own 1-photo inspection), 3D meshes.

## Limits (constants in the owner module)

- At most `MAX_PHOTOS` = 1,000 photos and `MAX_UNPACKED_BYTES` = 15 GiB unpacked per inspection, counted by
  `src/upload/archive.extract_zip` on the photos actually written (non-photo entries are not unpacked and do not count).
  Past either limit: 413 with the reason.

## Facts read from each photo

`sha256, filename, width, height, taken_at, lat, lon, altitude_m, relative_altitude_m, gimbal_pitch, gimbal_yaw,
flight_yaw, make, model`.

- Read from the photo itself: EXIF (Pillow, headers only, pixels are never decoded) and the XMP packet as text
  (DJI `drone-dji:RelativeAltitude`, `GimbalPitchDegree`, `GimbalYawDegree`, `FlightYawDegree`; any namespace prefix,
  attribute or element form). No XML parser: no entity expansion, no new dependency.
- Missing is None, never 0. A real 0 (altitude 0 m, pitch 0 degrees) is kept.
- Position: EXIF GPS latitude/longitude with their N/S and E/W references. No position (None) when the coordinates
  or a reference are missing, the receiver marked the fix void (`GPSStatus` = `V`), a value is not finite, or it is
  off the globe. A fix of exactly 0, 0 is kept as a value (it is not treated as missing).
- `altitude_m` is the GPS altitude (negative when `GPSAltitudeRef` says below sea level); `relative_altitude_m` is
  DJI's height above the take-off point.
- `taken_at` is ISO 8601 from `DateTimeOriginal` (else `DateTime`), with `OffsetTimeOriginal` when the camera wrote one;
  an unparseable date is None.
- `width`/`height` are the stored pixel size (EXIF orientation is not applied).
- A file with a photo extension that Pillow cannot read fails the upload with 400 naming the file.

## Placement and storage

- At least one photo must have a GPS position, else 400 ("None of the N photos has a GPS position..." or, for one
  photo, "This photo has no map position and no GPS position...").
- Each original is stored once, streamed from disk, at `inspections/<project_id>/<layer_id>/<sha256><ext>` (`ext` the
  original's, lower case). Identical photos share one object.
- The layer is a normal vector layer: one Point per photo with a position, properties = the facts + `photo_key`.
  Its metadata holds `{"photo_inspection": {"photos", "with_gps", "without_gps": [filenames], "storage_prefix"}}`.
- Camera points are not turned into Brain pages (they are not fields or places).

## Viewing a photo

- `GET /api/layer/{layer_id}/photos/{sha256}`: view rights on the layer's project (the layer-read dependency
  `get_layer`), `sha256` = 64 lower-case hex characters (else 400), and only a key under the prefix recorded on that
  layer and shaped `<prefix><sha256><photo ext>` is served (another layer's photo is 404). Switch off: 404.
