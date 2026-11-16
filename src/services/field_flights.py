"""One field across the flights of its place.

A field is a piece of ground, and a place does not move: the outline kept on one photo is measured, as it is, on
every other picture of the same ground in the project. Nothing is joined by a field's number or name (two photos
number their fields their own way), and no outline drawn on another photo is taken to be "the same field".

What is said for each flight is what that flight's own picture can bear, by the rules every surface already uses
(field_health): an index number only on reflectance, a temperature only in a unit the file states. A field's
place among fields is not said here, since each photo holds its own set of fields. Nothing here says a field got better or worse: two flights differ in light, hour and camera
as well as in the crop, and no cut-off for a real change has been tested.

The day of a flight is the day the reader gave (layer metadata "flown_on"). A photo with no day given is said by
the day it was added, in those words, so an upload day is never passed off as a flight day.
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from zoneinfo import ZoneInfo
from typing import Any, Awaitable, Callable, Optional

from shapely.errors import ShapelyError
from shapely.geometry import box, shape

from src.services import field_health, photo_plots

logger = logging.getLogger(__name__)

FLOWN_ON = "flown_on"  # layer metadata: the day of the flight as the reader gave it, "2026-03-14"
FLOWN_ON_BY = "flown_on_by"  # ... and who gave it, when: {"by", "at"}
MAX_FLIGHTS = 6  # other flights said for one field, the newest
MAX_PHOTOS = 8  # other pictures of the place asked for their fields in one answer, newest first
MAX_FIELDS = 60  # fields followed across flights in one answer: the reader's kept fields, or the first plots found
MIN_SHARE = 0.95  # a flight is set beside another only when its picture covers this much of the field
# An upload's day is the day it was where Ingabe's first farms are, not the server's (UTC) day: a photo added at one
# in the morning in Kigali was not added the day before.
UPLOAD_DAY_ZONE = ZoneInfo("Africa/Kigali")
EARLIEST = date(2000, 1, 1)  # a flight day before this is a slip of the hand
NOTE = ("Each flight is measured on the field's same outline. Flights differ in light, hour and camera as well as "
        "in the crop, and no cut-off for a real change has been tested, so the flights are given side by side "
        "with no verdict.")
NOT_YET = "being measured"
NOTHING = "no health index from this picture"
NOT_READ = "this picture could not be read just now"
PART = "this picture shows only part of the field"
RAW = "health index measured, but this file's values are on no known scale, so no number is given"


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
        return (uploaded.astimezone(UPLOAD_DAY_ZONE) if uploaded.tzinfo else uploaded).date(), False
    return None


def line(name: Optional[str], metadata: dict[str, Any], uploaded: Any, facts: list[Optional[str]],
         this: bool = False, nothing: str = NOTHING) -> Optional[dict[str, Any]]:
    """One flight of a field as every surface says it; None for a photo with no day at all."""
    day = when(metadata, uploaded)
    if day is None:
        return None
    told = [fact for fact in facts if fact]
    return {"day": day[0].isoformat(), "flown": day[1], "day_said": day_said(*day),
            "photo": _name(name), "this": this, "measured": bool(told),
            "said": " · ".join(told) if told else nothing}


def _name(name: Optional[str]) -> str:
    plain = field_health.plain_name(name or "\x00")  # nothing of a name left: plain_name's own fallback is not ours
    return "a photo" if plain == field_health.plain_name("\x00") else plain


def in_order(lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The flights whose day the reader gave, oldest first; then the photos only known by the day they were added,
    in that order. The two are not mixed into one line of time: a photo flown in January and added in April is not
    "after" a flight of March."""
    return sorted(lines, key=lambda entry: (not entry["flown"], entry["day"], entry["photo"]))


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


def facts_of(health: Optional[field_health.FieldHealth], index_from: Optional[str] = None,
             temperature_from: Optional[str] = None) -> list[Optional[str]]:
    """What one flight's measurement may say beside another flight's: the number alone. A field's place among
    fields ("2nd lowest of 9") is left out, since each photo holds its own set of fields and the places would be
    read against each other; so is a figure measured on less than nearly all of the field. A measurement that came
    from another file than the photo says which."""
    if health is None or (health.share is not None and health.share < MIN_SHARE):
        return []
    index = (f"health index (NDVI) {health.ndvi:.2f}" if health.ndvi is not None
             else RAW if health.raw_index else None)
    warmth = f"surface {health.temp_c:.1f} °C" if health.temp_c is not None else None
    return [f"{index}, measured on {index_from}" if index and index_from else index,
            f"{warmth}, measured on {temperature_from}" if warmth and temperature_from else warmth]


def other_flights(this_photo: Optional[dict[str, Any]],
                  photos: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    """(The photos that are other flights than this one, how many were set aside as not known to be). Two photos
    with different flight days given are two flights; with the same day, one. Where a day is missing, photos added
    within SAME_FLIGHT_HOURS of each other are not told apart: the colour, multispectral and thermal files of one
    flight are added together, and they must not be shown as flights of their own."""
    if not this_photo:
        return [], 0
    own_day, own_added = flown_on(this_photo.get("metadata") or {}), this_photo.get("uploaded")
    apart, unsure = [], 0
    for photo in photos:
        day = flown_on(photo["metadata"])
        if own_day is not None and day is not None:
            if day != own_day:
                apart.append(photo)
            continue
        added = photo.get("uploaded")
        if (isinstance(own_added, datetime) and isinstance(added, datetime)
                and abs((added - own_added).total_seconds()) >= field_health.SAME_FLIGHT_HOURS * 3600):
            apart.append(photo)
        else:
            unsure += 1
    return apart, unsure


async def of_features(s3: Any, bucket: str, features: list[dict[str, Any]], photos: list[dict[str, Any]],
                      link: Callable[[dict[str, Any]], Awaitable[str]]) -> dict[str, list[dict[str, Any]]]:
    """The other flights of each feature's outline, by field_health.key_of(geometry): one line a flight, for the
    photos (other_flights) that hold the whole outline. At most MAX_FIELDS features are followed. Never reads a
    photo itself: a flight not measured yet is measured beside the request (field_health) and says so until it
    is."""
    features = features[:MAX_FIELDS]
    out: dict[str, list[dict[str, Any]]] = {}
    looked = 0
    for photo in photos:
        # a field that has its newest flights already is not asked of older ones; a field no newer photo holds is
        covered = [feature for feature in _on_photo(features, photo["bounds"])
                   if len(out.get(field_health.key_of(feature["geometry"]), ())) < MAX_FLIGHTS]
        if not covered:
            continue
        if looked >= MAX_PHOTOS:
            break
        looked += 1
        health: dict[str, field_health.FieldHealth] = {}
        final, key_of_photo = True, photo_plots.photo_key(photo["metadata"])
        if photo["bands"] >= 4:  # three bands are a colour picture: nothing in it to measure a health index from
            try:
                health, final = await field_health.asked(s3, bucket, key_of_photo, await link(photo["metadata"]), covered)
            except Exception:  # this flight is left out of the answer; the others are still said
                logger.exception("field flights: the photo %s could not be asked for its fields", photo["name"])
                continue
        waiting = NOT_READ if not final and field_health.failed_lately(key_of_photo) else NOT_YET
        for feature in covered:
            key = field_health.key_of(feature["geometry"])
            measured = health.get(key)
            cut_off = measured is not None and measured.share is not None and measured.share < MIN_SHARE
            entry = line(photo["name"], photo["metadata"], photo["uploaded"], facts_of(measured),
                         nothing=waiting if measured is None and not final else PART if cut_off else NOTHING)
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
