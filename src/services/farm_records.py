"""A farm's own records, read from the documents it already has: soil lab reports and harvest records.

A reader adds a PDF, a photo of a page or a spreadsheet; the vision model (GPT-6 Luna, like the plot
looks in drone_vision.py) reads it into fixed fields, and the soil and farm-history cards then show
those numbers instead of model estimates. The model only transcribes: units are converted, numbers
judged and compared here, in code. Records are kept per project in object storage, with the original file.
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import uuid
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from typing import Any, Optional

from src.llm_defaults import thinking_kwargs
from src.services import drone_vision, llm_cache

logger = logging.getLogger(__name__)

SOIL_REPORT = "soil_report"
HARVEST_RECORDS = "harvest_records"
MAX_DOCUMENT_BYTES = 15 * 1024 * 1024
MAX_SHEET_ROWS = 400  # rows of a spreadsheet sent to the model
_STORE_PREFIX = "farm_records/v1"

_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp"}
_SHEET_EXTENSIONS = (".xlsx", ".xls", ".csv")

_NUMBER = {"type": ["number", "null"]}
# Text the document does not give comes back as "" (and is kept as None): the model's provider refuses a schema with
# more than 16 either-or fields (found 2026-11-15: every document read failed with 24), so only numbers may be null.
_TEXT = {"type": "string"}
_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "kind": {"type": "string", "enum": [SOIL_REPORT, HARVEST_RECORDS, "other"]},
        "title": _TEXT, "source": _TEXT, "date": _TEXT,
        "soil_samples": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "properties": {
                "sample": _TEXT, "place": _TEXT, "depth": _TEXT, "ph": _NUMBER,
                "organic_carbon": _NUMBER, "organic_carbon_unit": _TEXT,
                "nitrogen": _NUMBER, "nitrogen_unit": _TEXT,
                "phosphorus": _NUMBER, "phosphorus_unit": _TEXT, "phosphorus_method": _TEXT,
                "potassium": _NUMBER, "potassium_unit": _TEXT,
                "texture": _TEXT, "coordinates": _TEXT,
            },
            "required": ["sample", "place", "depth", "ph", "organic_carbon", "organic_carbon_unit", "nitrogen",
                         "nitrogen_unit", "phosphorus", "phosphorus_unit", "phosphorus_method", "potassium",
                         "potassium_unit", "texture", "coordinates"],
        }},
        "harvests": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "properties": {"plot": _TEXT, "farmer": _TEXT, "crop": _TEXT, "season": _TEXT, "date": _TEXT,
                           "harvest_kg": _NUMBER, "area_ha": _NUMBER, "coordinates": _TEXT},
            "required": ["plot", "farmer", "crop", "season", "date", "harvest_kg", "area_ha", "coordinates"],
        }},
        "warnings": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["kind", "title", "source", "date", "soil_samples", "harvests", "warnings"],
}

_PROMPT = (
    "This is a farm document from Rwanda: a soil laboratory report, harvest records, or something else. "
    "Copy what it says into the fields; do not calculate, convert or guess. Units exactly as printed "
    "(%, g/kg, mg/kg, ppm, cmol/kg, meq/100g). A number you cannot read is null; a text field the document does not "
    "give is an empty string. For harvest records, one row "
    "per entry, harvest in kg (if the page uses bags or tonnes, put that in warnings and leave harvest_kg null "
    "unless it gives kg). If the document prints a position for a sample or a plot (GPS latitude and longitude, "
    "or a grid reference), copy it into coordinates exactly as printed; otherwise an empty string. Put anything unclear, crossed out or unreadable in warnings, in plain words."
)


@dataclass(frozen=True)
class SoilSample:
    sample: Optional[str]
    place: Optional[str]
    depth: Optional[str]
    ph: Optional[float]
    organic_carbon_percent: Optional[float]
    nitrogen_percent: Optional[float]
    phosphorus_mg_kg: Optional[float]
    phosphorus_method: Optional[str]
    potassium_cmol_kg: Optional[float]
    texture: Optional[str]
    coordinates: Optional[str] = None  # the sample's position as the document prints it (record_links reads it)


@dataclass(frozen=True)
class Harvest:
    plot: Optional[str]
    farmer: Optional[str]
    crop: Optional[str]
    season: Optional[str]
    date: Optional[str]
    harvest_kg: Optional[float]
    area_ha: Optional[float]
    coordinates: Optional[str] = None  # the plot's position as the document prints it

    @property
    def tonnes_per_ha(self) -> Optional[float]:
        if self.harvest_kg is None or not self.area_ha:
            return None
        return self.harvest_kg / 1000 / self.area_ha


@dataclass(frozen=True)
class FarmDocument:
    id: str
    filename: str
    file_key: str
    kind: str
    title: Optional[str]
    source: Optional[str]
    date: Optional[str]
    added_at: str
    soil_samples: list[SoilSample] = field(default_factory=list)
    harvests: list[Harvest] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    model: str = ""


# --- Units: the model copies them, the code converts them ------------------------------------

def _unit(text: Optional[str]) -> str:
    return (text or "").lower().replace(" ", "").replace("(+)", "").replace("+", "")


def nitrogen_percent(value: Optional[float], unit: Optional[str]) -> Optional[float]:
    """Total nitrogen in % from % or g/kg (1 % = 10 g/kg); None for a unit not known here."""
    u = _unit(unit)
    if value is None:
        return None
    if u in ("%", "percent", "pct", ""):
        return value
    if u in ("g/kg", "gkg-1", "g/kgsoil"):
        return value / 10
    return None


def organic_carbon_percent(value: Optional[float], unit: Optional[str]) -> Optional[float]:
    return nitrogen_percent(value, unit)  # the same two units, the same factor


def phosphorus_mg_kg(value: Optional[float], unit: Optional[str]) -> Optional[float]:
    """Available phosphorus in mg/kg (= ppm)."""
    if value is None:
        return None
    return value if _unit(unit) in ("mg/kg", "ppm", "mgkg-1", "µg/g", "ug/g", "") else None


def potassium_cmol_kg(value: Optional[float], unit: Optional[str]) -> Optional[float]:
    """Exchangeable potassium in cmol(+)/kg, from cmol/kg, meq/100 g or mg/kg (39.1 mg/kg = 0.1 cmol/kg)."""
    u = _unit(unit)
    if value is None:
        return None
    if u in ("cmol/kg", "cmolc/kg", "cmolkg-1", "meq/100g", "me/100g", ""):
        return value
    if u in ("mg/kg", "ppm"):
        return value / 391
    return None


def _given(raw: dict[str, Any]) -> dict[str, Any]:
    """The model's fields with text it left empty as None."""
    return {k: (v.strip() or None) if isinstance(v, str) else v for k, v in raw.items()}


