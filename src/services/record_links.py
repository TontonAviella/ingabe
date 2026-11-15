"""Which field a row of a farm document belongs to: joined by place on the ground, never by a number that matches.

Roger, 2026-11-15: a field may carry one number in Ingabe and another in an uploaded document. Before this, a lab
sample labelled "7" was attached to Ingabe's Field 7, whose "7" only means the seventh field someone outlined. A
field is a place; its number is a label, and the document's number is somebody else's label. So a row is linked to
a field in two ways only:

- the document prints a position for the row, and that position lies inside one field, clear of its edge;
- a person says once which field the row is. What is kept is that field's outline, not its number, so the link
  holds when fields are numbered differently or outlined again a little differently, and does not pass to a
  different field drawn later over part of the same ground.

A name or number that matches is offered as a suggestion for a person to confirm, never applied. A row that is not
linked stays in the farm's records as "not linked to a field yet". Every surface (the cards, Sage, the field
panel) reads the links from `resolve`, and words a linked row with `row_said`.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional, Sequence

from shapely.geometry import Point, shape
from shapely.ops import transform, unary_union
from shapely.validation import make_valid

from src.services import farm_records
from src.services.field_facts import area_said

logger = logging.getLogger(__name__)

SAME_LAND = 0.5  # shared area over joined area: an outline a person pointed at and a field this alike are one land
EDGE_M = 5.0  # a printed position closer than this to a field's edge is a suggestion, not a link: a handheld GPS
# is good to a few metres, and a sample taken on a field's edge can be read into the next field
SIZE_APART = 2.0  # a record's own area this many times larger or smaller than the field's measured size is said
SOIL, HARVEST = "soil", "harvest"
BY_DOCUMENT, BY_PERSON = "document", "person"
NOT_LINKED = "not linked to a field yet"
_STORE_PREFIX = "farm_records/v1"


@dataclass(frozen=True)
class Row:
    key: str  # "<document id>:soil:<n>" or "<document id>:harvest:<n>"
    kind: str
    label: str  # what the document calls it
    names: tuple[str, ...]  # the document's own names for the place, for suggestions only
    value: str  # what it measured, as said everywhere
    document: str
    coordinates: Optional[str] = None  # the position as the document prints it
    area_ha: Optional[float] = None  # a harvest record's own area


@dataclass(frozen=True)
class Linked:
    row: Row
    number: Optional[int] = None  # the field it is linked to
    by: Optional[str] = None  # BY_DOCUMENT (its printed position) or BY_PERSON
    suggest: Optional[int] = None  # a field a person may confirm
    suggest_why: Optional[str] = None
    note: Optional[str] = None  # what a reader should know: sizes that disagree, a position off the photo
    # the row has a place (a person's link, or a position the document prints) and it is on no field of this photo:
    # another photo of the farm, or land not outlined here. It is not counted as waiting to be linked.
    elsewhere: bool = False


def _num(value: Optional[float], digits: int) -> str:
    return "n/a" if value is None else f"{value:.{digits}f}"


def soil_said(sample: farm_records.SoilSample) -> str:
    return (f"pH {_num(sample.ph, 1)} · N {_num(sample.nitrogen_percent, 2)} % · "
            f"P {_num(sample.phosphorus_mg_kg, 1)} mg/kg · K {_num(sample.potassium_cmol_kg, 2)} cmol/kg")


def harvest_said(harvest: farm_records.Harvest) -> str:
    parts = [f"{_num(harvest.harvest_kg, 0)} kg" + (f" of {harvest.crop}" if harvest.crop else "")]
    if harvest.tonnes_per_ha is not None:
        parts.append(f"{harvest.tonnes_per_ha:.1f} t/ha on the record's own area")
    return " · ".join(parts)


def _document(doc: farm_records.FarmDocument) -> str:
    lab = (doc.source or doc.title or doc.filename or "").split(";")[0].strip()
    return ", ".join(x for x in (lab, doc.date) if x)


def rows(records: Sequence[farm_records.FarmDocument]) -> list[Row]:
    """Every row of the project's documents, each with a key that stays the same while the document is kept."""
    out: list[Row] = []
    for doc in records:
        if doc.kind == farm_records.SOIL_REPORT:
            for n, s in enumerate(doc.soil_samples):
                out.append(Row(key=f"{doc.id}:{SOIL}:{n}", kind=SOIL,
                               label=" · ".join(x for x in (s.sample, s.place) if x) or f"Sample {n + 1}",
                               names=tuple(x for x in (s.place, s.sample) if x), value=soil_said(s),
                               document=_document(doc), coordinates=s.coordinates))
        elif doc.kind == farm_records.HARVEST_RECORDS:
            for n, h in enumerate(doc.harvests):
                out.append(Row(key=f"{doc.id}:{HARVEST}:{n}", kind=HARVEST,
                               label=" · ".join(x for x in (h.plot, h.farmer, h.season) if x) or f"Harvest {n + 1}",
                               names=tuple(x for x in (h.plot,) if x), value=harvest_said(h),
                               document=_document(doc), coordinates=h.coordinates, area_ha=h.area_ha))
    return out


