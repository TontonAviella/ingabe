"""A close look: a vision model (CLOSE_LOOK_MODEL) asked narrow questions about an inspection photo or a drone
orthomosaic, for what Ingabe's own models cannot say yet. It flags things for a person to check and says what it
cannot see; it never counts and never confirms, and nothing it says changes a count.

On a tower photo: the owner's eight worries from the whole photo (each "can see: yes / partly / no", what it shows,
what would answer it), then up to MAX_REGIONS close-ups it asks for at the photo's full resolution (rust, bolts,
cables, connectors, nests, the light), then the boxes the trained models suggested, MAX_CHECKS at most, shown zoomed
and numbered: what each one really is, and any damage seen. On an orthomosaic: the whole field once, then SQUARES
squares of SQUARE_M metres spread over it at full detail (crops, stage, bare soil, weeds, stress, anything odd).

Tried 2026-10-14 on 3 Louisville tower photos and 20 squares of Cyampirita and Kabarama (Opus 5.5, $1.82), judged by
eye, not measured: it said plainly what was out of frame, flagged slack cables, open pipe ends and possible nest
material, and caught boxes of ours that were wrong (an antenna top called a dish, a fibre closure called a radio
unit); its plant counts were too wide to use, so it is not asked to count.

Cost: each picture and question is asked once (llm_cache keeps the answer by content), and a day's spending on this
server stops at CLOSE_LOOK_DAILY_USD (default 5). Switch: CLOSE_LOOK (default on); the model's key is the chat key
(OPENAI_API_KEY at OPENAI_BASE_URL)."""

from __future__ import annotations

import asyncio
import base64
import datetime as dt
import hashlib
import io
import json
import logging
import os
import random
from dataclasses import dataclass
from typing import Any, Optional, Sequence

import numpy as np
from openai import AsyncOpenAI
from PIL import Image, ImageDraw, ImageFont
from redis.exceptions import LockError

from src.llm_defaults import resolve_chat_endpoint, thinking_kwargs
from src.services import llm_cache, stored_files

logger = logging.getLogger(__name__)

METHOD = "close-look/v1"  # changes when the questions do: answers of another method are not reused
MODEL = "claude-opus-5-5"  # override with CLOSE_LOOK_MODEL
EFFORT = "medium"
SEND_PX = 1568  # long side of every picture sent
MAX_REGIONS = 4
MAX_CHECKS = 18  # suggestions checked per photo, 6 to a sheet
PER_SHEET = 6
SQUARES = 10
THUMB_PX = 480  # the small photo kept beside a look, for the map
SQUARE_M = 40.0
STORE_PREFIX = "close_look/v1"
DAILY_USD = 5.0
LOCK_TTL_S = 600

WORRIES = ("structure", "theft_tampering", "alignment", "equipment", "damage", "hot_spots", "obstruction_light",
           "surroundings")
_WORRY_WORDS = """structure: rust, missing or loose bolts, bent or broken steel, cracked welds, foundation cracks, guy wires
theft_tampering: fence, gate, shelter, batteries, cables, generator: anything cut, open, missing or tampered with
alignment: which compass direction each antenna sector faces, and its downward tilt
equipment: the panel antennas, microwave dishes and radio units in view
damage: broken antenna covers, damaged or loose cables, connectors missing weatherproofing, bird nests
hot_spots: overheating equipment
obstruction_light: whether the aviation obstruction light at the top works
surroundings: vegetation, erosion, new buildings, the access road"""
HONEST = ("Answer only from what you can see. If something cannot be seen or judged in this picture, say so plainly "
          "(can_see 'no' or 'partly') and say what picture would show it; never guess to fill a field, and never count "
          "what you are not sure of.")


class OverBudget(RuntimeError):
    """Today's spending on close looks reached CLOSE_LOOK_DAILY_USD."""


class Unavailable(RuntimeError):
    """Close looks are switched off, or have no key here."""


def enabled() -> bool:
    return os.environ.get("CLOSE_LOOK", "1").strip() != "0" and bool(os.environ.get("OPENAI_API_KEY", "").strip())


