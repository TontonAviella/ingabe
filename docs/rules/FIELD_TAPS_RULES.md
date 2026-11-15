# Field outlines from taps: rules

Owner: `src/services/field_taps.py`. Switch: `FIELD_TAPS` (off by default). Prepare: `scripts/prepare_field_taps.py`.

## What it does

On a drone photo of a farm project, a person taps a field. They get up to three outlines, pick one, may correct it
with up to six more taps ("this part too" / "not this part"), and keep it with its area in hectares.

## Rules

1. The heavy half of the model (Segment Anything ViT-H) never runs in the app. It runs once per photo on our own
   machine over a fixed grid: windows 120 m wide read at 1024 px, centres every 60 m from the photo's top-left
   corner. A photo that was not prepared answers "not prepared"; nothing is guessed.
2. A manifest prepared with another grid or model is refused. Outlines from other settings were never measured.
3. The first tap picks the nearest window centre. Later taps must fall inside that window.
4. One tap gives three outlines; each keeps only the connected part under the first tap (read at the nearest
   pixel, as measured), with holes filled.
5. A correction needs a picked outline. At most six corrections: the most that were tested (Test 83; four did all
   the work there). A correction can make the outline worse before the next repairs it, so Back is always there and
   the screen says so after a correcting tap. Each correction runs the tap model once more per tap so far (seven
   runs at the limit, about 0.2 s each on the server as measured for one run; the seven together are not measured).
4a. Of the three outlines, one that covers nearly the same ground as an earlier one (overlap 0.9 or more) is
   dropped. None is dropped for being small: fields drawn by hand on Cyampirita go down to 0.008 ha.
6. A kept outline is computed again on the server from the taps. The one shape a browser may send is a line the
   reader moved by hand (rule 9); it is stored beside the model's outline, never in its place.
7. Areas are hectares on the ellipsoid (`ground_area.area_ha`): map area, as seen from above. On a slope the ground
   surface is larger (6% at 20 degrees); the caveat text says so.