def row_said(linked: Linked) -> str:
    """A linked row as it is said on every surface: "Soil sample S3 · Block A: pH 5.1 · ... (Lab, 2026-03)"."""
    row = linked.row
    kind = "Soil sample" if row.kind == SOIL else "Harvest"
    text = f"{kind} {row.label}: {row.value}" + (f" ({row.document})" if row.document else "")
    if linked.by == BY_DOCUMENT:  # the position it was linked by, so a reader can check it against the page
        text += f", taken at {row.coordinates} as the document prints it"
    return f"{text}. {linked.note}" if linked.note else text


# --- A position as a document prints it ---------------------------------------------------------

_ANGLE = re.compile(r"(-?\d+(?:\.\d+)?)\s*(?:°|º|deg)?\s*(?:(\d+(?:\.\d+)?)\s*['′’]\s*)?(?:(\d+(?:\.\d+)?)\s*(?:\"|″|”|'')\s*)?")
_LETTER = re.compile(r"(?<![A-Za-z])[NSEWnsew](?![A-Za-z])")


def positions(text: Optional[str]) -> list[tuple[float, float]]:
    """The (lon, lat) points a printed position can mean: decimal degrees or degrees-minutes-seconds, with or
    without N/S/E/W. One point when the letters say which is which; both orders when they do not (the photo then
    decides: only one of them can lie on it). Empty for anything else (a grid reference, a place name)."""
    if not text:
        return []
    plain = text.replace(",", " ").replace(";", " ")
    for dash in "\u2212\u2013\u2014":  # a minus sign as PDFs print it
        plain = plain.replace(dash, "-")
    angles = _ANGLE.findall(plain)
    letters = [m.upper() for m in _LETTER.findall(plain)]
    if len(angles) != 2 or len(letters) not in (0, 2):
        return []
    values = []
    for n, (degrees, minutes, seconds) in enumerate(angles):
        value = abs(float(degrees)) + float(minutes or 0) / 60 + float(seconds or 0) / 3600
        values.append(-value if degrees.startswith("-") or (letters and letters[n] in "SW") else value)
    a, b = values
    if letters:
        if (letters[0] in "NS") == (letters[1] in "NS"):
            return []
        orders = [(b, a)] if letters[0] in "NS" else [(a, b)]
    else:
        orders = [(b, a), (a, b)]  # latitude first is the common way; the other order is tried too
    return [(lon, lat) for lon, lat in orders if abs(lat) <= 90 and abs(lon) <= 180]


def precision_m(text: Optional[str]) -> float:
    """How far off a printed position can be from its last printed digit alone, in metres: "-1.95, 30.06" is good to
    about 550 m, five decimals to about half a metre, whole seconds of arc to about 15 m."""
    worst = 0.0
    for degrees, minutes, seconds in _ANGLE.findall(text or ""):
        last, per_degree = (seconds, 3600) if seconds else (minutes, 60) if minutes else (degrees, 1)
        decimals = len(last.split(".")[1]) if "." in last else 0
        worst = max(worst, 0.5 * 10 ** -decimals / per_degree * 111_320)
    return worst


