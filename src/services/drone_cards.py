"""Drone question cards: the questions Ingabe asks about a drone photo, and the answers it draws on it.

People do not know what to ask a drone photo, so Ingabe asks first. Rules for
this domain (brief agreed with Roger, 2026-10-06):

- Every answer comes in one order: what and where, why it matters for the crop
  or the decision, what to do, how sure (with what would make it surer).
- Numbers are measured here, never by a language model. Sage explains later.
- No money in any card: readers judge the value themselves.
- A card that cannot be answered yet says why: it needs the reader's help, a
  second flight, a special camera, lab tests, or it is still being built.
- All 11 services of the Gabiro Agribusiness Hub demo have a card.
- Which cards come first depends on what the photo shows, what is missing and
  who is reading; one card always teaches something.
- Every hard word in an answer links to an explanation in the reader's words.
"""

from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Callable, Optional

import numpy as np
import rasterio
from affine import Affine
from PIL import Image
from pyproj import Geod, Transformer
from rasterio.enums import Resampling
from rasterio.features import shapes
from rasterio.warp import transform_bounds
from shapely.geometry import box, mapping, shape
from shapely.ops import transform as reproject

from src.services import drone_first_look, drone_plots, isdasoil_service, wapor_service
from src.services.grvi import BARE_PIXEL, LOW_GREEN, grvi
from src.services.insurance_engine import AUDIENCE_LABELS, normalize_audience

logger = logging.getLogger(__name__)

# --- Services and statuses ---------------------------------------------------

SERVICES = {
    1: "Crop health",
    2: "Precision fertilizer",
    3: "Irrigation",
    4: "Pests and disease",
    5: "Field mapping",
    6: "Crop growth",
    7: "Stand count",
    8: "Yield prediction",
    9: "Spray planning",
    10: "Farm history",
    11: "Soil NPK",
}

READY = "ready"
PARTLY = "partly"
NEEDS_FLIGHT = "needs_flight"
NEEDS_CAMERA = "needs_camera"
TO_BUILD = "to_build"
LEARN = "learn"
WORKING = "working"

STATUS_LABELS = {
    READY: "Answer ready",
    PARTLY: "Partly ready",
    NEEDS_FLIGHT: "Needs a second flight",
    NEEDS_CAMERA: "Needs a special camera",
    TO_BUILD: "Coming soon",
    LEARN: "Learn · 1 minute",
    WORKING: "Working on it",
}

# How the cards are ranked: readiness first, then the situation, then the reader.
_BASE_SCORE = {READY: 100, PARTLY: 60, WORKING: 55, NEEDS_FLIGHT: 40, NEEDS_CAMERA: 40, TO_BUILD: 10, LEARN: 0}
_READER_BOOST = {
    "farmer": {"plots_green": 20, "weak_spots": 15, "plant_count": 10, "bare_ground": 5},
    "agronomist": {"plots_green": 20, "weak_spots": 15, "soil": 20, "fertilizer": 10, "water": 10},
    "insurance": {"bare_ground": 25, "history": 20, "field_outlines": 15, "growth": 10},
    "scientist": {"field_outlines": 20, "plots_green": 15, "weak_spots": 10, "soil": 10},
}
_SITUATION_BOOST = 30
BARE_SHARE_WORTH_ASKING = 0.03  # bare ground on 3% of the photo or more moves its card up
FOR_YOU_COUNT = 6  # cards shown first, the learning card included

# --- Bare ground measurement ---------------------------------------------------

BARE_SAMPLE_LONG_SIDE = 2048  # pixels read on the long side of the photo
BARE_BLOCK_M = 13.0  # bare share is smoothed over blocks this wide on the ground
BARE_SMOOTHED_CUT = 0.62  # an area is bare when this share of its neighbourhood is bare
MIN_PATCH_HA = 0.1  # smaller bare patches are not outlined or counted

MULTISPECTRAL_MIN_BANDS = 5  # RGB photos have 3 bands, 4 with transparency

_GEOD = Geod(ellps="WGS84")


@dataclass(frozen=True)
class BareGround:
    area_ha: float  # total of the outlined patches
    patches: int
    geojson: dict[str, Any]  # one feature per patch, largest first, in WGS84


@dataclass(frozen=True)
class PhotoAnalysis:
    layer_id: str
    look: drone_first_look.FirstLook
    band_count: int
    bounds: list[float]  # WGS84 [west, south, east, north]
    bare: BareGround
    zones: dict[str, Any]  # 3 x 3 zones of the photo in WGS84, named like the first look's zones

    @property
    def camera(self) -> str:
        return "multispectral" if self.band_count >= MULTISPECTRAL_MIN_BANDS else "colour"

    @property
    def centre(self) -> tuple[float, float]:
        west, south, east, north = self.bounds
        return (west + east) / 2, (south + north) / 2


@dataclass(frozen=True)
class Here:
    """What else is known about the photo's place: how many photos of it, and its plots."""

    photos: int = 1
    plots: Optional[drone_plots.PlotSet] = None
    plot_job: Optional[drone_plots.PlotJob] = None


def _to_wgs84(crs: Any) -> Callable[..., Any]:
    return Transformer.from_crs(crs, "EPSG:4326", always_xy=True).transform


def _area_ha(geom_wgs84: Any) -> float:
    return abs(_GEOD.geometry_area_perimeter(geom_wgs84)[0]) / 10_000


