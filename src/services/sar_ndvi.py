# Copyright (C) 2025 Ingabe Ltd.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""SAR → NDVI cloud gap filler.

Predicts NDVI from Sentinel-1 SAR backscatter when Sentinel-2 optical
imagery is blocked by clouds. Uses 30-day VV/VH time series features
with a GradientBoostingRegressor trained on historical S2+S1 paired
observations.

Clean-room implementation inspired by published SAR-to-vegetation-index
prediction techniques, adapted for Sentinel-1 C-band RTC data.

Usage:
    from src.services.sar_ndvi import get_sar_ndvi_predictor
    pred = get_sar_ndvi_predictor()
    result = pred.predict_ndvi(bbox=(29.3, -2.0, 29.4, -1.9))
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

logger = logging.getLogger(__name__)

# Default hyperparameters for GBR
_GBR_PARAMS = {
    "n_estimators": 100,
    "max_depth": 5,
    "learning_rate": 0.05,
    "subsample": 0.8,
    "random_state": 42,
}


def _extract_features(
    dates: List[str],
    vv_means: List[float],
    vh_means: List[float],
    vv_stds: List[float],
    vh_stds: List[float],
    last_known_ndvi: Optional[float] = None,
    n_days: int = 30,
) -> Optional[np.ndarray]:
    """Extract feature vector from S1 time series.

    Creates 120 lag features (30 days x 4 stats: vv_mean, vv_std, vh_mean, vh_std)
    plus optional last-known NDVI anchor.

    Returns 1D feature array or None if insufficient data.
    """
    if len(dates) < 2:
        return None

    # Parse dates to day offsets from most recent
    try:
        parsed = []
        for d in dates:
            if "T" in d:
                dt = datetime.fromisoformat(d.replace("Z", "+00:00")).replace(tzinfo=None)
            else:
                dt = datetime.strptime(d, "%Y-%m-%d")
            parsed.append(dt)
    except (ValueError, TypeError):
        return None

    latest = max(parsed)
    day_offsets = [(latest - d).days for d in parsed]

    # Interpolate to daily values over n_days window
    daily_vv = np.interp(
        range(n_days), sorted(day_offsets), [vv_means[i] for i in np.argsort(day_offsets)]
    )
    daily_vh = np.interp(
        range(n_days), sorted(day_offsets), [vh_means[i] for i in np.argsort(day_offsets)]
    )
    daily_vv_std = np.interp(
        range(n_days), sorted(day_offsets), [vv_stds[i] for i in np.argsort(day_offsets)]
    )
    daily_vh_std = np.interp(
        range(n_days), sorted(day_offsets), [vh_stds[i] for i in np.argsort(day_offsets)]
    )

    # Stack into feature vector: [vv_d0, vv_d1, ..., vv_d29, vh_d0, ..., vh_d29, vv_std_d0, ..., vh_std_d29]
    features = np.concatenate([daily_vv, daily_vh, daily_vv_std, daily_vh_std])

    if last_known_ndvi is not None:
        features = np.append(features, last_known_ndvi)

    return features


