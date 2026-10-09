"""Everything the question cards and Sage know about one drone photo: its analysis, its plots, the vision
model's looks with field checks applied, and the project's farm documents.

One home for this, so a card and Sage answer from the same facts. With `start_jobs`, plot finding and the
vision survey are started (or retried) in the background when they have not run yet.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from src.services import (
    background_jobs,
    drone_cards,
    drone_plots,
    drone_vision,
    farm_records,
    field_checks,
    photo_plots,
)
from src.services.insurance_engine import resolve_audience
from src.structures import async_read_conn

_LONG_URL_SECONDS = 4 * 3600  # the plot search and the survey read the photo for many minutes

# Distinct photos (by uploaded file) in the layer's project whose outline overlaps this layer's.
_PHOTOS_HERE_SQL = """
SELECT count(DISTINCT COALESCE(l.metadata->>'upload_etag', l.layer_id))
  FROM map_layers l
 WHERE l.type = 'raster'
   AND l.bounds IS NOT NULL
   AND l.bounds[1] < $4 AND l.bounds[3] > $2 AND l.bounds[2] < $5 AND l.bounds[4] > $3
   AND l.layer_id IN (
       SELECT unnest(m.layers) FROM user_mundiai_maps m
        WHERE m.soft_deleted_at IS NULL
          AND m.project_id IN (SELECT m2.project_id FROM user_mundiai_maps m2 WHERE $1 = ANY(m2.layers)))
"""

_PROJECT_SQL = """
SELECT m.project_id FROM user_mundiai_maps m
 WHERE $1 = ANY(m.layers) AND m.soft_deleted_at IS NULL
 ORDER BY m.last_edited DESC NULLS LAST LIMIT 1
"""


@dataclass(frozen=True)
class PhotoContext:
    analysis: drone_cards.PhotoAnalysis
    here: drone_cards.Here
    reader: str  # the audience the answers are written for


def is_photo_ready(metadata: dict[str, Any]) -> bool:
    return metadata.get("cog_status") == "ready" and bool(metadata.get("cog_key"))


async def project_of(conn: Any, layer_id: str) -> Optional[str]:
    return await conn.fetchval(_PROJECT_SQL, layer_id)


async def cog_url(s3: Any, bucket: str, metadata: dict[str, Any], seconds: int = 900) -> str:
    return await s3.generate_presigned_url("get_object", Params={"Bucket": bucket, "Key": metadata["cog_key"]},
                                           ExpiresIn=seconds)


async def _found_plots(s3: Any, bucket: str, metadata: dict[str, Any], *, start: bool,
                       retry_failed: bool) -> tuple[Optional[drone_plots.PlotSet], Optional[background_jobs.Job]]:
    key = photo_plots.photo_key(metadata)
    plots = await drone_plots.load_plots(s3, bucket, key)
    job = drone_plots.job(key)
    if start and plots is None and (job is None or (retry_failed and job.state == "failed")):
        job = drone_plots.start_finding(s3, bucket, key, await cog_url(s3, bucket, metadata, _LONG_URL_SECONDS))
    return plots, (None if plots is not None else job)


async def _survey(s3: Any, bucket: str, metadata: dict[str, Any], plots: drone_plots.PlotSet, place: Optional[str], *,
                  scope: str, start: bool,
                  retry_failed: bool) -> tuple[Optional[drone_vision.Survey], Optional[background_jobs.Job]]:
    key = drone_vision.survey_key(photo_plots.photo_key(metadata), plots, scope)
    survey = await drone_vision.load_survey(s3, bucket, key)
    job = drone_vision.job(key)
    if start and survey is None and (job is None or (retry_failed and job.state == "failed")):
        job = drone_vision.start_survey(s3, bucket, key, await cog_url(s3, bucket, metadata, _LONG_URL_SECONDS),
                                        plots, place, scope)
    return survey, (None if survey is not None else job)


async def load(s3: Any, bucket: str, *, layer_id: str, name: str, bounds: Any, metadata: dict[str, Any],
               user_id: str, org_id: Optional[str], audience: Optional[str], start_jobs: bool = True,
               retry_failed: bool = False, seed: int = 0) -> Optional[PhotoContext]:
    """The photo's context, or None when it is not a colour photo the cards can read."""
    url = await cog_url(s3, bucket, metadata)
    row = {"layer_id": layer_id, "name": name, "bounds": bounds, "metadata": metadata}
    async with async_read_conn("drone_cards", user_id=user_id) as conn:
        analysis = await drone_cards.analyse_layer(conn, row, url)
        if analysis is None:
            return None
        west, south, east, north = analysis.bounds
        photos_here = await conn.fetchval(_PHOTOS_HERE_SQL, layer_id, west, south, east, north)
        reader = await resolve_audience(conn, audience, user_id, org_id)
        maps = await photo_plots.plot_maps(conn, layer_id, analysis.bounds)
        project_id = await project_of(conn, layer_id)
    records = await farm_records.load_records(s3, bucket, project_id) if project_id else []
    plots, plot_map, map_error = await photo_plots.plots_from_chosen_map(
        s3, bucket, layer_id, metadata, user_id, url, maps)
    plot_job = None
    if plots is None:
        plots, plot_job = await _found_plots(s3, bucket, metadata, start=start_jobs, retry_failed=retry_failed)
    survey, survey_job = None, None
    if plots is not None:
        survey, survey_job = await _survey(s3, bucket, metadata, plots, analysis.look.place,
                                           scope=drone_vision.reference_scope(org_id, user_id), start=start_jobs,
                                           retry_failed=retry_failed)
    checks = (await field_checks.load_checks(s3, bucket, project_id,
                                             field_checks.plot_set_id(photo_plots.photo_key(metadata), plots))
              if plots is not None else {})
    here = drone_cards.Here(photos=max(1, int(photos_here or 0)), plots=plots, plot_job=plot_job,
                            plot_maps=tuple(m for m, _ in maps), plot_map=plot_map, plot_map_error=map_error,
                            survey=field_checks.apply(survey, checks), survey_job=survey_job, seed=seed,
                            records=tuple(records), checked=frozenset(checks),
                            record=field_checks.model_record(survey, checks) if checks else None)
    return PhotoContext(analysis=analysis, here=here, reader=reader)
