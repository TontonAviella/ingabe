"""Which field a row of a farm document belongs to: joined by place on the ground, never by a number that matches.

Roger, 2026-11-15: a field may carry one number in Ingabe and another in an uploaded document. Before this, a lab
sample labelled "7" was attached to Ingabe's Field 7, whose "7" only means the seventh field someone outlined. A
field is a place; its number is a label, and the document's number is somebody else's label. So a row is linked to
a field in two ways only:

- the document prints a position for the row, and that position lies inside one field, clear of its edge;
- a person says once which field the row is. What is kept is a point inside that field, not its number, so the
  link holds when fields are outlined again or numbered differently.

A name or number that matches is offered as a suggestion for a person to confirm, never applied. A row that is not
linked stays in the farm's records as "not linked to a field yet". Every surface (the cards, Sage, the field
panel) reads the links from `resolve`, and words a linked row with `row_said`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional, Sequence

from shapely.geometry import Point, shape
from shapely.ops import transform
from shapely.validation import make_valid

from src.services import farm_records
from src.services.field_facts import area_said

logger = logging.getLogger(__name__)

EDGE_M = 5.0  # a printed position closer than this to a field's edge is a suggestion, not a link: a handheld GPS
# is good to a few metres, and a sample taken on a field's edge can be read into the next field
SIZE_APART = 2.0  # a record's own area this many times larger or smaller than the field's measured size is said
SOIL, HARVEST = "soil", "harvest"
BY_DOCUMENT, BY_PERSON = "document", "person"
NOT_LINKED = "not linked to a field yet"
_STORE_PREFIX = "farm_records/v1"
_locks: dict[str, asyncio.Lock] = {}


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


# --- Which field a point is in -------------------------------------------------------------------

def name_key(name: Optional[str]) -> str:
    """A plot name reduced so 'Block A-01', 'A01' and 'a-1' compare equal. For suggestions only."""
    text = re.sub(r"\b(block|plot|field|parcel|umurima)\b", "", (name or "").lower())
    parts = re.findall(r"[a-z]+|\d+", text)
    return "".join(p.lstrip("0") or "0" if p.isdigit() else p for p in parts)


def _in_metres(geometry: Any, lat: float) -> Any:
    east = 111_320 * math.cos(math.radians(lat))
    return transform(lambda x, y, *_: (x * east, y * 110_540), geometry)


def _fields(features: Sequence[dict[str, Any]]) -> list[tuple[int, Any, dict[str, Any]]]:
    out = []
    for feature in features:
        try:
            out.append((feature["properties"]["number"], make_valid(shape(feature["geometry"])), feature["properties"]))
        except Exception:  # a broken outline holds no records; the others still do
            logger.warning("record links: an outline could not be read", exc_info=True)
    return out


def _around(point: tuple[float, float], fields: list[tuple[int, Any, dict[str, Any]]]) -> tuple[list[int], list[int]]:
    """(fields the point is inside and clear of the edge, fields whose edge is within EDGE_M of it)."""
    lon, lat = point
    spot = _in_metres(Point(lon, lat), lat)
    inside, near = [], []
    for number, outline, _ in fields:
        minx, miny, maxx, maxy = outline.bounds
        if not (minx - 0.001 <= lon <= maxx + 0.001 and miny - 0.001 <= lat <= maxy + 0.001):
            continue
        metres = _in_metres(outline, lat)
        to_edge = metres.boundary.distance(spot)
        if to_edge < EDGE_M:
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


def _resolve(row: Row, kept: Optional[dict[str, Any]], fields: list[tuple[int, Any, dict[str, Any]]]) -> Linked:
    by_number = {number: props for number, _, props in fields}
    if kept is not None:
        if kept.get("none"):
            return Linked(row)
        # the point a person's link keeps was taken inside the field, so the edge rule does not apply to it
        spot = Point(kept["lon"], kept["lat"])
        here = sorted((outline.area, number) for number, outline, _ in fields if outline.contains(spot))
        if here:
            number = kept.get("number_then") if kept.get("number_then") in {n for _, n in here} else here[0][1]
            return Linked(row, number=number, by=BY_PERSON, note=_size_note(row, by_number[number]))
        return Linked(row, note="The field this was linked to is not outlined on this photo now")
    for point in positions(row.coordinates):
        inside, near = _around(point, fields)
        if len(inside) == 1 and not near:
            return Linked(row, number=inside[0], by=BY_DOCUMENT, note=_size_note(row, by_number[inside[0]]))
        if inside or near:
            first = (near or inside)[0]
            return Linked(row, suggest=first, suggest_why=(
                f"the document's position ({row.coordinates}) is within {EDGE_M:.0f} m of this field's edge"
                if near else f"the document's position ({row.coordinates}) lies on more than one field"))
    if row.coordinates:
        note = (f"The document's position ({row.coordinates}) is not on a field outlined on this photo"
                if positions(row.coordinates) else
                f"The document gives a position Ingabe cannot read ({row.coordinates})")
        return Linked(row, note=note)
    suggestion = _by_name(row, fields)
    if suggestion is not None:
        return Linked(row, suggest=suggestion[0], suggest_why=suggestion[1])
    return Linked(row)


def resolve(records: Sequence[farm_records.FarmDocument], links: dict[str, dict[str, Any]],
            features: Sequence[dict[str, Any]]) -> list[Linked]:
    """Every row of the documents with the field it belongs to among `features` (GeoJSON features with a number
    and an area_ha), if it is linked. A row that cannot be resolved is still returned, not linked."""
    fields = _fields(features)
    out: list[Linked] = []
    for row in rows(records):
        try:
            out.append(_resolve(row, links.get(row.key), fields))
        except Exception:
            logger.exception("record links: row %s could not be placed", row.key)
            out.append(Linked(row))
    return out


def kept_features(kept_rows: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """The reader's kept fields in the shape `resolve` reads."""
    return [{"geometry": row["geometry"], "properties": {"number": row["number"], "area_ha": row.get("area_ha")}}
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
    return sum(1 for item in linked if item.number is None)


# --- Kept per project ---------------------------------------------------------------------------

def _key(project_id: str) -> str:
    return f"{_STORE_PREFIX}/{project_id}/links.json"


async def load(s3: Any, bucket: str, project_id: str) -> dict[str, dict[str, Any]]:
    try:
        response = await s3.get_object(Bucket=bucket, Key=_key(project_id))
    except s3.exceptions.NoSuchKey:
        return {}
    async with response["Body"] as body:
        data = json.loads(await body.read())
    return data if isinstance(data, dict) else {}


async def keep(s3: Any, bucket: str, project_id: str, row_key: str, geometry: Optional[dict[str, Any]],
               number: Optional[int], user_id: str) -> None:
    """A person's word on a row: this field (`geometry`, its outline), or no field (None), which also sets aside
    what the document's own position says. A point inside the field is kept, not the field's number."""
    entry: dict[str, Any] = {"by": user_id, "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    if geometry is None:
        entry["none"] = True
    else:
        spot = make_valid(shape(geometry)).representative_point()
        entry.update({"lon": spot.x, "lat": spot.y, "number_then": number})
    async with _locks.setdefault(project_id, asyncio.Lock()):
        links = await load(s3, bucket, project_id)
        links[row_key] = entry
        await s3.put_object(Bucket=bucket, Key=_key(project_id), ContentType="application/json",
                            Body=json.dumps(links).encode())


async def of_project(s3: Any, bucket: str, project_id: Optional[str],
                     features: Sequence[dict[str, Any]]) -> list[Linked]:
    """The project's document rows placed on these fields: two small reads."""
    if not project_id:
        return []
    records = await farm_records.load_records(s3, bucket, project_id)
    if not records:
        return []
    return resolve(records, await load(s3, bucket, project_id), features)
