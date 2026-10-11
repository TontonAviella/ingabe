# Towers rules

Tower work on a photo inspection in a Telecom Towers project: a person marks a few dishes, antennas or radio units
on a photo, the example finder suggests the rest, the person checks every suggestion, and only then is anything
counted. One owner (CODING_STANDARDS.md, H1): `src/services/tower_inspection.py` (the work, the counting rule, the
inventory, the photos to check, the camera facts, the same site before) with `src/services/tower_cards.py` (the
tower question cards and the facts Sage reads). Adapters only: `src/routes/tower_routes.py`,
`src/tools/tower_tools.py`. Tests: `src/services/test_tower_inspection.py`, `test_tower_cards.py`,
`src/routes/test_tower_routes.py`, `src/tools/test_tower_tools.py`, and the routing, industry and eval-corpus tests.

Switch: `TOWERS` (default off), read only by `tower_inspection.enabled()`. Off: every route below answers 404, and the
tower capabilities (`tower_inspection`, `tower_cards`, `get_tower_inspection_findings`,
`tower_photos_needing_review`) are not offered, run or shown for any industry (`industry.SWITCHES` /
`industry.available`); a tower question then routes as before (no tool offered, the full list sent). The routes also
need `PHOTO_INSPECTIONS` (which serves the photos) and, to find, `EXAMPLE_FINDER`.

Roger's decisions (2026-10-11): towers are the industry `telecom`; the cards' readers are the tower owner / asset
manager and the field technician; accuracy first: the app never presents an unreviewed suggestion as a count.

## Parts and what a person does

Parts: `tower_dish`, `tower_antenna`, `tower_radio_unit` (answer_levels' targets; their nouns "dishes", "antennas",
"radio units" are read from there). Per project, inspection layer, photo and part:

- **Marks**: 0 to 20 boxes (`example_finder.MAX_MARKS`), [x0, y0, x1, y1] in the photo's own pixels as it is shown
  (EXIF orientation applied: `PhotoFacts.upright_size`), inside the photo (`example_finder.check_shape` /
  `check_inside`, the finder's own checks).
- **Look-alike marks**: only for a part whose measured settings use them (`answer_levels` via
  `example_finder.settings(part).look_alike_marks`: antennas and radio units, not dishes): the other two parts'
  marks on the same photo (as measured), then the person's own look-alike marks, without repeats, at most 20. A
  person's own look-alike marks for dishes are refused (422), never ignored.
- **Find**: the example finder (`example_finder.start`) on the photo read from this inspection's own storage only
  (`example_finder_pictures.InspectionPhoto`), taken the way `photo_inspections.view_of` says (from the DJI gimbal
  pitch; a photo without one is refused with the reason, never guessed). Every refusal of the finder (a photo
  looking straight down, marks smaller than measured, a part never measured) comes back as 422 with its reason.
- **Suggestions**: the finds of the part's finder job, best first, as `{id, box, score}`; the id names the job
  (`<job id[:12]>-<n>`), so an id from older marks is refused, never taken for another suggestion.
- **Review**: the person keeps, rejects or fixes (a new box) each suggestion and adds the ones the finder missed.
  A review replaces the last one; every id must be a suggestion of the current job, given once. A review before the
  finder has finished is 409.
- **Changed marks** drop the part's finder job and review (they were for the old marks). Another part's changed
  marks leave a part's job and review alone; its look-alike marks are recomputed and the next find uses them.

## Counted by you (`tower_inspection.counted`)

- The level comes from `answer_levels.decide` for the photo's marks (size gate on the marks at the photo's long
  side, countability, the measured table). Every tower part is `FULL_REVIEW` today: the suggestions are never a
  count. A part whose level is below `FULL_REVIEW` on a photo is never counted there.
- `counted_by_you` = the marks + the kept + the fixed + the added, and only when the finder has finished, the
  person saved a review of this job's suggestions, and none is left waiting. Otherwise None ("not counted yet"),
  never 0. The rejected count for nothing.
