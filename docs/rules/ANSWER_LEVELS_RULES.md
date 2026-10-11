# Answer levels rules

How far an answer about objects that the **YOLOE visual-prompt finder**
(yoloe-11s-seg, shown marks the user drew) found in a picture may go. One
owner: `src/services/answer_levels.py`, with its measured table
`src/services/answer_levels_targets.json`, the evidence behind it
`src/services/answer_levels_evidence.json`, and tests in
`src/services/test_answer_levels.py`. Nothing else decides these levels for
this finder (H1), and no route, Sage tool or React component recomputes them
(H3): they show the `Answer` the module returns (`Answer.as_dict()`).

The levels were measured for this finder only. Each target names its finder
(`finder.model`), and `decide` refuses another finder's output (ValueError),
so FastSAM candidate polygons or green-crown counts never get a level from
this table (see "Other count wording in the repo" below).

Switch: `ANSWER_LADDER` (default off), read only by `ladder_enabled()`.
This module adds the rules; the consumers that show levels come in later PRs
and must check `ladder_enabled()` first. With the switch off they behave as
before.

## Levels

| Level | Label | Meaning |
|---|---|---|
| `COUNT` | Count | A number you can use as it is: it was checked to be right on pictures like this one. |
| `FULL_REVIEW` | Count after checking | Every object found is marked on the picture; a person checks each mark and adds any that were missed before the number is used. |
| `ESTIMATE` | Estimate | A number with a range around it, close enough to plan with, but not checked one by one. |
| `ROUGH` | Rough number | Tells a few from many; single marks are often wrong or missed, so it is not a count. |
| `AREA` | Area only | How much ground they cover, not how many there are. |
| `CANT_TELL` | Can't tell | This picture cannot answer the question; the reason says what would. |

No kind of object reaches `COUNT` today (tested). Reaching it needs a
measurement of counts, not of boxes, on pictures like the user's.

No answer reaches `AREA` today (tested): nobody has measured how well the
area objects cover is found, and box scores (AP50) say nothing about area.
The table refuses `AREA` as a target's level, and an evidence limit at `AREA`
gives `CANT_TELL`. Allowing it needs an area measurement with provenance in
the table and a change to the loader.

## How one answer is decided (`decide`)

1. **Finder.** The finder that found the objects must be the one the target
   was measured for; otherwise ValueError. The size gate must be the one
   worked out for the same target; otherwise ValueError.
2. **Table.** A target whose `max_level` is `CANT_TELL` (tree crowns, banana
   mats): `CANT_TELL`, with the table's reason.
