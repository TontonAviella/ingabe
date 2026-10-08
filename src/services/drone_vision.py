"""What a vision model sees in each plot of a drone photo: crop, stage, ground cover, weeds and visible problems.

Ingabe shows the model the plot (outlined in white, a few metres around it) and squares from inside it at
the photo's full detail, and asks for a fixed set of answers. One survey feeds several cards (what is
growing, which plots are behind, which need weeding, what problems show from the air), so the cards agree.
Areas, greenness and bare ground stay Ingabe's own measurements (drone_plots.py); the model only names
what it sees.

The crop is named only when two independent looks agree; otherwise the plot is "not sure" with the two
candidates kept. Measured on 2026-10-07 against 36 Cyampirita plots labelled by eye (not field truth):
one shrunken picture per plot was right 38% of the time (it read maize leaves as cassava "lobes");
squares at full detail with sizes spelt out, 83%; two looks that must agree (sizes, and reference
squares of checked crops), 27 of 27, with 9 of 36 left "not sure". Field checks (field_checks.py)
override the model and say how often it is right.

Model: GPT-6 Luna through OpenRouter (Roger's choice, 2026-10-06), at high reasoning effort: about
$0.0015 a plot for both looks. Fixed instructions go first as text, so the provider's prompt cache can
serve them; pictures go last.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import logging
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Optional

import numpy as np
import rasterio
from openai import AsyncOpenAI
from PIL import Image, ImageDraw
from pyproj import Transformer
from rasterio.enums import Resampling
from rasterio.windows import from_bounds
from shapely.geometry import Point, shape
from shapely.ops import transform as reproject

from src.llm_defaults import resolve_chat_endpoint
from src.services import background_jobs, drone_plots, llm_cache

logger = logging.getLogger(__name__)

VISION_MODEL = "openai/gpt-6-luna"  # override with DRONE_VISION_MODEL
IMAGE_LONG_SIDE = 768  # pixels of the picture shown per plot
CONTEXT_M = 4.0  # metres of surroundings shown around the plot
CLOSE_M = 16.0  # metres across each square shown from inside the plot
CLOSE_PX = 512  # its pixels: 32 to the metre, whatever the photo's own detail
CLOSEUPS_PER_LOOK = 2
EFFORT = "high"  # reasoning effort; "low" named the crop right half as often on the checked plots
CONCURRENT_LOOKS = 8
MAX_FAILED_SHARE = 0.3  # more failed looks than this and the survey counts as failed
_STORE_PREFIX = "drone_vision/v2"
REFS_PREFIX = "drone_vision/refs/v1"  # reference squares of checked crops, shared by every photo
MAX_REFS_PER_CROP = 1

CROPS = ["maize", "beans", "cassava", "banana", "sorghum", "rice", "irish_potato", "sweet_potato", "soybean",
         "groundnut", "pineapple", "coffee", "tea", "sugarcane", "vegetables", "fruit_trees", "grass_or_pasture",
         "woodlot", "fallow_or_bare", "other", "unsure"]
STAGES = ["bare", "just_planted", "young", "growing", "flowering", "mature", "harvested", "unsure"]
WEEDS = ["none", "few", "many", "unsure"]
PROBLEMS = ["yellowing", "gaps", "standing_water", "wilting", "lodging", "damage"]
STAGE_ORDER = {"just_planted": 1, "young": 2, "growing": 3, "flowering": 4, "mature": 5}

CROP_LABELS = {c: c.replace("_", " ").capitalize() for c in CROPS} | {
    "irish_potato": "Irish potato", "grass_or_pasture": "Grass or pasture", "fallow_or_bare": "Fallow or bare",
    "fruit_trees": "Fruit trees", "unsure": "Not sure"}
PROBLEM_LABELS = {"yellowing": "yellowing", "gaps": "gaps in the crop", "standing_water": "standing water",
                  "wilting": "wilting", "lodging": "plants lying down", "damage": "damage"}

_SIZES = (
    "How crops look from straight above. Sizes matter more than shapes: in the 16 m squares, 1 m is 32 pixels "
    "(the white bar).\n"
    "- maize: ONE plant is a star 1-1.5 m across made of 8-14 single long blades, each 5-8 cm wide and 60-100 cm "
    "long with a pale midrib; tall maize casts long shadows; rows 60-90 cm apart. A maize star can look like "
    "spread fingers, but each 'finger' is a whole long leaf, far bigger than a cassava lobe.\n"
    "- cassava: ONE leaf is a small hand only 15-25 cm across with 5-7 thin lobes; a plant is a cluster of many "
    "such small hands on long reddish stalks, so the canopy looks finely fringed, not made of long blades.\n"
    "- banana: leaves 1-3 m long and 30-60 cm wide, often torn into strips; crowns 3-4 m apart.\n"
    "- pineapple: stiff, spiky rosettes about 1 m across, grey-blue, in very regular tight double rows.\n"
    "- beans: a low dense mat of small rounded leaves 5-10 cm across; no stars, no long blades.\n"
    "- irish potato: low bushy plants with small compound leaves on raised ridges.\n"
    "- sweet potato: creeping vines with heart-shaped leaves covering mounds.\n"
    "Several crops often grow together (maize with beans below is common): the main crop is the tallest, most "
    "visible one; list the others.\n"
    "First write the evidence you see (leaf shape and size, plant size and spacing, rows, shadows), then the crop. "
    "If the evidence does not clearly fit one crop, the main crop is unsure. Never guess.")

_LOOK_SYSTEM = (
    "You look at drone photos of farm plots in Rwanda and report what is in each plot, for farmers.\n" + _SIZES +
    "\nAlso report: the crop's stage; how much of the ground the crop covers; weeds; and problems you can see from "
    "above (yellowing, gaps in the crop, standing water, wilting, plants lying down, damage). note: one short plain "
    "sentence a farmer understands.")

_CHECK_SYSTEM = (
    "You check which crop grows in a farm plot in Rwanda, from drone photos. You are shown reference squares of "
    "crops already checked, then the plot. Compare leaf shapes and sizes with the references.\n" + _SIZES)

_LOOK_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "evidence": {"type": "string"},
        "main_crop": {"type": "string", "enum": CROPS},
        "other_crops": {"type": "array", "items": {"type": "string", "enum": CROPS}},
        "stage": {"type": "string", "enum": STAGES},
        "crop_cover_percent": {"type": "integer"},
        "weeds": {"type": "string", "enum": WEEDS},
        "problems": {"type": "array", "items": {"type": "string", "enum": PROBLEMS}},
        "note": {"type": "string"},
        "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
    },
    "required": ["evidence", "main_crop", "other_crops", "stage", "crop_cover_percent", "weeds", "problems", "note",
                 "confidence"],
}

_CHECK_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "evidence": {"type": "string"},
        "main_crop": {"type": "string", "enum": CROPS},
        "other_crops": {"type": "array", "items": {"type": "string", "enum": CROPS}},
        "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
    },
    "required": ["evidence", "main_crop", "other_crops", "confidence"],
}


@dataclass(frozen=True)
class PlotLook:
    number: int
    main_crop: str  # "unsure" unless both looks named the same crop
    other_crops: list[str]
    stage: str
    crop_cover_percent: int
    weeds: str
    problems: list[str]
    note: str
    confidence: str
    candidates: tuple[str, ...] = ()  # when the two looks named different crops: both, for a field check
    evidence: str = ""  # what the first look says it saw


@dataclass(frozen=True)
class Reference:
    """A square of a crop someone checked, shown to the second look; `source` says how it was checked."""

    crop: str
    picture: bytes
    source: str


@dataclass(frozen=True)
class Survey:
    """The model's look at each plot of one plot set; plots it could not look at are missing, not guessed."""

    looks: dict[int, PlotLook]
    plots: int  # plots asked about
    model: str
    done_at: str
    cost_usd: float

    def look(self, number: int) -> Optional[PlotLook]:
        return self.looks.get(number)


