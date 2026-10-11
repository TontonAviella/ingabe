# Example finder rules

"Mark 3, find the rest": the user draws boxes around a few objects of one
kind on a picture, and the finder finds the other objects like them on the
same picture. One owner: `src/services/example_finder.py`, with
`src/services/example_finder_model.py` (the weights and the process the model
runs in) and `src/services/example_finder_pictures.py` (picture sources and
their coordinates). Tests: `src/services/test_example_finder.py`,
`test_example_finder_pictures.py`, `test_example_finder_model.py` (the real
model) and `src/routes/test_example_finder_routes.py`.

Switch: `EXAMPLE_FINDER` (default off), read only by
`example_finder.finder_enabled()`. Off, both routes answer 404 and nothing
else changes. `FINDER_THREADS` (default 4) sets the torch threads of the
finder process.

How far an answer about the finds may go (count, count after checking,
estimate, rough number, can't tell) is not decided here: that is
`src/services/answer_levels.py` (`docs/rules/ANSWER_LEVELS_RULES.md`). This
module finds; it shows no count and no level.

## The protocol (reproduced exactly)

The finder was measured on 2026-10-11 with `scripts/finder_eval/run_sameimage.py`
and `run_lookalike_towers.py`. The app does the same:

1. Model: YOLOE-11s segmentation (`yoloe-11s-seg`), the weights with sha256
   `8e439445...dce3e3`, a fresh model per picture (visual prompts replace the
   model's classes, so a reused model would carry the last picture's).
2. Reference picture: the same picture the objects are found in.
3. Prompts: the user's marks as class 0, plus look-alike marks as class 1
   when the target's measured settings use them (`look_alike_marks`).
4. `predict(conf=0.01, imgsz=<the target's finder_input_px>, max_det=300,
   device="cpu")` (`CONF`, `MAX_DET`).
5. Keep class 0; drop every find that overlaps one of the user's marks at
   IoU >= 0.3 (`MARK_IOU`, `boxes.iou`): that is the mark found again. Finds
   on look-alike marks are not dropped (they were not in the measurement).
6. A picture larger than the target's measured pictures (`image_px`, long
   side) is first shrunk so its long side equals `image_px` (area averaging,
   PIL BOX; the resize filter was not part of the measurement), the marks
   with it; the finds are mapped back to the picture's own pixels. A smaller
   picture is used as it is.
7. The picture goes to Ultralytics as a PIL image, which it reads like a file
   (RGB to BGR): on the tower photo both gave the same 27 boxes.

These values are the measurement, not tunables: changing one is a new
measurement in `answer_levels_targets.json`.

## Settings come from the measured table

`settings(target)` reads `image_px` (the measured pictures' long side),
`finder_input_px` and `look_alike_marks` from `answer_levels.target(...)`;
none is copied here (H2). A target the finder was never measured on (no
`finder_input_px` or `image_px`, e.g. `banana_mat`) or one measured for
another finder is refused. Targets whose level is `CANT_TELL` (tree crowns)
still run: what a reader is told about the finds is answer_levels' decision.

## Inputs (fail fast, `FinderInputError`, 422 at the route)

- 1 to 20 marks (`MIN_MARKS`, `MAX_MARKS`), 0 to 20 look-alike marks; four
  finite numbers each (left, top, right, bottom); a positive width and height.
- Every box inside the picture once mapped to its pixels (a box within
  0.01 px of the edge, from rounding a reprojected box, is pulled onto it).
- Look-alike marks only for targets measured with them (towers' antennas and
  radio units today); for others they are refused, not ignored.
- A picture is RGB with 8 bits a channel. A photo file must decode
  (JPEG, PNG, ...) and hold at most 40 million pixels (checked from its header
  before decoding); its EXIF orientation is applied, as a browser shows it.
- Marks are rounded to 0.001 px before they are used and hashed.

## Pictures (`example_finder_pictures.py`)

A source implements `async read() -> Picture` (pixels, a sha256, and the
frame its boxes are in). Two exist:

- `Photo(bytes)`: boxes in the photo's own pixels. For photo inspections (the
  inspections PR plugs its photos in here).
- `OrthophotoWindow(cog_url, bbox, crs)`: a window of a drone orthophoto's
  optimised photo, as a box in lon/lat (`"lonlat"`) or in the photo's own CRS
  (`"layer"`). It is read **through `raster_process`** (worker processes; a
  remote COG read must not run in a thread, Lessons log 2026-10-07) at the
  photo's own resolution, whole pixels covering the box, at most
  2048 x 2048 px (`MAX_WINDOW_PX`): a larger window is refused, never shrunk
  silently. Only the first three bands, 8 bits; anything else is refused. A
  lon/lat box is reprojected with 21 points an edge. Marks are given in the
  window's coordinates, and every find comes back in them (`box`) and in the
  window's pixels (`box_px`).

A window's sha256 covers its pixels, its place (CRS and transform) and the
frame of its boxes: the same pixels asked in lon/lat and in the layer's CRS
are two pictures, because their finds are written in different coordinates.

## Running it safely on a CPU server (`example_finder_model.py`)

- The model runs in **its own process** (spawn), never a thread: torch and
  Ultralytics hold the GIL and are not thread-safe. It mirrors
  `raster_process` (a module-level function, plain values in and out, the
  caller waits without the GIL) but is one long-lived process with a pipe,
  so a picture that never finishes can be killed (`WAIT_LIMIT_S`, 600 s,
  counting the send too). It stops itself after 15 idle minutes
  (`IDLE_EXIT_S`), giving its memory back; the next picture starts it again.
- One picture at a time per app process (a lock) and in the whole app (the
  Redis slot, below).
