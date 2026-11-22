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

A day the photo's own file gives (a date kept inside it, or in its name) is offered to the reader beside the place
where the day is asked, and is never taken as the flight day on its own: a photo map is often put together days
after the flight, a camera's clock can be wrong, and most programs that make photo maps write no date at all (of
the 8 photos kept on 2026-11-22, all from Pix4D, none had one). It becomes the flight day only when the reader
says so, and is then their word like any other day they give.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from datetime import date, datetime
from zoneinfo import ZoneInfo
from typing import Any, Awaitable, Callable, Iterable, Optional

import rasterio
from shapely.errors import ShapelyError
from shapely.geometry import box, shape

from src.services import field_health, photo_plots, raster_process, stored_files
from src.services.gdal_http import GDAL_HTTP_TIMEOUTS

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


# --- The day a photo's own file gives: offered to the reader, never taken as the flight day -----------------------

TAKEN, MADE = "taken", "made"  # what a date inside the file is the date of: the pictures, or the file's writing
FILE_DAY_PREFIX = "flight_day/v1"  # object storage: what a photo's file says of its day, read once per photo
FILE_DAY_METHOD = "file-day-1"  # change when what is read from a file changes: older records are read again
FILE_DAY_RETRY_SECONDS = 600  # a file that could not be read is not opened again sooner than this
FILE_DAY_WAIT_SECONDS = 4.0  # a request waits this long for a file's first reading; a slower one is said on a later ask
NO_FILE_DAY = "This file does not say when it was flown."
# Tags that say when the pictures were taken, then tags that only say when the file was written (GDAL's names).
_TAKEN_TAGS = ("ACQUISITIONDATETIME", "EXIF_DateTimeOriginal", "EXIF_DateTimeDigitized")
_MADE_TAGS = ("TIFFTAG_DATETIME", "EXIF_DateTime")
_XMP_MOMENT = r"(\d{4}-\d{2}-\d{2}(?:T[\d:.]+(?:Z|[+-]\d{2}:?\d{2})?)?)"
_XMP_TAKEN = re.compile(r"(?:exif:DateTimeOriginal|photoshop:DateCreated)\s*(?:=\s*[\"']|>)\s*" + _XMP_MOMENT)
_XMP_MADE = re.compile(r"xmp:CreateDate\s*(?:=\s*[\"']|>)\s*" + _XMP_MOMENT)
_TAG_DAY = re.compile(r"\s*(\d{4})[:-](\d{2})[:-](\d{2})")  # EXIF's "2026:03:14 10:15:00" and ISO's "2026-03-14T..."
# a moment that names its own time zone ("2026-03-14T23:30:00Z"): its day is taken where the first farms are
_ZONED_MOMENT = re.compile(r"\s*(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}(?::\d{2})?)(?:\.\d+)?\s*(Z|[+-]\d{2}:?\d{2})\s*$")
_MONTH_NUMBER = {name: number for number, names in enumerate((
    ("january", "jan"), ("february", "feb"), ("march", "mar"), ("april", "apr"), ("may",), ("june", "jun"),
    ("july", "jul"), ("august", "aug"), ("september", "sept", "sep"), ("october", "oct"), ("november", "nov"),
    ("december", "dec")), start=1) for name in names}
_MONTH_WORD = "|".join(sorted(_MONTH_NUMBER, key=len, reverse=True))
# A whole day in a name, in an order that cannot be read two ways: year first ("2026-03-14", "20260314"), or with the
# month in letters ("14Mar2026"). "03-04-2026" is not read: it is the 3rd of April to some and the 4th of March to others.
# A digit that ends a word is part of the word ("Kaber1_Jan2019" is January 2019, not the 1st).
_NAME_YEAR_FIRST = re.compile(r"(?<!\d)(20\d{2})[-_.]?(0[1-9]|1[0-2])[-_.]?(0[1-9]|[12]\d|3[01])(?!\d)")
_NAME_MONTH_IN_LETTERS = re.compile(
    rf"(?<![A-Za-z\d])(0?[1-9]|[12]\d|3[01])[-_. ]?({_MONTH_WORD})(?![a-z])[-_., ]*(20\d{{2}})(?!\d)", re.IGNORECASE)
_NAME_MONTH_ONLY = re.compile(rf"(?<![a-z])({_MONTH_WORD})(?![a-z])[-_., ]*(20\d{{2}})(?!\d)", re.IGNORECASE)
_file_day_failed_at: dict[str, float] = {}
_file_day_reading: dict[str, "asyncio.Task[Optional[dict[str, Any]]]"] = {}  # one reading at a time per photo