def daily_usd() -> float:
    try:
        return max(0.0, float(os.environ.get("CLOSE_LOOK_DAILY_USD") or DAILY_USD))
    except ValueError:
        return DAILY_USD


def _model() -> str:
    return os.environ.get("CLOSE_LOOK_MODEL", MODEL)


def _client() -> tuple[AsyncOpenAI, str]:
    endpoint = resolve_chat_endpoint(_model(), api_key=os.environ.get("OPENAI_API_KEY"),
                                     base_url=os.environ.get("OPENAI_BASE_URL"),
                                     ollama_base_url=os.environ.get("OLLAMA_BASE_URL"))
    return AsyncOpenAI(base_url=endpoint.base_url, api_key=endpoint.api_key), endpoint.model


def jpeg(img: Image.Image, max_side: int = SEND_PX, quality: int = 88) -> bytes:
    img = img.convert("RGB")
    scale = max_side / max(img.size)
    if scale < 1:
        img = img.resize((max(1, round(img.width * scale)), max(1, round(img.height * scale))), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=quality)
    return buf.getvalue()


def _budget_key(day: Optional[str] = None) -> str:
    return f"close_look:usd:{day or dt.date.today().isoformat()}"


async def spent_today(redis: Any) -> float:
    return float(await redis.get(_budget_key()) or 0.0)


async def _ask(redis: Any, name: str, prompt: str, pictures: Sequence[bytes], schema: dict[str, Any]) -> dict[str, Any]:
    """One question, answered once per pictures and words (llm_cache); refused when today's budget is spent."""
    if not enabled():
        raise Unavailable("Close looks are switched off on this server")
    client, model = _client()
    content: list[dict[str, Any]] = [{"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,"
                                                                          + base64.b64encode(p).decode()}}
                                     for p in pictures]
    content.append({"type": "text", "text": prompt})

    async def look() -> dict[str, Any]:
        if await spent_today(redis) >= daily_usd():
            raise OverBudget(f"Today's close looks reached ${daily_usd():.2f}; they continue tomorrow")
        response = await client.chat.completions.create(
            model=model, **thinking_kwargs(client.base_url, EFFORT), messages=[{"role": "user", "content": content}],
            response_format={"type": "json_schema", "json_schema": {"name": name, "strict": True, "schema": schema}})
        cost = llm_cache.record(f"close_look_{name}", response.usage, model)
        await redis.incrbyfloat(_budget_key(), cost)
        await redis.expire(_budget_key(), 3 * 86400)
        return {"answer": json.loads(response.choices[0].message.content or "{}"), "cost": cost}

    kept, _ = await llm_cache.answer("close_look", llm_cache.key_of(model, METHOD, EFFORT, prompt, *pictures, schema), look)
    return kept["answer"]


def _obj(properties: dict[str, Any]) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}


_S = {"type": "string"}
_CAN = {"type": "string", "enum": ["yes", "partly", "no"]}
_CONF = {"type": "string", "enum": ["high", "medium", "low"]}
_BOX = {"type": "array", "items": {"type": "number"}}
WHOLE_SCHEMA = _obj({
    "worries": {"type": "array", "items": _obj({"id": {"type": "string", "enum": list(WORRIES)}, "can_see": _CAN,
                                                "answer": _S, "confidence": _CONF, "needs": _S})},
    "regions": {"type": "array", "items": _obj({"name": _S, "box": _BOX, "why": _S})},
})
REGION_SCHEMA = _obj({"what_is_shown": _S,
                      "findings": {"type": "array", "items": _obj({"kind": _S, "where": _S, "can_see": _CAN,
                                                                   "confidence": _CONF, "flag": {"type": "boolean"}})},
                      "cannot_judge": {"type": "array", "items": _S}})
CHECK_SCHEMA = _obj({"items": {"type": "array", "items": _obj({
    "n": {"type": "integer"}, "really": _S, "agrees": {"type": "string", "enum": ["yes", "no", "unsure"]},
    "damage": _S, "confidence": _CONF})}})
