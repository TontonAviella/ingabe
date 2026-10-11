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
`finder_input_px`, `look_alike_marks` and `view` (which way the measured
pictures looked) from `answer_levels.target(...)`; none is copied here (H2).
A target the finder was never measured on (no `finder_input_px`, `image_px`
or `view`, e.g. `banana_mat`) or one measured for another finder is refused.

## Only pictures like the measured ones (`check_picture`)

The finder runs only where its measurement holds; anything else is refused
with the reason (`FinderInputError`, 422), never run and never kept:

- **Taken the same way.** Every picture says which way it looks
  (`answer_levels.OBLIQUE` or `NADIR`): an orthophoto window looks straight
  down; a photo's caller says it (DJI's gimbal pitch, read by
  `photo_inspections`, tells it). Tower dishes, antennas and radio units were
  measured on oblique tower photos, so they are refused on orthophotos;
  buildings, crops, weeds and tree crowns were measured on pictures looking
  straight down, so they are refused on oblique photos.
- **Marked objects at least as big as measured.** `answer_levels.size_gate`
  with the picture's long side and the marks' widths and heights in its
  pixels: the median mark, as the finder reads the picture (long side scaled
  to `finder_input_px`; shrinking to `image_px` first does not change this),
  must reach the target's size floor (the smallest marked size among its
  measured pictures). Example: 40 px maize plants in a 2048 px window are read
  at 25 px, under the 34 px maize was measured from, so the window is
  refused; in a 600 px window the same plants are read at 85 px and the
  finder runs. A target with no measured size (tree crowns) cannot be checked,
  so it is refused too. The passing gate is kept with the finds
  (`size_gate`: `object_px`, `needed_px`, `reason`).

## Inputs (fail fast, `FinderInputError`, 422 at the route)

- 1 to 20 marks (`MIN_MARKS`, `MAX_MARKS`), 0 to 20 look-alike marks; four
  finite numbers each (left, top, right, bottom); a positive width and height.
- Every box inside the picture once mapped to its pixels (a box within
  0.01 px of the edge, from rounding a reprojected box, is pulled onto it).
- Look-alike marks only for targets measured with them (towers' antennas and
  radio units today); for others they are refused, not ignored.
- The picture taken the way the target was measured, and the marks at least
  as big as measured (above).
- A picture is RGB with 8 bits a channel. A photo file must decode
  (JPEG, PNG, ...) and hold at most 40 million pixels (checked from its header
  before decoding); its EXIF orientation is applied, as a browser shows it.
- Marks are rounded to 0.001 px before they are used and hashed.

## Pictures (`example_finder_pictures.py`)

A source implements `async read() -> Picture` (pixels, a sha256, and the
frame its boxes are in). Two exist:

- `Photo(bytes, view)`: boxes in the photo's own pixels; `view` is said by
  the caller, never guessed. For photo inspections (their photos plug in
  here).
- `OrthophotoWindow(cog_url, bbox, crs)`: a window of a drone orthophoto's
  optimised photo (looking straight down, `NADIR`), as a box in lon/lat
  (`"lonlat"`) or in the photo's own CRS
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
  counting the send too).
- **One finder process in the whole app.** It peaks at 0.9-1.4 GB reading at
  640-1280 px and 2.8-3.3 GB at 2560 px (radio units), and keeps 0.9-1.5 GB
  after a picture (table below). After each picture, still holding the slot,
  the job stops its worker's finder process (`example_finder_model.stop`)
  unless the next job in line is one of the same worker's; so only the
  process of the worker at the front of the line lives, and the app's finder
  memory is one process at its peak (up to ~3.3 GB), not one per uvicorn
  worker (4). The price: a job that follows another worker's job starts a
  new process (13-50 s more in the measurement below). A process with no
  picture for 2 minutes also stops itself (`IDLE_EXIT_S`, the fallback).
- One picture at a time per app process (a lock) and in the whole app (the
  line and the slot, below).
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
  - a status per project and request (`...:status`): `queued` (waiting in
    line), `running`, `failed` (kept an hour, with the reason); the status is
    removed once the finds are kept, and `done` is read from the kept finds;
  - **the line** (`example_finder:v1:queue`, a sorted set of places
    `[user, project, job]` scored by a ticket from `...:ticket`): first come,
    first served for the whole app; a job runs only at the front. A job
    joins after taking its lock and leaves before its finds show (or when it
    fails); a place whose lock has expired (its worker died) is removed by
    whoever meets it, so it neither blocks the line nor counts.
  - **places are capped**: `MAX_JOBS_PER_USER` (3) waiting or running per
    user, `MAX_JOBS` (10) for the whole app, counted and taken in one Lua step
    so two workers cannot both take the last place. A full line refuses at
    once (`FinderBusy`: 429 for the user's own places, checked first; 503 for
    the app's), nothing is queued, and asking again later works. The slowest
    measured setting (radio units at 2560 px, a square 1333 px picture) took
    137 s in a new process, so a full line drains in at most about 23 minutes;
    no job waits behind more than 9 others, and no user can fill the line;
  - the slot (`example_finder:v1:slot`): taken by the job at the front, one
    picture at a time in the whole app even if a front job lost its place; a
    job waits up to 30 minutes in line (a backstop), then fails as busy.
- **A waiting job keeps only the picture the finder is given**: the window is
  shrunk to the target's `image_px` before the job joins the line (at most
  1333 x 1333 x 3 bytes, 5.3 MB, for towers; 640 x 640 x 3, 1.2 MB, for
  crops), never the full window (up to 2048 x 2048 x 3, 12.6 MB).
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
  `request.state.user_context`, which the limiter's key reads). 429 when the
  user already holds `MAX_JOBS_PER_USER` places in line, 503 when the app's
  `MAX_JOBS` are taken.
