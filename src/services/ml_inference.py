# Copyright (C) 2025 Ingabe Ltd.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

"""ML inference service for Rwanda agriculture using scikit-learn.

Scikit-learn is an optional dependency. The service gracefully degrades
if it is not installed, returning informative error messages.
"""

import logging
from typing import Any, Dict, List, Optional

import numpy as np

logger = logging.getLogger(__name__)

# Check for optional ML dependencies
_SKLEARN_AVAILABLE = False

try:
    from sklearn.cluster import KMeans

    _SKLEARN_AVAILABLE = True
except ImportError:
    logger.info("scikit-learn not installed — advanced ML features disabled")


def ml_available() -> bool:
    """Check if basic ML dependencies (sklearn) are available."""
    return _SKLEARN_AVAILABLE


class CropClassifier:
    """Crop type classification from satellite imagery bands.

    Uses spectral index thresholds (baseline), KMeans clustering (sklearn),
    Mann-Kendall trend test, and z-score anomaly detection.
    """

    # Spectral index thresholds for simple crop classification
    CROP_THRESHOLDS = {
        "dense_vegetation": {"ndvi_min": 0.6, "ndvi_max": 1.0},
        "moderate_vegetation": {"ndvi_min": 0.3, "ndvi_max": 0.6},
        "sparse_vegetation": {"ndvi_min": 0.15, "ndvi_max": 0.3},
        "bare_soil": {"ndvi_min": -0.1, "ndvi_max": 0.15},
        "water": {"ndvi_min": -1.0, "ndvi_max": -0.1},
    }

    def __init__(self, model_path: Optional[str] = None):
        self.model_path = model_path
        self._model = None

    def classify_from_ndvi(self, ndvi_values: List[float]) -> Dict[str, Any]:
        """Classify land cover from NDVI values using spectral thresholds.

        This is the baseline classifier using standard remote sensing thresholds.
        Includes statistical analysis: histogram, mode class, and Jenks natural breaks
        for larger datasets (>100 pixels).
        """
        arr = np.array(ndvi_values)

        classification = {}
        total = len(arr)

        for class_name, thresholds in self.CROP_THRESHOLDS.items():
            mask = (arr >= thresholds["ndvi_min"]) & (arr < thresholds["ndvi_max"])
            count = int(mask.sum())
            classification[class_name] = {
                "count": count,
                "percentage": round(count / total * 100, 2) if total > 0 else 0,
            }

        # Compute statistics
        histogram, bin_edges = np.histogram(arr, bins=20, range=(-1.0, 1.0))

        # Find mode class (most common) — None if empty
        mode_class = (
            max(classification.items(), key=lambda x: x[1]["count"])[0]
            if total > 0
            else None
        )

        result = {
            "method": "spectral_threshold",
            "total_pixels": total,
            "mean_ndvi": round(float(arr.mean()), 4) if total > 0 else None,
            "std_ndvi": round(float(arr.std()), 4) if total > 0 else None,
            "median_ndvi": round(float(np.median(arr)), 4) if total > 0 else None,
            "mode_class": mode_class,
            "classification": classification,
            "histogram": {
                "counts": histogram.tolist(),
                "bin_edges": bin_edges.tolist(),
            },
        }

        # Jenks natural breaks for larger datasets
        if total > 100:
            try:
                breaks = self._jenks_natural_breaks(arr, n_classes=5)
                result["jenks_breaks"] = [round(float(b), 4) for b in breaks]
            except Exception as e:
                logger.warning(f"Jenks breaks computation failed: {e}")

        return result

    def _jenks_natural_breaks(
        self, data: np.ndarray, n_classes: int = 5
    ) -> List[float]:
        """Compute Jenks natural breaks classification (Fisher-Jenks algorithm).

        Optimizes the arrangement of values into classes by minimizing variance
        within classes and maximizing variance between classes.
        """
        data_sorted = np.sort(data)
        n_data = len(data_sorted)

        # Initialize matrices for dynamic programming
        mat1 = np.zeros((n_data + 1, n_classes + 1))
        mat2 = np.zeros((n_data + 1, n_classes + 1))

        for i in range(1, n_classes + 1):
            mat1[1, i] = 1
            mat2[1, i] = 0
            for j in range(2, n_data + 1):
                mat2[j, i] = float("inf")

        v = 0.0
        for l in range(2, n_data + 1):
            s1 = 0.0
            s2 = 0.0
            w = 0.0
            for m in range(1, l + 1):
                i3 = l - m + 1
                val = float(data_sorted[i3 - 1])
                s2 += val * val
                s1 += val
                w += 1
                v = s2 - (s1 * s1) / w
                i4 = i3 - 1
                if i4 != 0:
                    for j in range(2, n_classes + 1):
                        if mat2[l, j] >= (v + mat2[i4, j - 1]):
                            mat1[l, j] = i3
                            mat2[l, j] = v + mat2[i4, j - 1]
            mat1[l, 1] = 1
            mat2[l, 1] = v

        # Extract break points
        k = n_data
        kclass = []
        for j in range(n_classes, 0, -1):
            idx = int(mat1[k, j]) - 2
            kclass.append(data_sorted[idx] if idx >= 0 else data_sorted[0])
            k = int(mat1[k, j]) - 1

        return sorted(kclass)

    def classify_multispectral(self, bands: Dict[str, np.ndarray]) -> Dict[str, Any]:
        """Classify land cover from multispectral bands using KMeans clustering.

        Args:
            bands: Dictionary of band arrays, e.g., {"B02": array, "B03": array, "B04": array, "B08": array}
                   B02 = Blue, B03 = Green, B04 = Red, B08 = NIR (Sentinel-2 band naming)

        Returns:
            Classification map with cluster assignments and statistics.
        """
        if not _SKLEARN_AVAILABLE:
            return {
                "error": "scikit-learn not installed — install with: pip install scikit-learn==1.6.1"
            }

        # Validate required bands
        required_bands = ["B03", "B04", "B08"]  # Green, Red, NIR
        if not all(band in bands for band in required_bands):
            return {
                "error": f"Missing required bands. Need: {required_bands}, got: {list(bands.keys())}"
            }

        try:
            # Compute spectral indices
            nir = bands["B08"].astype(np.float32)
            red = bands["B04"].astype(np.float32)
            green = bands["B03"].astype(np.float32)

            # NDVI: (NIR - Red) / (NIR + Red)
            ndvi = (nir - red) / (nir + red + 1e-8)

            # NDWI: (Green - NIR) / (Green + NIR) - water index
            ndwi = (green - nir) / (green + nir + 1e-8)

            # BSI: Bare Soil Index (simplified version using RGB+NIR)
            if "B02" in bands:  # Blue band available
                blue = bands["B02"].astype(np.float32)
                bsi = ((red + blue) - (nir + green)) / ((red + blue) + (nir + green) + 1e-8)
            else:
                # Fallback: use red-green ratio as proxy
                bsi = (red - green) / (red + green + 1e-8)

            # Stack indices into feature matrix
            shape = ndvi.shape
            n_pixels = ndvi.size

            features = np.stack([ndvi.ravel(), ndwi.ravel(), bsi.ravel()], axis=1)

            # Remove invalid pixels (NaN, inf)
            valid_mask = np.isfinite(features).all(axis=1)
            features_valid = features[valid_mask]

            if len(features_valid) < 10:
                return {"error": "Insufficient valid pixels for clustering"}

            # KMeans clustering
            from sklearn.cluster import KMeans

            kmeans = KMeans(n_clusters=5, random_state=42, n_init=10)
            labels_valid = kmeans.fit_predict(features_valid)

            # Reconstruct full classification map
            labels_full = np.full(n_pixels, -1, dtype=np.int32)
            labels_full[valid_mask] = labels_valid

            classification_map = labels_full.reshape(shape)

            # Analyze clusters by mean NDVI to assign land cover names
            cluster_stats = []
            for cluster_id in range(5):
                cluster_mask = labels_valid == cluster_id
                cluster_ndvi = features_valid[cluster_mask, 0]  # NDVI is first feature
                cluster_ndwi = features_valid[cluster_mask, 1]
                cluster_bsi = features_valid[cluster_mask, 2]

                mean_ndvi = float(cluster_ndvi.mean())
                mean_ndwi = float(cluster_ndwi.mean())
                mean_bsi = float(cluster_bsi.mean())

                # Map cluster to land cover type by spectral characteristics
                if mean_ndvi > 0.6:
                    land_cover = "dense_vegetation"
                elif mean_ndvi > 0.3:
                    land_cover = "moderate_vegetation"
                elif mean_ndvi > 0.15:
                    land_cover = "sparse_vegetation"
                elif mean_ndwi > 0.0:  # High NDWI indicates water
                    land_cover = "water"
                else:
                    land_cover = "bare_soil"

                cluster_stats.append(
                    {
                        "cluster_id": int(cluster_id),
                        "land_cover": land_cover,
                        "pixel_count": int(cluster_mask.sum()),
                        "percentage": round(
                            float(cluster_mask.sum()) / len(labels_valid) * 100, 2
                        ),
                        "mean_ndvi": round(mean_ndvi, 4),
                        "mean_ndwi": round(mean_ndwi, 4),
                        "mean_bsi": round(mean_bsi, 4),
                    }
                )

            # Sort by pixel count (descending)
            cluster_stats.sort(key=lambda x: x["pixel_count"], reverse=True)

            return {
                "method": "kmeans_clustering",
                "n_clusters": 5,
                "total_pixels": n_pixels,
                "valid_pixels": int(valid_mask.sum()),
                "invalid_pixels": int((~valid_mask).sum()),
                "classification_map": classification_map.tolist(),
                "cluster_stats": cluster_stats,
                "feature_names": ["NDVI", "NDWI", "BSI"],
            }

        except Exception as e:
            logger.exception("Multispectral classification failed")
            return {"error": f"Classification failed: {str(e)}"}

    def predict_yield_risk(
        self, ndvi_timeseries: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """Predict yield risk from NDVI time series using Mann-Kendall trend test.

        Uses Theil-Sen slope estimator (non-parametric, robust to outliers) and
        seasonal deviation analysis. Standard approach for environmental monitoring.
        """
        if not ndvi_timeseries:
            return {"error": "No NDVI data provided"}

        ndvi_values = [
            d.get("mean_ndvi", 0)
            for d in ndvi_timeseries
            if d.get("mean_ndvi") is not None
        ]

        if len(ndvi_values) < 2:
            return {"error": "Need at least 2 NDVI observations for trend analysis"}

        arr = np.array(ndvi_values)

        # Theil-Sen slope estimator: median of all pairwise slopes
        n = len(arr)
        slopes = []
        for i in range(n):
            for j in range(i + 1, n):
                slope = (arr[j] - arr[i]) / (j - i)
                slopes.append(slope)

        trend_slope = float(np.median(slopes)) if slopes else 0.0

        # Kendall's tau for trend significance (using numpy only)
        # Compute concordant and discordant pairs
        concordant = 0
        discordant = 0
        for i in range(n):
            for j in range(i + 1, n):
                diff = arr[j] - arr[i]
                if diff > 0:
                    concordant += 1
                elif diff < 0:
                    discordant += 1

        # Kendall's tau statistic
        tau = (concordant - discordant) / (n * (n - 1) / 2) if n > 1 else 0.0

        # Approximate significance (simplified z-test)
        # For n >= 10, tau is approximately normally distributed
        if n >= 10:
            var_s = n * (n - 1) * (2 * n + 5) / 18
            z_score = (concordant - discordant) / np.sqrt(var_s)
            # p-value approximation: |z| > 1.96 => significant at 95% confidence
            trend_significant = abs(z_score) > 1.96
        else:
            z_score = None
            trend_significant = abs(tau) > 0.5  # Simple threshold for small samples

        # Seasonal deviation analysis
        mean_ndvi = float(arr.mean())
        std_ndvi = float(arr.std())
        latest_ndvi = float(arr[-1])
        seasonal_deviation = (latest_ndvi - mean_ndvi) / (std_ndvi + 1e-8)

        # Risk classification based on trend, absolute level, and seasonal deviation
        if latest_ndvi < 0.2:
            risk_level = "critical"
            risk_description = "Very low NDVI — bare soil or severe crop failure"
        elif trend_slope < -0.02 and trend_significant:
            risk_level = "high"
            risk_description = (
                "NDVI declining significantly (statistically significant) — "
                "potential crop stress or drought"
            )
        elif seasonal_deviation < -2.0:
            risk_level = "high"
            risk_description = (
                "NDVI >2 standard deviations below seasonal average — "
                "abnormal crop condition"
            )
        elif trend_slope < -0.005 or seasonal_deviation < -1.0:
            risk_level = "moderate"
            risk_description = "NDVI declining or below seasonal norm — monitor for issues"
        elif trend_slope > 0.02 and latest_ndvi > 0.4:
            risk_level = "low"
            risk_description = "NDVI increasing and healthy — good vegetation growth"
        else:
            risk_level = "normal"
            risk_description = "NDVI stable — normal seasonal pattern"

        result = {
            "method": "mann_kendall_trend",
            "observations": n,
            "latest_ndvi": round(latest_ndvi, 4),
            "mean_ndvi": round(mean_ndvi, 4),
            "std_ndvi": round(std_ndvi, 4),
            "trend_slope": round(trend_slope, 6),
            "kendall_tau": round(tau, 4),
            "trend_significant": trend_significant,
            "seasonal_deviation": round(seasonal_deviation, 4),
            "risk_level": risk_level,
            "risk_description": risk_description,
        }

        if z_score is not None:
            result["z_score"] = round(float(z_score), 4)

        return result

    def detect_anomalies(
        self, ndvi_timeseries: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """Detect anomalies in NDVI time series using z-score approach.

        Identifies dates where NDVI is >2 standard deviations below the running mean.
        This indicates potential crop stress events, drought, or disease outbreaks.

        Args:
            ndvi_timeseries: List of dicts with 'date' and 'mean_ndvi' keys

        Returns:
            Dictionary with anomaly dates, severity scores, and summary statistics
        """
        if not ndvi_timeseries:
            return {"error": "No NDVI data provided"}

        # Extract data
        dates = []
        ndvi_values = []
        for entry in ndvi_timeseries:
            if entry.get("mean_ndvi") is not None:
                dates.append(entry.get("date", "unknown"))
                ndvi_values.append(entry["mean_ndvi"])

        if len(ndvi_values) < 3:
            return {"error": "Need at least 3 observations for anomaly detection"}

        arr = np.array(ndvi_values)
        n = len(arr)

        # Compute running statistics (using expanding window)
        anomalies = []
        for i in range(2, n):  # Start after first 2 observations
            # Use all previous observations for mean/std
            window = arr[: i + 1]
            mean = window.mean()
            std = window.std()

            # Z-score for current observation
            z_score = (arr[i] - mean) / (std + 1e-8)

            # Flag as anomaly if >2 std below mean
            if z_score < -2.0:
                severity = "high" if z_score < -3.0 else "moderate"
                anomalies.append(
                    {
                        "date": dates[i],
                        "ndvi": round(float(arr[i]), 4),
                        "expected_ndvi": round(float(mean), 4),
                        "z_score": round(float(z_score), 4),
                        "severity": severity,
                        "deviation_percent": round(
                            float((arr[i] - mean) / mean * 100), 2
                        ),
                    }
                )

        # Summary statistics
        mean_ndvi = float(arr.mean())
        std_ndvi = float(arr.std())
        anomaly_rate = len(anomalies) / n * 100

        return {
            "method": "z_score_anomaly_detection",
            "observations": n,
            "anomalies_detected": len(anomalies),
            "anomaly_rate_percent": round(anomaly_rate, 2),
            "mean_ndvi": round(mean_ndvi, 4),
            "std_ndvi": round(std_ndvi, 4),
            "threshold": "2 standard deviations below running mean",
            "anomalies": anomalies,
        }


    def detect_drought(
        self, ndvi_ndwi_timeseries: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """Detect drought conditions from NDVI and NDWI time series.

        Uses a combined vegetation-water stress index.  NDWI (Normalized
        Difference Water Index) measures canopy water content — values below
        a threshold indicate water stress.  Combined with declining NDVI,
        this provides a drought-specific signal distinct from generic anomalies.

        Args:
            ndvi_ndwi_timeseries: List of dicts with 'date', 'mean_ndvi',
                and optionally 'mean_ndwi' keys.

        Returns:
            Dictionary with drought status, severity, affected periods, and
            a Vegetation Condition Index (VCI).
        """
        if not ndvi_ndwi_timeseries:
            return {"error": "No data provided"}

        dates, ndvi_vals, ndwi_vals = [], [], []
        for entry in ndvi_ndwi_timeseries:
            if entry.get("mean_ndvi") is not None:
                dates.append(entry.get("date", "unknown"))
                ndvi_vals.append(float(entry["mean_ndvi"]))
                ndwi_vals.append(float(entry.get("mean_ndwi", 0.0)))

        if len(ndvi_vals) < 3:
            return {"error": "Need at least 3 observations for drought detection"}

        ndvi = np.array(ndvi_vals)
        ndwi = np.array(ndwi_vals)

        # ── Vegetation Condition Index (VCI) ──
        # VCI = (NDVI_current - NDVI_min) / (NDVI_max - NDVI_min) × 100
        # VCI < 35 → drought, VCI < 20 → severe drought (standard WMO threshold)
        ndvi_min, ndvi_max = float(ndvi.min()), float(ndvi.max())
        ndvi_range = ndvi_max - ndvi_min if ndvi_max != ndvi_min else 1e-8
        vci = ((ndvi[-1] - ndvi_min) / ndvi_range) * 100.0

        # ── Drought severity classification ──
        # Combine VCI with NDWI water-stress indicator
        has_ndwi = np.any(ndwi != 0)
        drought_periods = []

        for i in range(len(ndvi)):
            period_vci = ((ndvi[i] - ndvi_min) / ndvi_range) * 100.0
            water_stressed = ndwi[i] < 0.0 if has_ndwi else False

            if period_vci < 20 or (period_vci < 35 and water_stressed):
                severity = "severe" if period_vci < 20 else "moderate"
                drought_periods.append({
                    "date": dates[i],
                    "vci": round(period_vci, 2),
                    "ndvi": round(float(ndvi[i]), 4),
                    "ndwi": round(float(ndwi[i]), 4) if has_ndwi else None,
                    "severity": severity,
                })
            elif period_vci < 35:
                drought_periods.append({
                    "date": dates[i],
                    "vci": round(period_vci, 2),
                    "ndvi": round(float(ndvi[i]), 4),
                    "ndwi": round(float(ndwi[i]), 4) if has_ndwi else None,
                    "severity": "mild",
                })

        # Overall drought status
        if vci < 20:
            drought_status = "severe_drought"
            description = "VCI < 20 — severe vegetation water deficit"
        elif vci < 35:
            drought_status = "moderate_drought"
            description = "VCI < 35 — moderate vegetation stress"
        elif vci < 50:
            drought_status = "watch"
            description = "VCI 35-50 — below-normal vegetation condition"
        else:
            drought_status = "normal"
            description = "VCI ≥ 50 — adequate vegetation condition"

        return {
            "method": "vci_ndwi_drought",
            "observations": len(ndvi),
            "current_vci": round(vci, 2),
            "drought_status": drought_status,
            "description": description,
            "ndvi_range": {"min": round(ndvi_min, 4), "max": round(ndvi_max, 4)},
            "latest_ndvi": round(float(ndvi[-1]), 4),
            "latest_ndwi": round(float(ndwi[-1]), 4) if has_ndwi else None,
            "drought_periods": drought_periods,
            "drought_period_count": len(drought_periods),
            "drought_rate_percent": round(len(drought_periods) / len(ndvi) * 100, 2),
        }

    def analyze_crop_phenology(
        self, ndvi_timeseries: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """Identify crop growth stages from NDVI phenology curve.

        Analyzes the seasonal NDVI profile to detect phenological stages:
        - Dormant / bare soil: NDVI < 0.2
        - Green-up / emergence: NDVI rising, slope > 0.01
        - Peak vegetative: local maximum, NDVI > 0.5
        - Senescence / ripening: NDVI declining after peak
        - Harvest / post-harvest: NDVI drop > 0.15 in one step

        Uses first-derivative sign changes + absolute thresholds.

        Args:
            ndvi_timeseries: List of dicts with 'date' and 'mean_ndvi' keys

        Returns:
            Dictionary with per-date phenological stage labels, season summary,
            and key inflection dates.
        """
        if not ndvi_timeseries:
            return {"error": "No NDVI data provided"}

        dates, ndvi_vals = [], []
        for entry in ndvi_timeseries:
            if entry.get("mean_ndvi") is not None:
                dates.append(entry.get("date", "unknown"))
                ndvi_vals.append(float(entry["mean_ndvi"]))

        if len(ndvi_vals) < 4:
            return {"error": "Need at least 4 observations for phenology analysis"}

        ndvi = np.array(ndvi_vals)
        n = len(ndvi)

        # First derivative (finite differences)
        dndvi = np.diff(ndvi)

        # Classify each date into a phenological stage
        stages = []
        peak_idx = int(np.argmax(ndvi))
        peak_ndvi = float(ndvi[peak_idx])

        for i in range(n):
            val = float(ndvi[i])

            if val < 0.15:
                stage = "dormant"
            elif i < n - 1 and dndvi[i] > 0.01 and val < peak_ndvi * 0.8:
                stage = "green_up"
            elif i > 0 and i < n - 1:
                # Check if near peak (within 10% of max and derivative near zero)
                if val >= peak_ndvi * 0.9 and abs(dndvi[min(i, n - 2)]) < 0.02:
                    stage = "peak"
                elif i > peak_idx and i < n - 1 and dndvi[min(i, n - 2)] < -0.01:
                    stage = "senescence"
                elif i > 0 and (ndvi[i - 1] - val) > 0.15:
                    stage = "harvest"
                elif dndvi[min(i, n - 2)] > 0.01:
                    stage = "green_up"
                elif dndvi[min(i, n - 2)] < -0.01:
                    stage = "senescence"
                else:
                    stage = "stable"
            elif i == n - 1:
                if i > 0 and (ndvi[i - 1] - val) > 0.15:
                    stage = "harvest"
                elif val >= peak_ndvi * 0.9:
                    stage = "peak"
                elif i > peak_idx:
                    stage = "senescence"
                else:
                    stage = "stable"
            else:
                stage = "stable"

            stages.append({
                "date": dates[i],
                "ndvi": round(val, 4),
                "stage": stage,
            })

        # Identify key inflection points
        green_up_start = None
        peak_date = dates[peak_idx]
        senescence_start = None
        harvest_date = None

        for s in stages:
            if s["stage"] == "green_up" and green_up_start is None:
                green_up_start = s["date"]
            if s["stage"] == "senescence" and senescence_start is None:
                senescence_start = s["date"]
            if s["stage"] == "harvest" and harvest_date is None:
                harvest_date = s["date"]

        # Season length (green_up to senescence/harvest)
        growing_stages = [s for s in stages if s["stage"] in ("green_up", "peak", "stable")]
        season_length = len(growing_stages)

        # Stage distribution
        stage_counts = {}
        for s in stages:
            stage_counts[s["stage"]] = stage_counts.get(s["stage"], 0) + 1

        return {
            "method": "ndvi_phenology_curve",
            "observations": n,
            "peak_ndvi": round(peak_ndvi, 4),
            "peak_date": peak_date,
            "green_up_start": green_up_start,
            "senescence_start": senescence_start,
            "harvest_date": harvest_date,
            "growing_season_observations": season_length,
            "current_stage": stages[-1]["stage"],
            "stage_distribution": stage_counts,
            "stages": stages,
        }


class MLInferenceService:
    """Orchestrates ML inference for Rwanda agriculture."""

    def __init__(self):
        self.crop_classifier = CropClassifier()

    def classify_ndvi(self, ndvi_values: List[float]) -> Dict[str, Any]:
        """Classify land cover from single-band NDVI values."""
        return self.crop_classifier.classify_from_ndvi(ndvi_values)

    def classify_multispectral(self, bands: Dict[str, np.ndarray]) -> Dict[str, Any]:
        """Classify land cover from multispectral satellite bands using KMeans.

        Requires scikit-learn. Uses unsupervised clustering with spectral indices
        (NDVI, NDWI, BSI) to classify land cover types.
        """
        return self.crop_classifier.classify_multispectral(bands)

    def predict_yield_risk(
        self, ndvi_timeseries: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """Predict yield risk from NDVI time series.

        Uses Mann-Kendall trend test with Theil-Sen slope estimator and
        seasonal deviation analysis.
        """
        return self.crop_classifier.predict_yield_risk(ndvi_timeseries)

    def detect_anomalies(
        self, ndvi_timeseries: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """Detect anomalies in NDVI time series using z-score analysis.

        Identifies dates where NDVI drops >2 standard deviations below normal,
        indicating potential crop stress, drought, or disease.
        """
        return self.crop_classifier.detect_anomalies(ndvi_timeseries)

    def detect_drought(
        self, ndvi_ndwi_timeseries: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """Detect drought using VCI + NDWI water stress analysis."""
        return self.crop_classifier.detect_drought(ndvi_ndwi_timeseries)

    def analyze_crop_phenology(
        self, ndvi_timeseries: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """Identify crop growth stages from NDVI phenology curve."""
        return self.crop_classifier.analyze_crop_phenology(ndvi_timeseries)

    def get_status(self) -> Dict[str, Any]:
        """Get status of available ML methods and dependencies."""
        available_methods = [
            "spectral_threshold",
            "mann_kendall_trend",
            "z_score_anomaly",
            "vci_drought",
            "ndvi_phenology",
        ]

        if _SKLEARN_AVAILABLE:
            available_methods.append("kmeans_clustering")

        return {
            "sklearn_available": _SKLEARN_AVAILABLE,
            "ml_ready": ml_available(),
            "available_methods": available_methods,
            "method_descriptions": {
                "spectral_threshold": "NDVI-based land cover classification (standard remote sensing)",
                "kmeans_clustering": "Unsupervised multispectral classification using KMeans",
                "mann_kendall_trend": "Non-parametric trend analysis with Theil-Sen slope",
                "z_score_anomaly": "Statistical anomaly detection in time series",
                "vci_drought": "Vegetation Condition Index + NDWI drought detection (WMO standard)",
                "ndvi_phenology": "Crop growth stage identification from NDVI curve inflection points",
            },
        }


# Singleton
_ml_service: Optional[MLInferenceService] = None


def get_ml_service() -> MLInferenceService:
    global _ml_service
    if _ml_service is None:
        _ml_service = MLInferenceService()
    return _ml_service
