# Answer levels rules

How far an answer about objects in a picture may go. One owner:
`src/services/answer_levels.py`, with its measured table
`src/services/answer_levels_targets.json` and tests in
`src/services/test_answer_levels.py`. Nothing else decides these levels (H1),
and no route, Sage tool or React component recomputes them (H3): they show the
`Answer` the module returns (`Answer.as_dict()`).

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
measurement of counts, not of boxes, on pictures like the user's; no such
measurement exists yet.

## How one answer is decided (`decide`)

1. **Size gate.** Not passed (too small) or unknown size: `CANT_TELL`, with
   the reason in data terms ("maize plants are about 9 px wide at 3.2 cm per
   pixel; at least 50 px are needed").
2. **Countability NO** (plants touch, picture taken before planting, a tower
   part too small): `CANT_TELL` with that reason.
3. Otherwise the kind's `max_level` from the table.
4. **Countability MAYBE** (facts missing, or counting untested): at most
   `AREA`, and the reason lists what is missing.
5. **Evidence** about this one picture can only lower the level, never raise
   it. Raising a level needs a new measurement in the table.

## Size gate (`size_gate`)

- An object must be **50 px across or more** in the picture the finder reads.
  Measured 2026-10-11: below about 25-30 px every kind failed; from about
  50 px they did well. 50 is that floor, not a fitted cut-off.
- A box's size is the side of a square of the same area, `sqrt(width x height)`
  (objects are sized by area, as in COCO). The median of the user's marked
  boxes is used when there are marks; otherwise a size on the ground and the
  picture's cm per pixel. With neither, the size is **unknown**, which is not
  "small": the answer is `CANT_TELL` asking for two or three marks.
- The gate reads pixels of the picture as given, before any zoom.

## Countability (`countability`)

| Kind | Targets | Rule |
|---|---|---|
| `structure` | tower dish, antenna, radio unit; building | YES when the size gate passes, NO when it fails, MAYBE when the size is unknown |
| `annual_crop` | maize seedling, row-crop plant | Needs the planting date, the date the picture was taken and the share of ground under green leaves. Share 0.8 or more: NO, plants touch. Picture taken before planting: NO. Any of the three missing: MAYBE, listing them. A NO the known facts prove wins over missing facts. |
| `weed` | weed | Needs only the green cover share; 0.8 or more: NO, plants touch |
| `perennial_crown` | tree crowns (forest, savanna), banana mat | MAYBE, counting untested: the area is offered instead |

The green cover share is measured by the caller and passed in; a share of 0
is a value (bare ground), only None is missing.

## The measured table

YOLOE-11s with visual prompts (3 marks on the same picture), AP50 against
people's boxes, 20 pictures unless noted, all on 2026-10-11.

| Target | Value | Pictures | Data | Finder settings | Max level |
|---|---|---|---|---|---|
| tower_dish | 0.74 | 20 at 1280 px | celltower test set | zoom 1, look-alike marks | FULL_REVIEW |
| tower_antenna | 0.69 (+0.02 with look-alike marks) | 20 at 1280 px | celltower test set | zoom 1, look-alike marks | FULL_REVIEW |
| tower_radio_unit | 0.29 (0.32 with look-alike marks, mean of 3 draws, higher in 4 of 4 runs) | 14 at 2560 px | celltower test set | zoom 1, look-alike marks | FULL_REVIEW |
| building | 0.53 | 20 | Kenol tiles, 8.8 cm | zoom 1 | ROUGH |
| maize_seedling | 0.79 (plants ~56 px, Nerac); 0.44 (~24 px, Selommes) | 20 each | INRAe UAVmulticrop | zoom 2 | ROUGH |
| row_crop_plant | 0.53 | 20 | cotton UAV | zoom 2 | ROUGH |
| weed | 0.45 (labels incomplete: unboxed weeds count as false finds) | 20 at 896 px | not named | zoom 2 | ROUGH |
| tree_crown_forest | 0.22 | 20 | NEON TEAK | zoom 1 | AREA |
| tree_crown_savanna | 0.06 (marks from other pictures) | 20 | NEON SJER | zoom 1 | AREA |
| banana_mat | untested (no answer key) | none | none | zoom 1 | AREA |

The scripts that produced these numbers are in the measuring session's
scratchpad (`contest/src`), **not in this repository**, so the numbers cannot
be re-run from the repo yet. The table says so in each `where` field.

Loading the table fails (ValueError) when a target lacks a provenance field,
has a value outside 0-1, a measured value without its protocol, date,
picture count, metric or place, an unknown kind or level, or a zoom below 1.

## Not measured yet (open)

- **Object sizes on the ground** are null for every target: nobody measured
  them on our pictures, so the gate uses the user's marks. A size added later
  needs a source.
- **The 0.8 closed-leaves cut-off** came with the spec; it is not measured.
- **Zoom and pixel sizes.** The notes do not say whether the maize sizes
  (~56 px, ~24 px) are before or after the 2x zoom. If they are after, the
  floor in native pixels for plants would be about half, and the gate is too
  strict for plants by up to 2x. The zoom used for the cotton, weed and tree
  measurements is not recorded.
- **No age window** for annual crops: the dates are required, but only
  "taken before planting" is ruled on. `plant_counts.py`'s "2-4 weeks after
  emergence" is guidance for its own method, not a measured limit for the
  finder.
- **Buildings and weeds' countability** are not in the spec: buildings follow
  the tower-part rule (size gate), weeds the closed-leaves rule without dates.

## Relation to `plant_counts.py`

`src/services/plant_counts.py` counts plants with another method (excess
green, crowns as local peaks) and refuses photos coarser than
`MAX_CM_PER_PX = 5`. That floor is a different rule from this module's 50 px
gate: it limits the photo's resolution for that method, and it was checked on
young maize in Cyampirita at 3.2 cm per pixel (counts within about 10% of the
eye's). At 3.2 cm per pixel, 50 px is 1.6 m, far wider than a young maize
plant. It is left untouched (no expression or threshold is shared, so H2 does
not apply). Routing it through `size_gate` would change its results: the 50 px
gate would refuse the very photo it was checked on.

A consumer that shows a level next to a green-crown count must decide which
rule applies to that method; this module's levels were measured for the
picture finder, not for `plant_counts`.

## Gates

`src/services/test_answer_levels.py`: every rule above, table integrity,
the 2026-10-11 numbers, CAN'T TELL reasons, missing facts listed as MAYBE,
no COUNT anywhere, and the switch on and off.