# --- Which field a point is in -------------------------------------------------------------------

def name_key(name: Optional[str]) -> str:
    """A plot name reduced so 'Block A-01', 'A01' and 'a-1' compare equal. For suggestions only."""
    text = re.sub(r"\b(block|plot|field|parcel|umurima)\b", "", (name or "").lower())
    parts = re.findall(r"[a-z]+|\d+", text)
    return "".join(p.lstrip("0") or "0" if p.isdigit() else p for p in parts)


def _in_metres(geometry: Any, lat: float) -> Any:
    east = 111_320 * math.cos(math.radians(lat))
    return transform(lambda x, y, *_: (x * east, y * 110_540), geometry)


def _area_of(geometry: dict[str, Any]) -> Any:
    """An outline's area alone: repairing a broken outline can leave stray lines and points, which hold nothing."""
    valid = make_valid(shape(geometry))
    parts = [g for g in getattr(valid, "geoms", [valid]) if g.geom_type in ("Polygon", "MultiPolygon")]
    if not parts:
        raise ValueError("an outline with no area")
    return unary_union(parts)


def _fields(features: Sequence[dict[str, Any]]) -> list[tuple[int, Any, dict[str, Any]]]:
    out = []
    for feature in features:
        try:
            out.append((feature["properties"]["number"], _area_of(feature["geometry"]), feature["properties"]))
        except Exception:  # a broken outline holds no records; the others still do
            logger.warning("record links: an outline could not be read", exc_info=True)
    return out


def _around(point: tuple[float, float], fields: list[tuple[int, Any, dict[str, Any]]],
            clear_m: float) -> tuple[list[int], list[int]]:
    """(fields the point is inside and at least `clear_m` from the edge, fields whose edge is nearer than that)."""
    lon, lat = point
    spot = _in_metres(Point(lon, lat), lat)
    reach = clear_m / 100_000  # degrees, generously
    inside, near = [], []
    for number, outline, _ in fields:
        minx, miny, maxx, maxy = outline.bounds
        if not (minx - reach <= lon <= maxx + reach and miny - reach <= lat <= maxy + reach):
            continue
        metres = _in_metres(outline, lat)
        if metres.boundary.distance(spot) < clear_m:
            near.append(number)
        elif metres.contains(spot):
            inside.append(number)
    return inside, near


def _size_note(row: Row, props: dict[str, Any]) -> Optional[str]:
    measured = props.get("area_ha")
    if row.kind != HARVEST or not row.area_ha or not measured:
        return None
    if row.area_ha / measured >= SIZE_APART or measured / row.area_ha >= SIZE_APART:
        return (f"The record gives this plot as {area_said(row.area_ha)}; the field it is linked to "
                f"measures {area_said(measured)}, so check that it is the same land")
    return None


def _by_name(row: Row, fields: list[tuple[int, Any, dict[str, Any]]]) -> Optional[tuple[int, str]]:
    keys = {name_key(n) for n in row.names} - {""}
    hits = [(number, props) for number, _, props in fields
            if keys & {name_key(props.get("name")), name_key(str(number))} - {""}]
    if len(hits) != 1:
        return None
    number, props = hits[0]
    same = "name" if name_key(props.get("name")) in keys else "number"
    return number, f"the document uses the same {same}; that alone does not make it the same land"


def _same_land(then: Any, fields: list[tuple[int, Any, dict[str, Any]]]) -> tuple[Optional[int], Optional[int]]:
    """(the field that is the land of an outline a person pointed at, a field that only shares ground with it).
    The same land: shared area over joined area of SAME_LAND or more, so a field outlined again a little
    differently is still it, and a different field drawn over part of the ground is not."""
    best, number = 0.0, None
    for n, outline, _ in fields:
        joined = outline.union(then).area
        share = outline.intersection(then).area / joined if joined > 0 else 0.0
        if share > best:
            best, number = share, n
    if number is None:
        return None, None
    return (number, None) if best >= SAME_LAND else (None, number)