3. **Size gate.** Not passed (too small) or unknown size: `CANT_TELL`, with
   the reason in data terms ("the marked dishes are about 12 px wide as the
   finder reads them (40 px in this 4000 px picture, read at 1280 px);
   dishes were measured only from 17 px wide, so this picture is outside the
   measurement").
4. **Countability** not YES: `CANT_TELL` with its reason. NO says why
   (plants touch, picture taken before planting); MAYBE says what is
   missing ("to count maize plants this needs the planting date and the date
   the picture was taken").
5. Otherwise the target's `max_level` from the table.
6. **Evidence** about this one picture can only lower the level, never raise
   it. Raising a level needs a new measurement in the table.

## Size gate (`size_gate`)

- Sizes are compared **as the finder reads the picture**. The finder scales
  the picture's long side to its input size (`finder_input_px`, the size the
  target was measured at), so a size in the picture's own pixels becomes
  `px x finder_input_px / picture_long_px` (`finder_px`). A 640 px maize tile
  read at 1280 px doubles; a 4000 px photo read at 1280 px shrinks about 3x.
  The caller passes the long side of the picture (or tile) it gives the
  finder.
- A box's size is the side of a square of the same area, `sqrt(width x height)`
  (objects are sized by area, as in COCO). The median of the user's marked
  boxes is used when there are marks; otherwise a size on the ground and the
  picture's cm per pixel. With neither, the size is **unknown**, which is not
  "small": the answer is `CANT_TELL` asking for two or three marks.
- **The floor is per target**: the smallest marked size (median of the 3
  marks on one picture, as the finder read it) among the pictures the
  target's level rests on (`size_evidence`), rounded down. The loader
  computes it from the marks in the evidence file. Smaller objects are
  outside the measurement: the score was never measured there, and where
  sizes were compared it fell with size (maize, radio units). So the gate
  never refuses a picture like the ones a level was measured on (tested on
  every measured picture).

| Target | Floor (finder px) | From |
|---|---|---|
| tower_dish | 17 | 20 tower photos (median dish 29 px) |
| tower_antenna | 33 | 15 tower photos (median 43 px) |
| tower_radio_unit | 21 | 14 tower photos read at 2560 px (median 37 px) |
| building | 29 | 20 Kenol tiles (median 67 px) |
| maize_seedling | 34 | Nerac (smallest 90 px, median 116) and Selommes (smallest 34, median 53), 20 tiles each |
| row_crop_plant | 34 | 20 cotton tiles (median 60 px) |
| weed | 31 | 20 weed tiles read at 896 px (median 48 px) |

There is no single floor for every kind: dishes about 29 px wide scored
0.745, the best of all targets, while radio units about 37 px wide scored
0.291. An earlier draft had one 50 px floor "below 25-30 px every target
failed"; the measurements contradict it, and it would have refused most of
the tower pictures the tower levels were measured on.

## Countability (`countability`)

| Kind | Targets | Rule |
|---|---|---|
| `structure` | tower dish, antenna, radio unit; building | YES: they do not grow or touch. Size is not a countability fact; `decide` applies the size gate once. |
| `annual_crop` | maize seedling, row-crop plant | Needs the planting date, the date the picture was taken and the share of ground under green leaves. Share 0.8 or more: NO, plants touch. Picture taken before planting: NO. Any of the three missing: MAYBE, listing them. A NO the known facts prove wins over missing facts. |
| `weed` | weed | Needs only the green cover share; 0.8 or more: NO, plants touch |
| `perennial_crown` | tree crowns (forest, savanna), banana mat | MAYBE, counting untested |

The green cover share is measured by the caller and passed in; a share of 0
is a value (bare ground), only None is missing.

## The measured table

YOLOE-11s with visual prompts, AP50 against people's boxes, all on
2026-10-11. Same-picture protocol unless noted: 3 boxes on a test picture
are the user's marks, the finder finds the rest there, scored on the rest.
"Read at" is the finder's input size; "pictures" is the long side as given.
Each measurement also says which way its pictures looked (`view`): the
tower photos are **oblique** (taken at an angle from a drone flying around
the tower), every other set is **nadir** (orthophoto or UAV tiles looking
straight down), as the datasets describe them. A value holds only for
pictures taken the same way (`OBLIQUE`, `NADIR`).

| Target | Value | Pictures | Data | Read at | Look-alike marks | Max level |
|---|---|---|---|---|---|---|
| tower_dish | 0.745 | 20, 1333 px | celltower test set | 1280 (2304: same score) | no (did not help) | FULL_REVIEW |
| tower_antenna | 0.695 | 15, 1333 px | celltower test set | 1280 | yes (+0.02, 2 of 2 draws) | FULL_REVIEW |
| tower_radio_unit | 0.291 | 14, 1333 px | celltower test set | 2560 (1280: 0.246, 3200: 0.194) | yes (0.325 vs 0.280, 4 of 4 runs) | FULL_REVIEW |
| building | 0.525 | 20, 512 px | Kenol tiles, 8.8 cm | 640 | not tried | ROUGH |
| maize_seedling | 0.787 Nerac; 0.437 Selommes | 20 each, 640 px | INRAe UAVmulticrop | 1280 (640: 0.573 / 0.277) | not tried | ROUGH |
| row_crop_plant | 0.533 | 20, 585 px | cotton and weed UAV tiles, cotton boxes | 640 | not tried | ROUGH |
| weed | 0.447 (labels incomplete: unboxed weeds count as false finds) | 20, 585 px | the same tiles, weed boxes | 896 (640: 0.428) | no (did not help) | ROUGH |
| tree_crown_forest | 0.216 | 20, 400 px | NEON TEAK | 640 | not tried | CANT_TELL |
| tree_crown_savanna | 0.059 (10 marks from OTHER pictures, mean of 3 draws) | 41, 400 px | NEON SJER | 640 | not tried | CANT_TELL |
| banana_mat | untested (no answer key) | none | none | none | none | CANT_TELL |

The table's `finder` settings are the ones each score was measured at;
`finder.note` says what was compared and what was not tried. A setting
nobody compared is not called "best".

Each value's `evidence` selects rows of `answer_levels_evidence.json`, rows
copied verbatim from the measuring session's `results/*.jsonl` (a field a
row did not record is filled and named in its `filled`). The tests check
every value, picture count, finder size and `also_measured` value against
them, and every size floor against the marks. The scripts and pictures are
**not in this repository**, so the numbers cannot be re-run from the repo
yet. The table says so in each `where` field.