def _generate_training_data(
    bbox: Tuple[float, float, float, float],
    days_back: int = 180,
) -> Tuple[Optional[np.ndarray], Optional[np.ndarray]]:
    """Generate training data from historical S1+S2 paired observations.

    For each cloud-free S2 observation, extract the corresponding S1
    time series features for the preceding 30 days.

    Returns (X, y) arrays or (None, None) if insufficient data.
    """
    from src.services.sentinel1_service import get_sentinel1_service

    s1 = get_sentinel1_service()
    end_date = datetime.utcnow()
    start_date = end_date - timedelta(days=days_back)
    date_range = f"{start_date.strftime('%Y-%m-%d')}/{end_date.strftime('%Y-%m-%d')}"

    # Get full S1 time series
    ts = s1.get_time_series(bbox, date_range, limit=50)
    if ts.get("status") != "success" or len(ts["dates"]) < 5:
        logger.warning("Insufficient S1 data for training: %d scenes", len(ts.get("dates", [])))
        return None, None

    # Try to get S2 NDVI observations from STAC
    try:
        from src.services.stac_service import get_stac_service
        stac = get_stac_service("earth_search")
        ndvi_result = stac.compute_admin_ndvi(
            bbox=list(bbox), days=days_back, max_cloud_cover=30.0, max_scenes=20
        )
        observations = ndvi_result.get("observations", [])
    except Exception as e:
        logger.warning("Failed to get S2 NDVI for training: %s", e)
        return None, None

    if len(observations) < 5:
        logger.warning("Insufficient S2 observations for training: %d", len(observations))
        return None, None

    X_list: List[np.ndarray] = []
    y_list: List[float] = []

    all_dates = ts["dates"]
    all_vv = ts["vv_means"]
    all_vh = ts["vh_means"]
    all_vv_std = ts["vv_stds"]
    all_vh_std = ts["vh_stds"]

    for obs in observations:
        ndvi = obs.get("mean_ndvi")
        obs_date = obs.get("datetime", "")
        if ndvi is None or not obs_date:
            continue

        # Find S1 scenes within 30 days before this observation
        try:
            if "T" in obs_date:
                obs_dt = datetime.fromisoformat(obs_date.replace("Z", "+00:00")).replace(tzinfo=None)
            else:
                obs_dt = datetime.strptime(obs_date[:10], "%Y-%m-%d")
        except (ValueError, TypeError):
            continue

        window_start = obs_dt - timedelta(days=30)

        # Filter S1 scenes to this window
        idx_in_window = []
        for i, d in enumerate(all_dates):
            try:
                if "T" in d:
                    s1_dt = datetime.fromisoformat(d.replace("Z", "+00:00")).replace(tzinfo=None)
                else:
                    s1_dt = datetime.strptime(d[:10], "%Y-%m-%d")
                if window_start <= s1_dt <= obs_dt:
                    idx_in_window.append(i)
            except (ValueError, TypeError):
                continue

        if len(idx_in_window) < 2:
            continue

        win_dates = [all_dates[i] for i in idx_in_window]
        win_vv = [all_vv[i] for i in idx_in_window]
        win_vh = [all_vh[i] for i in idx_in_window]
        win_vv_std = [all_vv_std[i] for i in idx_in_window]
        win_vh_std = [all_vh_std[i] for i in idx_in_window]

        features = _extract_features(win_dates, win_vv, win_vh, win_vv_std, win_vh_std)
        if features is not None:
            X_list.append(features)
            y_list.append(ndvi)

    if len(X_list) < 5:
        logger.warning("Insufficient paired samples for training: %d", len(X_list))
        return None, None

    return np.array(X_list), np.array(y_list)


def _enrich_ndvi_with_cropland(
    result: Dict[str, Any],
    bbox: Tuple[float, float, float, float],
) -> Dict[str, Any]:
    """Add cropland_fraction and cropland_warning to an NDVI prediction result."""
    try:
        from src.services.deafrica_stac import _cached_cropland, _round_bbox
        crop = _cached_cropland(_round_bbox(bbox))
        if crop is not None:
            fraction, year = crop
            result["cropland_fraction"] = fraction
            result["validation_data_year"] = year
            if fraction < 0.3:
                result["cropland_warning"] = (
                    f"Low cropland fraction ({fraction:.2f}), area may not be farmland"
                )
    except Exception as e:
        logger.warning("Cropland enrichment failed for NDVI prediction: %s", e)
    return result