def _model() -> str:
    return os.environ.get("DRONE_VISION_MODEL", VISION_MODEL)


def vision_client() -> tuple[AsyncOpenAI, str]:
    """The vision model's client and its name (DRONE_VISION_MODEL); also used to read farm documents."""
    return _client()


def _client() -> tuple[AsyncOpenAI, str]:
    endpoint = resolve_chat_endpoint(_model(), api_key=os.environ.get("OPENAI_API_KEY"),
                                     base_url=os.environ.get("OPENAI_BASE_URL"),
                                     ollama_base_url=os.environ.get("OLLAMA_BASE_URL"))
    return AsyncOpenAI(base_url=endpoint.base_url, api_key=endpoint.api_key), endpoint.model


def plot_picture(ds: Any, outline: Any) -> tuple[bytes, float, float]:
    """(JPEG of the plot outlined in white with a margin, metres across, cm per pixel); outline in the photo's CRS."""
    minx, miny, maxx, maxy = outline.buffer(CONTEXT_M).bounds
    window = from_bounds(minx, miny, maxx, maxy, ds.transform)
    scale = max(1.0, max(window.width, window.height) / IMAGE_LONG_SIDE)
    width, height = max(1, int(window.width / scale)), max(1, int(window.height / scale))
    bands = ds.read([1, 2, 3], window=window, out_shape=(3, height, width), resampling=Resampling.average,
                    boundless=True, fill_value=0)
    image = Image.fromarray(np.transpose(bands, (1, 2, 0)).astype("uint8"))
    to_pixel = ~ds.window_transform(window)
    polygons = getattr(outline, "geoms", [outline])
    draw = ImageDraw.Draw(image)
    for polygon in polygons:
        points = [((to_pixel * (x, y))[0] / scale, (to_pixel * (x, y))[1] / scale) for x, y in polygon.exterior.coords]
        draw.line(points + [points[0]], fill=(255, 255, 255), width=3)
    out = io.BytesIO()
    image.save(out, format="JPEG", quality=88)
    metres = maxx - minx  # photo CRS units; Ingabe's drone photos are in metres (UTM or Web Mercator)
    return out.getvalue(), metres, 100 * metres / max(width, 1)


