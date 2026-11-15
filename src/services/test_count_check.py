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
    assert both["said"] == ("In the 2 of 13 parts you checked, the app counted 48 plants and you counted 51. "
                            "2 of its rings were not plants, and it missed 5.")
    one = count_check.summed([{"app": 9, "wrong": 1, "missed": 0, "at": "x"}])
    assert one["said"] == ("In the 1 part you checked, the app counted 9 plants and you counted 8. "
                           "1 of its rings was not a plant, and it missed 0.")


@pytest.mark.anyio
async def test_a_square_is_shown_with_the_counts_plants_and_the_readers_check_is_kept(tmp_path, finder):
    refused, runs = finder
    path, store, row = _marked(tmp_path, (2, 1)), Store(), _row()
    shown = await count_check.square(path, store, None, "b", "photo", "L1", row, [])
    j, i = shown["square"]
    whole = plant_finder.find_plants(path, {"type": "Feature", "geometry": row["geometry"],
                                            "properties": {"number": 3, "area_ha": row["area_ha"]}}, None)
    other = await count_check.square(path, store, None, "b", "photo", "L1", row, [shown["square"]])
    assert other["square"] != shown["square"]
    assert shown["app"] + other["app"] == whole.plants  # the plants shown are the count's, square by square
    assert shown["app"] == len(shown["plants"]) > 0 and shown["squares_in_field"] == 2 and shown["checked"] is None
    assert shown["side_m"] == 12.5

    before = len(runs)
    missed = [_tap(j * N + 30 + STEP, 30), _tap(j * N + 30 + STEP, 30 + STEP)]  # two tufts in an odd column
    kept = await count_check.keep(path, store, None, "b", "photo", "L1", row, (j, i), [0, 0, 3], missed, "u1")
    assert len(runs) == before  # the square shown a moment ago is not read again
    assert kept["here"] == {"app": shown["app"], "you": shown["app"], "wrong": 2, "missed": 2}
    assert kept["checked"]["parts"] == 1 and "of 2 part" in kept["checked"]["said"]
    record = json.loads(next(iter(store.kept.values())))
    assert record[0]["by"] == "u1" and record[0]["struck"] == [0, 3] and len(record[0]["added"]) == 2

    # the checked square is passed over; checking it again replaces the first check
    again = await count_check.square(path, store, None, "b", "photo", "L1", row, [])
    assert again["square"] == other["square"] and again["checked"]["parts"] == 1
    await count_check.keep(path, store, None, "b", "photo", "L1", row, (j, i), [], [], "u1")
    assert (await count_check.summary(store, "b", "photo", "L1", row))["wrong"] == 0
    with pytest.raises(count_check.CheckProblem, match="has been shown"):
        await count_check.square(path, store, None, "b", "photo", "L1", row, [other["square"]])

    # a field whose line changed has no checks: they were made on another line
    moved = _row(cols=1)
    assert await count_check.summary(store, "b", "photo", "L1", moved) is None


@pytest.mark.anyio
async def test_what_a_check_may_not_say(tmp_path, finder):
    refused, _ = finder
    path, store, row = _marked(tmp_path, (2, 1)), Store(), _row()
    shown = await count_check.square(path, store, None, "b", "photo", "L1", row, [])
    j, i = shown["square"]
    with pytest.raises(count_check.CheckProblem, match="not in this part"):
        await count_check.keep(path, store, None, "b", "photo", "L1", row, (j, i), [shown["app"]], [], "u1")
    outside = _tap((1 - j) * N + 40, 40)  # in the field, but in the other square
    with pytest.raises(count_check.CheckProblem, match="outside this part"):
        await count_check.keep(path, store, None, "b", "photo", "L1", row, (j, i), [], [outside], "u1")
    with pytest.raises(count_check.CheckProblem, match="not in this field"):
        await count_check.keep(path, store, None, "b", "photo", "L1", row, (5, 5), [], [], "u1")
    assert not store.kept
    refused.add((1 - j, i))
    with pytest.raises(count_check.CheckProblem, match="not counted in that part"):
        await count_check.keep(path, store, None, "b", "photo", "L1", row, (1 - j, i), [], [], "u1")


@pytest.mark.anyio
async def test_a_square_the_picture_check_refuses_is_never_offered(tmp_path, finder):
    refused, _ = finder
    path, store, row = _marked(tmp_path, (2, 1)), Store(), _row()
    refused.update({(0, 0), (1, 0)})
    with pytest.raises(count_check.CheckProblem):
        await count_check.square(path, store, None, "b", "photo", "L1", row, [])
    refused.discard((1, 0))
    assert (await count_check.square(path, store, None, "b", "photo", "L1", row, []))["square"] == [1, 0]