def _resolve(row: Row, kept: Optional[dict[str, Any]], fields: list[tuple[int, Any, dict[str, Any]]],
             shared: int) -> Linked:
    by_number = {number: props for number, _, props in fields}
    refused: Optional[int] = None  # the field a person said this row is not
    if kept is not None:
        same, overlaps = _same_land(_area_of(kept["outline"]), fields)
        if kept.get("linked"):
            if same is not None:
                return Linked(row, number=same, by=BY_PERSON, note=_size_note(row, by_number[same]))
            if overlaps is not None:
                return Linked(row, suggest=overlaps, suggest_why=(
                    "a person linked it to an outline that covered part of this field; the outline has changed since"))
            return Linked(row, elsewhere=True,
                          note="A person linked this to land that has no field outlined on this photo")
        refused = same
    clear = max(EDGE_M, precision_m(row.coordinates))
    for point in positions(row.coordinates):
        inside, near = _around(point, fields, clear)
        if not inside and not near:
            continue
        first = (inside or near)[0]
        if first == refused:
            return Linked(row)
        where = f"the document's position ({row.coordinates})"
        if len(inside) == 1 and not near and shared < 2:
            return Linked(row, number=first, by=BY_DOCUMENT, note=_size_note(row, by_number[first]))
        if len(inside) == 1 and not near:
            why = (f"{where} lies in this field, but the document prints that same position for {shared} rows, so "
                   "it may be the farm's position and not this row's")
        elif len(inside) > 1:
            why = f"{where} lies on more than one field"
        elif clear > EDGE_M:
            why = (f"{where} is only good to about {clear:.0f} m as printed, and this field's edge is nearer than "
                   "that")
        elif inside:
            why = f"{where} lies in this field, but within {EDGE_M:.0f} m of another field's edge"
        else:
            why = f"{where} is within {EDGE_M:.0f} m of this field's edge"
        return Linked(row, suggest=first, suggest_why=why)
    if positions(row.coordinates):
        return Linked(row, elsewhere=True,
                      note=f"The document's position ({row.coordinates}) is not on a field outlined on this photo")
    if row.coordinates:
        return Linked(row, note=f"The document gives a position Ingabe cannot read ({row.coordinates})")
    suggestion = _by_name(row, fields)
    if suggestion is not None and suggestion[0] != refused:
        return Linked(row, suggest=suggestion[0], suggest_why=suggestion[1])
    return Linked(row)


def resolve(records: Sequence[farm_records.FarmDocument], links: dict[str, dict[str, Any]],
            features: Sequence[dict[str, Any]]) -> list[Linked]:
    """Every row of the documents with the field it belongs to among `features` (GeoJSON features with a number
    and an area_ha), if it is linked. A row that cannot be resolved is still returned, not linked."""
    fields = _fields(features)
    every = rows(records)
    # a position printed for several rows of one document is likely the farm's, not each row's
    printed: dict[tuple[str, str], int] = {}
    for row in every:
        if row.coordinates:
            at = (row.key.split(":")[0], " ".join(row.coordinates.split()))
            printed[at] = printed.get(at, 0) + 1
    out: list[Linked] = []
    for row in every:
        shared = printed.get((row.key.split(":")[0], " ".join((row.coordinates or "").split())), 0)
        try:
            out.append(_resolve(row, links.get(row.key), fields, shared))
        except Exception:
            logger.exception("record links: row %s could not be placed", row.key)
            out.append(Linked(row))
    return out