FIELD_SCHEMA = _obj({
    "land_uses": {"type": "array", "items": _obj({"what": _S, "where": _S})},
    "roads_buildings_water": _S, "worth_a_look": {"type": "array", "items": _S},
    "cannot_tell": {"type": "array", "items": _S}})
SQUARE_SCHEMA = _obj({
    "crops": {"type": "array", "items": _obj({"crop": _S, "where": _S, "stage": _S, "confidence": _CONF})},
    "bare_or_gaps": _S, "weeds": _S, "stress_or_disease": _S, "odd": _S, "cannot_tell": {"type": "array", "items": _S}})


# --------------------------------------------------------------------------- tower photos

@dataclass(frozen=True)
class Shown:
    """A box the trained models suggested on the photo and the person is shown."""

    id: str
    part: str
    box: tuple[float, float, float, float]


def _numbered_sheet(photo: Image.Image, boxes: Sequence[Shown], first: int) -> bytes:
    tiles = []
    for j, b in enumerate(boxes):
        x0, y0, x1, y1 = b.box
        pad = 0.35 * max(x1 - x0, y1 - y0)
        crop = photo.crop((int(max(0, x0 - pad)), int(max(0, y0 - pad)), int(min(photo.width, x1 + pad)),
                           int(min(photo.height, y1 + pad))))
        crop.thumbnail((520, 520))
        tile = Image.new("RGB", (540, 560), (20, 16, 14))
        tile.paste(crop, (10, 40))
        ImageDraw.Draw(tile).text((10, 8), f"#{first + j}", fill=(255, 220, 120), font=ImageFont.load_default(size=28))
        tiles.append(tile)
    sheet = Image.new("RGB", (540 * 3, 560 * ((len(tiles) + 2) // 3)), (20, 16, 14))
    for j, tile in enumerate(tiles):
        sheet.paste(tile, ((j % 3) * 540, (j // 3) * 560))
    return jpeg(sheet, max_side=1620)


async def look_at_photo(redis: Any, photo: Image.Image, facts: str, shown: Sequence[Shown],
                        nouns: dict[str, str]) -> dict[str, Any]:
    """The close look at one tower photo: the worries, the close-ups and the checked boxes, all in the photo's own
    pixels."""
    whole_bytes = jpeg(photo)
    sent = Image.open(io.BytesIO(whole_bytes)).size
    k = photo.width / sent[0]
    whole = await _ask(redis, "tower_whole", f"""A drone photo of a telecom tower ({facts}; the photo is {photo.width}x{photo.height} px, sent at {sent[0]}x{sent[1]}).
A tower owner worries about these 8 things:
{_WORRY_WORDS}
For each worry say what THIS photo shows. Then name up to {MAX_REGIONS} regions worth a close look at full resolution (the
top of the tower where the light sits, the base and ground, a steel joint, a cable run, a mount), as boxes
[x0, y0, x1, y1] in the SENT picture's pixels. {HONEST}""", [whole_bytes], WHOLE_SCHEMA)
    regions = []
    for i, reg in enumerate(whole["regions"][:MAX_REGIONS]):
        if len(reg["box"]) != 4:
            continue
        x0, y0, x1, y1 = (max(0.0, v * k) for v in reg["box"])
        x1, y1 = min(float(photo.width), x1), min(float(photo.height), y1)
        if x1 - x0 < 16 or y1 - y0 < 16:
            continue
        pad = 0.1 * max(x1 - x0, y1 - y0)
        box = (max(0.0, x0 - pad), max(0.0, y0 - pad), min(float(photo.width), x1 + pad), min(float(photo.height), y1 + pad))
        answer = await _ask(redis, "tower_region", f"""A full-resolution close-up cut from a drone photo of a telecom tower ({facts}).
It was chosen as "{reg['name']}" because "{reg['why']}". Look closely for rust, missing or loose bolts, bent or broken
steel, cracked welds, broken antenna covers, damaged or loose cables, connectors without weatherproofing, bird nests,
the aviation light (is it there; can you tell if it works), the fence, gate and shelter, vegetation, erosion and the
access road. Set flag true only on a finding a person should go and check. {HONEST}""",
                            [jpeg(photo.crop(tuple(int(v) for v in box)))], REGION_SCHEMA)
        regions.append({"n": i + 1, "name": reg["name"], "why": reg["why"], "box": [round(v) for v in box], **answer})
    checks = []
    chosen = list(shown)[:MAX_CHECKS]
    for g in range(0, len(chosen), PER_SHEET):
        group = chosen[g:g + PER_SHEET]
        said = {f"#{g + j + 1}": nouns.get(b.part, b.part) for j, b in enumerate(group)}
        answer = await _ask(redis, "tower_check", f"""Numbered close-ups cut from a drone photo of a telecom tower ({facts}). A trained detector
thinks each one is the part listed: {json.dumps(said)}. For each, say what it really is (panel antenna, radio unit,
microwave dish, several of them, or something else), whether that agrees with the detector, and any damage seen
(broken cover, loose or damaged cable, connector without weatherproofing, rust, nest). {HONEST}""",
                            [_numbered_sheet(photo, group, g + 1)], CHECK_SCHEMA)
        for item in answer["items"]:
            idx = item["n"] - 1
            if 0 <= idx - g < len(group):
                b = group[idx - g]
                checks.append({"suggestion_id": b.id, "part": b.part, **{k2: item[k2] for k2 in ("really", "agrees", "damage", "confidence")}})
    return {"method": METHOD, "worries": whole["worries"], "regions": regions, "checks": checks}


def photo_key(project_id: str, layer_id: str, sha256: str) -> str:
    return f"{STORE_PREFIX}/photo/{project_id}/{layer_id}/{sha256}.json"


def thumb_key(project_id: str, layer_id: str, sha256: str) -> str:
    return f"{STORE_PREFIX}/photo/{project_id}/{layer_id}/{sha256}.jpg"


async def load_photo(s3: Any, bucket: str, project_id: str, layer_id: str, sha256: str) -> Optional[dict[str, Any]]:
    data = await stored_files.load_json(s3, bucket, photo_key(project_id, layer_id, sha256))
    return data if data is not None and data.get("method") == METHOD else None


def flags(result: dict[str, Any]) -> list[str]:
    """What the close look asks a person to check on a photo: flagged close-up findings and boxes it disagrees with."""
    out = [f["kind"] for r in result.get("regions", []) for f in r.get("findings", []) if f.get("flag")]
    out += [f"#{i + 1}: {c['really']}" for i, c in enumerate(result.get("checks", [])) if c.get("agrees") == "no"]
    return out


# --------------------------------------------------------------------------- orthomosaics

def squares_in(width: int, height: int, size: int, valid: np.ndarray, seed: str, count: int = SQUARES) -> list[tuple[int, int]]:
    """Up to `count` squares of `size` px over the photo's valid pixels (`valid`: a small mask of the whole photo),
    apart from each other, the same for the same photo (seeded by it)."""
    if size <= 0 or size > width or size > height:
        return []
    rnd = random.Random(int(hashlib.sha256(seed.encode()).hexdigest()[:8], 16))
    sy, sx = valid.shape[0] / height, valid.shape[1] / width
    picked: list[tuple[int, int]] = []
    for _ in range(5000):
        if len(picked) >= count:
            break
        x, y = rnd.randrange(0, width - size + 1), rnd.randrange(0, height - size + 1)
        cell = valid[int(y * sy):int((y + size) * sy) + 1, int(x * sx):int((x + size) * sx) + 1]
        if cell.size == 0 or cell.mean() < 0.97:
            continue
        if any(abs(x - px) < size * 1.5 and abs(y - py) < size * 1.5 for px, py in picked):
            continue
        picked.append((x, y))
    return picked


def raster_key(cog_key: str) -> str:
    return f"{STORE_PREFIX}/raster/{hashlib.sha256(cog_key.encode()).hexdigest()[:32]}"


async def load_raster(s3: Any, bucket: str, cog_key: str) -> Optional[dict[str, Any]]:
    data = await stored_files.load_json(s3, bucket, f"{raster_key(cog_key)}/look.json")
    return data if data is not None and data.get("method") == METHOD else None


def _read_raster(cog_url: str, place: str) -> tuple[bytes, float, float, list[dict[str, Any]], list[bytes]]:
    """Blocking: (overview JPEG, width m, height m, the squares' places, their JPEGs) of an orthomosaic."""
    import rasterio  # lazy: heavy GDAL binding, loaded only when an orthomosaic is looked at
    from rasterio.enums import Resampling
    from rasterio.warp import transform as warp
    from rasterio.windows import Window

    with rasterio.open(cog_url) as ds:
        if ds.count < 3:
            raise Unavailable("A close look is for colour (RGB) photos")
        metres_per_unit = 111_320.0 if ds.crs and ds.crs.is_geographic else 1.0
        res = abs(ds.transform.a) * metres_per_unit
        ov_w = SEND_PX
        ov_h = max(1, round(ds.height * ov_w / ds.width))
        ov = ds.read(out_shape=(ds.count, ov_h, ov_w), resampling=Resampling.average)
        rgb = np.moveaxis(ov[:3], 0, -1).astype(np.uint8)
        valid = (ov[3] > 0) if ds.count >= 4 else (rgb.sum(-1) > 0)
        size = int(round(SQUARE_M / res))
        places, pictures = [], []
        for x, y in squares_in(ds.width, ds.height, size, valid, cog_url.split("?")[0] + place):
            a = ds.read(window=Window(x, y, size, size))
            tile = Image.fromarray(np.moveaxis(a[:3], 0, -1).astype(np.uint8))
            bar = int(round(5 / res))
            draw = ImageDraw.Draw(tile)
            draw.rectangle([20, tile.height - 40, 20 + bar, tile.height - 30], fill=(255, 255, 255))
            draw.text((20, tile.height - 70), "5 m", fill=(255, 255, 255), font=ImageFont.load_default(size=26))
            cx, cy = ds.xy(y + size // 2, x + size // 2)
            lons, lats = warp(ds.crs, "EPSG:4326", [cx], [cy]) if ds.crs else ([cx], [cy])
            places.append({"lon": round(lons[0], 7), "lat": round(lats[0], 7), "cm_per_px": round(res * 100, 2)})
            pictures.append(jpeg(tile, quality=86))
        return jpeg(Image.fromarray(rgb)), ds.width * res, ds.height * res, places, pictures


async def look_at_raster(redis: Any, s3: Any, bucket: str, cog_key: str, cog_url: str, place: str) -> dict[str, Any]:
    """The close look at an orthomosaic: the whole field, then each square; the squares' pictures are kept beside it."""
    overview, w_m, h_m, places, pictures = await asyncio.to_thread(_read_raster, cog_url, place)
    field = await _ask(redis, "field_overview", f"""A drone orthomosaic (looking straight down) of farmland at {place}, Rwanda; the
whole area is about {w_m:.0f} m x {h_m:.0f} m, shrunk to fit. Describe the main land uses (crops, fallow, forest,
settlement) and where, the roads and paths, buildings and water, and anything a farm manager or insurer should look
at. Where a crop cannot be identified at this scale, say so. {HONEST}""", [overview], FIELD_SCHEMA)
    squares = []
    for n, (where, picture) in enumerate(zip(places, pictures), 1):
        answer = await _ask(redis, "field_square", f"""A {SQUARE_M:.0f} m x {SQUARE_M:.0f} m square of a drone orthomosaic (straight down, about
{where['cm_per_px']} cm per pixel; the white bar is 5 m) of farmland at {place}, Rwanda. Say which crops you see and
where in the square, their growth stage, gaps or bare soil, weeds, signs of stress or disease, and anything odd.
Crops in Rwanda often grow mixed (maize, beans, cassava, banana, sweet potato, sorghum); if you cannot tell a crop
apart, say so. Do not count plants. {HONEST}""", [picture], SQUARE_SCHEMA)
        await s3.put_object(Bucket=bucket, Key=f"{raster_key(cog_key)}/square{n}.jpg", Body=picture, ContentType="image/jpeg")
        squares.append({"n": n, **where, **answer})
    return {"method": METHOD, "place": place, "field": field, "squares": squares}


# --------------------------------------------------------------------------- running once, in the background

_tasks: set[asyncio.Task] = set()


async def run_once(redis: Any, lock_name: str, work: Any) -> bool:
    """Run `work()` in the background unless a run with this name is going on (across workers). True while one runs."""
    lock = redis.lock(f"close_look:{lock_name}", timeout=LOCK_TTL_S, thread_local=False)
    if not await lock.acquire(blocking=False):
        return True

    async def run() -> None:
        try:
            await work()
        except (OverBudget, Unavailable) as e:
            logger.info("close look %s left: %s", lock_name, e)
        except Exception:
            logger.exception("close look %s stopped", lock_name)
        finally:
            try:
                await lock.release()
            except LockError:
                pass

    task = asyncio.get_running_loop().create_task(run())
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return True


async def running(redis: Any, lock_name: str) -> bool:
    return bool(await redis.exists(f"close_look:{lock_name}"))


# --------------------------------------------------------------------------- one tower photo, start to finish

AUTO_PHOTOS = 12  # an inspection with at most this many photos is looked at whole; a larger one photo by photo


def facts_of(photo: Any, compass: Any) -> str:
    """The photo's own facts in words (taken when, camera heading and tilt, height), for the questions."""
    words = []
    if photo.taken_at:
        words.append(f"taken {photo.taken_at[:16].replace('T', ' ')} local time")
    if photo.gimbal_yaw is not None:
        words.append(f"camera heading {photo.gimbal_yaw % 360:.0f}° ({compass(photo.gimbal_yaw)})")
    if photo.gimbal_pitch is not None:
        words.append(f"tilted {abs(photo.gimbal_pitch):.0f}° down")
    if photo.relative_altitude_m is not None:
        words.append(f"{photo.relative_altitude_m:.0f} m above take-off")
    return ", ".join(words) or "no camera facts recorded"


async def keep_photo(redis: Any, s3: Any, bucket: str, project_id: str, layer_id: str, sha256: str,
                     read_picture: Any, facts: str, shown: Sequence[Shown], nouns: dict[str, str]) -> None:
    """Look at one tower photo now and keep the look, unless one is kept."""
    if await load_photo(s3, bucket, project_id, layer_id, sha256) is not None:
        return
    pixels = await read_picture()
    picture = Image.fromarray(pixels)
    result = await look_at_photo(redis, picture, facts, shown, nouns)
    await s3.put_object(Bucket=bucket, Key=thumb_key(project_id, layer_id, sha256), Body=jpeg(picture, THUMB_PX, 80),
                        ContentType="image/jpeg")
    await s3.put_object(Bucket=bucket, Key=photo_key(project_id, layer_id, sha256),
                        Body=json.dumps(result).encode(), ContentType="application/json")


async def ensure_photo(redis: Any, s3: Any, bucket: str, project_id: str, layer_id: str, sha256: str,
                       read_picture: Any, facts: str, shown: Sequence[Shown], nouns: dict[str, str]) -> bool:
    """Look at one tower photo in the background unless its look is kept (or one runs). True while one runs."""
    if not enabled() or await load_photo(s3, bucket, project_id, layer_id, sha256) is not None:
        return False
    return await run_once(redis, f"photo:{project_id}:{layer_id}:{sha256}",
                          lambda: keep_photo(redis, s3, bucket, project_id, layer_id, sha256, read_picture, facts,
                                             shown, nouns))


async def ensure_raster(redis: Any, s3: Any, bucket: str, cog_key: str, cog_url: str, place: str) -> bool:
    """Look at an orthomosaic in the background unless its look is kept (or one runs). True while one runs."""
    if not enabled():
        return False
    if await load_raster(s3, bucket, cog_key) is not None:
        return False

    async def work() -> None:
        if await load_raster(s3, bucket, cog_key) is not None:
            return
        result = await look_at_raster(redis, s3, bucket, cog_key, cog_url, place)
        await s3.put_object(Bucket=bucket, Key=f"{raster_key(cog_key)}/look.json", Body=json.dumps(result).encode(),
                            ContentType="application/json")

    return await run_once(redis, f"raster:{raster_key(cog_key)}", work)
