"""Check a square on a made-up photo (bare soil, a green tuft every 2 m). The plant finder is replaced by a stand-in
that boxes every tuft in an even column, so the tests are about the check itself: which square is offered, that the
plants shown are the count's, what a reader may mark, and what is kept and said."""

from __future__ import annotations

import json

import pytest

from src.services import count_check, plant_finder
from src.services.test_plant_finder import N, _photo, _plot
from src.services.test_my_crop import _tap

STEP = 62


class Store:
    """Object storage in memory, as the async client the service is given."""

    class exceptions:  # noqa: N801 - boto3's name
        class NoSuchKey(Exception):
            pass

    def __init__(self):
        self.kept: dict[str, bytes] = {}

    async def get_object(self, Bucket, Key):  # noqa: N803 - boto3's names
        if Key not in self.kept:
            raise self.exceptions.NoSuchKey()
        raw = self.kept[Key]

        class Body:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_):
                return False

            async def read(self):
                return raw

        return {"Body": Body()}

    async def put_object(self, Bucket, Key, Body, **_):  # noqa: N803
        self.kept[Key] = Body


@pytest.fixture
def stand_ins(monkeypatch):
    refused: set[tuple[int, int]] = set()
    runs: list[int] = []

    monkeypatch.setattr(plant_finder, "load_models", lambda s3, bucket: None)
    monkeypatch.setattr(count_check, "_squares", type(count_check._squares)())
    monkeypatch.setattr(count_check, "_refused", type(count_check._refused)())
    monkeypatch.setattr(count_check, "_locks", {})
    return refused, runs


def _row(field_id="f1", cols=2, rows=1):
    feature = _plot(0, 0, cols * N, rows * N)
    return {"id": field_id, "number": 3, "geometry": feature["geometry"], "area_ha": feature["properties"]["area_ha"]}


@pytest.fixture
def finder(monkeypatch, stand_ins):
    """The finder boxes the green tufts it sees in even columns of the square; a square in `refused` fails the
    picture check. Squares are told apart by a mark the photo carries in each square's corner."""
    refused, runs = stand_ins

    def where(rgb):
        return int(rgb[0, 0, 0]), int(rgb[0, 0, 1])

    def boxes(models, rgb):
        runs.append(1)
        j, i = where(rgb)
        found = []
        for cy in range(30, 100 * N, STEP):
            for k, cx in enumerate(range(30, 100 * N, STEP)):
                x, y = cx - j * N, cy - i * N
                if k % 2 == 0 and 0 <= x < N and 0 <= y < N:
                    found.append((x - 9.0, y - 9.0, x + 9.0, y + 9.0))
        return found

    monkeypatch.setattr(plant_finder, "plant_boxes", boxes)
    monkeypatch.setattr(plant_finder, "picture_score", lambda models, rgb: 0.1 if where(rgb) in refused else 0.9)
    return refused, runs


def _marked(tmp_path, squares):
    from src.services.test_my_crop import _photo as marked_photo

    return marked_photo(tmp_path, squares)


def test_the_order_of_squares_is_fixed_for_a_field_and_whole_squares_come_first():
    covered = {(0, 0): 1.0, (1, 0): 0.2, (2, 0): 0.9, (3, 0): 0.6}
    first = count_check.in_order("f1", covered, list(covered))
    assert first == count_check.in_order("f1", covered, list(reversed(list(covered))))  # nobody's choice
    assert first[-1] == (1, 0)  # the sliver is offered last
    orders = {tuple(count_check.in_order(f"f{n}", covered, list(covered))) for n in range(12)}
    assert len(orders) > 1  # another field, another order


def test_what_the_checks_say_together():
    assert count_check.summed([]) is None
    checks = [{"app": 38, "wrong": 2, "missed": 5, "at": "2026-11-15T08:00:00+00:00"},
              {"app": 10, "wrong": 0, "missed": 0, "at": "2026-11-15T09:00:00+00:00"}]
    both = count_check.summed(checks, 13)
    assert (both["app"], both["you"], both["wrong"], both["missed"], both["parts"]) == (48, 51, 2, 5, 2)
    # said of the person who checked, not of whoever reads it: an insurer reading it checked nothing
    assert both["said"] == ("In the 2 of 13 parts of this field checked by hand, the app counted 48 plants and the "
                            "person checking counted 51. 2 of the app's rings were not plants, and it missed 5.")
    one = count_check.summed([{"app": 9, "wrong": 1, "missed": 0, "at": "x"}])
    assert one["said"] == ("In the 1 part of this field checked by hand, the app counted 9 plants and the person "
                           "checking counted 8. 1 of the app's rings was not a plant, and it missed 0.")


def test_a_plant_added_twice_or_on_a_ring_left_standing_is_one_plant():
    lat = -1.95
    metre = 1 / 111_320
    standing = [[30.0, lat]]
    taps = [(30.0 + 0.05 * metre, lat),  # on the ring: the plant it already counts
            (30.0 + 2 * metre, lat), (30.0 + 2.05 * metre, lat),  # the same plant tapped twice
            (30.0 + 4 * metre, lat)]
    assert count_check.added_plants(taps, standing) == [taps[1], taps[3]]
    assert count_check.added_plants(taps, []) == [taps[0], taps[1], taps[3]]  # the ring was struck: a plant may stand there


