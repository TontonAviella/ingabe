"""The NDVI of each field a person outlined on a drone photo, from the clearest recent Sentinel-2 pass, kept nightly.

Each night the pipelines' nightly_parcel_ndvi reads, for every field kept on a drone photo of a farm project
(field_taps: "Your outlined fields"), the Sentinel-2 passes of the last LOOK_BACK_DAYS over it, and keeps one row
in ndvi_parcel_cache: the NDVI of the field's clear 10 m pixels on the clearest pass, with that pass's day
(`pass_date`) and the share of the field it saw clear (`clear_share`). Sage reads the rows (get_parcel_ndvi_stats).

Why the store stayed empty before (found 2026-11-24): the job looked only for vector layers whose metadata said
`rwanda_parcels: true`, which nothing in the app ever sets, so every night ended "no_parcels" as a success. Had
one been tagged, its features were read from a table named with a bound parameter (`postgis_layer_%s`, a syntax
error) and then from `bounds->>'west'` on a float array (another error), both swallowed; and it looked at the
last 7 days only, so a cloudy week left nothing either. Outlined fields live in object storage beside their
photo, not in a table; this module reads them there.

Cloudy passes: a pass is used only when at least MIN_CLEAR_SHARE of the field's pixels are clear on it (the
scene classification's cloud, shadow, snow and no-data classes are not clear); of those the clearest is taken,
the newest of equally clear ones, and its day is kept and said. A field no pass saw clear enough in the window
gets no row that night and is counted as cloudy, so an empty night is told apart from a broken one. A field
smaller than one 10 m pixel's middle gets no row either (`too_small`).
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Callable, Iterable, Optional, Sequence

from src.services import field_facts, photo_context

logger = logging.getLogger(__name__)

LOOK_BACK_DAYS = 30  # Sentinel-2 passes every 5 days: about six chances, in a rainy season often all cloudy
MIN_CLEAR_SHARE = 0.5  # a pass that saw less than half of the field clear does not speak for the field

# Every outlined field kept on a photo of the newest map of a farm project: the photos are the ones the night's
# satellite views go through (satellite_ahead.PHOTOS_SQL), but kept fields belong to each photo layer, so copies
# of one photo are not merged here.


@dataclass(frozen=True)
class Field:
    layer_id: str
    number: int
    name: str
    geometry: dict[str, Any]
    area_ha: Optional[float]

    @property
    def parcel_id(self) -> str:
        """Stable for the field: a field keeps its number on its photo layer for good (field_taps)."""
        return str(uuid.uuid5(uuid.NAMESPACE_URL, f"{self.layer_id}/field/{self.number}"))


def photo_layers(rows: Iterable[Sequence[Any]]) -> list[tuple[str, str]]:
    """(layer id, stored photo key) for each row of satellite_ahead.PHOTOS_SQL that is a finished photo."""
    photos: list[tuple[str, str]] = []
    for layer_id, bounds, metadata in rows:
        if isinstance(metadata, str):
            metadata = json.loads(metadata)
        metadata = metadata or {}
        if not bounds or not metadata.get("cog_key") or not photo_context.is_photo_ready(metadata):
            continue
        photos.append((str(layer_id), str(metadata["cog_key"])))
    return photos


def fields_of(layer_id: str, kept_rows: Iterable[dict[str, Any]]) -> list[Field]:
    """The kept outlines of one photo layer (field_taps.kept) as fields, named as everywhere else."""
    fields = []
    for row in kept_rows:
        if "number" not in row or not row.get("geometry"):
            continue
        fields.append(Field(layer_id=layer_id, number=int(row["number"]),
                            name=field_facts.name(int(row["number"]), True, row.get("name")),
                            geometry=row["geometry"], area_ha=row.get("area_ha")))
    return fields


def _pass_day(interval: dict[str, Any]) -> Optional[date]:
    text = str(interval.get("date_from") or "")[:10]
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def clearest_pass(intervals: Iterable[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """The pass to say for a field: of those that saw at least MIN_CLEAR_SHARE of it clear, the clearest, the
    newest of equally clear ones. {"day", "clear_share", "stats"}, or None when no pass saw it clear enough.

    `intervals` are get_field_stats' (clipped to the field), each with "ndvi": {"valid_pixels", "inside_pixels",
    "mean", ...}; valid pixels are the clear ones inside the field."""
    best: Optional[dict[str, Any]] = None
    for interval in intervals:
        stats = interval.get("ndvi") or {}
        inside = int(stats.get("inside_pixels") or 0)
        clear = int(stats.get("valid_pixels") or 0)
        day = _pass_day(interval)
        if inside <= 0 or clear <= 0 or day is None:
            continue
        share = min(1.0, clear / inside)
        if share < MIN_CLEAR_SHARE:
            continue
        candidate = {"day": day, "clear_share": round(share, 3), "stats": stats}
        if best is None or (candidate["clear_share"], day) > (best["clear_share"], best["day"]):
            best = candidate
    return best


def measure(field: Field, today: date, field_stats: Callable[..., dict[str, Any]]) -> dict[str, Any]:
    """One field's night: {"outcome": "kept" | "cloudy" | "no_pass" | "too_small" | "failed", "row"?, "error"?}.

    `field_stats` is DEAfricaService.get_field_stats (passed in so tests need no network)."""
    result = field_stats(geometry=field.geometry,
                         date_from=(today - timedelta(days=LOOK_BACK_DAYS)).isoformat(),
                         date_to=today.isoformat(), index="ndvi", clip_to_geometry=True, drop_water=True)
    if "error" in result:
        return {"outcome": "failed", "error": str(result["error"])}
    intervals = result.get("intervals") or []
    if not intervals:
        return {"outcome": "no_pass"}
    if all(int((iv.get("ndvi") or {}).get("inside_pixels") or 0) == 0 for iv in intervals):
        return {"outcome": "too_small"}
    chosen = clearest_pass(intervals)
    if chosen is None:
        return {"outcome": "cloudy"}
    stats, day = chosen["stats"], chosen["day"]
    return {"outcome": "kept", "row": {
        "parcel_id": field.parcel_id,
        "parcel_name": field.name,
        "layer_id": field.layer_id,
        "week_start": day - timedelta(days=day.weekday()),
        "pass_date": day,
        "clear_share": chosen["clear_share"],
        "mean_ndvi": stats.get("mean"),
        "std_ndvi": stats.get("std"),
        "min_ndvi": stats.get("min"),
        "max_ndvi": stats.get("max"),
        "valid_pixels": int(stats.get("valid_pixels") or 0),
        "area_ha": field.area_ha,
    }}


ROW_COLUMNS = ("parcel_id", "parcel_name", "layer_id", "week_start", "pass_date", "clear_share", "mean_ndvi",
               "std_ndvi", "min_ndvi", "max_ndvi", "valid_pixels", "area_ha")

# One row a field a pass: measuring the same pass again (it stays the clearest for nights) replaces its row.
DELETE_SAME_PASS_SQL = "DELETE FROM ndvi_parcel_cache WHERE parcel_id = %s AND pass_date = %s"
INSERT_SQL = (f"INSERT INTO ndvi_parcel_cache ({', '.join(ROW_COLUMNS)}, computed_at) "
              f"VALUES ({', '.join(['%s'] * len(ROW_COLUMNS))}, now())")


def run(fields: Sequence[Field], today: date, field_stats: Callable[..., dict[str, Any]],
        keep: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
    """Measure every field and keep each row; one field's failure does not stop the others.

    Returns what the night did: {"status", "fields", "kept", "cloudy", "no_pass", "too_small", "failed": [...]}.
    The status is "failed" when every field failed, so a broken night is not reported as a quiet one."""
    counts = {"kept": 0, "cloudy": 0, "no_pass": 0, "too_small": 0}
    failed: list[dict[str, str]] = []
    passes: list[str] = []
    for field in fields:
        try:
            outcome = measure(field, today, field_stats)
            if outcome["outcome"] == "kept":
                keep(outcome["row"])
                passes.append(outcome["row"]["pass_date"].isoformat())
        except Exception as e:  # noqa: BLE001 - one field must not end the night for the others
            logger.exception("field NDVI: %s on layer %s could not be measured", field.name, field.layer_id)
            outcome = {"outcome": "failed", "error": str(e)}
        if outcome["outcome"] == "failed":
            failed.append({"layer_id": field.layer_id, "field": field.name, "error": outcome.get("error", "")})
        else:
            counts[outcome["outcome"]] += 1
    status = "failed" if fields and len(failed) == len(fields) else ("ok" if fields else "no_fields")
    return {"status": status, "fields": len(fields), **counts, "failed": failed[:10], "failed_count": len(failed),
            "newest_pass": max(passes) if passes else None,
            "window": f"{(today - timedelta(days=LOOK_BACK_DAYS)).isoformat()}/{today.isoformat()}"}


def today_utc() -> date:
    return datetime.utcnow().date()
