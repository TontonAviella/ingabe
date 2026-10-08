"""What a vision model sees in each plot of a drone photo: crop, stage, ground cover, weeds and visible problems.

Ingabe shows the model one picture per plot (the plot outlined in white, a few metres around it) and asks
for a fixed set of answers. One look per plot feeds several cards (what is growing, which plots are behind,
which need weeding, what problems show from the air), so the cards agree with each other. Areas, greenness
and bare ground stay Ingabe's own measurements (drone_plots.py); the model only names what it sees.

Model: GPT-6 Luna through OpenRouter (Roger's choice, 2026-10-06), at low reasoning effort. On 12
Cyampirita plots it answered in 5-11 s each for about $0.0002 a plot. Not yet checked against what is
really planted: it called two plots that look alike "cassava" and "pineapple". Every card says so.
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
from shapely.geometry import shape
from shapely.ops import transform as reproject

from src.llm_defaults import resolve_chat_endpoint
from src.services import background_jobs, drone_plots

logger = logging.getLogger(__name__)

VISION_MODEL = "openai/gpt-6-luna"  # override with DRONE_VISION_MODEL
IMAGE_LONG_SIDE = 768  # pixels of the picture shown per plot
CONTEXT_M = 4.0  # metres of surroundings shown around the plot
CONCURRENT_LOOKS = 8
MAX_FAILED_SHARE = 0.3  # more failed looks than this and the survey counts as failed
_STORE_PREFIX = "drone_vision/v1"

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

_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "main_crop": {"type": "string", "enum": CROPS},
        "other_crops": {"type": "array", "items": {"type": "string", "enum": CROPS}},
        "stage": {"type": "string", "enum": STAGES},
        "crop_cover_percent": {"type": "integer"},
        "weeds": {"type": "string", "enum": WEEDS},
        "problems": {"type": "array", "items": {"type": "string", "enum": PROBLEMS}},
        "note": {"type": "string"},
        "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
    },
    "required": ["main_crop", "other_crops", "stage", "crop_cover_percent", "weeds", "problems", "note", "confidence"],
}

_PROMPT = (
    "Top-down drone photo of one farm plot in Rwanda, outlined in white, about {width:.0f} m across, "
    "{cm:.0f} cm per pixel.{place} Look only inside the white outline. Say what is growing (crops common in "
    "Rwanda: maize, beans, cassava, banana, sorghum, rice, potatoes, soybean, groundnut, pineapple, coffee, tea, "
    "sugarcane, vegetables), its stage, how much of the ground the crop covers, weeds, and any problem you can "
    "see from above. If you cannot tell, say unsure and low confidence; do not guess a crop you cannot see. "
    "note: one short plain sentence a farmer understands."
)


@dataclass(frozen=True)
class PlotLook:
    number: int
    main_crop: str
    other_crops: list[str]
    stage: str
    crop_cover_percent: int
    weeds: str
    problems: list[str]
    note: str
    confidence: str


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


async def _ask(client: AsyncOpenAI, model: str, picture: bytes, width_m: float, cm: float,
               place: Optional[str]) -> tuple[dict[str, Any], float]:
    """The model's answers for one plot picture, and what the call cost in USD."""
    prompt = _PROMPT.format(width=width_m, cm=cm, place=f" Place: {place}." if place else "")
    response = await client.chat.completions.create(
        model=model, reasoning_effort="low",
        messages=[{"role": "user", "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(picture).decode()}},
        ]}],
        response_format={"type": "json_schema", "json_schema": {"name": "plot_look", "strict": True, "schema": _SCHEMA}},
        extra_body={"usage": {"include": True}},
    )
    content = response.choices[0].message.content or ""
    usage = response.usage
    cost = float(getattr(usage, "cost", None) or ((usage.model_extra or {}).get("cost") if usage else 0) or 0)
    return json.loads(content), cost


def _look(number: int, answer: dict[str, Any]) -> PlotLook:
    """A plot look from the model's answer, with values outside the allowed lists dropped."""
    def one_of(value: Any, allowed: list[str], fallback: str) -> str:
        return value if value in allowed else fallback
    return PlotLook(
        number=number,
        main_crop=one_of(answer.get("main_crop"), CROPS, "unsure"),
        other_crops=[c for c in answer.get("other_crops") or [] if c in CROPS and c != answer.get("main_crop")],
        stage=one_of(answer.get("stage"), STAGES, "unsure"),
        crop_cover_percent=int(min(100, max(0, int(answer.get("crop_cover_percent") or 0)))),
        weeds=one_of(answer.get("weeds"), WEEDS, "unsure"),
        problems=[p for p in answer.get("problems") or [] if p in PROBLEMS],
        note=str(answer.get("note") or "")[:300],
        confidence=one_of(answer.get("confidence"), ["low", "medium", "high"], "low"),
    )


async def survey(cog_url: str, plots: drone_plots.PlotSet, place: Optional[str],
                 progress: background_jobs.Progress) -> Survey:
    """Ask the vision model about every measured plot (plots off the photo are skipped)."""
    client, model = _client()
    asked = [f for f in plots.geojson["features"] if f["properties"].get("greenness") is not None]
    looks: dict[int, PlotLook] = {}
    failed = 0
    cost = 0.0
    done = 0
    gate = asyncio.Semaphore(CONCURRENT_LOOKS)
    with rasterio.open(cog_url) as ds:
        to_photo = Transformer.from_crs("EPSG:4326", ds.crs, always_xy=True).transform
        read_lock = asyncio.Lock()  # one rasterio dataset: read one picture at a time

        async def one(feature: dict[str, Any]) -> None:
            nonlocal failed, cost, done
            number = feature["properties"]["number"]
            async with gate:
                try:
                    outline = reproject(to_photo, shape(feature["geometry"]))
                    async with read_lock:
                        picture, width_m, cm = await asyncio.to_thread(plot_picture, ds, outline)
                    answer, spent = await _ask(client, model, picture, width_m, cm, place)
                    looks[number] = _look(number, answer)
                    cost += spent
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


# --- Kept surveys and running jobs ---------------------------------------------------------

_surveys: dict[str, Survey] = {}


def survey_key(photo_key: str, plots: drone_plots.PlotSet) -> str:
    """One survey per photo, plot set and model."""
    return f"{photo_key}|{plots.source}|{plots.found_at}|{_model()}"


def _store_key(key: str) -> str:
    return f"{_STORE_PREFIX}/{hashlib.sha256(key.encode()).hexdigest()[:32]}.json"


def _to_json(result: Survey) -> bytes:
    return json.dumps({"looks": [asdict(look) for look in result.looks.values()], "plots": result.plots,
                       "model": result.model, "done_at": result.done_at, "cost_usd": result.cost_usd}).encode()


def _from_json(raw: bytes) -> Survey:
    data = json.loads(raw)
    looks = {item["number"]: PlotLook(**item) for item in data["looks"]}
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
        result = await survey(cog_url, plots, place, progress)
        await s3.put_object(Bucket=bucket, Key=_store_key(key), Body=_to_json(result), ContentType="application/json")
        _surveys[key] = result
        logger.info("vision survey of %d plots for %s: %d looks, $%.4f", result.plots, key, len(result.looks),
                    result.cost_usd)

    return background_jobs.start(f"survey:{key}", work)