def picture_of_plot(cog_url: str, feature: dict[str, Any]) -> bytes:
    """The picture the model is shown for one plot feature (WGS84)."""
    with rasterio.open(cog_url) as ds:
        to_photo = Transformer.from_crs("EPSG:4326", ds.crs, always_xy=True).transform
        picture, _, _ = plot_picture(ds, reproject(to_photo, shape(feature["geometry"])))
    return picture


def closeups(ds: Any, outline: Any, number: int, count: int) -> list[bytes]:
    """`count` squares of CLOSE_M from inside the plot at full detail, each with a 1 m bar; the same plot always
    gets the same squares. A plot too small to hold a square is shown whole."""
    import random

    inner = outline.buffer(-CLOSE_M / 2)
    area = outline if inner.is_empty else inner
    rng = random.Random(number)
    minx, miny, maxx, maxy = area.bounds
    centres: list[Any] = []
    for _ in range(400):
        if len(centres) == count:
            break
        point = Point(rng.uniform(minx, maxx), rng.uniform(miny, maxy))
        if area.contains(point) and all(point.distance(c) >= CLOSE_M for c in centres):
            centres.append(point)
    if not centres:
        centres = [outline.representative_point()]
    pictures = []
    per_m = CLOSE_PX / CLOSE_M
    for centre in centres:
        half = CLOSE_M / 2
        window = from_bounds(centre.x - half, centre.y - half, centre.x + half, centre.y + half, ds.transform)
        bands = ds.read([1, 2, 3], window=window, out_shape=(3, CLOSE_PX, CLOSE_PX), resampling=Resampling.average,
                        boundless=True, fill_value=0)
        image = Image.fromarray(np.transpose(bands, (1, 2, 0)).astype("uint8"))
        draw = ImageDraw.Draw(image)
        draw.rectangle([12, CLOSE_PX - 22, 12 + per_m, CLOSE_PX - 14], fill=(255, 255, 255))
        draw.text((14, CLOSE_PX - 38), "1 m", fill=(255, 255, 255))
        out = io.BytesIO()
        image.save(out, format="JPEG", quality=88)
        pictures.append(out.getvalue())
    return pictures


