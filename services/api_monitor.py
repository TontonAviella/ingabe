#!/usr/bin/env python3
"""
FastAPI wrapper for field monitoring v3.

Three endpoints matching the three insurance questions:
  POST /verify    — "Is there a crop? Is it healthy?"
  POST /trend     — "Is it growing or declining?"
  POST /claim     — "Did the crop fail? Multi-signal evidence."
  GET  /health    — Health check

All endpoints work for any crop. No crop identification.
Full signal stack: optical (NDVI+PSRI+BSI+NDMI+MSI+S2REP) + SAR + soil moisture.
"""
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from monitor_field_v3 import verify_field, compare_field

app = FastAPI(
    title="Mundi Field Monitor",
    description="Crop insurance field monitoring — any crop, full signal stack",
    version="3.0",
)


# ─── Request models ───

class VerifyRequest(BaseModel):
    lat: float = Field(..., ge=-90, le=90, description="Latitude")
    lon: float = Field(..., ge=-180, le=180, description="Longitude")
    days_back: int = Field(60, ge=1, le=365, description="How far back to search for scenes")


class TrendRequest(BaseModel):
    lat: float = Field(..., ge=-90, le=90)
    lon: float = Field(..., ge=-180, le=180)
    before_start: int = Field(90, description="Start of 'before' window (days ago)")
    before_end: int = Field(30, description="End of 'before' window (days ago)")
    after_days: int = Field(30, description="'After' window (most recent N days)")


class ClaimRequest(BaseModel):
    lat: float = Field(..., ge=-90, le=90)
    lon: float = Field(..., ge=-180, le=180)
    before_start: int = Field(90)
    before_end: int = Field(30)
    after_days: int = Field(30)


# ─── Endpoints ───

@app.post("/verify")
def verify(req: VerifyRequest):
    """Q1: Is there a crop? Is it healthy?"""
    report = verify_field(req.lat, req.lon, days_back=req.days_back)

    if report.get('status') == 'NO_DATA':
        raise HTTPException(status_code=404, detail=report.get('message', 'No satellite data available'))

    return report


@app.post("/trend")
def trend(req: TrendRequest):
    """Q2: Is vegetation growing or declining?"""
    result = compare_field(
        req.lat, req.lon,
        before_days=(req.before_start, req.before_end),
        after_days=req.after_days,
    )

    if result.get('status') not in ('OK',):
        raise HTTPException(status_code=404, detail=result.get('message', 'Insufficient satellite data'))

    ndvi_chg = result['ndvi_change']
    psri_chg = result['psri_change']
    ndmi_chg = result['ndmi_after'] - result['ndmi_before']

    signals_up = 0
    signals_down = 0
    trend_details = []

    if ndvi_chg > 0.05:
        signals_up += 1
        trend_details.append(f"NDVI up {ndvi_chg:+.3f}")
    elif ndvi_chg < -0.05:
        signals_down += 1
        trend_details.append(f"NDVI down {ndvi_chg:+.3f}")
    else:
        trend_details.append(f"NDVI stable {ndvi_chg:+.3f}")

    if psri_chg < -0.02:
        signals_up += 1
        trend_details.append(f"PSRI improving {psri_chg:+.3f}")
    elif psri_chg > 0.02:
        signals_down += 1
        trend_details.append(f"PSRI worsening {psri_chg:+.3f}")

    if ndmi_chg > 0.05:
        signals_up += 1
        trend_details.append(f"NDMI wetter {ndmi_chg:+.3f}")
    elif ndmi_chg < -0.05:
        signals_down += 1
        trend_details.append(f"NDMI drier {ndmi_chg:+.3f}")

    if signals_up > signals_down:
        result['trend'] = 'GROWING'
    elif signals_down > signals_up:
        result['trend'] = 'DECLINING'
    else:
        result['trend'] = 'STABLE'

    result['trend_details'] = trend_details
    return result


@app.post("/claim")
def claim(req: ClaimRequest):
    """Q3: Did the crop fail? Multi-signal evidence scoring."""
    result = compare_field(
        req.lat, req.lon,
        before_days=(req.before_start, req.before_end),
        after_days=req.after_days,
    )

    if result.get('status') not in ('OK',):
        raise HTTPException(status_code=404, detail=result.get('message', 'Insufficient satellite data'))

    support = result.get('claim_support', 'UNKNOWN')
    ndvi_chg = result.get('ndvi_change', 0)

    if support == 'STRONG':
        result['decision'] = 'APPROVE'
        result['decision_detail'] = 'Multiple satellite signals confirm crop failure.'
    elif support == 'MODERATE':
        result['decision'] = 'INVESTIGATE'
        result['decision_detail'] = 'Some evidence of damage. Recommend field verification.'
    elif support == 'NONE' and ndvi_chg > 0.05:
        result['decision'] = 'REJECT'
        result['decision_detail'] = 'Vegetation is growing. No evidence of crop failure.'
    else:
        result['decision'] = 'INSUFFICIENT'
        result['decision_detail'] = 'Not enough satellite evidence to confirm or deny.'

    return result


@app.get("/health")
def health():
    """Health check."""
    return {
        "status": "ok",
        "version": "3.0",
        "signals": [
            "NDVI", "PSRI", "BSI", "NDMI", "MSI", "S2REP",
            "SAR_VH_VV", "soil_moisture"
        ],
    }


if __name__ == '__main__':
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8001)