def _real_day(year: Any, month: Any, day: Any) -> Optional[date]:
    try:
        return date(int(year), int(month), int(day))
    except ValueError:
        return None


def _tag_day(text: str) -> Optional[date]:
    """The day in a tag's text. A camera's own clock ("2026:03:14 10:15:00") is taken as it stands; a moment that
    names its zone is the day it was in UPLOAD_DAY_ZONE (23:30 UTC on the 14th is the 15th in Kigali)."""
    found = _TAG_DAY.match(text)
    day = _real_day(*found.groups()) if found else None
    zoned = _ZONED_MOMENT.match(text) if day is not None else None
    if zoned:
        offset = "+00:00" if zoned.group(3) == "Z" else zoned.group(3)
        try:
            return datetime.fromisoformat(f"{zoned.group(1)}T{zoned.group(2)}{offset}").astimezone(UPLOAD_DAY_ZONE).date()
        except ValueError:
            return day
    return day


def day_in_tags(tags: dict[str, dict[str, Any]]) -> Optional[tuple[date, str]]:
    """(day, TAKEN or MADE) from a file's tags by domain ("" for the default one), the day the pictures were taken
    before the day the file was written; None when no tag holds a day."""
    plain: dict[str, Any] = {}
    for domain, items in tags.items():
        if "xmp" not in domain.lower():
            plain.update(items)
    xmp = " ".join(str(value) for domain, items in tags.items() if "xmp" in domain.lower() for value in items.values())
    for names, pattern, kind in ((_TAKEN_TAGS, _XMP_TAKEN, TAKEN), (_MADE_TAGS, _XMP_MADE, MADE)):
        texts = [str(plain[name]) for name in names if plain.get(name)] + pattern.findall(xmp)
        for text in texts:
            day = _tag_day(text)
            if day is not None:
                return day, kind
    return None


def day_in_name(name: str) -> tuple[Optional[date], Optional[tuple[int, int]]]:
    """(a whole day a file's name holds, else the (year, month) it holds); (None, None) for a name with neither."""
    found = _NAME_YEAR_FIRST.search(name)
    if found:
        day = _real_day(*found.groups())
        if day is not None:
            return day, None
    found = _NAME_MONTH_IN_LETTERS.search(name)
    if found:
        day = _real_day(found.group(3), _MONTH_NUMBER[found.group(2).lower()], found.group(1))
        if day is not None:
            return day, None
    found = _NAME_MONTH_ONLY.search(name)
    if found:
        return None, (int(found.group(2)), _MONTH_NUMBER[found.group(1).lower()])
    return None, None


def _could_be(day: date, today: date) -> bool:
    try:
        check_day(day, today)
    except ValueError:
        return False
    return True


def file_day_offered(in_file: Optional[tuple[date, str]], names: Iterable[Optional[str]], today: date,
                     file_read: bool = True) -> Optional[dict[str, Optional[str]]]:
    """What the photo's file says of its day, as the reader is told it: {"day": the day offered (ISO) or None,
    "said": the sentence}. The day the pictures were taken comes first; then a whole day in the file's name (someone
    wrote it there for the flight); then the day the file was written (often days later); then a month in the name.
    A day that cannot be a flight is not offered. None when the name tells nothing and the file could not be read:
    it is not said to hold no date."""
    inside = in_file if in_file is not None and _could_be(in_file[0], today) else None
    if inside is not None and inside[1] == TAKEN:
        return {"day": inside[0].isoformat(),
                "said": f"The file says its pictures were taken on {day_said(inside[0], True)}."}
    month: Optional[tuple[int, int]] = None
    for name in names:
        day, in_month = day_in_name(name or "")
        if day is not None and _could_be(day, today):
            return {"day": day.isoformat(), "said": f"The file's name says {day_said(day, True)}."}
        month = month or in_month
    if inside is not None:
        return {"day": inside[0].isoformat(),
                "said": (f"The file says it was made on {day_said(inside[0], True)}. "
                         "A photo map is often put together some days after the flight.")}
    if month is not None and _could_be(date(month[0], month[1], 1), today):
        return {"day": None, "said": f"The file's name says {date(month[0], month[1], 1).strftime('%B %Y')}."}
    return {"day": None, "said": NO_FILE_DAY} if file_read else None