def closeup_of_plot(cog_url: str, feature: dict[str, Any]) -> bytes:
    """One square from inside a plot feature (WGS84) at full detail, as the looks are shown."""
    with rasterio.open(cog_url) as ds:
        to_photo = Transformer.from_crs("EPSG:4326", ds.crs, always_xy=True).transform
        return closeups(ds, reproject(to_photo, shape(feature["geometry"])), feature["properties"]["number"], 1)[0]


def _image(picture: bytes) -> dict[str, Any]:
    return {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(picture).decode()}}


async def _ask(client: AsyncOpenAI, model: str, system: str, content: list[dict[str, Any]],
               schema: dict[str, Any], name: str) -> tuple[dict[str, Any], float]:
    """The model's answers, and what the call cost in USD. The fixed instructions come first, as text, so the
    provider can serve them from its prompt cache; the pictures come last."""
    async def look() -> dict[str, Any]:
        response = await client.chat.completions.create(
            model=model, reasoning_effort=EFFORT,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": content}],
            response_format={"type": "json_schema", "json_schema": {"name": name, "strict": True, "schema": schema}},
            extra_body={"usage": {"include": True}},
        )
        cost = llm_cache.record(f"vision_{name}", response.usage)
        return {"answer": json.loads(response.choices[0].message.content or ""), "cost": cost}

    # The same pictures with the same instructions (a survey run again, plots found again) cost nothing.
    kept, reused = await llm_cache.answer("vision", llm_cache.key_of(model, EFFORT, system, content, schema), look)
    return kept["answer"], 0.0 if reused else kept["cost"]


def _look_content(overview: bytes, metres: float, cm: float, squares: list[bytes], place: Optional[str]) -> list[dict[str, Any]]:
    where = f" in {place}" if place else ""
    text = (f"A plot{where}. Picture 1: the whole plot outlined in white, {metres:.0f} m across. Pictures 2-"
            f"{1 + len(squares)}: {CLOSE_M:.0f} m squares from inside it, from a photo with {cm:.1f} cm per pixel; "
            "judge the crop from these.")
    return [{"type": "text", "text": text}, _image(overview)] + [_image(p) for p in squares]


def _check_content(references: list[Reference], overview: bytes, metres: float, squares: list[bytes],
                   place: Optional[str]) -> list[dict[str, Any]]:
    content: list[dict[str, Any]] = []
    for i, ref in enumerate(references, 1):
        content += [{"type": "text", "text": f"Reference {i}: {CROP_LABELS[ref.crop].lower()} ({ref.source})."},
                    _image(ref.picture)]
    where = f" in {place}" if place else ""
    content += [{"type": "text", "text": f"Now the plot to check{where}: the whole plot outlined in white, "
                                         f"{metres:.0f} m across, then {len(squares)} {CLOSE_M:.0f} m squares from inside it."},
                _image(overview)] + [_image(p) for p in squares]
    return content


def _one_of(value: Any, allowed: list[str], fallback: str) -> str:
    return value if value in allowed else fallback


