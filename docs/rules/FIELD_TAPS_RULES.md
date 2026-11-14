# Field outlines from taps: rules

Owner: `src/services/field_taps.py`. Switch: `FIELD_TAPS` (off by default). Prepare: `scripts/prepare_field_taps.py`.

## What it does

On a drone photo of a farm project, a person taps a field. They get up to three outlines, pick one, may correct it
with up to two more taps ("this part too" / "not this part"), and keep it with its area in hectares.

## Rules

1. The heavy half of the model (Segment Anything ViT-H) never runs in the app. It runs once per photo on our own
   machine over a fixed grid: windows 120 m wide read at 1024 px, centres every 60 m from the photo's top-left
   corner. A photo that was not prepared answers "not prepared"; nothing is guessed.
2. A manifest prepared with another grid or model is refused. Outlines from other settings were never measured.
3. The first tap picks the nearest window centre. Later taps must fall inside that window.
4. One tap gives three outlines; each keeps only the connected part under the first tap (read at the nearest
   pixel, as measured), with holes filled.
5. A correction needs a picked outline. At most two corrections: that is what was measured.
4a. Of the three outlines, one that covers nearly the same ground as an earlier one (overlap 0.9 or more) is
   dropped. None is dropped for being small: fields drawn by hand on Cyampirita go down to 0.008 ha.
6. A kept outline is computed again on the server from the taps. The one shape a browser may send is a line the
   reader moved by hand (rule 9); it is stored beside the model's outline, never in its place.
7. Areas are hectares on the ellipsoid (`ground_area.area_ha`): map area, as seen from above. On a slope the ground
   surface is larger (6% at 20 degrees); the caveat text says so.
8. The text about what was measured comes from `field_taps.MEASURED`; screens do not write their own.
9. The reader may pull the line of the picked outline to the true edge ("Move the line"); the line bends around
   where it is grabbed. The server checks the moved line
   is one shape that does not cross itself and still covers the first tap, and measures its area. Kept, it is
   marked `by_hand`, with the model's outline, the model's area and the overlap of the two on the same record.
   A hand-moved outline is the reader's, not the model's: it is never counted in a test of the model.
10. Every answered tap that ends without a kept outline is recorded with its reason (`none_of_these`,
   `started_again`, `closed`, `no_outline`), so the share of taps that are kept can be counted. Kept outlines
   that were later removed are not turned-down taps.

11. A kept field gets a number that never changes and is never given to another field on that photo, even after
   the field is removed. A click on a kept field shows its number and size; a question typed there goes to Sage
   with the field's number, size and centre.
12. Kept fields are a source of the photo's plots (`photo_plots`): with nothing else chosen they are the plots the
   question cards and Sage read, each field under its own number (Field 7 is plot 7). A reader's chosen plot map
   still wins, and "Found by Ingabe" in the card's "Plots from" choice goes back to Ingabe's own plots. Keeping
   or removing a field makes a new measured plot set (greenness, bare spots and fingerprints are measured again),
   but a person's crop checks stay with their field: a kept field never changes shape and its number is never
   reused (`field_checks.checks_set_id`). A photo holds at most 500 kept fields. With `DRONE_VISION_SURVEY=1`
   every new plot set would be surveyed again at two paid calls a plot; the survey is off by default.

13. A hand-moved corner must be a real place within 500 m of the first tap; anything else is refused in plain
   words. Kept and turned-down records are read, changed and written as one step per photo, so two keeps at the
   same moment are both kept, each with its own number. This guard is in the app's one process
   (`UVICORN_WORKERS=1`); more workers would need a guard in storage itself.
14. At most two outlines are computed at once, and the tap model is fetched, checked and loaded off the event loop,
   one caller at a time. Turned-down taps are counted for good; the latest 2,000 are kept in full.
15. A manifest prepared with other settings, or a window missing from storage, reads as "not prepared", never as
   a server error. Who kept a field stays in the record and is not sent to the screen.
16. A first-tap choice with 120 m or more of itself on the border of its window (`EDGE_CHOICE_M`) is left out:
   there the window cut it, not the ground. The answer says how many were left out (`left_out`) and the screen says
   so. Nothing is left out when no other choice remains, so a field larger than a window can still be outlined.
   A rule for plots smaller than a window; a choice that was left out can be neither corrected nor kept.
17. Each kept field carries its green cover: the share of its ground under living green plants of any kind, crop
   and weeds alike, from the photo's colour by the rule Tests 77 and 78 measured, unchanged (excess green 2g - r - b
   of the 3 x 3 pixel mean above 0.15, counted 0.5 m in from the line; `green_cover`). It is only given where it
   was measured: on a photo marked as checked (`scripts/mark_cover_checked.py`, run only after a pre-registered
   test of the rule on that photo passed; the mark names the test and the threshold, and a mark for another
   threshold counts as none), for fields of 0.007 to 0.7 ha no wider than 150 m, on photos in metres with 2.5 to
   4.5 cm pixels. Elsewhere the field says why not and shows no figure. Shown in whole per cent. Cyampirita is the
   one photo checked (Tests 77 and 78). What it means is said by the server (`COVER_MEASURED`).
