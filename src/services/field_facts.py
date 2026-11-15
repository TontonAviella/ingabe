"""One field, one set of facts: what the question cards, Sage and the field panel all say about a field.

Roger, 2026-11-15: the cards, Sage, Outline a field and the uploaded documents must work as one body. Before this,
green cover lived only on the field panel, a plant count only in Sage's answer, a person's check of the count in a
third place, and the same field was "Field 7" on one screen and "Plot 7" on another. Each fact is still made where
it was (field_taps, plant_finder, count_check); this module is the one place that gathers them per field and the
one place that words a field's name, its size and a plant count. Every surface reads it; none words these itself.

Reads only: nothing is counted or measured here, so asking is cheap and never starts work.
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
from typing import Any, Optional

from shapely.geometry import shape

from src.services import count_check, drone_plots, field_taps, plant_finder

SAME_FIELD = 0.999  # shared area over joined area: a plot and a kept field this alike are one field. A plot set
# stores each outline after a trip to the photo's coordinates and back, so the two are never equal to the last bit.
MAX_FIELDS = 200  # past this many plots the per-field records are not read (one small read each)
_AT_ONCE = 8


def same_field(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """Whether two outlines (GeoJSON geometries) are the same field."""
    one, other = shape(a), shape(b)
    joined = one.union(other).area
    return joined > 0 and one.intersection(other).area / joined >= SAME_FIELD


def outlined(plots: Any) -> bool:
    """Whether these plots are the fields the reader outlined (else Ingabe's own plots, or the reader's map)."""
    return plots is not None and plots.source == drone_plots.OUTLINED_FIELDS_NAME


def name(number: int, of_outlined: bool, own: Optional[str] = None) -> str:
    """What a field is called everywhere: the reader's own name from their map, else "Field 7" for a field they
    outlined and "Plot 7" for one Ingabe found."""
    return own or f"{'Field' if of_outlined else 'Plot'} {number}"


def area_said(area_ha: float) -> str:
    """A field's size as it is said everywhere: hectares from 0.05 ha up, square metres below."""
    if area_ha >= 1:
        return f"{area_ha:.1f} ha"
    if area_ha >= 0.05:
        return f"{area_ha:.2f} ha"
    m2 = area_ha * 10_000
    if m2 < 10:
        return "under 10 m²"
    return f"{round(m2, -1) if m2 >= 100 else round(m2):,.0f} m²"


def about(count: int) -> int:
    """A plant count as it is said: two figures (702 -> 700, 163 -> 160, 95 -> 95, 4,987 -> 5,000). The test put the
    total within 18%, so the third figure means nothing."""
    if count < 100:
        return count
    step = 10 ** (len(str(count)) - 2)
    return int(round(count / step) * step)


def plants_said(count: int) -> str:
    return f"about {about(count):,} plant{'' if about(count) == 1 else 's'}"


@dataclass(frozen=True)
class FieldFacts:
    number: int
    name: str
    area_ha: float
    area_said: str
    kept_id: Optional[str] = None  # the field's id on the field panel, when the reader outlined it
    green_cover: Optional[float] = None  # share of the field under green plants, where it was measured and checked
    cover_said: Optional[str] = None
    plants_about: Optional[int] = None  # the plant finder's count, once someone asked for it
    plants_said: Optional[str] = None
    counted_share: Optional[float] = None  # the share of the field where plants could be counted
    checked_said: Optional[str] = None  # a person's own check of that count, in parts the app chose

    def shown(self) -> dict[str, Any]:
        return asdict(self)


def said(facts: FieldFacts) -> str:
    """One line for a field, the same on every surface: "Field 7 · 0.18 ha · green cover 62% · about 700 plants"."""
    parts = [facts.name, facts.area_said, facts.cover_said, facts.plants_said]
    return " · ".join(part for part in parts if part)


async def of_plots(s3: Any, bucket: str, photo_key: str, layer_id: str, plots: Any) -> list[FieldFacts]:
    """The facts of every plot in a plot set, in the set's order. The reader's kept fields bring green cover and
    checks; a plot bears them only when it is that field (same number, same outline)."""
    features = plots.geojson["features"]
    of_outlined = outlined(plots)
    rows = await field_taps.kept(s3, bucket, photo_key, layer_id) if field_taps.enabled() else []
    covered = bool(rows) and await field_taps.cover_checked(s3, bucket, photo_key)
    by_number = {row["number"]: row for row in rows}
    limit = asyncio.Semaphore(_AT_ONCE)
    read = len(features) <= MAX_FIELDS

    async def one(feature: dict[str, Any]) -> FieldFacts:
        props = feature["properties"]
        row = by_number.get(props["number"])
        if row is not None and not same_field(row["geometry"], feature["geometry"]):
            row = None
        cover = field_taps.cover_share(row) if row is not None and covered else None
        found = check = None
        if read:
            async with limit:
                found = await plant_finder.load_found(s3, bucket, photo_key, feature)
                if row is not None and found is not None:
                    check = await count_check.summary(s3, bucket, photo_key, layer_id, row)
        counted = found is not None and found.squares_counted > 0
        return FieldFacts(
            number=props["number"], name=name(props["number"], of_outlined, props.get("name")),
            area_ha=props["area_ha"], area_said=area_said(props["area_ha"]),
            kept_id=row["id"] if row is not None else None,
            green_cover=cover, cover_said=f"green cover {round(cover * 100)}%" if cover is not None else None,
            plants_about=about(found.plants) if found is not None and counted else None,
            plants_said=plants_said(found.plants) if found is not None and counted else None,
            counted_share=found.counted_share if found is not None and counted else None,
            checked_said=check["said"] if check else None)

    return list(await asyncio.gather(*(one(feature) for feature in features)))