def _look(number: int, answer: dict[str, Any], check: Optional[dict[str, Any]] = None) -> PlotLook:
    """A plot look from the model's answers, with values outside the allowed lists dropped. With a second
    look (`check`), the crop stands only if both named the same one; otherwise both are kept as candidates."""
    first = _one_of(answer.get("main_crop"), CROPS, "unsure")
    crop, candidates, confidence = first, (), _one_of(answer.get("confidence"), ["low", "medium", "high"], "low")
    if check is not None:
        second = _one_of(check.get("main_crop"), CROPS, "unsure")
        if second != first:
            crop, confidence = "unsure", "low"
            candidates = tuple(sorted({c for c in (first, second) if c != "unsure"}))
    return PlotLook(
        number=number,
        main_crop=crop,
        other_crops=[c for c in answer.get("other_crops") or [] if c in CROPS and c != crop],
        stage=_one_of(answer.get("stage"), STAGES, "unsure"),
        crop_cover_percent=int(min(100, max(0, int(answer.get("crop_cover_percent") or 0)))),
        weeds=_one_of(answer.get("weeds"), WEEDS, "unsure"),
        problems=[p for p in answer.get("problems") or [] if p in PROBLEMS],
        note=str(answer.get("note") or "")[:300],
        confidence=confidence,
        candidates=candidates,
        evidence=str(answer.get("evidence") or "")[:600],
    )


async def survey(cog_url: str, plots: drone_plots.PlotSet, place: Optional[str],
                 progress: background_jobs.Progress, references: Optional[list[Reference]] = None) -> Survey:
    """Look at every measured plot twice (plots off the photo are skipped): once with the crops' sizes spelt
    out, once against reference squares of checked crops, each look on its own squares from inside the plot."""
    client, model = _client()
    refs = references or []
    asked = [f for f in plots.geojson["features"] if f["properties"].get("greenness") is not None]
    looks: dict[int, PlotLook] = {}
    failed = 0
    cost = 0.0
    done = 0
    gate = asyncio.Semaphore(CONCURRENT_LOOKS)
    with rasterio.open(cog_url) as ds:
        to_photo = Transformer.from_crs("EPSG:4326", ds.crs, always_xy=True).transform
        read_lock = asyncio.Lock()  # one rasterio dataset: read one plot's pictures at a time

        def pictures(outline: Any, number: int) -> tuple[bytes, float, float, list[bytes]]:
            overview, metres, cm = plot_picture(ds, outline)
            return overview, metres, cm, closeups(ds, outline, number, 2 * CLOSEUPS_PER_LOOK)

        async def one(feature: dict[str, Any]) -> None:
            nonlocal failed, cost, done
            number = feature["properties"]["number"]
            async with gate:
                try:
                    outline = reproject(to_photo, shape(feature["geometry"]))
                    async with read_lock:
                        overview, metres, cm, squares = await asyncio.to_thread(pictures, outline, number)
                    cm_photo = 100 * abs(ds.transform.a)
                    first, second = squares[:CLOSEUPS_PER_LOOK], squares[CLOSEUPS_PER_LOOK:] or squares[:CLOSEUPS_PER_LOOK]
                    (answer, spent_a), (check, spent_b) = await asyncio.gather(
                        _ask(client, model, _LOOK_SYSTEM, _look_content(overview, metres, cm_photo, first, place),
                             _LOOK_SCHEMA, "plot_look"),
                        _ask(client, model, _CHECK_SYSTEM if refs else _LOOK_SYSTEM,
                             _check_content(refs, overview, metres, second, place) if refs
                             else _look_content(overview, metres, cm_photo, second, place),
                             _CHECK_SCHEMA, "plot_check"))
                    looks[number] = _look(number, answer, check)
                    cost += spent_a + spent_b
                except Exception:  # one plot's failure leaves that plot unknown; many failures fail the survey
                    logger.exception("vision look failed for plot %s", number)
                    failed += 1
                done += 1
                progress(done, len(asked))

        progress(0, len(asked))
        await asyncio.gather(*(one(f) for f in asked))
    if asked and failed > MAX_FAILED_SHARE * len(asked):
        raise RuntimeError(f"the vision model could not look at {failed} of {len(asked)} plots")
    return Survey(looks=looks, plots=len(asked), model=model,
                  done_at=datetime.now(timezone.utc).isoformat(timespec="seconds"), cost_usd=round(cost, 4))


# --- Reference squares of checked crops ------------------------------------------------------

