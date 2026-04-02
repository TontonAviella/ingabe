#!/usr/bin/env python3
"""
FastAPI for combined insurance intelligence.

The insurance worker's single endpoint — weather + satellite in one call.

  POST /report       — Full field report (Q1 + Q2 + Q3 + verdict)
  POST /monitor      — Quick health check (Q1 only, faster)
  POST /claim        — Claim decision with full evidence (Q3 focused)
  GET  /health       — Service health
"""
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from insurance_report import get_insurance_report

app = FastAPI(
    title="Mundi Insurance Intelligence",
    description=(
        "Combined weather + satellite field intelligence for crop insurance. "
        "Weather explains WHY. Satellite shows WHAT. Together = the evidence package."
    ),
    version="1.0",
)


class FieldRequest(BaseModel):
    lat: float = Field(..., ge=-90, le=90, description="Field latitude")
    lon: float = Field(..., ge=-180, le=180, description="Field longitude")


class ReportRequest(FieldRequest):
    include_forecast: bool = Field(True, description="Include 10-day weather forecast")


@app.post("/report")
def full_report(req: ReportRequest):
    """Full insurance field report."""
    return get_insurance_report(req.lat, req.lon, include_forecast=req.include_forecast)


@app.post("/monitor")
def quick_monitor(req: FieldRequest):
    """Quick field health check — Q1 and Q2 only."""
    report = get_insurance_report(req.lat, req.lon, include_forecast=True)

    return {
        "field": report["field"],
        "report_date": report["report_date"],
        "crop_present": report["q1_crop_present"],
        "crop_trend": report["q2_crop_trend"],
        "risk": report["risk_summary"],
        "processing_time_s": report["processing_time_s"],
    }


@app.post("/claim")
def claim_decision(req: FieldRequest):
    """Claim decision with full evidence package."""
    report = get_insurance_report(req.lat, req.lon, include_forecast=True)

    return {
        "field": report["field"],
        "report_date": report["report_date"],
        "satellite_evidence": report["q3_claim_verdict"]["satellite_evidence"],
        "weather_evidence": report["q3_claim_verdict"]["weather_evidence"],
        "verdict": report["q3_claim_verdict"]["combined_verdict"],
        "confidence": report["q3_claim_verdict"]["confidence"],
        "detail": report["q3_claim_verdict"]["detail"],
        "crop_status": {
            "present": report["q1_crop_present"]["satellite"].get("answer"),
            "health": report["q1_crop_present"]["satellite"].get("health"),
            "trend": report["q2_crop_trend"]["satellite"].get("trend"),
        },
        "risk": report["risk_summary"],
        "data_sources": report["data_sources"],
        "processing_time_s": report["processing_time_s"],
    }


@app.get("/health")
def health():
    """Service health check."""
    return {
        "status": "ok",
        "version": "1.0",
        "signals": {
            "satellite": ["NDVI", "PSRI", "BSI", "NDMI", "MSI", "S2REP", "SAR_VH_VV", "soil_moisture"],
            "weather": ["precipitation", "temperature", "drought_risk", "flood_risk", "forecast_10d"],
        },
        "data_sources": [
            "Sentinel-2 (optical)",
            "Sentinel-1 (SAR)",
            "ERA5-Land (soil moisture + observed weather)",
            "ECMWF IFS + GFS + ICON + GraphCast (forecast)",
        ],
    }


if __name__ == '__main__':
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8002)
