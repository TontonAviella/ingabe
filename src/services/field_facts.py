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
import logging
from dataclasses import asdict, dataclass
from typing import Any, Optional

from shapely.geometry import shape
from shapely.validation import make_valid

from src.services import count_check, drone_plots, field_taps, plant_finder

logger = logging.getLogger(__name__)

SAME_FIELD = 0.999  # shared area over joined area: a plot and a kept field this alike are one field. A plot set
# stores each outline after a trip to the photo's coordinates and back, so the two are never equal to the last bit.


def same_field(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """Whether two outlines (GeoJSON geometries) are the same field. An outline that cannot be compared is not."""
    try:
        one, other = make_valid(shape(a)), make_valid(shape(b))
        joined = one.union(other).area
        return joined > 0 and one.intersection(other).area / joined >= SAME_FIELD
    except Exception:  # a broken outline is no match; the field is then shown with its size alone
        logger.warning("field facts: two outlines could not be compared", exc_info=True)
        return False


def outlined(plots: Any) -> bool:
    """Whether these plots are the fields the reader outlined (else Ingabe's own plots, or the reader's map)."""
    return plots is not None and plots.source == drone_plots.OUTLINED_FIELDS_NAME


def name(number: int, of_outlined: bool, own: Optional[str] = None) -> str:
    """What a field is called everywhere: the reader's own name with its number ("Maize A · plot 3" from their map,
    "Block A · field 7" for a field they outlined and named: the number is what they ask Sage by), else "Field 7"
    for a field they outlined and "Plot 7" for one Ingabe found."""
    if own:
        return f"{own} · {'field' if of_outlined else 'plot'} {number}"
    return f"{'Field' if of_outlined else 'Plot'} {number}"


def area_said(area_ha: float) -> str:
    """A field's size as it is said everywhere: whole hectares from 10 ha, a decimal from 1 ha, two from 0.05 ha,
    and square metres below that (the frontend's `ha` in FieldTaps.tsx words an outline being drawn the same way)."""
    if area_ha >= 10:
        return f"{area_ha:.0f} ha"
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


def plants_said(count: int, counted_share: Optional[float] = None) -> str:
    """A plant count as it is said everywhere. When plants could be counted in a part of the field only, the count
    never goes without that part: "about 280 plants in the 40% of it that can be counted"."""
    part = (f" in the {counted_share:.0%} of it that can be counted"
            if counted_share is not None and counted_share < 0.995 else "")
    return f"about {about(count):,} plant{'' if about(count) == 1 else 's'}{part}"


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
    # rows of the farm's documents linked to this field by place (record_links), each as it is said everywhere
    records_said: tuple[str, ...] = ()

    def shown(self) -> dict[str, Any]:
        return asdict(self)


def said(facts: FieldFacts) -> str:
    """One line for a field, the same on every surface: "Field 7 · 0.18 ha · green cover 62% · about 700 plants"."""
    parts = [facts.name, facts.area_said, facts.cover_said, facts.plants_said]
    return " · ".join(part for part in parts if part)


def _counted(entry: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """A count from the photo's record when plants could be counted in the plot at all."""
    return entry if entry and entry.get("squares_counted", 0) > 0 and isinstance(entry.get("plants"), int) else None


def _facts(number: int, label: str, area_ha: float, row: Optional[dict[str, Any]], cover: Optional[float],
           count: Optional[dict[str, Any]], check: Optional[dict[str, Any]],
           records: Optional[dict[int, list[str]]] = None) -> FieldFacts:
    return FieldFacts(
        number=number, name=label, area_ha=area_ha, area_said=area_said(area_ha),
        kept_id=row["id"] if row is not None else None,
        green_cover=cover, cover_said=f"green cover {round(cover * 100)}%" if cover is not None else None,
        plants_about=about(count["plants"]) if count else None,
        plants_said=plants_said(count["plants"], count.get("counted_share")) if count else None,
        counted_share=count.get("counted_share") if count else None,
        checked_said=check["said"] if check else None, records_said=tuple((records or {}).get(number, ())))


async def _check(s3: Any, bucket: str, photo_key: str, layer_id: str, row: Optional[dict[str, Any]],
                 count: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """A person's check of this field's count; one unreadable check loses that check, not the field."""
    if row is None or count is None:
        return None
    try:
        return await count_check.summary(s3, bucket, photo_key, layer_id, row)
    except Exception:
        logger.exception("field facts: the count check of field %s could not be read", row.get("number"))
        return None


async def _kept_and_counts(s3: Any, bucket: str, photo_key: str,
                           layer_id: str) -> tuple[list[dict[str, Any]], bool, dict[str, dict[str, Any]]]:
    """(the reader's kept fields, whether green cover may be said on this photo, the photo's record of counts):
    three small reads, whatever the number of plots."""
    rows = await field_taps.kept(s3, bucket, photo_key, layer_id) if field_taps.enabled() else []
    covered = bool(rows) and await field_taps.cover_checked(s3, bucket, photo_key)
    try:
        counts = await plant_finder.counted(s3, bucket, photo_key)
    except Exception:  # the fields are still said, without their counts
        logger.exception("field facts: the photo's record of counts could not be read")
        counts = {}
    return rows, covered, counts


async def of_plots(s3: Any, bucket: str, photo_key: str, layer_id: str, plots: Any,
                   records: Optional[dict[int, list[str]]] = None) -> list[FieldFacts]:
    """The facts of every plot in a plot set, in the set's order. The reader's kept fields bring green cover and
    checks; a plot bears them only when it is that field (same number, same outline). `records`: what the farm's
    documents say per plot number, already linked by place (record_links.by_field)."""
    rows, covered, counts = await _kept_and_counts(s3, bucket, photo_key, layer_id)
    of_outlined = outlined(plots)
    by_number = {row["number"]: row for row in rows}
    out: list[FieldFacts] = []
    for feature in plots.geojson["features"]:
        props = feature["properties"]
        row = by_number.get(props["number"])
        if row is not None and not same_field(row["geometry"], feature["geometry"]):
            row = None
        count = _counted(counts.get(plant_finder.answer_key(photo_key, feature)))
        out.append(_facts(props["number"], name(props["number"], of_outlined, props.get("name")), props["area_ha"], row,
                          field_taps.cover_share(row) if row is not None and covered else None, count,
                          await _check(s3, bucket, photo_key, layer_id, row, count), records))
    return out


async def of_kept(s3: Any, bucket: str, photo_key: str, layer_id: str,
                  records: Optional[dict[int, list[str]]] = None) -> dict[str, FieldFacts]:
    """The facts of the reader's kept fields, by field id, for the field panel: read from the fields themselves and
    the photo's record of counts, so nothing is measured and no plot set is built. A count is the field's when it
    was made on the field's own outline (same number, same outline)."""
    rows, covered, counts = await _kept_and_counts(s3, bucket, photo_key, layer_id)
    out: dict[str, FieldFacts] = {}
    for row in rows:
        count = next((_counted(entry) for entry in counts.values()
                      if entry.get("number") == row["number"] and isinstance(entry.get("geometry"), dict)
                      and same_field(entry["geometry"], row["geometry"])), None)
        out[row["id"]] = _facts(row["number"], name(row["number"], True, row.get("name")), row.get("area_ha") or 0.0, row,
                                field_taps.cover_share(row) if covered else None, count,
                                await _check(s3, bucket, photo_key, layer_id, row, count), records)
    return out