@pytest.mark.anyio
async def test_the_app_chooses_each_part_in_turn_and_the_readers_check_is_kept(tmp_path, finder):
    refused, runs = finder
    path, store, row = _marked(tmp_path, (2, 1)), Store(), _row()
    shown = await count_check.square(path, store, None, "b", "photo", "L1", row)
    j, i = shown["square"]
    whole = plant_finder.find_plants(path, {"type": "Feature", "geometry": row["geometry"],
                                            "properties": {"number": 3, "area_ha": row["area_ha"]}}, None)
    assert shown["app"] == len(shown["plants"]) > 0 and shown["squares_in_field"] == 2 and shown["checked"] is None
    assert shown["side_m"] == 12.5 and shown["part"]["type"] == "MultiPolygon"
    lats = [p[1] for p in shown["plants"]]
    assert lats[0] > lats[-1]  # top row first: an order that is not the model's
    # asked again, it is the same part: there is no passing over, and it is not read twice
    before = len(runs)
    assert (await count_check.square(path, store, None, "b", "photo", "L1", row))["square"] == shown["square"]
    assert len(runs) == before

    missed = [_tap(j * N + 30 + STEP, 30), _tap(j * N + 30 + STEP, 30 + STEP)]  # two tufts in an odd column
    kept = await count_check.keep(path, store, None, "b", "photo", "L1", row, (j, i), shown["plants_mark"],
                                  [0, 0, 3], missed + missed[:1], "u1")
    assert len(runs) == before
    assert kept["here"] == {"app": shown["app"], "you": shown["app"], "wrong": 2, "missed": 2}
    assert kept["checked"]["parts"] == 1 and "1 of 2 part" in kept["checked"]["said"]
    record = json.loads(next(iter(store.kept.values())))
    assert record[0]["by"] == "u1" and record[0]["struck"] == [0, 3] and len(record[0]["added"]) == 2

    # now the other part is the one to check; the first cannot be kept a second time
    other = await count_check.square(path, store, None, "b", "photo", "L1", row)
    assert other["square"] != shown["square"] and other["checked"]["parts"] == 1
    assert shown["app"] + other["app"] == whole.plants  # the plants shown are the count's, part by part
    with pytest.raises(count_check.CheckProblem, match="not the part to check now"):
        await count_check.keep(path, store, None, "b", "photo", "L1", row, (j, i), shown["plants_mark"], [], [], "u2")
    await count_check.keep(path, store, None, "b", "photo", "L1", row, tuple(other["square"]), other["plants_mark"],
                           [], [], "u2")
    both = await count_check.summary(store, "b", "photo", "L1", row)
    assert both["parts"] == 2 and both["app"] == whole.plants
    with pytest.raises(count_check.CheckProblem, match="every part"):
        await count_check.square(path, store, None, "b", "photo", "L1", row)

    # a field whose line changed has no checks: they were made on another line
    assert await count_check.summary(store, "b", "photo", "L1", _row(cols=1)) is None
    # a record written in an older shape is passed over, not a crash
    store.kept[next(iter(store.kept))] = json.dumps([{"square": [0, 0]}, "nonsense"]).encode()
    assert await count_check.summary(store, "b", "photo", "L1", row) is None


@pytest.mark.anyio
async def test_what_a_check_may_not_say(tmp_path, finder):
    path, store, row = _marked(tmp_path, (2, 1)), Store(), _row()
    shown = await count_check.square(path, store, None, "b", "photo", "L1", row)
    j, i = shown["square"]

    async def keep(at, mark, wrong, missed):
        return await count_check.keep(path, store, None, "b", "photo", "L1", row, at, mark, wrong, missed, "u1")

    with pytest.raises(count_check.CheckProblem, match="not in this part"):
        await keep((j, i), shown["plants_mark"], [shown["app"]], [])
    outside = _tap((1 - j) * N + 40, 40)  # in the field, but in the other part
    with pytest.raises(count_check.CheckProblem, match="outside this part"):
        await keep((j, i), shown["plants_mark"], [], [outside])
    with pytest.raises(count_check.CheckProblem, match="not the part to check now"):  # a part the app did not offer
        await keep((1 - j, i), shown["plants_mark"], [], [])
    with pytest.raises(count_check.CheckProblem, match="not the part to check now"):  # not the plants that were shown
        await keep((j, i), "0" * 16, [], [])
    assert not store.kept


@pytest.mark.anyio
async def test_a_part_the_picture_check_refuses_is_never_offered_and_is_read_once(tmp_path, finder):
    refused, _ = finder
    path, store, row = _marked(tmp_path, (2, 1)), Store(), _row()
    scored: list[int] = []
    score = plant_finder.picture_score
    plant_finder.picture_score = lambda models, rgb: (scored.append(1), score(models, rgb))[1]
    try:
        refused.update({(0, 0), (1, 0)})
        for _ in range(2):
            with pytest.raises(count_check.CheckProblem, match="every part"):
                await count_check.square(path, store, None, "b", "photo", "L1", row)
        assert len(scored) == 2  # each part judged once, not again on the second ask
    finally:
        plant_finder.picture_score = score


@pytest.mark.anyio
async def test_too_many_reads_at_once_are_told_to_come_back(tmp_path, finder, monkeypatch):
    path, store, row = _marked(tmp_path, (2, 1)), Store(), _row()
    monkeypatch.setattr(count_check, "_waiting", count_check.MAX_WAITING)
    with pytest.raises(count_check.Busy):
        await count_check.square(path, store, None, "b", "photo", "L1", row)