18. Opening a photo only reads. A field's cover is measured when it is kept (one read at a time); fields still
   without a figure are measured in the background, one job per photo layer, 20 fields between two writes, the
   photo opened once a batch with the network timeouts every photo read uses. The state says `cover_filling` while
   a job runs and the screen asks again every 3 s until it is done. A photo that cannot be read stops the job and
   is not tried again for 5 minutes. Every limit is checked before a pixel is read: the box around a field decides
   how much is read, not its area. A stored figure carries the threshold that made it and is measured again when
   the threshold changes. On a photo that is not marked, nothing is stored: each field is shown as not measured.
19. Kept fields and turned-down taps belong to one photo layer, the thing whose access the routes check
   (`_fields_prefix`). The prepared windows and the cover mark belong to the stored image, which copies of a layer
   share. A field kept on one copy of a photo never appears on another copy, in the cards or to Sage; a person's
   checks on outlined fields are kept per layer too (`field_checks.checks_set_id`).
20. A hand-moved outline may be at most 2 ha. The answers to keeping a field and to turning taps down leave out
   who did it, as the state does.

## What was measured (plans/PLAN_RWANDA_CROPS.md, 2026-11-12)

| Test | What | Result |
|---|---|---|
| 68 | One tap, best of three, 26 exam fields (Cyampirita) | usable outline (IoU >= 0.5) on 20 |
| 70 | One tap + up to two corrections, same fields | 22 after one, 25 after two; area error 4.7% (median); none made worse |
| Server timing | Light half in a throw-away container on the server, one thread | the model alone 0.18 s a tap (median); a whole first tap with this module's code (model, cleaning three outlines, shapes) 0.4 to 0.6 s on six fields |
| 74 | Area of a first-tap outline (best of three, no correction) against a hand drawing, 20 exam fields | median area error 7.6%; within 5% on 9 of 20. A band of 2.17 m x perimeter / area holds 18 of 20 but is too wide to print (a third of the area at the median) |
| 75 | Area of an outline after up to two correcting taps, through the app's own code, 26 exam fields | median area error 5.9%; within 5% on 11 of 26. A band of 1.44 m x perimeter / area holds 23 of 26 but is a fifth of the area at the median: still too wide to print |
| 76 | Rule 16 on the three choices of a first tap, 26 exam fields (70 choices) | 12 choices left out; all 12 were unusable (IoU < 0.5) and 2 to 35 times the drawn field; no field lost its best choice. It catches 12 of 21 over-large choices; the rest took in a neighbour away from the border. On the 31 tuning fields: 15 left out, 14 unusable, one 0.6 ha field lost its best choice |
| 77 | Green cover from colour, threshold set on 31 tuning fields, against two blind readers of 40 random points in each of 26 exam fields | threshold 0.15; point accuracy 88%, median field error 0.06, 16 of 26 within 0.10: good enough to build on, not for clients. Colour read high on sparse ground (median error 0.16 on 9 fields under half cover; 0.03 on 16 fields of 0.7 or more) |
| 78 | Tiebreak on the 103 points where readers and colour disagreed, by two readers of another model with an 8 m view, and Test 77 scored again | the tiebreak sided with colour on 42% (split); 25 points stayed undecidable. Scored again: point accuracy 94.5%, median field error 0.03, 20 of 24 within 0.10: passes the client bar, narrowly (counting the 25 undecidable points against colour gives 18 of 26) |
| 79 | The same rule on a second photo (Kabarama, 4.56 cm, muted colour), 24 random 20 m squares; stopped before any reader | the rule called 21 of 24 squares 0 to 1% green although the photo shows plants; a colour threshold does not carry from one camera and day to another, so green cover is given only on photos it was checked on |

Not measured: green cover on any photo but Cyampirita, or against the ground itself (the readers read the same photo); taps by a real person (the test placed them with a script from hand-drawn outlines), corners, ground
that is not a field, any photo other than Cyampirita. The first correction made the outline worse on 6 of 26 fields
before the second repaired it, so the screen keeps a "back" step.

## How it shows (design rules, plans/PLAN_RWANDA_CROPS.md review part 2, G and H)

- An area under 0.05 ha is shown in square metres (to 10 m2 from 100 m2 up); never "0.00 ha" for a real field. No plus-or-minus is printed: Tests 74 and 75 found no band that is both safe and narrow.
- "This part too" and "Not this part" taps carry a plus and a minus, not colour alone. Every outline has a dark edge.
- An answer slower than about a second says what is happening and how long it can take.
- Removing a kept field asks once. The list of kept fields only selects; removal is on the selected field.
- Small text actions have a 36 px tap height.