def _sample(raw: dict[str, Any]) -> SoilSample:
    raw = _given(raw)
    return SoilSample(
        sample=raw.get("sample"), place=raw.get("place"), depth=raw.get("depth"), ph=raw.get("ph"),
        organic_carbon_percent=organic_carbon_percent(raw.get("organic_carbon"), raw.get("organic_carbon_unit")),
        nitrogen_percent=nitrogen_percent(raw.get("nitrogen"), raw.get("nitrogen_unit")),
        phosphorus_mg_kg=phosphorus_mg_kg(raw.get("phosphorus"), raw.get("phosphorus_unit")),
        phosphorus_method=raw.get("phosphorus_method"),
        potassium_cmol_kg=potassium_cmol_kg(raw.get("potassium"), raw.get("potassium_unit")),
        texture=raw.get("texture"), coordinates=raw.get("coordinates"),
    )


def _harvest(raw: dict[str, Any]) -> Harvest:
    raw = _given(raw)
    return Harvest(plot=raw.get("plot"), farmer=raw.get("farmer"), crop=raw.get("crop"), season=raw.get("season"),
                   date=raw.get("date"), harvest_kg=raw.get("harvest_kg"), area_ha=raw.get("area_ha"),
                   coordinates=raw.get("coordinates"))


# --- Reading a document -----------------------------------------------------------------------

def _sheet_text(content: bytes, filename: str) -> str:
    """A spreadsheet as plain rows of text (the model reads text, not .xlsx)."""
    import pandas as pd

    if filename.lower().endswith(".csv"):
        # as text: a lab's file often opens with title lines of one cell, which a table reader refuses
        return "\n".join(content.decode("utf-8-sig", errors="replace").splitlines()[:MAX_SHEET_ROWS])
    frame = pd.read_excel(io.BytesIO(content), header=None, dtype=str)
    lines = [" | ".join("" if pd.isna(v) else str(v) for v in row) for row in frame.head(MAX_SHEET_ROWS).itertuples(index=False)]
    return "\n".join(lines)


def _content_part(content: bytes, filename: str, content_type: str) -> dict[str, Any]:
    if filename.lower().endswith(_SHEET_EXTENSIONS):
        return {"type": "text", "text": "Spreadsheet rows:\n" + _sheet_text(content, filename)}
    data = base64.b64encode(content).decode()
    if content_type in _IMAGE_TYPES:
        return {"type": "image_url", "image_url": {"url": f"data:{content_type};base64,{data}"}}
    if content_type == "application/pdf" or filename.lower().endswith(".pdf"):
        return {"type": "file", "file": {"filename": filename, "file_data": f"data:application/pdf;base64,{data}"}}
    raise ValueError("Add a PDF, a photo (JPEG, PNG) or a spreadsheet (xlsx, csv)")


async def read_document(content: bytes, filename: str, content_type: str) -> tuple[dict[str, Any], str]:
    """What the vision model reads in a document (raw fields), and the model's name."""
    if len(content) > MAX_DOCUMENT_BYTES:
        raise ValueError(f"The file is over {MAX_DOCUMENT_BYTES // (1024 * 1024)} MB")
    client, model = drone_vision.vision_client()
    part = _content_part(content, filename, content_type)

    async def read() -> dict[str, Any]:
        await drone_vision.pace(model)
        response = await client.chat.completions.create(
            model=model, **thinking_kwargs(client.base_url, "low"),
            messages=[{"role": "system", "content": _PROMPT}, {"role": "user", "content": [part]}],
            response_format={"type": "json_schema", "json_schema": {"name": "farm_document", "strict": True,
                                                                    "schema": _SCHEMA}},
        )
        llm_cache.record("document", response.usage, model)
        return json.loads(response.choices[0].message.content or "{}")

    # The same file read again (another upload, another project) costs nothing.
    raw, _ = await llm_cache.answer("document", llm_cache.key_of(model, _PROMPT, _SCHEMA, content), read)
    return raw, model