- Torch threads: `FINDER_THREADS` (default 4). Ultralytics resets torch to
  its own count (CPU count - 1) whenever it sets a model up on the CPU, so
  the finder process also sets Ultralytics' `NUM_THREADS`: before that,
  `FINDER_THREADS=1` still kept ~5.8 cores busy; after it, 1 thread used
  1 core and 4 threads 3.75.
- In the finder process only: `YOLO_OFFLINE=true` (Ultralytics 8.4 reads
  only `"true"`, not `"1"`) and `YOLO_AUTOINSTALL=False`. The app's FastSAM
  path is left as it was.
- Weights come only from object storage: key
  `models/yoloe-11s-seg-8e439445c87338b7.pt`, fetched once to
  `/tmp/ingabe_cache/models/` and refused when the sha256 differs
  (`stored_files.ensure_local_copy`, shared with the crop fingerprints).
  Nothing is downloaded from the internet at run time.

## Jobs and kept finds

- A request is identified by `content_sha`: the picture's sha256, the marks
  and look-alikes in pixels, the target, the model's sha256, the settings
  and the protocol values. It is the job id.
- The finds are kept in object storage **per project**:
  `finds/v1/<project_id>/<content_sha>.json`, with every input that made
  them, the seconds the model took and the frame. The same request in the
  same project is answered from there and never computed twice. Another
  project computes its own (no finds cross projects). The layer's project is
  `photo_context.project_of` (the most recently edited live map showing it),
  as for farm records and field checks.
- Every app worker (uvicorn runs 4) shares the jobs through Redis:
  - a lock per project and request (`example_finder:v1:<project>:<sha>:lock`,
    60 s, renewed every 15 s while the job lives): the worker that takes it
    runs the job, the others report its status;
  - a status per project and request (`...:status`): `queued` (waiting for
    the slot), `running`, `failed` (kept an hour, with the reason); the
    status is removed once the finds are kept, and `done` is read from the
    kept finds;
  - the slot (`example_finder:v1:slot`): one picture at a time in the whole
    app; a job waits up to 30 minutes for it, then fails as busy.
- The worker running a job checks the kept finds again after taking the lock
  (another worker may have finished in between). A worker that dies stops
  renewing: its lock and status expire within a minute, the job then reads as
  unknown (404) and starting it again is safe. Starting a failed request again
  retries it.
- Job states are constants in `example_finder` (`QUEUED`, `RUNNING`, `DONE`,
  `FAILED`); compare against them, never a literal.

## Routes (`src/routes/example_finder_routes.py`, adapter only)

- `POST /api/layer/{layer_id}/finder`: EDIT rights on the layer
  (`edit_layer`); body `{target, window: {bbox, crs}, marks,
  lookalike_marks}`; returns `{job_id, status}` (and `finds` when the same
  request was answered before). Rate limit: the app's limiter, the heavy
  tier (`RATE_LIMIT_HEAVY`, 10 a minute), keyed per user (the route sets
  `request.state.user_context`, which the limiter's key reads).
- `GET /api/layer/{layer_id}/finder/{job_id}`: VIEW rights (`get_layer`);
  status, and `finds` (`box`, `box_px`, `score`, best first) when done; 404
  for a job this layer's project does not have.
- 400 for a layer that is not a raster, 409 while its photo is being
  processed or when it is in no project, 422 for a request that cannot be
  answered (the reason in data terms), 502 when the photo could not be read.
- Today a project viewer or editor who does not own the layer gets 404 from
  both routes: `map_layers` row-level security shows a layer only to its
  owner, so `get_layer` finds nothing (checked in the route tests'
  exploration on 2026-10-11). Reading without starting is tested in
  `view_only` mode.

## Measured run time (2026-10-11)

In Docker on the M1 Mac (amd64 image under emulation), the 1333 x 750 tower
photo read at 1280 px: about 13 s wall (49 s CPU) a picture with
`FINDER_THREADS=4`, 26 s with 1; the first picture after the process starts
takes about 13 s more (process start, importing torch and Ultralytics). The
measurement itself ran natively on the M1 at 1.3 s a picture
(`answer_levels_evidence.json`); a real x86 server will be between the two
and has not been measured.

## Not done yet (open)

- **Inspections** (photo-of-a-photo, PR #177) are not on this branch: their
  photos plug in through `Photo` / `PictureSource` with a route of their own.
- **No caller shows answer levels** for the finds yet; a consumer must read
  `answer_levels.ladder_enabled()` and `decide` (with the size gate from the
  user's marks) before saying how far a number may go.
- **The resize filter** for pictures larger than the measured size, and
  pictures much larger than the measured ones (a 2048 px window shrunk to a
  640 px maize tile makes plants 3x smaller), were not measured; the size
  gate in answer_levels is what refuses objects too small for the finder.
- **Run time on the CPU server** is not measured.

## Gates

`src/services/test_example_finder.py`: settings from the table (no copied
numbers), input checks, the protocol around a recorded real Ultralytics result
(`test_fixtures/example_finder/recorded_yoloe_result.json`, recorded by
`scripts/finder_eval/record_test_result.py`), shrinking and mapping back, the
weights' storage key and sha256 check, `FINDER_THREADS`, the process time
limit and a model failure coming back from the process, kept finds reused,
two workers running a request once, per-project keys, failures and retries,
one picture at a time. `test_example_finder_pictures.py`: windows read
through the raster workers, frames and their round trips, refused windows,
photos. `test_example_finder_routes.py`: switch off 404, rights, kept finds,
422 reasons, the per-user rate limit. `test_example_finder_model.py` (only
with `FINDER_TEST_WEIGHTS`): the real model on the tower photo, marks not
given back, unmarked dishes found, a 2x picture giving the same finds in its
own pixels, and the recorded result reproduced.