- **Deviation from the first spec, on purpose:** the spec said kept + fixed + added. The marks are objects the
  person drew around (the finder drops finds on them, IoU 0.3), so leaving them out would undercount every photo
  by the number of marks.
- **A saved review is required even when the finder found nothing** (no suggestions): saving it is where the person
  adds the ones it missed; until then the photo is "to confirm" (`CONFIRM`) and not counted.
- `waiting` = suggestions not yet kept, rejected or fixed.

## Across an inspection (`inventory`)

- Photos overlap: the same dish seen from two sides is on two photos. So the number a reader sees is **at least N**:
  the most counted on one photo (`at_least`, with that photo). The sum over photos (`counted_in_photos`) is shown
  only as "added up over photos (a part seen on two photos counts twice)", never as the number on the tower.
- Per part: photos marked, photos counted, suggestions waiting (and on how many photos), photos to confirm, photos
  the finder is working on, photos marked but not found yet (or the finder failed).
- **Photos checked**: photos where something is marked and every marked part is counted.

## Photos to check (`photos_to_check`), the technician's order

1. suggestions waiting (most first); 2. the finder found no more, to confirm; 3. marked but not found yet (or
failed); 4. the finder still working; 5. nothing marked. Within each, in flight order (the camera's local time, then
the file name; photos without a time last). A photo where something is counted and nothing marked is open is not
listed.

## Where the photos were taken from (`camera_facts`)

Read from each photo's own facts (`photo_inspections`): photos, with GPS, the names of those without; height above
the take-off point (DJI relative altitude: least, median, most); camera tilt (gimbal pitch) and how many photos were
taken at an angle, straight down, or say neither; the direction faced (gimbal yaw as one of eight compass words,
round to the nearest 45 degrees); first and last time taken (camera time); cameras. Missing stays missing.

## The same site before (`earlier_inspection`)

Another photo inspection in the **same project** (never another project) whose camera positions centre (median
lat, lon of the photos with GPS) is within `SAME_SITE_M` = 100 m of this one's, and whose last photo was taken
before this one's first. Unknown positions or times: not compared. The latest such inspection with something
counted by you is compared; without one, the changes card says a second inspection is needed. The 100 m is a
cut-off set on 2026-10-11, not measured (towers in a city can stand closer).

## Storage

- A person's work: `towers/v1/<project>/<layer>/<sha256>.json` (version 1), per project, never shared across
  projects, even for byte-identical photos; the finder keeps its finds per project too (`finds/v1/<project>/`).
- Changes to one photo's work are made one at a time across app workers (a Redis lock per project, layer and photo,
  `towers:v1:...:lock`, 60 s; a change waits up to 10 s, then 409).
- The project of a layer is `photo_context.project_of` (the most recently edited live map showing it), as for the
  finder, the farm records and the field checks; its industry gates every route and tool.
- The inspection's photos come from the facts kept at upload (`photo_inspections.load_facts`), one per stored photo
  (a photo uploaded twice keeps its first name); an inspection uploaded before 2026-10-11 has none: 409 "upload the
  photos again". Kept in memory per inspection (they never change).

## Routes (`src/routes/tower_routes.py`, the shared API contract)

All under `/api/towers/layer/{layer_id}`; 404 with a switch off, for a layer that is not a photo inspection, or in a
project that is not Telecom Towers; reads need view rights (`get_layer`), writes edit rights (`edit_layer`).

- `GET /photos` -> `{"photos": [{sha256, url, width, height, taken_at, lat, lon, relative_altitude_m,
  gimbal_pitch, gimbal_yaw}]}` (width and height as shown; url is the photo route of `PHOTO_INSPECTIONS_RULES.md`).
- `GET /photos/{sha256}/work` -> `{"parts": {<part>: {marks, lookalike_marks, job: {job_id, status} | null,
  suggestions: [{id, box, score}], reviewed: {kept, rejected, fixed: [{id, box}], added}, counted_by_you, waiting}}}`.
