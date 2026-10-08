"""Farm records read from documents, with the vision model replaced by fixed readings (no paid calls)."""

from __future__ import annotations

import asyncio
import io

import pytest

from src.services import farm_records

SOIL_READING = {
    "kind": "soil_report", "title": "Soil Analysis Report", "source": "Example Soil Laboratory", "date": "26/09/2026",
    "soil_samples": [
        {"sample": "S1", "place": "Block A-01", "depth": "0-20 cm", "ph": 5.2, "organic_carbon": 11.0,
         "organic_carbon_unit": "g/kg", "nitrogen": 0.9, "nitrogen_unit": "g/kg", "phosphorus": 6.4,
         "phosphorus_unit": "mg/kg", "phosphorus_method": "Bray II", "potassium": 70.4, "potassium_unit": "mg/kg",
         "texture": "Sandy clay loam"},
        {"sample": "S2", "place": "Block A-04", "depth": "0-20 cm", "ph": 5.8, "organic_carbon": 1.65,
         "organic_carbon_unit": "%", "nitrogen": 0.14, "nitrogen_unit": "%", "phosphorus": 11.2, "phosphorus_unit": "ppm",
         "phosphorus_method": "Bray II", "potassium": 0.31, "potassium_unit": "cmol(+)/kg", "texture": "Clay loam"},
    ],
    "harvests": [], "warnings": [],
}


def test_units_are_converted_in_code():
    assert farm_records.nitrogen_percent(0.9, "g/kg") == pytest.approx(0.09)
    assert farm_records.nitrogen_percent(0.14, "%") == 0.14
    assert farm_records.potassium_cmol_kg(70.4, "mg/kg") == pytest.approx(0.18, abs=0.001)
    assert farm_records.potassium_cmol_kg(0.31, "meq/100 g") == 0.31
    assert farm_records.phosphorus_mg_kg(6.4, "ppm") == 6.4
    assert farm_records.nitrogen_percent(3.0, "lbs/acre") is None  # a unit Ingabe does not know is not guessed


def test_a_soil_reading_becomes_samples_in_one_set_of_units():
    doc = farm_records.to_document(SOIL_READING, "report.pdf", "key", "openai/gpt-6-luna", "d1")
    assert doc.kind == farm_records.SOIL_REPORT and len(doc.soil_samples) == 2
    s1 = doc.soil_samples[0]
    assert (s1.nitrogen_percent, s1.organic_carbon_percent) == (pytest.approx(0.09), pytest.approx(1.1))
    assert s1.potassium_cmol_kg == pytest.approx(0.18, abs=0.001) and s1.phosphorus_method == "Bray II"
    assert doc.harvests == []


def test_harvest_yields_need_an_area():
    raw = {"kind": "harvest_records", "title": None, "source": None, "date": None, "soil_samples": [], "warnings": [],
           "harvests": [{"plot": "A-01", "farmer": "Test farmer 1", "crop": "maize", "season": "2026A",
                         "date": "18/07/2026", "harvest_kg": 210, "area_ha": 0.08},
                        {"plot": "A-02", "farmer": None, "crop": "maize", "season": "2026A", "date": None,
                         "harvest_kg": 500, "area_ha": None}]}
    doc = farm_records.to_document(raw, "register.jpg", "key", "m", "d2")
    assert doc.harvests[0].tonnes_per_ha == pytest.approx(2.625)
    assert doc.harvests[1].tonnes_per_ha is None


def test_other_documents_add_nothing():
    raw = {**SOIL_READING, "kind": "other"}
    doc = farm_records.to_document(raw, "invoice.pdf", "key", "m", "d3")
    assert doc.kind == "other" and doc.soil_samples == [] and doc.harvests == []


def test_spreadsheets_are_sent_as_text_and_unknown_files_refused():
    import pandas as pd

    out = io.BytesIO()
    pd.DataFrame({"Block": ["A-01"], "Harvest (kg)": [210]}).to_excel(out, index=False)
    part = farm_records._content_part(out.getvalue(), "register.xlsx", "application/vnd.ms-excel")
    assert part["type"] == "text" and "A-01 | 210" in part["text"]
    assert farm_records._content_part(b"%PDF-1.4", "r.pdf", "application/pdf")["type"] == "file"
    with pytest.raises(ValueError):
        farm_records._content_part(b"x", "notes.docx", "application/msword")


class _FakeS3:
    class exceptions:
        class NoSuchKey(Exception):
            pass

    def __init__(self):
        self.objects: dict[str, bytes] = {}

    async def put_object(self, Bucket, Key, Body, ContentType):
        self.objects[Key] = Body

    async def get_object(self, Bucket, Key):
        if Key not in self.objects:
            raise self.exceptions.NoSuchKey()
        data = self.objects[Key]

        class _Body:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def read(self):
                return data

        return {"Body": _Body()}


def test_documents_are_kept_per_project_with_their_file(monkeypatch):
    async def fake_read(content, filename, content_type):
        return SOIL_READING, "openai/gpt-6-luna"

    monkeypatch.setattr(farm_records, "read_document", fake_read)
    s3 = _FakeS3()
    doc = asyncio.run(farm_records.add_document(s3, "b", "P1", b"%PDF", "report.pdf", "application/pdf"))
    assert s3.objects[doc.file_key] == b"%PDF"
    records = asyncio.run(farm_records.load_records(s3, "b", "P1"))
    assert [d.id for d in records] == [doc.id] and len(farm_records.soil_samples(records)) == 2
    assert asyncio.run(farm_records.load_records(s3, "b", "P2")) == []