8. The text about what was measured comes from `field_taps.MEASURED`; screens do not write their own.
9. The reader may pull the line of the picked outline ("Move the line"): a press on the line (within 28 px, a
   finger's width) takes hold of it, the drag moves it, and letting go anywhere ends the pull; there is no place a
   pull must start or end (`lib/traceEdge.ts`). The line around the press follows, fully at the press and falling
   smoothly to nothing along the line both ways, over a stretch that grows with the length of the pull (twice the
   pull, at least 40 px, never most of the outline), so a pull bends the line and never tears it. A pull that
   would leave the first tap outside the outline changes nothing. "Undo last" takes back one pull, "Undo all"
   every one. Only the pulled stretch gets new corners: every corner out of reach stays exactly where it was,
   however many pulls are made. A pull longer than the line can follow is shortened so the line bends and does
   not fold over itself. If the view changes under a pull (a wheel or pinch zoom, a rotated phone) the pull is
   dropped and the outline is as it was. A press away from the line, or with two fingers, moves the map as usual. Freehand drawing of the
   edge was removed on 2026-11-14: Roger found it hard on the live site (start on the line, trace, end on the
   line). The
   server measures the moved line. A pull can slip the line a hair past its neighbour at a tight bend and make a
   loop too small to see; that is the tool's doing, so the server cuts such loops away, keeps the piece around the
   first tap as the field, and sends the tidied line back for the screen to draw (`moved_line`, `tidied`). The
   reader is never told a line "crosses itself" or asked to repair one (Roger hit exactly that on the live site,
   2026-11-14, with an outline that looked right). The only refusals left are a line that no longer covers the
   first tap ("Use Undo last"), one over 2 ha, and one further than 500 m from the tap. Kept, it is
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
21. Only my crop (`src/services/my_crop.py`, route `POST .../kept/{id}/my-crop`): in a kept field the reader taps 3
   to 10 plants of their crop and 3 to 10 green things that are not; of the plants the plant finder boxed, one is
   "their crop" when its look (DINOv2-small, the square read at 784 px, the 3 x 3 patches around the plant) lies
   nearer the crop taps than the other taps. The count is an estimate and every screen says "about": it passed the
   client bar only where one crop grows (Test 81), and taps on one square sorting the plants of another, a person's
   own taps and tree crops were not measured. The words about this come from `my_crop.MEASURED`. A tap outside the
   field, or on ground the picture check refused, is refused in plain words. Other plants and other green are
   drawn apart, never dropped. Photos with other pixels than about 3 cm and fields over 0.5 ha are refused, as
   the plant finder refuses them.
22. The look model is slow (about 6 s a square on an M1 directly, about 32 s in the local amd64 container; the
   server is not measured). So a field's squares are read once in the background, one field at a time in the
   whole app, and kept by photo and square; the screen is told how many parts are done and asks again with the
   same taps. New taps on a field already read answer at once. The job lives in the app's memory: a restart loses
   the progress shown, not the squares already kept.
23. Counting a crop is for people who may edit the layer (`edit_layer`): the first ask of a field starts minutes of
   work and writes to storage, so a viewer cannot start it. At most 5 fields are read or wait at once
   (`my_crop.MAX_WAITING`); a sixth is told to come back (503). A reading that fails leaves only its reason in
   words, says it once, and the next ask starts again. Kept squares are not removed when a photo is deleted, and
   the queue lives in one process: with more than one app worker each would read on its own.
24. Check a square (`src/services/count_check.py`, routes `.../kept/{id}/count-check` and `.../count-check/square`):
    a person checks the plant count on their own field. The app chooses the part (a 12.5 m square of the photo's
    grid) in an order fixed by the field's line: squares mostly inside the field first, a square the picture check
    refuses never. A part cannot be passed over and cannot be chosen: the next one comes only when this one's
    check is kept, and the server keeps a check only for the part that is next and only against the list of plants
    it showed (Pre-Landing Review 2026-11-15: with "Another part" a reader could skip to the easy parts, and the
    sentence then read as a fair sample when it was a chosen one). The plants shown are the count's own (the same
    finder, centre inside the field). The person taps a ring that is not a plant and the middle of a plant with no
    ring; a plant added within 15 cm of a ring left standing, or of another added plant, is counted once. What is
    said is those parts alone, in the third person ("in the 2 of 9 parts checked by hand, the app counted 48 and
    the person checking counted 51"), because others read it too. Nothing is scaled up to the field: how well a
    few parts stand for a field was not measured, and a figure scaled from them would be a new number that needs
    its own test. A check belongs to the field's line as it is and to the finder's method; change either and it no
    longer counts. Running the finder and keeping a check are for people who may edit the layer; anyone who may
    see the layer reads what was checked. Sage's plant count gives it as `checked_by_a_person`.
    Limits: it is a judgement from the photo, not a count on the ground; a kept check cannot be redone; each part
    is checked by itself, so a plant ringed in two neighbouring parts does not show; a slightly different outline
    of the same field is dealt a new order; one read of the photo at a time and at most 4 waiting (503 past it);
    the lock that orders two keeps on one field lives in one process, like rule 22's queue.
25. One field, one set of facts (`src/services/field_facts.py`; Roger 2026-11-15: the cards, Sage, the field panel
    and the uploaded documents are one body). A field's name ("Field 7" when the reader outlined it, "Plot 7" when
    Ingabe found it, the reader's own name from their map), its size ("0.18 ha", "390 m²") and a plant count
    ("about 700 plants") are worded in that module and nowhere else. It gathers per field what is already kept:
    green cover (only on a photo the rule was checked on), the plant finder's count once someone asked for it, and
    a person's check of that count. The question cards (labels on the photo, the stand count card), Sage
    (`get_drone_photo_findings` lists every field; `count_plants_in_plot`) and the field panel read it. A plot is a
    kept field when it has the field's number and its outline (shared area over joined area at least 0.999: a
    plot set stores outlines after a trip to the photo's coordinates and back, so they are never equal to the last
    bit; found 2026-11-15 when Sage never reported a check that had been kept). It reads only and starts no work.
    A count or a check made on one surface makes the others ask again (the field's row, the cards).
    Not joined yet: uploaded soil and harvest records are matched to a field by name on the cards and for Sage
    but are not shown on the field panel; "only my crop" is not kept, so no other surface can show it; the green
    of the "least green" card (relative) and green cover (absolute) are two measures with two names.

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
| 81 | Only my crop: plant-finder boxes sorted by 3 + 3 taps drawn from two markers' dots, 16 fully counted exam squares, 44 cases | all: 80% of highlights right, 88% of the crop found (56% right with no taps): good enough to build on. One crop in the square: 89% and 91%: passes the client bar. Two or more: 74% and 86%: fails, narrowly. The app's ONNX look model gives the tested looks (lowest patch cosine 1.000 on 12 squares) |
| 82 | More than two correcting taps, limit chosen on the 31 tuning fields by a rule fixed beforehand | the rule said keep two (at exactly three taps two fields were briefly spoiled) and the exam was not run. The rule was wrong for a limit that is a maximum; tuning showed good outlines 21 -> 27 and usable 29 -> 30 by six taps, none lost at five or six |
| 83 | Up to six correcting taps, 26 exam fields, the app's own code, scripted reader, outline as it stands after the last tap | usable 26 (26 at two), close outlines (IoU 0.8) 25 (18 at two), none lost, none worse than its first outline, median area error 4.9% (7.0% at two): raise the limit. One field dipped under usable on the way. No field gained from a fifth or sixth tap |

Rows 70, 75 and 83 give different figures for the same 26 fields at two corrections (25 / 25 / 26 usable; area error 4.7% / 5.9% / 7.0%). They are different runs: Test 70 used the test script's own outline code, Test 75 the app's, and Test 83 the app's after rule 16 stopped offering first choices that run along the window border, which changes which outline is picked on some fields. The text shown to readers (`field_taps.MEASURED`) quotes Test 83's run for both two and six corrections, so its figures compare like with like.

Not measured: green cover on any photo but Cyampirita, or against the ground itself (the readers read the same photo); taps by a real person (the test placed them with a script from hand-drawn outlines), corners, ground
that is not a field, any photo other than Cyampirita. The first correction made the outline worse on 6 of 26 fields
before the second repaired it, so the screen keeps a "back" step.

## How it shows (design rules, plans/PLAN_RWANDA_CROPS.md review part 2, G and H)

- An area under 0.05 ha is shown in square metres (to 10 m2 from 100 m2 up); never "0.00 ha" for a real field. No plus-or-minus is printed: Tests 74 and 75 found no band that is both safe and narrow.
- "This part too" and "Not this part" taps carry a plus and a minus, not colour alone. Every outline has a dark edge.
- An answer slower than about a second says what is happening and how long it can take.
- Removing a kept field asks once. The list of kept fields only selects; removal is on the selected field.
- The photo is the object: while a field is being outlined the layer list, the cards, the legends and the Sage box
  and Sage's answer step aside and come back when the panel closes; "Hide panels" on the map does the same at any time and is
  remembered on the device. The tap panel folds to its line of buttons (the arrow at its corner), also remembered.
- While nothing is being outlined and no field is picked, the tap panel sits at the bottom left, clear of the bottom
  centre where Sage's answer shows: an answer on screen never covers "Outline a field". Once a field is being outlined
  or is picked, the panel takes the bottom centre (Sage's answer has stepped aside by then).
- "Count my crop" is on a picked field. It asks for the crop taps first, then the not-my-crop taps (marked + and ×,
  not by colour alone), each can be taken back, and the other cards step aside as they do while outlining. While
  the field is read it says how many parts are done and that the first count takes minutes. The answer leads with
  "About N plants of your crop", then other plants, other green, the share of the field counted when not all of it
  was, and "How sure is this" in the server's words. Plants are drawn as rings so each shows inside its ring; your crop is the largest ring.
- A plant count, wherever it is drawn (Sage's count of a plot, "Count my crop"), is a ring around each plant
  counted, with the plant showing inside its ring, so the count can be checked against the photo by eye. Solid
  dots are not used: they hide what they mark. Sage's count draws the plants alone; other green is drawn only
  when the person asks where the weeds or grass are (`show_other_green`), since unasked it buries the plants.
  The number is said to two figures ("about 700", never "702"): the test put the total within 18%. Roger,
  2026-11-14, on the live site: "nobody would know what that is by looking at it".
- Small text actions have a 36 px tap height.