- `GET /api/layer/{layer_id}/finder/{job_id}`: VIEW rights (`get_layer`);
  status, and `finds` (`box`, `box_px`, `score`, best first) when done; 404
  for a job this layer's project does not have.
- 400 for a layer that is not a raster, 409 while its photo is being
  processed or when it is in no project, 422 for a request that cannot be
  answered (the reason in data terms), 502 when the photo could not be read.
- **Gap, for Roger to decide:** the spec gives project editors EDIT and
  project viewers VIEW rights, but today a project editor or viewer who does
  not own the layer gets 404 from both routes: `map_layers` row-level
  security (alembic `c1d2e3f4a5b9`) shows a layer only to its owner, so
  `get_layer` finds nothing before any rights check runs. This is app-wide
  (every route on `get_layer`), so it is not changed here. The route tests
  pin today's 404 for a viewer who starts (`finder.calls == []`) and state the
  spec as strict xfail tests (`test_spec_*`: viewer POST 403, editor POST 200,
  viewer GET 200) that turn red when the gap is closed. Reading without
  starting is tested in `view_only` mode.

## Measured run time and memory (2026-10-11)

In Docker on the M1 Mac (amd64 image under emulation), `FINDER_THREADS=4`,
the tower photo resized to each size, one new finder process per row: the
first picture (with the process start: importing torch and Ultralytics),
then a second picture in the same process; peak and kept memory are the
process's VmHWM and VmRSS after the second.

| Picture | Read at | Setting it stands for | 1st / 2nd picture | Peak | Kept |
|---|---|---|---|---|---|
| 512 x 512 | 640 | building | 18.3 / 5.0 s | 0.92 GB | 0.88 GB |
| 585 x 585 | 896 | weed | 23.4 / 8.5 s | 1.01 GB | 0.95 GB |
| 640 x 640 | 1280 | maize seedling | 38.5 / 18.0 s | 1.38 GB | 1.08 GB |
| 1333 x 750 | 1280 | tower dish, antenna | 27.8 / 14.4 s | 1.22 GB | 1.08 GB |
| 1333 x 750 | 2560 | tower radio unit | 77.8 / 59.9 s | 2.76 GB | 1.47 GB |
| 1333 x 1333 | 2560 | tower radio unit, square | 137.1 / 88.9 s | 3.27 GB | 1.07 GB |

With 1 thread, 1333 x 750 at 1280 px took 26 s warm. The measurement itself
ran natively on the M1 at 1.3 s a picture at 1280 px
(`answer_levels_evidence.json`); a real x86 server will be between the two
and has not been measured. Script:
`scripts/finder_eval/measure_finder_process.py` (in the app image).

## Not done yet (open)

- **Inspections** (PR #177, now in the base branch) are not wired: their
  photos plug in through `Photo(bytes, view)` / `PictureSource` with a route
  of their own, the view from the photo's gimbal pitch.
- **No caller shows answer levels** for the finds yet; a consumer must read
  `answer_levels.ladder_enabled()` and `decide` (with the kept size gate)
  before saying how far a number may go.
- **The resize filter** for pictures larger than the measured size was not
  measured (the size gate keeps the objects at a measured size; how the
  picture is shrunk to get there was not compared).
- **Project editors and viewers** get 404 (the rights gap above).
- **Run time on the CPU server** is not measured.

## Gates

`src/services/test_example_finder.py`: settings from the table (no copied
numbers), input checks, pictures taken another way and marks smaller than
measured refused (a 2048 px window refused, the same marks in a 600 px one
run), a target with no measured size refused, the protocol around a recorded
real Ultralytics result
(`test_fixtures/example_finder/recorded_yoloe_result.json`, recorded by
`scripts/finder_eval/record_test_result.py`), shrinking and mapping back, the
weights' storage key and sha256 check, `FINDER_THREADS`, the offline flags
checked against Ultralytics itself, the process time limit and a model
failure coming back from the process, kept finds reused,
two workers running a request once, per-project keys, failures and retries,
one picture at a time, the line capped per user and for the app (N+1
refused, nothing left behind), first come first served across workers, dead
places skipped, a waiting job holding only the shrunk picture, and the
finder process stopped unless the same worker runs the next job.
`test_example_finder_pictures.py`: windows read through the raster workers
(looking straight down), frames and their round trips, refused windows,
photos and their view. `test_example_finder_routes.py`: switch off 404,
rights (today's 404 pinned, the spec as strict xfail tests), kept finds, 422
reasons (view, size gate, unmeasured size), 429 / 503 for a full line, the
per-user rate limit. `test_example_finder_model.py` (only
with `FINDER_TEST_WEIGHTS`): the real model on the tower photo, marks not
given back, unmarked dishes found, a 2x picture giving the same finds in its
own pixels, the recorded result reproduced, and the CPU it used within
`FINDER_THREADS` (Lessons log 2026-10-11).
