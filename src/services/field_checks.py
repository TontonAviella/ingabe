"""What people found when they checked a plot on the ground: the crop really in it.

A check overrides the vision model for that plot in every card, and the checks together say how often the
model was right; nothing else in Ingabe can say that. Kept per photo in object storage:
`field_checks/v1/<sha of the photo key>.json`.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from src.services import drone_vision

_STORE_PREFIX = "field_checks/v1"
# Crops a person can pick when checking (the model's lists, without its own "unsure" and "other").
CHECK_CROPS = [c for c in drone_vision.CROPS if c not in ("unsure",)]


@dataclass(frozen=True)
class Check:
    number: int  # the plot
    crop: str
    at: str


def _key(photo_key: str) -> str:
    return f"{_STORE_PREFIX}/{hashlib.sha256(photo_key.encode()).hexdigest()[:32]}.json"


async def load_checks(s3: Any, bucket: str, photo_key: str) -> dict[int, Check]:
    try:
        response = await s3.get_object(Bucket=bucket, Key=_key(photo_key))
    except s3.exceptions.NoSuchKey:
        return {}
    async with response["Body"] as body:
        data = json.loads(await body.read())
    return {int(item["number"]): Check(**item) for item in data.get("checks", [])}


async def add_check(s3: Any, bucket: str, photo_key: str, number: int, crop: str) -> dict[int, Check]:
    """Record the crop really in a plot (a later check of the same plot replaces it); all checks are returned."""
    if crop not in CHECK_CROPS:
        raise ValueError(f"unknown crop {crop!r}")
    checks = await load_checks(s3, bucket, photo_key)
    checks[number] = Check(number=number, crop=crop, at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
    body = json.dumps({"checks": [asdict(c) for c in sorted(checks.values(), key=lambda c: c.number)]}).encode()
    await s3.put_object(Bucket=bucket, Key=_key(photo_key), Body=body, ContentType="application/json")
    return checks


def apply(survey: Optional[drone_vision.Survey], checks: dict[int, Check]) -> Optional[drone_vision.Survey]:
    """The survey with checked plots' crops replaced by what was found, so every card uses them."""
    if survey is None or not checks:
        return survey
    looks = dict(survey.looks)
    for number, check in checks.items():
        look = looks.get(number)
        if look is not None:
            looks[number] = dataclasses.replace(look, main_crop=check.crop, candidates=(), confidence="high",
                                                other_crops=[c for c in look.other_crops if c != check.crop])
    return dataclasses.replace(survey, looks=looks)


@dataclass(frozen=True)
class Record:
    """How the model did on the checked plots where it named a crop; `unsure` = checked plots it left unnamed."""

    right: int
    named: int
    unsure: int

    @property
    def share(self) -> Optional[float]:
        return self.right / self.named if self.named else None


def model_record(survey: Optional[drone_vision.Survey], checks: dict[int, Check]) -> Record:
    """Compare the model's own looks (before checks are applied) with the checks."""
    right = named = unsure = 0
    for number, check in checks.items():
        look = survey.look(number) if survey else None
        if look is None:
            continue
        if look.main_crop == "unsure":
            unsure += 1
            continue
        named += 1
        right += look.main_crop == check.crop
    return Record(right=right, named=named, unsure=unsure)