Loading the table fails (ValueError) when a target lacks a provenance field;
has a value outside 0-1; has a measured value without its data, view (one of `VIEWS`), protocol,
picture count, picture size, finder size, date, metric, evidence or place;
has an unknown kind or level; is given `AREA`; is given a level above
`CANT_TELL` without a measured value or without marked pictures for its
size floor; is `CANT_TELL` without a note saying why; names no finder; or
takes its size floor from pictures read at another size.

## Not measured yet (open)

- **Object sizes on the ground** are null for every target: nobody measured
  them on our pictures, so the gate uses the user's marks. A size added later
  needs a source.
- **The 0.8 closed-leaves cut-off** came with the spec; it is not measured.
- **Areas.** No area answer is measured for any kind (see Levels).
- **Other finder sizes** for buildings, cotton and tree crowns, and
  look-alike marks for buildings, plants and tree crowns, were not tried.
- **No age window** for annual crops: the dates are required, but only
  "taken before planting" is ruled on. `plant_counts.py`'s "2-4 weeks after
  emergence" is guidance for its own method, not a measured limit for the
  finder.
- **Buildings and weeds' countability** are not in the spec: buildings follow
  the tower-part rule, weeds the closed-leaves rule without dates.

## Other count wording in the repo

Three places already say how far a count of objects in a picture goes, for
other finders, with their own words. They stay separate because no level
was measured for their finder, and `decide` refuses their output; moving
them here needs a measurement of their finder against people's boxes:

- `src/services/raster_object_candidates.py` (and the fast route in
  `src/routes/message_routes.py`): FastSAM candidate polygons,
  `count_semantics` `candidate_screening`, `evidence_level`
  `candidate_polygons_not_confirmed_assets`, `confirmed_count` False.
- `src/dependencies/sage_routing.py`: `evidence_level`
  `candidate_count_until_validated` for building counts from uploads.
- `src/services/plant_counts.py`: green crowns (excess green, crowns as local
  peaks), refuses photos coarser than `MAX_CM_PER_PX = 5`, gives ±15%. That
  floor limits the photo's resolution for that method; it was checked on
  young maize in Cyampirita at 3.2 cm per pixel (counts within about 10% of
  the eye's). No expression or threshold is shared, so H2 does not apply.

## Gates

`src/services/test_answer_levels.py`: every rule above, the table against
its evidence, the size floors against the marks, the gate on every measured
picture, a large photo the finder shrinks, CAN'T TELL reasons, missing facts
listed, no AREA and no COUNT anywhere, another finder refused, and the switch
on and off.