def _day_in_copies(urls: list[str], env: dict[str, str]) -> tuple[int, Optional[tuple[date, str]]]:
    """(which of these copies of a photo opened, the day its tags hold), the first copy that opens (blocking, in a
    worker process: no logging here). The file as it was added comes first: the app's own copy may not carry every
    tag over. Only the day comes back, never the tags (an XMP packet can be megabytes), and an error names no link:
    the links are signed."""
    for number, url in enumerate(urls):
        try:
            with rasterio.Env(**env), rasterio.open(url) as ds:
                tags = {"": dict(ds.tags())}
                for domain in ds.tag_namespaces() or ():
                    if domain not in ("IMAGE_STRUCTURE", "DERIVED_SUBDATASETS"):
                        tags[domain] = dict(ds.tags(ns=domain))
                return number, day_in_tags(tags)
        except rasterio.errors.RasterioIOError:
            continue
    raise OSError("no copy of the photo could be opened")


def file_day_key(photo_key: str) -> str:
    """Where what a photo's file says of its day is kept (deleted with the photo's last layer)."""
    return f"{FILE_DAY_PREFIX}/{hashlib.sha256(photo_key.encode()).hexdigest()[:32]}.json"


def _not_read(photo_key: str) -> None:
    now = time.monotonic()
    if len(_file_day_failed_at) > 2000:  # photos that never open: the ones past their wait are let go
        for key in [key for key, at in _file_day_failed_at.items() if now - at >= FILE_DAY_RETRY_SECONDS]:
            del _file_day_failed_at[key]
    _file_day_failed_at[photo_key] = now


async def _read_file_day(s3: Any, bucket: str, photo_key: str, urls: list[str]) -> Optional[dict[str, Any]]:
    """What the file says, read now and kept; None when it could not be told (said as not read, tried again later)."""
    try:
        copy, found = await asyncio.to_thread(raster_process.run, _day_in_copies, urls, GDAL_HTTP_TIMEOUTS)
    except Exception as why:  # noqa: BLE001 - whatever stopped the reading, the fields are still shown
        # the error's own words can hold the signed link to the photo: only its kind is logged
        logger.warning("flight day: photo %s could not be read for its date (%s)", photo_key, type(why).__name__)
        _not_read(photo_key)
        return None
    if found is None and copy > 0:
        # only a later copy opened, and it holds no day: the file as it was added may still hold one. Not kept as
        # "no date", which would then be said for good.
        _not_read(photo_key)
        return None
    record = {"method": FILE_DAY_METHOD, "day": found[0].isoformat() if found else None,
              "kind": found[1] if found else None}
    try:
        await s3.put_object(Bucket=bucket, Key=file_day_key(photo_key), Body=json.dumps(record).encode(),
                            ContentType="application/json")
    except Exception as why:  # noqa: BLE001 - said this time all the same; read again on a later ask
        logger.warning("flight day: what the file of photo %s says could not be kept (%s)", photo_key,
                       type(why).__name__)
        _not_read(photo_key)
    return record


async def day_in_file(s3: Any, bucket: str, photo_key: str,
                      urls: list[str]) -> tuple[bool, Optional[tuple[date, str]]]:
    """(whether the file was read, the day inside it with what it is the day of), from the first of `urls` (links to
    copies of the photo, the file as it was added first) that opens. The file is opened once per photo and what it
    says is kept. A request waits FILE_DAY_WAIT_SECONDS for a first reading, which goes on beside it; a file not
    read yet, or one that could not be read, is said as not read and asked again later. Never raises for the file."""
    try:
        kept = await stored_files.load_json(s3, bucket, file_day_key(photo_key))
    except ValueError:  # a kept record that is no JSON is read as none, and written again
        kept = None
    if not (isinstance(kept, dict) and kept.get("method") == FILE_DAY_METHOD):
        if time.monotonic() - _file_day_failed_at.get(photo_key, float("-inf")) < FILE_DAY_RETRY_SECONDS:
            return False, None
        reading = _file_day_reading.get(photo_key)
        if reading is None or reading.done():
            reading = asyncio.create_task(_read_file_day(s3, bucket, photo_key, urls))
            _file_day_reading[photo_key] = reading
            reading.add_done_callback(
                lambda done, key=photo_key: _file_day_reading.pop(key, None) if _file_day_reading.get(key) is done else None)
        try:
            kept = await asyncio.wait_for(asyncio.shield(reading), FILE_DAY_WAIT_SECONDS)
        except asyncio.TimeoutError:
            return False, None
        if kept is None:
            return False, None
    try:
        day = date.fromisoformat(str(kept.get("day") or ""))
    except ValueError:
        return True, None
    return True, (day, MADE if kept.get("kind") != TAKEN else TAKEN)


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
