# Copyright (C) 2025 Ingabe Ltd.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""REST API routes for crop insurance monitoring.

Endpoints:
  POST /insurance/report   - Full insurance report (Q1+Q2+Q3 with weather+satellite)
  POST /insurance/monitor  - Quick health check (Q1+Q2 only, no claim verdict)
  POST /insurance/claim    - Claim decision with full evidence package
  GET  /insurance/health   - Service status and signal inventory
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from src.dependencies.session import UserContext, verify_session_required

logger = logging.getLogger(__name__)

insurance_router = APIRouter(prefix="/insurance")


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------

class FieldRequest(BaseModel):
    lat: float = Field(..., ge=-90, le=90, description="Latitude")
    lon: float = Field(..., ge=-180, le=180, description="Longitude")


class ReportRequest(FieldRequest):
    include_forecast: bool = Field(True, description="Include 10-day weather forecast")


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@insurance_router.post("/report")
async def full_report(
    req: ReportRequest,
    user: UserContext = Depends(verify_session_required),
):
    """Full insurance report answering all 3 questions with weather + satellite evidence."""
    from src.services.insurance_service import get_insurance_report
    try:
        report = get_insurance_report(req.lat, req.lon, include_forecast=req.include_forecast)
        return report
    except Exception as e:
        logger.exception("Insurance report failed for (%s, %s)", req.lat, req.lon)
        raise HTTPException(status_code=500, detail=f"Report generation failed: {e}")


@insurance_router.post("/monitor")
async def quick_monitor(
    req: FieldRequest,
    user: UserContext = Depends(verify_session_required),
):
    """Quick field health check: Q1 (crop present?) + Q2 (on track?)."""
    from src.services.insurance_service import get_insurance_report
    try:
        report = get_insurance_report(req.lat, req.lon, include_forecast=True)
        return {
            "field": report["field"],
            "report_date": report["report_date"],
            "q1_crop_present": report["q1_crop_present"],
            "q2_crop_trend": report["q2_crop_trend"],
            "risk_summary": report["risk_summary"],
            "processing_time_s": report["processing_time_s"],
        }
    except Exception as e:
        logger.exception("Insurance monitor failed for (%s, %s)", req.lat, req.lon)
        raise HTTPException(status_code=500, detail=f"Monitor check failed: {e}")


@insurance_router.post("/claim")
async def claim_decision(
    req: FieldRequest,
    user: UserContext = Depends(verify_session_required),
):
    """Claim verdict with satellite + weather evidence package."""
    from src.services.insurance_service import get_insurance_report
    try:
        report = get_insurance_report(req.lat, req.lon, include_forecast=True)
        return {
            "field": report["field"],
            "report_date": report["report_date"],
            "q3_claim_verdict": report["q3_claim_verdict"],
            "risk_summary": report["risk_summary"],
            "data_sources": report["data_sources"],
            "processing_time_s": report["processing_time_s"],
        }
    except Exception as e:
        logger.exception("Insurance claim failed for (%s, %s)", req.lat, req.lon)
        raise HTTPException(status_code=500, detail=f"Claim evaluation failed: {e}")


@insurance_router.get("/health")
async def health():
    """Service status and signal inventory."""
    return {
        "status": "ok",
        "version": "1.0",
        "signals": {
            "optical": ["NDVI", "PSRI", "BSI", "NDMI", "MSI", "S2REP", "NDRE"],
            "radar": ["SAR_VH_VV_cross_ratio", "SAR_VV_dB", "SAR_VH_dB"],
            "soil_moisture": ["surface_0_7cm", "root_zone_7_28cm", "30d_trend"],
            "weather_observed": ["rainfall_30d", "temperature", "dry_spell_length"],
            "weather_forecast": ["ECMWF_IFS", "GFS", "ICON", "GraphCast"],
        },
        "data_sources": {
            "satellite": "Planetary Computer STAC (Sentinel-1 + Sentinel-2)",
            "weather": "Open-Meteo (ERA5-Land archive + multi-model forecast)",
            "soil_moisture": "Open-Meteo ERA5-Land",
            "rainfall_normals": "Open-Meteo ERA5 climate API (pixel-level 1991-2020)",
        },
    }