async def load_references(s3: Any, bucket: str) -> list[Reference]:
    """The reference squares kept for the second look (at most MAX_REFS_PER_CROP a crop), newest first."""
    try:
        response = await s3.get_object(Bucket=bucket, Key=f"{REFS_PREFIX}/index.json")
    except s3.exceptions.NoSuchKey:
        return []
    async with response["Body"] as body:
        index = json.loads(await body.read())
    refs: list[Reference] = []
    for entry in index:
        if entry.get("crop") not in CROPS or sum(1 for r in refs if r.crop == entry["crop"]) >= MAX_REFS_PER_CROP:
            continue
        picture = await s3.get_object(Bucket=bucket, Key=entry["key"])
        async with picture["Body"] as body:
            refs.append(Reference(crop=entry["crop"], picture=await body.read(), source=entry["source"]))
    return refs


async def add_reference(s3: Any, bucket: str, crop: str, picture: bytes, source: str) -> None:
    """Keep a checked square as a reference (newest first); older ones of the same crop stay in the index."""
    if crop not in CROPS or crop in ("unsure", "other"):
        return
    key = f"{REFS_PREFIX}/{crop}-{hashlib.sha256(picture).hexdigest()[:16]}.jpg"
    await s3.put_object(Bucket=bucket, Key=key, Body=picture, ContentType="image/jpeg")
    try:
        response = await s3.get_object(Bucket=bucket, Key=f"{REFS_PREFIX}/index.json")
        async with response["Body"] as body:
            index = json.loads(await body.read())
    except s3.exceptions.NoSuchKey:
        index = []
    index = [{"crop": crop, "key": key, "source": source,
              "added_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}] + [e for e in index if e["key"] != key]
    await s3.put_object(Bucket=bucket, Key=f"{REFS_PREFIX}/index.json", Body=json.dumps(index).encode(),
                        ContentType="application/json")


# --- Kept surveys and running jobs ---------------------------------------------------------

_surveys: dict[str, Survey] = {}


def survey_key(photo_key: str, plots: drone_plots.PlotSet) -> str:
    """One survey per photo, plot set, model and way of looking (the store prefix's version)."""
    return f"{_STORE_PREFIX}|{photo_key}|{plots.source}|{plots.found_at}|{_model()}"


def _store_key(key: str) -> str:
    return f"{_STORE_PREFIX}/{hashlib.sha256(key.encode()).hexdigest()[:32]}.json"


def _to_json(result: Survey) -> bytes:
    return json.dumps({"looks": [asdict(look) for look in result.looks.values()], "plots": result.plots,
                       "model": result.model, "done_at": result.done_at, "cost_usd": result.cost_usd}).encode()


def _from_json(raw: bytes) -> Survey:
    data = json.loads(raw)
    looks = {item["number"]: PlotLook(**{**item, "candidates": tuple(item.get("candidates") or ())}) for item in data["looks"]}
    return Survey(looks=looks, plots=data["plots"], model=data["model"], done_at=data["done_at"],
                  cost_usd=data["cost_usd"])


async def load_survey(s3: Any, bucket: str, key: str) -> Optional[Survey]:
    if key in _surveys:
        return _surveys[key]
    try:
        response = await s3.get_object(Bucket=bucket, Key=_store_key(key))
    except s3.exceptions.NoSuchKey:
        return None
    async with response["Body"] as body:
        result = _from_json(await body.read())
    _surveys[key] = result
    return result


def job(key: str) -> Optional[background_jobs.Job]:
    return background_jobs.status(f"survey:{key}")


def start_survey(s3: Any, bucket: str, key: str, cog_url: str, plots: drone_plots.PlotSet,
                 place: Optional[str]) -> background_jobs.Job:
    """Look at every plot in the background, once; the running or failed job is returned."""

    async def work(progress: background_jobs.Progress) -> None:
        result = await survey(cog_url, plots, place, progress, await load_references(s3, bucket))
        await s3.put_object(Bucket=bucket, Key=_store_key(key), Body=_to_json(result), ContentType="application/json")
        _surveys[key] = result
        logger.info("vision survey of %d plots for %s: %d looks, $%.4f", result.plots, key, len(result.looks),
                    result.cost_usd)

    return background_jobs.start(f"survey:{key}", work)