def to_document(raw: dict[str, Any], filename: str, file_key: str, model: str, doc_id: str) -> FarmDocument:
    """A kept document from the model's raw reading; values in units Ingabe does not know become None."""
    raw = _given(raw)
    kind: str = raw["kind"] if raw.get("kind") in (SOIL_REPORT, HARVEST_RECORDS) else "other"
    return FarmDocument(
        id=doc_id, filename=filename, file_key=file_key, kind=kind, title=raw.get("title"), source=raw.get("source"),
        date=raw.get("date"), added_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        soil_samples=[_sample(s) for s in raw.get("soil_samples") or []] if kind == SOIL_REPORT else [],
        harvests=[_harvest(h) for h in raw.get("harvests") or []] if kind == HARVEST_RECORDS else [],
        warnings=[str(w) for w in raw.get("warnings") or []][:10], model=model,
    )


# --- Kept per project --------------------------------------------------------------------------

_locks: dict[str, asyncio.Lock] = {}


def _lock(project_id: str) -> asyncio.Lock:
    return _locks.setdefault(project_id, asyncio.Lock())


def _records_key(project_id: str) -> str:
    return f"{_STORE_PREFIX}/{project_id}/records.json"


def _known(cls: Any, data: dict[str, Any]) -> dict[str, Any]:
    """Only the fields this build knows, so records written by a later build still load after a step back."""
    names = {f.name for f in fields(cls)}
    return {k: v for k, v in data.items() if k in names}


def _from_dict(data: dict[str, Any]) -> FarmDocument:
    return FarmDocument(**{**_known(FarmDocument, data),
                           "soil_samples": [SoilSample(**_known(SoilSample, s)) for s in data.get("soil_samples", [])],
                           "harvests": [Harvest(**_known(Harvest, h)) for h in data.get("harvests", [])]})


async def load_records(s3: Any, bucket: str, project_id: str) -> list[FarmDocument]:
    try:
        response = await s3.get_object(Bucket=bucket, Key=_records_key(project_id))
    except s3.exceptions.NoSuchKey:
        return []
    async with response["Body"] as body:
        return [_from_dict(d) for d in json.loads(await body.read())]


async def add_document(s3: Any, bucket: str, project_id: str, content: bytes, filename: str,
                       content_type: str) -> FarmDocument:
    """Keep the file, read it with the vision model, and add what it says to the project's records."""
    doc_id = uuid.uuid4().hex[:12]
    extension = "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    file_key = f"{_STORE_PREFIX}/{project_id}/files/{doc_id}{extension}"
    raw, model = await read_document(content, filename, content_type)
    document = to_document(raw, filename, file_key, model, doc_id)
    await s3.put_object(Bucket=bucket, Key=file_key, Body=content, ContentType=content_type or "application/octet-stream")
    async with _lock(project_id):  # with remove_document: one change to the project's list at a time in this process
        records = await load_records(s3, bucket, project_id) + [document]
        await s3.put_object(Bucket=bucket, Key=_records_key(project_id), ContentType="application/json",
                            Body=json.dumps([asdict(d) for d in records]).encode())
    logger.info("farm document %s read as %s: %d soil samples, %d harvests", doc_id, document.kind,
                len(document.soil_samples), len(document.harvests))
    return document


async def remove_document(s3: Any, bucket: str, project_id: str, doc_id: str) -> Optional[FarmDocument]:
    """Take a document out of the project's records, with the file that was kept; None when there is none with
    this id. Its rows leave every card with it. The records go first: a file left behind is seen by nothing."""
    async with _lock(project_id):
        records = await load_records(s3, bucket, project_id)
        gone = next((d for d in records if d.id == doc_id), None)
        if gone is None:
            return None
        await s3.put_object(Bucket=bucket, Key=_records_key(project_id), ContentType="application/json",
                            Body=json.dumps([asdict(d) for d in records if d.id != doc_id]).encode())
    try:
        await s3.delete_object(Bucket=bucket, Key=gone.file_key)
    except Exception:
        logger.exception("farm document %s: its file %s could not be deleted", doc_id, gone.file_key)
    logger.info("farm document %s removed from project %s", doc_id, project_id)
    return gone


def soil_samples(records: list[FarmDocument]) -> list[tuple[FarmDocument, SoilSample]]:
    return [(d, s) for d in records if d.kind == SOIL_REPORT for s in d.soil_samples]


def harvests(records: list[FarmDocument]) -> list[tuple[FarmDocument, Harvest]]:
    return [(d, h) for d in records if d.kind == HARVEST_RECORDS for h in d.harvests]