def kept_features(kept_rows: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """The reader's kept fields in the shape `resolve` reads."""
    return [{"geometry": row["geometry"], "properties": {"number": row["number"], "area_ha": row.get("area_ha"),
                                                          "name": row.get("name")}}
            for row in kept_rows]


def soil(records: Sequence[farm_records.FarmDocument], linked: Sequence[Linked],
         ) -> list[tuple[farm_records.FarmDocument, farm_records.SoilSample, Optional[Linked]]]:
    """Every soil sample with its link, in the documents' order."""
    known = {item.row.key: item for item in linked}
    return [(doc, s, known.get(f"{doc.id}:{SOIL}:{n}")) for doc in records if doc.kind == farm_records.SOIL_REPORT
            for n, s in enumerate(doc.soil_samples)]


def harvests(records: Sequence[farm_records.FarmDocument], linked: Sequence[Linked],
             ) -> list[tuple[farm_records.FarmDocument, farm_records.Harvest, Optional[Linked]]]:
    known = {item.row.key: item for item in linked}
    return [(doc, h, known.get(f"{doc.id}:{HARVEST}:{n}")) for doc in records
            if doc.kind == farm_records.HARVEST_RECORDS for n, h in enumerate(doc.harvests)]


def by_field(linked: Sequence[Linked]) -> dict[int, list[str]]:
    """What the linked rows say, per field number."""
    out: dict[int, list[str]] = {}
    for item in linked:
        if item.number is not None:
            out.setdefault(item.number, []).append(row_said(item))
    return out


def not_linked(linked: Sequence[Linked]) -> int:
    """Rows still waiting for a field: on no field, and not placed on other land either."""
    return sum(1 for item in linked if item.number is None and not item.elsewhere)


# --- Kept per project ---------------------------------------------------------------------------
# One small object per row a person spoke for, so two people linking at once (the server runs several workers)
# cannot overwrite each other. A read is one listing; an entry is fetched again only when it changed.

_entries: dict[tuple[str, str], dict[str, Any]] = {}  # (object key, ETag) -> entry
_MAX_CACHED = 5000


def _prefix(project_id: str) -> str:
    return f"{_STORE_PREFIX}/{project_id}/links/"


def _key(project_id: str, row_key: str) -> str:
    return f"{_prefix(project_id)}{hashlib.sha256(row_key.encode()).hexdigest()[:32]}.json"


async def _entry(s3: Any, bucket: str, key: str, etag: str) -> Optional[dict[str, Any]]:
    if (key, etag) in _entries:
        return _entries[(key, etag)]
    try:
        response = await s3.get_object(Bucket=bucket, Key=key)
        async with response["Body"] as body:
            entry = json.loads(await body.read())
        if not (isinstance(entry, dict) and isinstance(entry.get("row"), str) and isinstance(entry.get("outline"), dict)):
            raise ValueError("not a link")
    except Exception:  # one unreadable link loses that link; its row is then shown as not linked
        logger.exception("record links: %s cannot be read", key)
        return None
    if len(_entries) >= _MAX_CACHED:
        _entries.clear()
    _entries[(key, etag)] = entry
    return entry


async def load(s3: Any, bucket: str, project_id: str) -> dict[str, dict[str, Any]]:
    """What people said about rows, by row key."""
    listed: list[tuple[str, str]] = []
    token: Optional[str] = None
    while True:
        more = {"ContinuationToken": token} if token else {}
        page = await s3.list_objects_v2(Bucket=bucket, Prefix=_prefix(project_id), **more)
        listed += [(o["Key"], str(o.get("ETag", ""))) for o in page.get("Contents", [])]
        token = page.get("NextContinuationToken")
        if not token:
            break
    entries = await asyncio.gather(*(_entry(s3, bucket, key, etag) for key, etag in listed))
    return {e["row"]: e for e in entries if e is not None}


async def keep(s3: Any, bucket: str, project_id: str, row_key: str, geometry: dict[str, Any], linked: bool,
               user_id: str) -> None:
    """A person's word on a row and a field: it is this land (`linked`), or it is not. The field's outline is
    kept, not its number. Saying "not this land" also sets aside a position the document prints inside it."""
    _area_of(geometry)  # refuses an outline with no area
    entry = {"row": row_key, "outline": geometry, "linked": linked, "by": user_id,
             "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    await s3.put_object(Bucket=bucket, Key=_key(project_id, row_key), ContentType="application/json",
                        Body=json.dumps(entry).encode())


async def of_project(s3: Any, bucket: str, project_id: Optional[str],
                     features: Sequence[dict[str, Any]]) -> list[Linked]:
    """The project's document rows placed on these fields: the records, and one listing of the links."""
    if not project_id:
        return []
    records = await farm_records.load_records(s3, bucket, project_id)
    if not records:
        return []
    return resolve(records, await load(s3, bucket, project_id), features)
