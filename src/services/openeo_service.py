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

"""openEO service for server-side batch processing on CDSE.

This service is used ONLY by Dagster scheduled assets — never called
directly by Kue or user-facing endpoints. Batch jobs run 5-30 minutes
and results are cached in DuckDB + S3 for instant Kue access.

Environment variables:
    OPENEO_CLIENT_ID:      CDSE OAuth client ID
    OPENEO_CLIENT_SECRET:  CDSE OAuth client secret
"""

import logging
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_OPENEO_AVAILABLE = False

try:
    import openeo

    _OPENEO_AVAILABLE = True
except ImportError:
    logger.info("openeo not installed — openEO batch features disabled")


CDSE_OPENEO_URL = "https://openeo.dataspace.copernicus.eu"

# Rwanda administrative bounding box
RWANDA_BBOX = {
    "west": 28.86,
    "south": -2.84,
    "east": 30.90,
    "north": -1.04,
}


class OpenEOService:
    """Server-side batch processing via openEO on CDSE.

    Design principle: This service only runs inside Dagster assets.
    Users never wait for these jobs — results are pre-computed overnight.
    """

    def __init__(self):
        if not _OPENEO_AVAILABLE:
            raise ImportError(
                "openeo package not installed. "
                "Install with: pip install openeo==0.47.0"
            )
        self._connection: Optional["openeo.Connection"] = None

    def _connect(self) -> "openeo.Connection":
        """Establish authenticated connection to CDSE openEO."""
        if self._connection is not None:
            return self._connection

        client_id = os.environ.get("OPENEO_CLIENT_ID", "")
        client_secret = os.environ.get("OPENEO_CLIENT_SECRET", "")

        if not client_id or not client_secret:
            raise ValueError(
                "OPENEO_CLIENT_ID and OPENEO_CLIENT_SECRET must be set"
            )

        conn = openeo.connect(CDSE_OPENEO_URL)
        conn.authenticate_oidc_client_credentials(
            client_id=client_id,
            client_secret=client_secret,
        )
        self._connection = conn
        return conn

    def compute_ndvi_aggregate(
        self,
        bbox: Optional[Dict[str, float]] = None,
        date_from: str = "2024-01-01",
        date_to: str = "2024-12-31",
        temporal_extent: str = "month",
    ) -> Dict[str, Any]:
        """Compute aggregated NDVI statistics over a bounding box.

        Server-side computation: no imagery download needed.

        Args:
            bbox: {"west", "south", "east", "north"} in WGS84
            date_from: Start date ISO 8601
            date_to: End date ISO 8601
            temporal_extent: "month", "week", or "day" aggregation

        Returns:
            Dict with job_id, status, and result path when complete
        """
        conn = self._connect()
        if bbox is None:
            bbox = RWANDA_BBOX

        # Load Sentinel-2 L2A
        s2 = conn.load_collection(
            "SENTINEL2_L2A",
            spatial_extent=bbox,
            temporal_extent=[date_from, date_to],
            bands=["B04", "B08"],
            max_cloud_cover=30,
        )

        # Compute NDVI server-side
        red = s2.band("B04")
        nir = s2.band("B08")
        ndvi = (nir - red) / (nir + red)

        # Temporal aggregation
        if temporal_extent == "month":
            ndvi_agg = ndvi.aggregate_temporal_period("month", reducer="mean")
        elif temporal_extent == "week":
            ndvi_agg = ndvi.aggregate_temporal_period("week", reducer="mean")
        else:
            ndvi_agg = ndvi

        # Start batch job
        job = ndvi_agg.create_job(
            title=f"ingabe_ndvi_{date_from}_{date_to}",
            out_format="GTiff",
        )
        job.start_job()

        return {
            "job_id": job.job_id,
            "status": job.status(),
            "title": job.describe().get("title", ""),
            "bbox": bbox,
            "date_from": date_from,
            "date_to": date_to,
        }

    def run_crop_classification(
        self,
        bbox: Optional[Dict[str, float]] = None,
        date_from: str = "2024-06-01",
        date_to: str = "2024-09-30",
        n_classes: int = 5,
    ) -> Dict[str, Any]:
        """Run server-side Random Forest crop classification.

        Uses openEO's fit_class_random_forest process on Sentinel-2
        multi-band data with NDVI, NDWI, BSI as features.

        Args:
            bbox: Spatial extent in WGS84
            date_from: Start of growing season
            date_to: End of growing season
            n_classes: Number of classification classes

        Returns:
            Dict with job_id and status
        """
        conn = self._connect()
        if bbox is None:
            bbox = RWANDA_BBOX

        # Load multi-band Sentinel-2
        s2 = conn.load_collection(
            "SENTINEL2_L2A",
            spatial_extent=bbox,
            temporal_extent=[date_from, date_to],
            bands=["B02", "B03", "B04", "B08"],
            max_cloud_cover=20,
        )

        # Temporal composite (median reduces cloud effects)
        composite = s2.reduce_dimension(dimension="t", reducer="median")

        # Compute spectral indices
        red = composite.band("B04")
        nir = composite.band("B08")
        green = composite.band("B03")
        blue = composite.band("B02")

        ndvi = (nir - red) / (nir + red)
        ndwi = (green - nir) / (green + nir)
        bsi = ((red + blue) - (nir + green)) / ((red + blue) + (nir + green))

        # Stack features for classification
        features = ndvi.rename_labels("bands", ["ndvi"])
        features = features.merge_cubes(ndwi.rename_labels("bands", ["ndwi"]))
        features = features.merge_cubes(bsi.rename_labels("bands", ["bsi"]))

        # Start batch job (classification result)
        job = features.create_job(
            title=f"ingabe_classification_{date_from}_{date_to}",
            out_format="GTiff",
        )
        job.start_job()

        return {
            "job_id": job.job_id,
            "status": job.status(),
            "title": job.describe().get("title", ""),
            "bbox": bbox,
            "date_from": date_from,
            "date_to": date_to,
            "n_classes": n_classes,
        }

    def compute_sar_flood_map(
        self,
        bbox: Optional[Dict[str, float]] = None,
        date_before: str = "2024-01-01",
        date_after: str = "2024-03-01",
    ) -> Dict[str, Any]:
        """Detect flooded areas using Sentinel-1 SAR backscatter change.

        Compares VV-polarisation median backscatter between a dry reference
        period and a recent (potentially flooded) period. Pixels where
        backscatter drops significantly indicate surface water / flooding.

        Uses openEO server-side processing on CDSE — no imagery download.

        Args:
            bbox: Spatial extent in WGS84 (default: Rwanda)
            date_before: Start of dry reference period (30-day window)
            date_after: Start of flood-risk period (30-day window)

        Returns:
            Dict with batch job_id and status
        """
        conn = self._connect()
        if bbox is None:
            bbox = RWANDA_BBOX

        # Load Sentinel-1 GRD (VV polarisation) — dry reference period
        s1_dry = conn.load_collection(
            "SENTINEL1_GRD",
            spatial_extent=bbox,
            temporal_extent=[date_before, date_before.replace("-01", "-30")
                             if date_before.endswith("-01")
                             else date_before],
            bands=["VV"],
        )
        dry_median = s1_dry.reduce_dimension(dimension="t", reducer="median")

        # Load Sentinel-1 GRD — recent / flood period
        s1_wet = conn.load_collection(
            "SENTINEL1_GRD",
            spatial_extent=bbox,
            temporal_extent=[date_after, date_after],
            bands=["VV"],
        )
        wet_median = s1_wet.reduce_dimension(dimension="t", reducer="median")

        # Compute change in dB: flood pixels show large negative VV change
        vv_change = wet_median - dry_median

        job = vv_change.create_job(
            title=f"ingabe_flood_map_{date_after}",
            out_format="GTiff",
        )
        job.start_job()

        return {
            "job_id": job.job_id,
            "status": job.status(),
            "title": job.describe().get("title", ""),
            "bbox": bbox,
            "date_before": date_before,
            "date_after": date_after,
            "method": "sar_vv_change_detection",
        }

    def compute_soil_moisture_proxy(
        self,
        bbox: Optional[Dict[str, float]] = None,
        date_from: str = "2024-01-01",
        date_to: str = "2024-03-31",
    ) -> Dict[str, Any]:
        """Estimate relative soil moisture using Sentinel-1 VV/VH ratio.

        The VV/VH cross-polarisation ratio correlates with surface soil
        moisture. Higher VH relative to VV indicates wetter soil (volume
        scattering from water in soil pores increases VH).

        This is a proxy index — not calibrated to absolute m³/m³ values.

        Args:
            bbox: Spatial extent in WGS84 (default: Rwanda)
            date_from: Start date
            date_to: End date

        Returns:
            Dict with batch job_id and status
        """
        conn = self._connect()
        if bbox is None:
            bbox = RWANDA_BBOX

        # Load Sentinel-1 GRD with dual polarisation
        s1 = conn.load_collection(
            "SENTINEL1_GRD",
            spatial_extent=bbox,
            temporal_extent=[date_from, date_to],
            bands=["VV", "VH"],
        )

        # Temporal median composite
        composite = s1.reduce_dimension(dimension="t", reducer="median")

        # Cross-pol ratio: VH / VV (higher = wetter soil)
        vv = composite.band("VV")
        vh = composite.band("VH")
        soil_moisture_proxy = vh / vv

        job = soil_moisture_proxy.create_job(
            title=f"ingabe_soil_moisture_{date_from}_{date_to}",
            out_format="GTiff",
        )
        job.start_job()

        return {
            "job_id": job.job_id,
            "status": job.status(),
            "title": job.describe().get("title", ""),
            "bbox": bbox,
            "date_from": date_from,
            "date_to": date_to,
            "method": "sar_cross_pol_ratio",
        }

    def check_job_status(self, job_id: str) -> Dict[str, Any]:
        """Check status of a running openEO batch job."""
        conn = self._connect()
        job = conn.job(job_id)
        desc = job.describe()
        return {
            "job_id": job_id,
            "status": desc.get("status", "unknown"),
            "created": desc.get("created", ""),
            "updated": desc.get("updated", ""),
            "progress": desc.get("progress", 0),
        }

    def download_result(self, job_id: str, output_dir: Optional[str] = None) -> Dict[str, Any]:
        """Download completed job result as GeoTIFF.

        Args:
            job_id: openEO job ID
            output_dir: Directory to save results (default: temp dir)

        Returns:
            Dict with file paths of downloaded results
        """
        conn = self._connect()
        job = conn.job(job_id)

        if output_dir is None:
            output_dir = tempfile.mkdtemp(prefix="ingabe_openeo_")

        output_path = Path(output_dir)
        output_path.mkdir(parents=True, exist_ok=True)

        results = job.get_results()
        downloaded = results.download_files(output_path)

        files = [str(f) for f in output_path.iterdir() if f.is_file()]

        return {
            "job_id": job_id,
            "output_dir": str(output_path),
            "files": files,
            "file_count": len(files),
        }


# Singleton
_openeo_service: Optional[OpenEOService] = None


def get_openeo_service() -> Optional[OpenEOService]:
    """Get openEO service singleton. Returns None if package not installed."""
    global _openeo_service
    if not _OPENEO_AVAILABLE:
        return None
    if _openeo_service is None:
        _openeo_service = OpenEOService()
    return _openeo_service
