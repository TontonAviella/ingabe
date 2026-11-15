"""A document row is joined to a field by place, never by a matching number (src/services/record_links.py)."""

from __future__ import annotations

import pytest

from src.services import farm_records, record_links


def _field(number, west, south=-2.0, size=0.001, **extra):
    return {"type": "Feature", "properties": {"number": number, "area_ha": 1.2, **extra},
            "geometry": {"type": "Polygon", "coordinates": [[[west, south], [west + size, south], [west + size, south + size],
                                                             [west, south + size], [west, south]]]}}


FIELDS = [_field(7, 30.000), _field(8, 30.001), _field(9, 30.010, name="Block A")]  # 7 and 8 share an edge


def _soil(*samples):
    return farm_records.FarmDocument(
        id="lab", filename="lab.pdf", file_key="k", kind=farm_records.SOIL_REPORT, title="Soil report", source="Lab",
        date="2026-03", added_at="2026-11-15T00:00:00+00:00",
        soil_samples=[farm_records.SoilSample(name, place, "0-20 cm", 5.1, 1.0, 0.1, 4.0, None, 0.2, None, where)
                      for name, place, where in samples])


def _harvests(*rows):
    return farm_records.FarmDocument(
        id="book", filename="book.jpg", file_key="k", kind=farm_records.HARVEST_RECORDS, title=None, source=None,
        date=None, added_at="2026-11-15T00:00:00+00:00",
        harvests=[farm_records.Harvest(plot, None, "maize", "2026A", None, 600, area, where) for plot, area, where in rows])


def test_a_printed_position_is_read_in_the_ways_documents_print_it():
    assert record_links.positions("1.9536 S, 30.0605 E") == [(30.0605, -1.9536)]
    assert record_links.positions("30.0605E 1.9536S") == [(30.0605, -1.9536)]
    (lon, lat), = record_links.positions("S 01°57'13\" E 030°03'38\"")
    assert (round(lon, 4), round(lat, 4)) == (30.0606, -1.9536)
    # no letters: latitude first is tried, and the other order too; the photo decides which lies on it
    assert record_links.positions("-1.9536, 30.0605") == [(30.0605, -1.9536), (-1.9536, 30.0605)]
    for not_a_position in (None, "", "Block A", "7", "36M 512345 9784321", "1.95 N, 2.10 N"):
        assert record_links.positions(not_a_position) == []


def test_a_matching_number_is_never_a_link_only_a_suggestion():
    # the lab called its sample "7"; the photo's Field 7 is only the seventh field someone outlined
    (row,) = record_links.resolve([_soil(("7", None, None))], {}, FIELDS)
    assert row.number is None and row.by is None
    assert row.suggest == 7 and "same number" in row.suggest_why
    (named,) = record_links.resolve([_soil(("S1", "block a", None))], {}, FIELDS)
    assert named.number is None and named.suggest == 9 and "same name" in named.suggest_why
    assert record_links.by_field([row, named]) == {} and record_links.not_linked([row, named]) == 2


def test_a_position_in_the_document_links_the_row_to_the_field_it_lies_in_whatever_the_names_say():
    # sample "7" was taken in the middle of the photo's Field 8
    (row,) = record_links.resolve([_soil(("7", None, "-1.99950, 30.00150"))], {}, FIELDS)
    assert (row.number, row.by, row.suggest) == (8, record_links.BY_DOCUMENT, None)
    assert record_links.by_field([row]) == {8: ["Soil sample 7: pH 5.1 · N 0.10 % · P 4.0 mg/kg · K 0.20 cmol/kg (Lab, 2026-03), taken at -1.99950, 30.00150 "
                                                 "as the document prints it"]}