class SARNDVIPredictor:
    """Predict NDVI from SAR backscatter when optical imagery is cloudy."""

    def __init__(self) -> None:
        self._model: Any = None
        self._model_rmse: Optional[float] = None
        self._model_r2: Optional[float] = None
        self._n_training_samples: int = 0

    def predict_ndvi(
        self,
        bbox: Tuple[float, float, float, float],
        target_date: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Predict NDVI from SAR for a given area and date.

        If no trained model exists, trains one on-the-fly from historical
        S1+S2 paired observations.

        Args:
            bbox: (lon_min, lat_min, lon_max, lat_max)
            target_date: YYYY-MM-DD, defaults to today

        Returns:
            Dict with predicted_ndvi, confidence, last_optical_date,
            sar_dates_used, model_rmse.
        """
        from src.services.sentinel1_service import get_sentinel1_service

        s1 = get_sentinel1_service()

        if target_date:
            target = datetime.strptime(target_date, "%Y-%m-%d")
        else:
            target = datetime.utcnow()

        # Get S1 time series for last 30 days
        start = (target - timedelta(days=30)).strftime("%Y-%m-%d")
        end = target.strftime("%Y-%m-%d")
        ts = s1.get_time_series(bbox, f"{start}/{end}", limit=10)

        if ts.get("status") != "success" or len(ts["dates"]) < 2:
            return {
                "status": "insufficient_sar_data",
                "error": f"Only {len(ts.get('dates', []))} S1 scenes in last 30 days, need at least 2",
                "sar_dates_used": len(ts.get("dates", [])),
            }

        # Extract features
        features = _extract_features(
            ts["dates"], ts["vv_means"], ts["vh_means"],
            ts["vv_stds"], ts["vh_stds"],
        )

        if features is None:
            return {
                "status": "feature_extraction_failed",
                "error": "Could not extract features from S1 time series",
            }

        # Train model if needed
        if self._model is None:
            train_result = self.train_model(bbox)
            if train_result.get("status") == "error":
                # Fall back to simple empirical relationship
                return self._empirical_prediction(ts)

        if self._model is None:
            return self._empirical_prediction(ts)

        # Predict
        try:
            features_2d = features.reshape(1, -1)
            # Ensure feature count matches model
            n_expected = self._model.n_features_in_
            if features_2d.shape[1] != n_expected:
                # Pad or truncate
                if features_2d.shape[1] < n_expected:
                    padded = np.zeros((1, n_expected))
                    padded[0, :features_2d.shape[1]] = features_2d[0]
                    features_2d = padded
                else:
                    features_2d = features_2d[:, :n_expected]

            predicted = float(self._model.predict(features_2d)[0])
            # Clamp to valid NDVI range
            predicted = max(-1.0, min(1.0, predicted))

            confidence = self._compute_confidence(ts)

            result = {
                "status": "success",
                "predicted_ndvi": round(predicted, 4),
                "confidence": round(confidence, 2),
                "sar_dates_used": len(ts["dates"]),
                "model_rmse": self._model_rmse,
                "model_r2": self._model_r2,
                "n_training_samples": self._n_training_samples,
                "target_date": end,
                "method": "gradient_boosting",
                "source": "Sentinel-1 RTC (Planetary Computer) + scikit-learn prediction",
            }
            return _enrich_ndvi_with_cropland(result, bbox)
        except Exception as e:
            logger.exception("NDVI prediction failed")
            return self._empirical_prediction(ts, bbox)

    def train_model(
        self,
        bbox: Tuple[float, float, float, float],
        days_back: int = 180,
    ) -> Dict[str, Any]:
        """Train GBR model from historical S1+S2 paired observations."""
        from sklearn.ensemble import GradientBoostingRegressor
        from sklearn.model_selection import cross_val_score

        X, y = _generate_training_data(bbox, days_back)
        if X is None or y is None:
            return {"status": "error", "error": "Insufficient training data"}

        model = GradientBoostingRegressor(**_GBR_PARAMS)

        # Cross-validate
        try:
            scores = cross_val_score(model, X, y, cv=min(3, max(2, len(y))), scoring="neg_root_mean_squared_error")
            rmse = float(-scores.mean())
        except Exception:
            rmse = None

        # Fit on full data
        model.fit(X, y)

        # R² on training data (not ideal but indicates fit)
        r2 = float(model.score(X, y))

        self._model = model
        self._model_rmse = round(rmse, 4) if rmse else None
        self._model_r2 = round(r2, 4)
        self._n_training_samples = len(y)

        logger.info(
            "SAR→NDVI model trained: %d samples, RMSE=%.4f, R²=%.4f",
            len(y), rmse or 0, r2,
        )

        return {
            "status": "success",
            "n_samples": len(y),
            "rmse": self._model_rmse,
            "r2": self._model_r2,
        }

    def _empirical_prediction(self, ts: Dict[str, Any], bbox: Optional[Tuple[float, float, float, float]] = None) -> Dict[str, Any]:
        """Fallback: predict NDVI from simple VH/VV cross-pol ratio.

        Based on research thresholds from working/test_sar_monitoring.py:
        - Cross-pol ratio (VH/VV) < 0.15 → bare soil (NDVI ~0.15)
        - 0.15-0.25 → sparse veg (NDVI ~0.30)
        - 0.25-0.40 → crop (NDVI ~0.55)
        - > 0.40 → dense veg (NDVI ~0.75)
        """
        if not ts.get("vv_means") or not ts.get("vh_means"):
            return {
                "status": "error",
                "error": "No SAR data available for prediction",
            }

        # Use most recent scene
        vv = ts["vv_means"][-1]
        vh = ts["vh_means"][-1]

        # Cross-pol ratio in linear scale (data is in dB)
        # Convert dB to linear: 10^(dB/10)
        try:
            vv_lin = 10 ** (vv / 10.0)
            vh_lin = 10 ** (vh / 10.0)
            ratio = vh_lin / vv_lin if vv_lin > 0 else 0.0
        except (OverflowError, ZeroDivisionError):
            ratio = 0.2

        # Piecewise linear mapping
        if ratio < 0.15:
            predicted = 0.10 + ratio * 0.33
        elif ratio < 0.25:
            predicted = 0.20 + (ratio - 0.15) * 2.0
        elif ratio < 0.40:
            predicted = 0.40 + (ratio - 0.25) * 2.33
        else:
            predicted = min(0.85, 0.75 + (ratio - 0.40) * 0.5)

        result = {
            "status": "success",
            "predicted_ndvi": round(predicted, 4),
            "confidence": 0.45,  # Low confidence for empirical method
            "sar_dates_used": len(ts["dates"]),
            "model_rmse": None,
            "model_r2": None,
            "target_date": ts["dates"][-1][:10] if ts["dates"] else None,
            "method": "empirical_cross_pol_ratio",
            "vh_vv_ratio": round(ratio, 4),
            "source": "Sentinel-1 RTC (Planetary Computer) + empirical VH/VV mapping",
        }
        if bbox is not None:
            return _enrich_ndvi_with_cropland(result, bbox)
        return result

    def _compute_confidence(self, ts: Dict[str, Any]) -> float:
        """Compute prediction confidence from data quality indicators."""
        confidence = 0.5

        # More SAR scenes → higher confidence
        n_scenes = len(ts.get("dates", []))
        if n_scenes >= 5:
            confidence += 0.2
        elif n_scenes >= 3:
            confidence += 0.1

        # Model quality
        if self._model_rmse is not None:
            if self._model_rmse < 0.10:
                confidence += 0.2
            elif self._model_rmse < 0.15:
                confidence += 0.1

        # Training data size
        if self._n_training_samples >= 20:
            confidence += 0.1
        elif self._n_training_samples >= 10:
            confidence += 0.05

        return min(0.95, confidence)


_singleton: Optional[SARNDVIPredictor] = None


def get_sar_ndvi_predictor() -> SARNDVIPredictor:
    """Return the shared SARNDVIPredictor instance."""
    global _singleton
    if _singleton is None:
        _singleton = SARNDVIPredictor()
    return _singleton
