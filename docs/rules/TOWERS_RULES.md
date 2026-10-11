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
  person's own look-alike marks for dishes are refused (422), never ignored. The work shows this whole list as
  `lookalike_marks`; `PUT marks` takes the same list back and keeps only the person's own: a box that is another
  part's mark on the photo is a look-alike already, so it is not stored as the person's own
  (`tower_inspection.own_lookalikes`). Sending the shown list back unchanged changes nothing (the job and the
  review stay); leaving a box out removes that own look-alike (a changed look-alike drops the job, as changed
  marks do). When the other parts' marks alone fill the 20 places, the own ones are not shown, and sending the
  shown list back removes them.
- **Find**: the example finder (`example_finder.start`) on the photo read from this inspection's own storage only
  (`example_finder_pictures.InspectionPhoto`), taken the way `photo_inspections.view_of` says (from the DJI gimbal
  pitch; a photo without one is refused with the reason, never guessed). Every refusal of the finder (a photo
  looking straight down, marks smaller than measured, a part never measured) comes back as 422 with its reason.
- **Suggestions**: the finds of the part's finder job, best first, as `{id, box, score}`; the id names the job
  (`<job id[:12]>-<n>`), so an id from older marks is refused, never taken for another suggestion. For antennas
  and radio units they are the finder's filtered finds (its suggestion filter, on where it was measured not to
  lower AP50; dishes not: `EXAMPLE_FINDER_RULES.md`).
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
- **Photos with every marked part counted** (`photos_with_marked_parts_counted`): photos where something is marked
  and every marked part is counted. Not "checked for every part": a part nobody marked on a photo was not looked for
  there (there is no "none on this photo"). **Photos with nothing marked** (`photos_unmarked`): nobody has looked
  for parts on them.

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
- The project whose work the routes read and write is the one live project whose shown maps list the layer
  (`src/services/shown_maps.py`: its live maps no live map was forked from, so an older version still listing a
  removed layer does not count and two sibling forks from simultaneous uploads both do; deleted projects do not count;
  `photo_context.projects_of`, `tower_inspection.work_project`; the earlier-inspection lookup uses the same rule); a layer on the current maps of more than one Telecom project (added to another
  project's map) is refused with 409 "remove it from the maps of the others", never followed to the last edited map
  (which would move its work between projects). Removing it from the other map, or deleting that project, clears it.
  The industry is checked first: unless every project showing the layer is Telecom Towers (switch on), it is 404, so a
  farm inspection on two farm maps is not a tower conflict. Sage's tower tools use the chat's own project
  (`meta.project_id`, whose map must show the layer). That project's industry gates every route and tool.
- The inspection's photos come from the facts kept at upload (`photo_inspections.load_facts`), one per stored photo
  (a photo uploaded twice keeps its first name); an inspection uploaded before 2026-10-11 has none: 409 "upload the
  photos again". Kept in memory per inspection (they never change).

## Routes (`src/routes/tower_routes.py`, the shared API contract)

All under `/api/towers/layer/{layer_id}`; 404 with a switch off (checked before the body is read, so a malformed
request is 404 too: `src/routes/switched_routes.py`), for a layer that is not a photo inspection, or in a project that
is not Telecom Towers; 409 for a layer on the maps of more than one Telecom project; reads need view rights
(`get_layer`), writes edit rights (`edit_layer`). The panel (`TowerCards.tsx`) shows only after the server answered
for the layer (data, or a 409): never while asking, on a 404 or on a failed request.

- `GET /photos` -> `{"photos": [{sha256, url, width, height, taken_at, lat, lon, relative_altitude_m,
  gimbal_pitch, gimbal_yaw}]}` (width and height as shown; url is the photo route of `PHOTO_INSPECTIONS_RULES.md`).
- `GET /photos/{sha256}/work` -> `{"parts": {<part>: {marks, lookalike_marks, job: {job_id, status} | null,
  suggestions: [{id, box, score}], reviewed: {kept, rejected, fixed: [{id, box}], added}, counted_by_you, waiting}}}`.
- `PUT /photos/{sha256}/marks` `{part, marks, lookalike_marks}` -> the part's work. `lookalike_marks` is the list
  the work shows (the other parts' marks, then the person's own) or any part of it: the other parts' marks in it are
  not stored, the rest are the person's own look-alikes (above).
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
| `equipment` "What equipment is on this tower?" | can't tell (nothing counted), working (only the finder working), partly (counted, but a part counted on no photo, a marked photo still open, or a photo with nothing marked), ready (every part counted, nothing open, something marked on every photo) | at least N per part, counted by you, on which photo; photos with every marked part counted X of Y; photos with nothing marked; suggestions waiting; the sum over photos named as such. Nothing marked: says so and how to start, no number. Marked but nothing counted: what is still open and the next step, never "mark 3 to start". |
| `photos_to_check` "Which photos still need checking?" | ready | the list in the technician's order, with why each is listed |
| `camera_positions` "Where were the photos taken from?" | ready (can't tell without photos) | heights, tilt, directions, times, cameras; photos without GPS listed; the directions no photo faces |
| `changes` "What changed since the last inspection?" | needs a second inspection (none of the site) / can't tell (an earlier one with nothing counted on it, or nothing counted now) / ready | at least N then and now per part, the change in plain words, both photos |
| `learn_review` "Why does a person check every suggestion?" | learn | what the finder was measured to do (value, photos, data, date read from `answer_levels`), AP50 explained plainly, and the person's own checks so far (kept or fixed of those checked, missed ones added) |

- How sure: "medium" for equipment only when it is ready (every part counted, nothing open, something marked on
  every photo), else "low", naming the parts counted on no photo and the photos with nothing marked; the camera
  facts "high" (read from the photos); the changes "low".
- With `ANSWER_LADDER` on, the level's label and meaning (`answer_levels`: "Count after checking: ...") open the
  "how sure" reasons; off, no level words are shown (answer_levels' rule for its consumers).
- No money in any card.

## Sage (`src/tools/tower_tools.py`)

`get_tower_inspection_findings` and `tower_photos_needing_review` (telecom only, `TOWERS` on; routing category
`towers`) return the equipment and camera cards' facts and the photos-to-check list (`tower_cards.findings` /
`photos_needing_review`), with the rule "give only the counts listed here; never count equipment from a photo
yourself, never present suggestions as a count". Sage gets "at least N" per part and never the sum over photos
(it would count a part seen on two photos twice). With nothing marked they say there is no count and how to start;
with marked photos still open, what is open. They read the chat's own project's work. With `TOWERS` on, the Telecom
prompt note says the same (`industry.prompt_note`).

Routing: tower words name the `towers` category; it narrows the tool list to the tower tools only when the turn is
offered a tower tool (`sage_routing.route_chat(offered=...)`) and the question asks what they answer: a count, the
equipment, or photos to check (`asks_about_tower_work`, `categories_answering`). Tower words alone (a storm before
tower climbs, "describe the tower photo", a flood near a mast, damage on a mast) keep the full list, which holds
the tower tools, so the weather, flood and raster tools that answer them stay offered; the abdication guard lets
an honest "Ingabe cannot tell" stand on them as before tower tools existed. A count question about a single raster
photo is narrowed to the tower tools (Sage never counts from a picture); whether the model then answers in words
is the routing eval's to measure with `TOWERS=1` (not run yet).

## Not done yet (open)

- **Phone photos** (no DJI gimbal pitch) cannot be searched: which way they looked is unknown. A field technician's
  phone photos of a tower are almost always at an angle; accepting them needs Roger's decision.
- **The 10 degrees** for "straight down" and **the 100 m** for "same site" are cut-offs, not measured.
- **Inspections uploaded before 2026-10-11** have no kept facts: they must be uploaded again.
- **A layer shown in two projects** is refused by the tower routes (409) until it is removed from all but one
  project's maps; the finder and field checks still follow `project_of` (the last edited map).
- **No "none on this photo" action**: a part not visible on a photo stays unmarked there (not counted, never 0).
- **Rights gap** above; **Hermes** (`MUNDI_USE_HERMES`, off) is never given a tool behind a switch: the plugin skips
  `industry.SWITCHES` (it picks tools by words, not by industry or switch). It is still given the farm-only drone
  tools for every industry (a gap on main, not of this build).
- **Switched off later**: earlier tower tool calls and their counts are dropped from the replayed chat history
  (`industry.available`), and a tower tool called anywhere it is not offered gets "There is no tool named ...".