def test_a_position_near_an_edge_or_off_the_photo_is_not_a_link():
    # 2 m inside Field 7 from the edge it shares with Field 8: a GPS cannot tell those apart
    (edge,) = record_links.resolve([_soil(("S1", None, "-1.99950, 30.00098"))], {}, FIELDS)
    assert edge.number is None and edge.suggest in (7, 8) and "within 5 m" in edge.suggest_why
    # printed to two decimals a position is good to about 550 m: never a link, however deep in a field it falls
    (coarse,) = record_links.resolve([_soil(("S6", None, "-2.00, 30.00"))], {}, [_field(1, 29.997, south=-2.003, size=0.006)])
    assert coarse.number is None and coarse.suggest == 1 and "only good to about" in coarse.suggest_why
    # one position printed for every row of a document is likely the farm's, not each sample's
    same = record_links.resolve([_soil(("S7", None, "-1.99950, 30.00150"), ("S8", None, "-1.99950, 30.00150"))], {}, FIELDS)
    assert [(r.number, r.suggest) for r in same] == [(None, 8), (None, 8)] and "same position for 2 rows" in same[0].suggest_why
    (off,) = record_links.resolve([_soil(("S2", None, "-1.5, 29.7"))], {}, FIELDS)
    assert off.number is None and off.suggest is None and off.elsewhere and "not on a field outlined" in off.note
    # a minus sign as a PDF prints it is still a minus
    assert record_links.positions("\u22121.9995, 30.0015") == record_links.positions("-1.9995, 30.0015")
    # clearly inside Field 9 but beside a field drawn over its edge: offered for Field 9, not the neighbour
    beside = FIELDS + [_field(10, 30.0105, size=0.00048)]
    (both,) = record_links.resolve([_soil(("S4", None, "-1.99980, 30.01047"))], {}, beside)
    assert both.number is None and both.suggest == 9
    # an outline that repairs into an area and a stray line still holds its samples
    spike = {"type": "Feature", "properties": {"number": 5, "area_ha": 1.2}, "geometry": {"type": "Polygon", "coordinates": [[
        [31.0, -2.0], [31.001, -2.0], [31.001, -1.999], [31.002, -1.999], [31.001, -1.999], [31.0, -1.999], [31.0, -2.0]]]}}
    (held,) = record_links.resolve([_soil(("S5", None, "-1.9995, 31.0005"))], {}, [spike])
    assert held.number == 5
    (grid,) = record_links.resolve([_soil(("S3", None, "36M 512345 9784321"))], {}, FIELDS)
    assert grid.number is None and "cannot read" in grid.note


class _Store:
    def __init__(self):
        self.objects, self.exceptions = {}, type("E", (), {"NoSuchKey": KeyError})

    async def get_object(self, Bucket, Key):
        body = self.objects[Key]

        class Body:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def read(self):
                return body
        return {"Body": Body()}

    async def put_object(self, Bucket, Key, Body, **_):
        self.objects[Key] = Body

    async def list_objects_v2(self, Bucket, Prefix, **_):
        return {"Contents": [{"Key": k, "ETag": str(hash(v))} for k, v in self.objects.items() if k.startswith(Prefix)]}


@pytest.mark.asyncio
async def test_a_persons_link_is_to_the_land_so_it_outlasts_the_numbering():
    s3, records = _Store(), [_harvests(("7", 0.3, None))]
    key = record_links.rows(records)[0].key
    await record_links.keep(s3, "b", "p", key, FIELDS[2]["geometry"], True, "user")
    links = await record_links.load(s3, "b", "p")
    (row,) = record_links.resolve(records, links, FIELDS)
    assert (row.number, row.by) == (9, record_links.BY_PERSON)
    # the fields are outlined again, a little differently, and the same land is now number 2: the link follows the land
    again = [_field(1, 30.000), _field(2, 30.01003)]
    assert record_links.resolve(records, links, again)[0].number == 2
    # the record says 0.3 ha, the field measures 1.2 ha: said, not hidden
    assert "0.30 ha" in row.note and "1.2 ha" in row.note and row.note in record_links.row_said(row)
    # a different, larger field drawn later over that ground is not what the person pointed at: offered, never linked
    other = record_links.resolve(records, links, [_field(4, 30.0095, south=-2.001, size=0.003)])[0]
    assert other.number is None and other.suggest == 4 and "outline has changed" in other.suggest_why
    # the land has no field on this photo: not linked, said why, and not counted as waiting to be linked here
    gone = record_links.resolve(records, links, FIELDS[:2])[0]
    assert gone.number is None and gone.elsewhere and "no field outlined on this photo" in gone.note
    assert record_links.not_linked([gone]) == 0
    # a person can say a row is not this field, which sets aside the document's own position in it, there only
    placed = [_soil(("S9", None, "-1.99950, 30.00150"))]
    key = record_links.rows(placed)[0].key
    assert record_links.resolve(placed, {}, FIELDS)[0].number == 8
    await record_links.keep(s3, "b", "p", key, FIELDS[1]["geometry"], False, "user")
    links = await record_links.load(s3, "b", "p")
    assert len(links) == 2  # each row's word is its own object: two people linking at once cannot overwrite each other
    assert record_links.resolve(placed, links, FIELDS)[0].number is None
    # one unreadable link loses that link only
    s3.objects["farm_records/v1/p/links/broken.json"] = b"{"
    assert set(await record_links.load(s3, "b", "p")) == set(links)


def test_records_kept_before_positions_were_read_still_load():
    old = {"id": "d", "filename": "f", "file_key": "k", "kind": farm_records.SOIL_REPORT, "title": None, "source": None,
           "date": None, "added_at": "x", "warnings": [], "model": "", "harvests": [],
           "soil_samples": [{"sample": "S1", "place": None, "depth": None, "ph": 5.0, "organic_carbon_percent": None,
                             "nitrogen_percent": None, "phosphorus_mg_kg": None, "phosphorus_method": None,
                             "potassium_cmol_kg": None, "texture": None}]}
    assert farm_records._from_dict(old).soil_samples[0].coordinates is None