- `PUT /photos/{sha256}/marks` `{part, marks, lookalike_marks}` -> the part's work.
- `POST /photos/{sha256}/find` `{part}` -> `{job_id, status}`; 422 with the reason (size gate, view, marks), 429 /
  503 when the finder's line is full (per user / for the app), 502 when the photo could not be read, 404 while
  `EXAMPLE_FINDER` is off. Rate limit: the heavy tier, per user.
- `PUT /photos/{sha256}/review` `{part, kept, rejected, fixed, added}` -> the part's work; 409 before the finder
  has finished.
- `GET /cards?audience=owner|technician` -> `{"cards": [{id, title, status, preview}]}`; `GET /cards/{card_id}` ->
  the drone cards' answer shape (`what, why, todo, how_sure, facts, terms, items, downloads, progress, overlay,
  choices, upload, field_check, ask_sage`, plus `question` and `status_label`). Unknown audience: 422.
- **Rights gap (as every route on `get_layer`)**: a project editor or viewer who does not own the layer gets 404
  (map_layers row security shows a layer to its owner only; EXAMPLE_FINDER_RULES.md). Pinned by a test; Roger's
  decision.

## Cards (`tower_cards.py`)

Statuses reuse the drone cards' vocabulary (`card_status`); labels are the towers' own ("Needs a second
inspection"). Ranking: readiness, then the reader (the owner's equipment question first, also before anything is
counted, since its answer says how to start; the technician's photos to check first); the learning card last.

| Card | Status | Answer |
|---|---|---|
| `equipment` "What equipment is on this tower?" | can't tell (nothing counted), working (only the finder working), partly (counted, some open), ready | at least N per part, counted by you, on which photo; photos checked X of Y; suggestions waiting; the sum over photos named as such. Nothing counted: says so and how to start, no number. |
| `photos_to_check` "Which photos still need checking?" | ready | the list in the technician's order, with why each is listed |
| `camera_positions` "Where were the photos taken from?" | ready (can't tell without photos) | heights, tilt, directions, times, cameras; photos without GPS listed; the directions no photo faces |
| `changes` "What changed since the last inspection?" | needs a second inspection / can't tell (nothing counted now) / ready | at least N then and now per part, the change in plain words, both photos |
| `learn_review` "Why does a person check every suggestion?" | learn | what the finder was measured to do (value, photos, data, date read from `answer_levels`), AP50 explained plainly, and the person's own checks so far (kept or fixed of those checked, missed ones added) |

- How sure: "medium" for equipment only when every marked photo is counted, else "low"; the camera facts "high"
  (read from the photos); the changes "low".
- With `ANSWER_LADDER` on, the level's label and meaning (`answer_levels`: "Count after checking: ...") open the
  "how sure" reasons; off, no level words are shown (answer_levels' rule for its consumers).
- No money in any card.

## Sage (`src/tools/tower_tools.py`)

`get_tower_inspection_findings` and `tower_photos_needing_review` (telecom only, `TOWERS` on; routing category
`towers`) return the equipment and camera cards' facts and the photos-to-check list (`tower_cards.findings` /
`photos_needing_review`), with the rule "give only the counts listed here; never count equipment from a photo
yourself, never present suggestions as a count". With nothing checked they say there is no count and how to start.
With `TOWERS` on, the Telecom prompt note says the same (`industry.prompt_note`). A towers question narrows the tool
list only when the turn is offered a tower tool (`sage_routing.route_chat(offered=...)`).

## Not done yet (open)

- **Phone photos** (no DJI gimbal pitch) cannot be searched: which way they looked is unknown. A field technician's
  phone photos of a tower are almost always at an angle; accepting them needs Roger's decision.
- **The 10 degrees** for "straight down" and **the 100 m** for "same site" are cut-offs, not measured.
- **Inspections uploaded before 2026-10-11** have no kept facts: they must be uploaded again.
- **A layer shown in two projects** follows `project_of` (the last edited map), so its work can seem to move between
  projects; the same holds for the finder and field checks.
- **No "none on this photo" action**: a part not visible on a photo stays unmarked there (not counted, never 0).
- **Rights gap** above; **Hermes** (`MUNDI_USE_HERMES`) does not have the tower tools (nor the drone tools).