def _ground_metres_per_pixel(ds: Any, sample_width: int) -> float:
    west, south, east, north = transform_bounds(ds.crs, "EPSG:4326", *ds.bounds)
    lat = (south + north) / 2
    return _GEOD.inv(west, lat, east, lat)[2] / sample_width


def _smoothed(mask: np.ndarray, block: int) -> np.ndarray:
    """Share of True pixels around each pixel, over blocks of `block` pixels."""
    h, w = mask.shape
    hb, wb = max(1, h // block), max(1, w // block)
    trimmed = mask[:hb * block, :wb * block].astype("float32")
    frac = trimmed.reshape(hb, block, wb, block).mean(axis=(1, 3))
    image = Image.fromarray((frac * 255).astype("uint8")).resize((w, h), Image.BILINEAR)
    return np.asarray(image).astype("float32") / 255


def measure_bare_ground(ds: Any) -> BareGround:
    """Bare patches of at least MIN_PATCH_HA in an RGB photo, outlined in WGS84."""
    factor = max(1, max(ds.width, ds.height) // BARE_SAMPLE_LONG_SIDE)
    rows, cols = max(1, ds.height // factor), max(1, ds.width // factor)
    bands = ds.read([1, 2], out_shape=(2, rows, cols), resampling=Resampling.average, masked=True)
    nodata = np.ma.getmaskarray(bands).any(axis=0)
    bare_pixels = (grvi(bands[0], bands[1]) < BARE_PIXEL) & ~nodata

    block = max(2, round(BARE_BLOCK_M / _ground_metres_per_pixel(ds, cols)))
    bare_area = (_smoothed(bare_pixels, block) > BARE_SMOOTHED_CUT) & ~nodata

    transform = ds.transform * Affine.scale(ds.width / cols, ds.height / rows)
    to_wgs84 = _to_wgs84(ds.crs)
    patches = []
    for geom, _ in shapes(bare_area.astype("uint8"), mask=bare_area, transform=transform):
        outline = reproject(to_wgs84, shape(geom).simplify(abs(transform.a)))
        area = _area_ha(outline)
        if area >= MIN_PATCH_HA:
            patches.append((area, outline))
    patches.sort(key=lambda p: p[0], reverse=True)
    features = [
        {"type": "Feature", "geometry": mapping(outline),
         "properties": {"rank": i + 1, "area_ha": round(area, 2), "label": f"Bare patch {i + 1}: {_ha(area)}"}}
        for i, (area, outline) in enumerate(patches)
    ]
    return BareGround(
        area_ha=sum(area for area, _ in patches),
        patches=len(patches),
        geojson={"type": "FeatureCollection", "features": features},
    )


def photo_zones(ds: Any, look: drone_first_look.FirstLook) -> dict[str, Any]:
    """The first look's 3 x 3 zones as WGS84 rectangles, each with its low-green share."""
    to_wgs84 = _to_wgs84(ds.crs)
    features = []
    zones = iter(look.green.zones)
    for i in range(3):
        for j in range(3):
            zone = next(zones)
            x0, y0 = ds.transform * (j * ds.width / 3, i * ds.height / 3)
            x1, y1 = ds.transform * ((j + 1) * ds.width / 3, (i + 1) * ds.height / 3)
            rect = reproject(to_wgs84, box(min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)))
            features.append({"type": "Feature", "geometry": mapping(rect),
                             "properties": {"name": zone.name, "low_share": round(zone.low_share, 3)}})
    return {"type": "FeatureCollection", "features": features}


def _measure_photo(cog_url: str, look: drone_first_look.FirstLook) -> tuple[BareGround, dict[str, Any], list[float]]:
    with rasterio.open(cog_url) as ds:
        bounds = list(transform_bounds(ds.crs, "EPSG:4326", *ds.bounds))
        return measure_bare_ground(ds), photo_zones(ds, look), bounds


# One analysis per photo version; the photo never changes once uploaded.
_CACHE_SIZE = 16
_cache: "OrderedDict[tuple[str, str], PhotoAnalysis]" = OrderedDict()
_locks: dict[tuple[str, str], asyncio.Lock] = {}


async def analyse_layer(conn: Any, layer: dict[str, Any], cog_url: str) -> Optional[PhotoAnalysis]:
    """Measure a drone photo layer (layer_id, name, bounds, metadata with cog_key); None if not an RGB photo."""
    key = (layer["layer_id"], layer["metadata"].get("cog_key") or "")
    if key in _cache:
        _cache.move_to_end(key)
        return _cache[key]
    lock = _locks.setdefault(key, asyncio.Lock())
    async with lock:
        if key in _cache:
            return _cache[key]
        look = await drone_first_look.look_at_layer(conn, layer, cog_url)
        if look is None:
            return None
        bare, zones, bounds = await asyncio.wait_for(asyncio.to_thread(_measure_photo, cog_url, look), timeout=120)
        analysis = PhotoAnalysis(layer_id=layer["layer_id"], look=look,
                                 band_count=int(layer["metadata"].get("band_count") or 0),
                                 bounds=bounds, bare=bare, zones=zones)
        _cache[key] = analysis
        while len(_cache) > _CACHE_SIZE:
            _cache.popitem(last=False)
        return analysis


# --- Words -------------------------------------------------------------------------

def _tone(audience: str) -> str:
    return "technical" if audience in ("agronomist", "scientist") else "plain"


def _ha(value: float) -> str:
    if value >= 10:
        return f"{value:.0f} ha"
    if value >= 1:
        return f"{value:.1f} ha"
    return f"{value:.2f} ha"


def _pct(share: float) -> str:
    if 0 < share < 0.01:
        return "under 1%"
    return f"{round(share * 100)}%"


def _soil_digits(value: float) -> int:
    """Decimals for about three significant figures: 167, 10.6, 1.44."""
    return 0 if value >= 100 else 1 if value >= 10 else 2


def _camera_label(analysis: PhotoAnalysis) -> str:
    return "Multispectral camera" if analysis.camera == "multispectral" else "Normal colour camera"


# Explanations of the hard words, plain for farmers and insurers, technical for agronomists and scientists.
TERMS: dict[str, dict[str, Any]] = {
    "bare_ground": {
        "word": "Bare ground",
        "plain": ("Soil with no crop on it that the camera can see. It can be a field just harvested, "
                  "ready for planting, or left empty this season.",
                  "It shows quickly which fields are not growing a crop right now, so you know where to go and ask why."),
        "technical": (f"Pixels with GRVI below {BARE_PIXEL}: soil redder than it is green. Smoothed over about "
                      f"{BARE_BLOCK_M:.0f} m; patches under {MIN_PATCH_HA} ha are dropped.",
                      "Separates unplanted, prepared and harvested plots from standing crop without a field visit."),
    },
    "colour_camera": {
        "word": "Normal colour camera",
        "plain": ("A camera that sees what your eyes see: red, green and blue.",
                  "It shows where the crop is green and where it is not. It cannot see stress that still looks green."),
        "technical": ("An RGB sensor: three visible bands, no near-infrared or red-edge band.",
                      "Supports greenness indices such as GRVI and VARI, not NDVI or NDRE."),
    },
    "near_infrared": {
        "word": "Near-infrared light",
        "plain": ("A light our eyes cannot see. Healthy leaves send a lot of it back; stressed and dying leaves send less.",
                  "A special camera that sees it can show trouble before the leaves change colour."),
        "technical": ("Reflectance around 800 nm, high from healthy leaf structure; the red edge (about 700 nm) "
                      "is where healthy and stressed vegetation differ most.",
                      "Needed for NDVI and NDRE, which track vigour and nitrogen better than visible-band indices."),
    },
    "greenness": {
        "word": "Greenness",
        "plain": ("How green the photo is at each spot, worked out from its colours.",
                  "Less green can mean plants short of water or food, fewer plants, or bare soil."),
        "technical": (f"GRVI = (green - red) / (green + red). Below {LOW_GREEN} a pixel counts as low green.",
                      "The best vegetation signal an RGB photo allows; it saturates earlier than NDVI."),
    },
    "hectare": {
        "word": "Hectare (ha)",
        "plain": ("A square of land 100 metres by 100 metres. A football pitch is about 0.7 ha.",
                  "Field sizes, harvests and inputs are usually counted per hectare."),
        "technical": ("10,000 square metres, measured on the ellipsoid from the photo's outline.",
                      "The unit for rates, yields and areas in every answer."),
    },
    "soil_estimate": {
        "word": "Soil estimate",
        "plain": ("A guess of the soil's nutrients made by a computer model for all of Africa, at 30 m.",
                  "It shows what the soil may be short of before you test. A lab test of real samples decides."),
        "technical": ("iSDAsoil: machine-learning predictions at 30 m for the top 20 cm. The likely range is "
                      "one model standard deviation either side (about 68%).",
                      "A prior for sampling design and first rates; it does not replace laboratory analysis."),
    },
    "water_use": {
        "word": "Water use",
        "plain": ("How much water plants and soil give off to the air each day, measured from satellite.",
                  "When it drops while a crop is growing, the crop may be short of water."),
        "technical": ("Actual evapotranspiration and interception (FAO WaPOR v3, 100 m, 10-day steps), in mm per day.",
                      "Tracks crop water use; with rainfall it shows whether irrigation is keeping up."),
    },
}


def _terms(ids: list[str], audience: str) -> list[dict[str, str]]:
    tone = _tone(audience)
    return [{"id": term_id, "word": TERMS[term_id]["word"], "simple": TERMS[term_id][tone][0],
             "why": TERMS[term_id][tone][1]} for term_id in ids]


def _how_sure(level: str, because: list[str], surer: Optional[str]) -> dict[str, Any]:
    return {"level": level, "label": level.capitalize(), "bars": {"low": 1, "medium": 2, "high": 3}[level],
            "because": because, "surer": surer}


# --- The cards -------------------------------------------------------------------------

@dataclass(frozen=True)
class _CardDef:
    id: str
    service: int
    question: str


_CARDS = [
    _CardDef("plots_green", 1, "Which plots are the least green?"),
    _CardDef("weak_spots", 1, "Which parts of my crop look weak?"),
    _CardDef("bare_ground", 1, "Which fields are bare right now?"),
    _CardDef("fertilizer", 2, "Which zones need more fertilizer?"),
    _CardDef("water", 3, "Is water reaching every part of the field?"),
    _CardDef("pests", 4, "Where might pests or disease be starting?"),
    _CardDef("field_outlines", 5, "Where exactly are my plots, and how big is each?"),
    _CardDef("growth", 6, "Is the crop growing on schedule?"),
    _CardDef("plant_count", 7, "How many plants came up, and where are the gaps?"),
    _CardDef("yield", 8, "How much will this field yield?"),
    _CardDef("spray", 9, "Where should we spray, and where not?"),
    _CardDef("history", 10, "How did this field do in past seasons?"),
    _CardDef("soil", 11, "What do my soils need, zone by zone?"),
    _CardDef("learn_camera", 1, "Is my crop sick, or just less green?"),
]
_CARD_BY_ID = {card.id: card for card in _CARDS}


_PLOT_CARDS = ("field_outlines", "plots_green")


def _plot_status(here: Here) -> str:
    if here.plots is not None:
        return READY
    if here.plot_job is not None and here.plot_job.state == "running":
        return WORKING
    return PARTLY


def _status(card_id: str, analysis: PhotoAnalysis, here: Here) -> str:
    if card_id in ("weak_spots", "bare_ground"):
        return READY
    if card_id in _PLOT_CARDS:
        return _plot_status(here)
    if card_id in ("water", "history", "soil"):
        return PARTLY
    if card_id == "fertilizer":
        return NEEDS_CAMERA if analysis.camera == "colour" else TO_BUILD
    if card_id == "growth":
        return NEEDS_FLIGHT if here.photos < 2 else TO_BUILD
    if card_id == "learn_camera":
        return LEARN
    return TO_BUILD


def _plot_job_words(job: Optional[drone_plots.PlotJob]) -> str:
    if job is None:
        return "Ingabe has not looked for the plots in this photo yet."
    if job.state == "failed":
        return "Finding the plots did not work this time."
    left = job.minutes_left
    return ("Finding the plots in this photo" + (f": about {left} minute{'s' if left != 1 else ''} left."
                                                 if left is not None else ". It takes a few minutes."))


def _preview(card_id: str, analysis: PhotoAnalysis, here: Here) -> str:
    look, bare, plots = analysis.look, analysis.bare, here.plots
    if card_id in _PLOT_CARDS and plots is None:
        return _plot_job_words(here.plot_job)
    if card_id == "plots_green" and plots is not None:
        least = _least_green(plots)
        if not least:
            return f"{plots.count} plots found; too few measured to compare."
        return f"Plots {_numbers(least[:3])} are the least green of {plots.count}."
    if card_id == "weak_spots":
        hot = look.green.hotspots
        if hot:
            return f"The {' and '.join(z.name for z in hot)} look less green than the rest."
        greenest = look.green.greenest
        return "No part stands out." + (f" The {greenest.name} is greenest." if greenest else "")
    if card_id == "bare_ground":
        if not bare.patches:
            return f"No bare patches larger than {MIN_PATCH_HA} ha."
        return f"About {_ha(bare.area_ha)} is bare ground, in {bare.patches} patches."
    if card_id == "fertilizer":
        return ("Needs a multispectral photo and soil tests per zone." if analysis.camera == "colour"
                else "Zones with a rate each, and a file for the spreader.")
    if card_id == "water":
        return "Satellite water use for this area; dry patches on the photo are coming."
    if card_id == "pests":
        return "Spots that look different, ranked; a leaf close-up names the cause."
    if card_id == "field_outlines" and plots is not None:
        return f"{plots.count} plots, numbered and measured, {_ha(plots.total_ha)} in all."
    if card_id == "growth":
        return ("This is the only photo of this place. Fly again to compare."
                if here.photos < 2 else "Height and greenness against the last flight.")
    if card_id == "plant_count":
        return "Every plant counted, gaps marked, checked against 3 hand counts."
    if card_id == "yield":
        return "A harvest range for each field that narrows through the season."
    if card_id == "spray":
        return "Spray zones and the area to treat, from the pest map."
    if card_id == "history":
        return f"{here.photos} photo{'s' if here.photos != 1 else ''} of this place kept so far."
    if card_id == "soil":
        return "A first estimate of N, P and K here; lab tests decide."
    return "Your camera sees colour, not health. See what a special camera adds."


def _card(card_id: str, analysis: PhotoAnalysis, here: Here) -> dict[str, Any]:
    definition = _CARD_BY_ID[card_id]
    status = _status(card_id, analysis, here)
    return {
        "id": card_id,
        "service": definition.service,
        "service_name": SERVICES[definition.service],
        "question": definition.question,
        "preview": _preview(card_id, analysis, here),
        "status": status,
        "status_label": STATUS_LABELS[status],
    }


def _score(card: dict[str, Any], analysis: PhotoAnalysis, audience: str) -> int:
    score = _BASE_SCORE[card["status"]] + _READER_BOOST.get(audience, {}).get(card["id"], 0)
    look_area = analysis.look.area_ha
    bare_share = analysis.bare.area_ha / look_area if look_area else 0.0
    if card["id"] == "bare_ground" and bare_share >= BARE_SHARE_WORTH_ASKING:
        score += _SITUATION_BOOST
    if card["id"] == "weak_spots" and analysis.look.green.hotspots:
        score += _SITUATION_BOOST
    return score


def _least_green(plots: drone_plots.PlotSet) -> list[dict[str, Any]]:
    """The least green fifth of the plots, least green first."""
    least = [p for p in plots.plots() if p["group"] == drone_plots.LEAST_GREEN]
    return sorted(least, key=lambda p: p["greenness"])


def _numbers(plots: list[dict[str, Any]]) -> str:
    numbers = [str(p["number"]) for p in plots]
    return numbers[0] if len(numbers) == 1 else ", ".join(numbers[:-1]) + " and " + numbers[-1]


def build_deck(analysis: PhotoAnalysis, audience: Optional[str], here: Here) -> dict[str, Any]:
    """The photo's summary, the cards to show first, and every card grouped by service."""
    reader = normalize_audience(audience)
    cards = [_card(card.id, analysis, here) for card in _CARDS]
    learn = next(card for card in cards if card["status"] == LEARN)
    ranked = sorted((card for card in cards if card is not learn),
                    key=lambda card: _score(card, analysis, reader), reverse=True)
    look = analysis.look
    summary = [f"About {_ha(look.area_ha)}" if look.area_ha is not None else None, _camera_label(analysis),
               f"{look.resolution_cm:.1f} cm per pixel" if look.resolution_cm is not None else None]
    return {
        "photo": {
            "summary": " · ".join(part for part in summary if part),
            "layer_id": analysis.layer_id,
            "name": look.layer_name,
            "place": look.place,
            "area_ha": look.area_ha,
            "resolution_cm": look.resolution_cm,
            "camera": analysis.camera,
            "camera_label": _camera_label(analysis),
            "bounds": analysis.bounds,
            "photos_here": here.photos,
        },
        "audience": reader,
        "audience_label": AUDIENCE_LABELS[reader],
        "audiences": [{"id": key, "label": label} for key, label in AUDIENCE_LABELS.items()],
        "for_you": ranked[:FOR_YOU_COUNT - 1] + [learn],
        "services": [
            {"service": number, "name": name, "cards": [card for card in cards if card["service"] == number]}
            for number, name in SERVICES.items()
        ],
    }


# --- The answers -----------------------------------------------------------------------

def _answer(card_id: str, analysis: PhotoAnalysis, here: Here, *, what: str, why: str, todo: str,
            how_sure: dict[str, Any], terms: list[str], audience: str,
            overlay: Optional[dict[str, Any]] = None, facts: Optional[list[dict[str, str]]] = None,
            items: Optional[list[dict[str, Any]]] = None, downloads: Optional[list[dict[str, str]]] = None,
            progress: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """One answer. items: places to go, each with a point; progress: set while the answer is still being worked out."""
    card = _card(card_id, analysis, here)
    return {**card, "what": what, "why": why, "todo": todo, "how_sure": how_sure,
            "overlay": overlay, "facts": facts or [], "terms": _terms(terms, audience),
            "items": items or [], "downloads": downloads or [], "progress": progress}


def _weak_spots(analysis: PhotoAnalysis, audience: str, here: Here) -> dict[str, Any]:
    green = analysis.look.green
    technical = _tone(audience) == "technical"
    hot = green.hotspots
    zone_features = analysis.zones["features"]
    if hot:
        names = [z.name for z in hot]
        detail = "; ".join(f"{z.name} {_pct(z.low_share)} low green" for z in hot)
        what = (f"The {' and '.join(names)} of the photo look less green than the rest ({detail}, "
                f"against {_pct(green.low_share)} across the photo).")
        todo = (f"Scout the {names[0]} first: rule out water, nutrients, missing plants and pests." if technical
                else f"Walk to the {names[0]} part first and look at the leaves and the soil.")
        shown = [f for f in zone_features if f["properties"]["name"] in names]
        overlay = {"kind": "attention", "legend": "Less green than the rest",
                   "geojson": {"type": "FeatureCollection", "features": shown}}
    else:
        greenest = green.greenest
        what = (f"No part of the photo stands out: low green is spread across it ({_pct(green.low_share)} of the photo)."
                + (f" The {greenest.name} is the greenest part." if greenest else ""))
        todo = ("Nothing to chase first. Use the bare-ground question to find empty fields, and fly again "
                "in a few weeks to see what changes.")
        shown = [f for f in zone_features if greenest and f["properties"]["name"] == greenest.name]
        overlay = ({"kind": "good", "legend": "Greenest part of the photo",
                    "geojson": {"type": "FeatureCollection", "features": shown}} if shown else None)
    if technical:
        what += f" Mean GRVI {green.mean:.3f} ({analysis.look.verdict_message.rstrip('.').lower()})."
    why = ("Less green can mean plants short of water or food, fewer plants, or bare soil. Roofs and roads "
           "are not green either.")
    how_sure = _how_sure("medium", [
        "Measured from colour: greenness, not health",
        "Not checked on the ground",
        "Roofs, roads and paths count as low green",
    ], "Check one weak part on foot and mark what you see.")
    return _answer("weak_spots", analysis, here, what=what, why=why, todo=todo, how_sure=how_sure,
                   terms=["greenness", "colour_camera"], audience=audience, overlay=overlay)


def _bare_ground(analysis: PhotoAnalysis, audience: str, here: Here) -> dict[str, Any]:
    bare, area = analysis.bare, analysis.look.area_ha
    if not bare.patches:
        what = f"No bare patches larger than {MIN_PATCH_HA} ha in this photo."
        overlay = None
    else:
        of_photo = f" of the {_ha(area)} in this photo" if area is not None else ""
        what = f"About {_ha(bare.area_ha)}{of_photo} is bare ground right now, in {bare.patches} patches, outlined on the photo."
        overlay = {"kind": "bare", "legend": "Bare ground right now", "geojson": bare.geojson}
    if audience == "insurance":
        why = ("One photo cannot tell a field planted and lost from one never planted. An earlier photo "
               "or planting records can.")
        todo = "Ask for an earlier flight or the planting records for the largest patches before judging a claim."
    elif _tone(audience) == "technical":
        why = "Bare can be harvested, prepared for planting, or not planted this season; paths add a little."
        todo = "Visit the largest patches first and record harvested, prepared or unplanted for each."
    else:
        why = "Bare now can mean just harvested, ready to plant, or not planted this season. Each needs a different step."
        todo = "Tap a patch to see its size. Walk to the largest ones and note which it is."
    how_sure = _how_sure("medium", [
        "Measured from colour only",
        "Not checked on the ground",
        "The cut between bare and green was checked by eye on one photo",
    ], "Mark what you find at 3 patches.")
    return _answer("bare_ground", analysis, here, what=what, why=why, todo=todo, how_sure=how_sure,
                   terms=["bare_ground", "hectare", "colour_camera"], audience=audience, overlay=overlay)


def _soil(analysis: PhotoAnalysis, audience: str, here: Here) -> dict[str, Any]:
    lon, lat = analysis.centre
    result = isdasoil_service.query_soil_point(
        lon, lat, properties=["nitrogen_total", "phosphorous_extractable", "potassium_extractable", "ph"])
    props = result.get("properties") if result.get("status") == "success" else None
    facts = []
    for key in ("nitrogen_total", "phosphorous_extractable", "potassium_extractable", "ph"):
        entry = (props or {}).get(key) or {}
        if entry.get("value") is None:
            continue
        unit = f" {entry['unit']}" if entry.get("unit") else ""
        digits = _soil_digits(entry["value"])
        value = f"{entry['value']:.{digits}f}{unit}"
        if entry.get("likely_range"):
            low, high = entry["likely_range"]
            value += f" (likely {low:.{digits}f}-{high:.{digits}f})"
        facts.append({"label": entry["label"], "value": value})
    if facts:
        what = "A first estimate for the soil in the middle of this photo, top 20 cm. The values are below."
    else:
        logger.warning("soil estimate unavailable for %s: %s", analysis.layer_id, result.get("error"))
        what = "The soil estimate could not be read right now. Try again in a moment."
    return _answer(
        "soil", analysis, here, what=what,
        why="It shows what the soil may be short of before you test, and where testing matters most.",
        todo=("Take soil samples in the parts of the photo that look different and test them in a lab. "
              "Ingabe will then show nitrogen, phosphorus and potassium zone by zone."),
        how_sure=_how_sure("low", [
            "Estimated by a model for all of Africa at 30 m, not measured here",
            "A drone cannot measure soil nutrients",
        ], "Lab tests of samples from each zone."),
        terms=["soil_estimate"], audience=audience, facts=facts)


def _water(analysis: PhotoAnalysis, audience: str, here: Here) -> dict[str, Any]:
    lon, lat = analysis.centre
    today = date.today()
    result = wapor_service.query_et(lat, lon, date_from=today - timedelta(days=45), date_to=today - timedelta(days=5))
    mean = (result.get("summary") or {}).get("mean_et_mm_per_day") if result.get("status") == "success" else None
    facts = []
    if mean is not None:
        what = (f"Over the last weeks, the crops and soil in this area used about {mean} mm of water a day "
                "(satellite estimate). Dry patches inside the photo are not measured yet.")
        facts.append({"label": "Water use, satellite (100 m)", "value": f"{mean} mm a day"})
        facts.append({"label": "Period", "value": str(result.get("date_range"))})
    else:
        logger.warning("water use unavailable for %s: %s", analysis.layer_id, result.get("error"))
        what = "There is no satellite reading of water use for this place yet."
    return _answer(
        "water", analysis, here, what=what,
        why="Water use that drops while the crop is growing can mean it is short of water.",
        todo=("Compare with when the field was irrigated or rained on. To find dry patches inside a field, "
              "a drone with a heat (thermal) camera works best."),
        how_sure=_how_sure("low", [
            "Satellite pixels are 100 m: the whole area, not each field",
            "Dry patches on this photo are not measured yet",
        ], "A thermal drone flight, or irrigation records for the same weeks."),
        terms=["water_use"], audience=audience, facts=facts)


def _photo_outline(analysis: PhotoAnalysis) -> dict[str, Any]:
    west, south, east, north = analysis.bounds
    return {"kind": "outline", "legend": "This photo",
            "geojson": {"type": "FeatureCollection", "features": [
                {"type": "Feature", "geometry": mapping(box(west, south, east, north)), "properties": {}}]}}


def _plots_pending(card_id: str, analysis: PhotoAnalysis, audience: str, here: Here) -> dict[str, Any]:
    """A plot card while the plots are still being found, or after finding them failed."""
    job = here.plot_job
    failed = job is not None and job.state == "failed"
    progress = None if job is None or failed else {
        "done": job.parts_done, "parts": job.parts, "minutes_left": job.minutes_left}
    return _answer(
        card_id, analysis, here,
        what=_plot_job_words(job) + ("" if failed else " This answer fills in by itself."),
        why="Every answer can then be given plot by plot, each with its own size, greenness and bare ground.",
        todo=("Open this question again to try once more." if failed
              else "Keep the photo open, or come back later: the plots are kept once found."),
        how_sure=_how_sure("low", ["Not answered yet"], None),
        terms=["hectare"], audience=audience, overlay=_photo_outline(analysis), progress=progress)


def _plot_downloads(analysis: PhotoAnalysis) -> list[dict[str, str]]:
    base = f"/api/layer/{analysis.layer_id}/plots"
    return [{"label": "Excel table", "href": f"{base}.xlsx"},
            {"label": "Shapefile", "href": f"{base}.zip"},
            {"label": "GeoJSON", "href": f"{base}.geojson"}]


def _plot_source(plots: drone_plots.PlotSet) -> str:
    return (f"Outlined by {drone_plots.FIELD_MODEL_NAME}, a field-outlining model, at {plots.read_m_per_px:g} m "
            "per pixel" if plots.source == "found" else f"Your own plot map: {plots.source}")


def _field_outlines(analysis: PhotoAnalysis, audience: str, here: Here) -> dict[str, Any]:
    plots = here.plots
    if plots is None:
        return _plots_pending("field_outlines", analysis, audience, here)
    areas = sorted(p["area_ha"] for p in plots.plots())
    found = plots.source == "found"
    what = (f"Ingabe found {plots.count} plots in this photo, {_ha(plots.total_ha)} in all, numbered from the "
            "north-west." if found else f"Your map {plots.source} has {plots.count} plots on this photo, "
            f"{_ha(plots.total_ha)} in all.")
    facts = [{"label": "Plots", "value": str(plots.count)}, {"label": "All plots together", "value": _ha(plots.total_ha)}]
    if areas:
        median = areas[len(areas) // 2]
        what += f" Half the plots are smaller than {_ha(median)}."
        facts += [{"label": "Middle plot size", "value": _ha(median)},
                  {"label": "Smallest and largest", "value": f"{_ha(areas[0])} and {_ha(areas[-1])}"}]
    facts.append({"label": "Outlines", "value": _plot_source(plots)})
    features = [{**f, "properties": {**f["properties"],
                                     "label": f"Plot {f['properties']['number']} · {_ha(f['properties']['area_ha'])}"}}
                for f in plots.geojson["features"]]
    because = [_plot_source(plots), "Areas measured from the outlines"]
    if found:
        because += ["Checked by eye on one photo, not against surveyed edges",
                    "Some plots are missed or joined, mostly where neighbours look alike"]
    if audience == "insurance":
        why = "A claim or a policy can then name the plot, its size and its place, from the same photo."
    elif _tone(audience) == "technical":
        why = "Every measurement can then be given per plot, with its own area, and joined to your records by number."
    else:
        why = "Every answer can then be given plot by plot, each with its own size."
    return _answer(
        "field_outlines", analysis, here, what=what, why=why,
        todo=("Tap a plot to see its number and size. Check the outlines of plots you know, then download the "
              "table or the map to share it."),
        how_sure=_how_sure("medium" if found else "high", because,
                           "Walk the edges of a few plots with a phone GPS and compare." if found else None),
        terms=["hectare"], audience=audience, facts=facts, downloads=_plot_downloads(analysis),
        overlay={"kind": "plots", "legend": "Plots, numbered from the north-west",
                 "geojson": {"type": "FeatureCollection", "features": features}})


def _plots_green(analysis: PhotoAnalysis, audience: str, here: Here) -> dict[str, Any]:
    plots = here.plots
    if plots is None:
        return _plots_pending("plots_green", analysis, audience, here)
    technical = _tone(audience) == "technical"
    least = _least_green(plots)
    measured = [p for p in plots.plots() if p["greenness"] is not None]
    group_label = {drone_plots.LEAST_GREEN: "least green", drone_plots.GREENEST: "greenest",
                   drone_plots.BETWEEN: "in between", drone_plots.UNKNOWN: "not measured"}

    def label(p: dict[str, Any]) -> str:
        text = f"Plot {p['number']} · {_ha(p['area_ha'])} · {group_label[p['group']]}"
        return text + (f" · GRVI {p['greenness']:.3f}" if technical and p["greenness"] is not None else "")

    features = [{**f, "properties": {**f["properties"], "label": label(f["properties"])}}
                for f in plots.geojson["features"]]
    overlay = {"kind": "plot_groups", "legend": "Least green fifth of the plots",
               "geojson": {"type": "FeatureCollection", "features": features}}
    if not least:
        return _answer(
            "plots_green", analysis, here,
            what=f"{plots.count} plots found, but too few could be measured to compare them.",
            why="Comparing needs at least 5 plots that lie fully on the photo.",
            todo="Fly a wider area, or ask about the whole photo instead.",
            how_sure=_how_sure("low", ["Too few plots measured"], None),
            terms=["greenness"], audience=audience, overlay=overlay)
    first = least[:3]
    least_ha = sum(p["area_ha"] for p in least)
    what = (f"Of the {len(measured)} plots measured, {len(least)} are the least green ({_ha(least_ha)}), "
            f"filled on the photo. Least green first: plots {_numbers(least[:5])}.")
    if technical:
        greenness = sorted(p["greenness"] for p in measured)
        what += (f" Mean GRVI of the least green plot {least[0]['greenness']:.3f}; middle plot "
                 f"{greenness[len(greenness) // 2]:.3f}.")
    if audience == "insurance":
        why = ("Least green can be damage, but also a crop planted later or just harvested. One photo cannot "
               "tell them apart.")
        todo = (f"Before judging a claim on plots {_numbers(first)}, compare with an earlier flight or the "
                "planting dates.")
    elif technical:
        why = ("Low greenness can mean water or nutrient stress, gaps, weeds cleared, or a different stage. "
               "It is relative to the other plots in this photo.")
        todo = f"Scout plots {_numbers(first)} first: check the stage, gaps, water, nutrients and pests."
    else:
        why = ("Less green can mean plants short of water or food, missing plants, or bare soil. A plot just "
               "planted or just harvested is also less green, so look before acting.")
        todo = f"Walk to plots {_numbers(first)} first and look at the plants and the soil. Tap a plot to see its number."
    items = [{"id": str(p["number"]), "title": f"Plot {p['number']}",
              "detail": f"{_ha(p['area_ha'])}" + (f" · {_pct(p['bare_share'])} bare" if p["bare_share"] else ""),
              "lon": p["lon"], "lat": p["lat"]} for p in least[:5]]
    facts = [{"label": "Plots measured", "value": str(len(measured))},
             {"label": "Least green fifth", "value": f"{len(least)} plots, {_ha(least_ha)}"},
             {"label": "Compared with", "value": "the other plots in this photo"},
             {"label": "Outlines", "value": _plot_source(plots)}]
    return _answer(
        "plots_green", analysis, here, what=what, why=why, todo=todo,
        how_sure=_how_sure("medium", [
            "Greenness from a colour camera, not health",
            "Compared only with the other plots in this photo",
            "Not checked on the ground",
        ], f"Look at plots {_numbers(first)} on foot and note what you find."),
        terms=["greenness", "colour_camera"], audience=audience, overlay=overlay, facts=facts, items=items,
        downloads=_plot_downloads(analysis))


def _history(analysis: PhotoAnalysis, audience: str, here: Here) -> dict[str, Any]:
    return _answer(
        "history", analysis, here,
        what=f"Ingabe keeps {here.photos} photo{'s' if here.photos != 1 else ''} of this place in this project.",
        why="Comparing seasons shows which fields keep doing well or badly, and why.",
        todo="Add each new flight to this project. A timeline for each field, with findings and harvests, is coming.",
        how_sure=_how_sure("high", ["Counted from the photos in this project"], None),
        terms=[], audience=audience)


def _learn_camera(analysis: PhotoAnalysis, audience: str, here: Here) -> dict[str, Any]:
    colour = analysis.camera == "colour"
    return _answer(
        "learn_camera", analysis, here,
        what=("Your photo comes from a normal colour camera. It sees green, but a leaf can be stressed and still look "
              "green." if colour else "Your photo comes from a multispectral camera, which also sees near-infrared light."),
        why="Healthy leaves send back a light our eyes cannot see. It drops before the leaves change colour.",
        todo=("To see stress early, fly a multispectral camera. Ingabe will always say when it would tell you more."
              if colour else "Ingabe will use the extra bands for health and fertilizer zones."),
        how_sure=_how_sure("high", ["How leaves reflect light is well established"], None),
        terms=["colour_camera", "near_infrared"], audience=audience)


# Cards not answered yet: what they will show, why that matters, and what they need.
_COMING = {
    "fertilizer": ("the field split into a few zones, each with its own rate, and a file for the spreader",
                   "Each part of the field gets what it needs: no shortage where the crop is weak, no waste where it is strong.",
                   "a multispectral photo (its red-edge band shows how much nitrogen the leaves hold), soil tests per "
                   "zone, and your agronomist's rates"),
    "pests": ("spots that look different from their neighbours, ranked, and the likely pest or disease named from a "
              "close-up photo of a leaf",
              "An outbreak found while it is small can be stopped before it spreads across the field.",
              "a drone photo, a phone close-up of a leaf, and a check on foot before spraying"),
    "growth": ("height and greenness compared with the last flight and with the stage the crop should be at",
               "A crop falling behind its stage is an early warning you can still act on.",
               "two or more flights of the same place, ground visible for height, and a ruler on a few plants"),
    "plant_count": ("every plant counted, the gaps marked, and plants per hectare for each field",
                    "Knowing early how many plants came up tells you whether to fill the gaps while there is time.",
                    "a low flight soon after the crop comes up, and 3 small squares counted by hand to check it"),
    "yield": ("an expected harvest range for each field, which narrows as the season goes on",
              "Knowing the harvest early helps plan labour, storage and transport.",
              "plant counts, growth, weather, and past harvests to learn from"),
    "spray": ("spray zones and the area to treat, with a route or file for the sprayer",
              "Spraying only where it is needed protects the crop, the workers and the water around the field.",
              "the pest, weed or disease map from the pests question"),
}


def _coming(card_id: str, analysis: PhotoAnalysis, audience: str, here: Here) -> dict[str, Any]:
    shows, why, needs = _COMING[card_id]
    status = _status(card_id, analysis, here)
    prefix = ("This is the only photo of this place, so there is nothing to compare yet. "
              if card_id == "growth" and status == NEEDS_FLIGHT else "")
    return _answer(
        card_id, analysis, here, what=f"{prefix}When this is ready, Ingabe will show {shows}.",
        why=why, todo=f"What it needs: {needs}.",
        how_sure=_how_sure("low", [f"Not answered yet: {STATUS_LABELS[status].lower()}"], None),
        terms=["near_infrared"] if card_id == "fertilizer" else [], audience=audience)


_ANSWERS: dict[str, Callable[[PhotoAnalysis, str, Here], dict[str, Any]]] = {
    "plots_green": _plots_green,
    "weak_spots": _weak_spots,
    "bare_ground": _bare_ground,
    "soil": _soil,
    "water": _water,
    "field_outlines": _field_outlines,
    "history": _history,
    "learn_camera": _learn_camera,
}


def answer_card(card_id: str, analysis: PhotoAnalysis, audience: Optional[str], here: Here) -> dict[str, Any]:
    """The answer to one card; KeyError for an unknown card. Soil and water read external data (blocking)."""
    if card_id not in _CARD_BY_ID:
        raise KeyError(card_id)
    reader = normalize_audience(audience)
    if card_id in _ANSWERS:
        return _ANSWERS[card_id](analysis, reader, here)
    return _coming(card_id, analysis, reader, here)
