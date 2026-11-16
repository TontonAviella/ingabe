"""One field across the flights of its place.

A field is a piece of ground, and a place does not move: the outline kept on one photo is measured, as it is, on
every other picture of the same ground in the project. Nothing is joined by a field's number or name (two photos
number their fields their own way), and no outline drawn on another photo is taken to be "the same field".

What is said for each flight is what that flight's own picture can bear, by the rules every surface already uses
(field_health): an index number only on reflectance, a place among the fields otherwise, a temperature only in a
unit the file states. Nothing here says a field got better or worse: two flights differ in light, hour and camera
as well as in the crop, and no cut-off for a real change has been tested.

The day of a flight is the day the reader gave (layer metadata "flown_on"). A photo with no day given is said by
the day it was added, in those words, so an upload day is never passed off as a flight day.
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any, Awaitable, Callable, Optional

from shapely.errors import ShapelyError
from shapely.geometry import box, shape

from src.services import field_health, photo_plots

logger = logging.getLogger(__name__)

FLOWN_ON = "flown_on"  # layer metadata: the day of the flight as the reader gave it, "2026-03-14"
MAX_FLIGHTS = 6  # other pictures of the place looked at for one photo's fields, newest first
EARLIEST = date(2000, 1, 1)  # a flight day before this is a slip of the hand
NOTE = ("Each flight is measured on the field's same outline. Flights differ in light, hour and camera as well as "
        "in the crop, and no cut-off for a real change has been tested, so the flights are given side by side "
        "with no verdict.")
NOT_YET = "being measured"
NOTHING = "no health index from this picture"


def flown_on(metadata: dict[str, Any]) -> Optional[date]:
    """The flight day the reader gave this photo, when it is a day."""
    try:
        return date.fromisoformat(str(metadata.get(FLOWN_ON) or ""))
    except ValueError:
        return None


def check_day(day: date, today: date) -> None:
    """ValueError, in words for the reader, for a flight day that cannot be."""
    if day > today:
        raise ValueError("A flight cannot be in the days to come.")
    if day < EARLIEST:
        raise ValueError("That day is too long ago to be a drone flight.")


def day_said(day: date, flown: bool) -> str:
    words = f"{day.day} {day.strftime('%B %Y')}"
    return words if flown else f"added {words}"


def when(metadata: dict[str, Any], uploaded: Any) -> Optional[tuple[date, bool]]:
    """(the day this photo is said by, whether that is its flight day); None when neither is known."""
    given = flown_on(metadata)
    if given is not None:
        return given, True
    if isinstance(uploaded, datetime):
        return uploaded.date(), False
    return None


def line(name: Optional[str], metadata: dict[str, Any], uploaded: Any, facts: list[Optional[str]],
         this: bool = False, nothing: str = NOTHING) -> Optional[dict[str, Any]]:
    """One flight of a field as every surface says it; None for a photo with no day at all."""
    day = when(metadata, uploaded)
    if day is None:
        return None
    told = [fact for fact in facts if fact]
    return {"day": day[0].isoformat(), "flown": day[1], "day_said": day_said(*day),
            "photo": field_health.plain_name(name) if str(name or "").strip() else "a photo", "this": this, "measured": bool(told),
            "said": " · ".join(told) if told else nothing}


def in_order(lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Oldest first; on one day, flights whose day the reader gave before those only known by their upload."""
    return sorted(lines, key=lambda entry: (entry["day"], not entry["flown"], entry["photo"]))


def _on_photo(features: list[dict[str, Any]], bounds: Any) -> list[dict[str, Any]]:
    """The features whose whole outline lies inside a photo's box: a field half off a picture is not followed."""
    if not bounds or len(bounds) != 4:
        return []
    try:
        photo = box(*bounds)
    except (TypeError, ValueError):
        return []
    out = []
    for feature in features:
        try:
            if isinstance(feature.get("geometry"), dict) and photo.contains(shape(feature["geometry"])):
                out.append(feature)
        except (ShapelyError, ValueError, TypeError, KeyError, AttributeError):
            continue  # this outline, not the others
    return out


async def of_features(s3: Any, bucket: str, features: list[dict[str, Any]], photos: list[dict[str, Any]],
                      link: Callable[[dict[str, Any]], Awaitable[str]],
                      leave_out: Optional[str] = None) -> dict[str, list[dict[str, Any]]]:
    """The other flights of each feature's outline, by field_health.key_of(geometry): one line a flight, for the
    photos (photo_context.flights_here) that hold the whole outline. Never reads a photo itself: a flight not
    measured yet is measured beside the request (field_health) and says so until it is. `leave_out`: the name of a
    file whose bands this photo's own facts were already measured on (the same flight, not another one)."""
    out: dict[str, list[dict[str, Any]]] = {}
    looked = 0
    for photo in photos:
        if leave_out and photo["name"] == leave_out:
            continue
        covered = _on_photo(features, photo["bounds"])
        if not covered:
            continue
        if looked >= MAX_FLIGHTS:
            break
        looked += 1
        health: dict[str, field_health.FieldHealth] = {}
        final = True
        if photo["bands"] >= 4:  # three bands are a colour picture: nothing in it to measure a health index from
            try:
                health, final = await field_health.asked(
                    s3, bucket, photo_plots.photo_key(photo["metadata"]), await link(photo["metadata"]), covered)
            except Exception:  # this flight is left out of the answer; the others are still said
                logger.exception("field flights: the photo %s could not be asked for its fields", photo["name"])
                continue
        for feature in covered:
            key = field_health.key_of(feature["geometry"])
            measured = health.get(key)
            entry = line(photo["name"], photo["metadata"], photo["uploaded"],
                         [measured.said, measured.temperature_said] if measured else [],
                         nothing=NOT_YET if measured is None and not final else NOTHING)
            if entry is not None:
                out.setdefault(key, []).append(entry)
    return out


def with_this(own: Optional[dict[str, Any]], others: list[dict[str, Any]]) -> tuple[dict[str, Any], ...]:
    """A field's flights in order, this photo's line among them; () when there is no other flight to set it
    beside, so a field on one photo says nothing about flights."""
    if not others or own is None:
        return ()
    return tuple(in_order([own, *others]))


def told(flights: tuple[dict[str, Any], ...]) -> Optional[list[str]]:
    """A field's flights for Sage: "14 March 2026 (this photo): health index (NDVI) 0.45"."""
    if not flights:
        return None
    return [f"{entry['day_said']}{' (this photo)' if entry['this'] else ''}: {entry['said']}" for entry in flights]
