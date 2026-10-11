"""Everything the question cards and Sage know about one drone photo: its analysis, its plots, the vision
model's looks with field checks applied, and the project's farm documents.

One home for this, so a card and Sage answer from the same facts. With `start_jobs`, plot finding and the
vision survey are started (or retried) in the background when they have not run yet; the survey only while
DRONE_VISION_SURVEY is on (drone_vision.survey_enabled).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from src.services import (
    background_jobs,
    crop_fingerprints,
    drone_cards,
    drone_plots,
    drone_vision,
    farm_records,
    field_checks,
    photo_plots,
)
from src.services.insurance_engine import resolve_audience
from src.services.remote_cog_layer import validate_remote_cog_bounds
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

# A project's current map is the last of user_mundiai_projects.maps; its earlier versions (removing a layer forks a
# new map and keeps the old one) and deleted projects do not count.
_PROJECTS_SQL = """
SELECT p.id AS project_id FROM user_mundiai_projects p
  JOIN user_mundiai_maps m ON m.id = p.maps[array_length(p.maps, 1)]
 WHERE $1 = ANY(m.layers) AND m.soft_deleted_at IS NULL AND p.soft_deleted_at IS NULL
 ORDER BY p.id
"""


@dataclass(frozen=True)
class PhotoContext:
    analysis: drone_cards.PhotoAnalysis
    here: drone_cards.Here
    reader: str  # the audience the answers are written for


def is_photo_ready(metadata: dict[str, Any]) -> bool:
    return metadata.get("cog_status") == "ready" and bool(metadata.get("cog_key"))


def is_georeferenced(metadata: dict[str, Any], bounds: Any) -> bool:
    """Whether the photo has a place on the map. The upload marks a file with no coordinate system (`crs_missing`:
    a phone picture of a mast); its outline is then its pixel corners, so the outline must also be an ordered
    WGS84 box (the remote-COG layers' rule)."""
    if metadata.get("crs_missing"):
        return False
    try:
        validate_remote_cog_bounds(list(bounds or ()))
    except (TypeError, ValueError):
        return False
    return True


async def project_of(conn: Any, layer_id: str) -> Optional[str]:
    return await conn.fetchval(_PROJECT_SQL, layer_id)


async def projects_of(conn: Any, layer_id: str) -> list[str]:
    """Every live project whose current map shows the layer (as `conn` may see them): more than one when the layer
    was added to another project's map and is still on it."""
    return [row["project_id"] for row in await conn.fetch(_PROJECTS_SQL, layer_id)]


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


async def _fingerprints(s3: Any, bucket: str, metadata: dict[str, Any], plots: drone_plots.PlotSet, *, start: bool,
                        retry_failed: bool) -> tuple[Optional[dict[int, Any]], Optional[background_jobs.Job]]:
    """The plots' fingerprints when kept, else the job making them (running or failed; None when not started). With
    `start` they are started in the background (minutes per photo), and a failed run again with `retry_failed`."""
    key = crop_fingerprints.store_key(photo_plots.photo_key(metadata), plots)
    prints = await crop_fingerprints.load(s3, bucket, key)
    job = crop_fingerprints.job(key)
    if start and prints is None and (job is None or (retry_failed and job.state == "failed")):
        job = crop_fingerprints.start(s3, bucket, key, metadata["cog_key"], plots)
    return prints, (None if prints is not None else job)


def _picture_record(looked: Optional[drone_vision.Survey], examples: dict[int, str],
                    checks: dict[int, field_checks.Check]) -> Optional[field_checks.Record]:
    """How the crop calls agree with the plots named from the pictures (ground checks left out: they have their own
    record). The calls on those plots were made with their own names hidden."""
    named = {n: field_checks.Check(number=n, crop=c, at="") for n, c in examples.items() if n not in checks}
    return field_checks.model_record(looked, named) if named else None


@dataclass(frozen=True)
class PlotLooks:
    """What is seen in each plot of one plot set, as the cards and Sage both take it: the vision looks, with each
    plot's crop from its fingerprint call and then from the ground checks, and how the calls did against both."""

    survey: Optional[drone_vision.Survey] = None  # with crop calls and checks applied; None until plots are looked at
    survey_job: Optional[background_jobs.Job] = None
    checked: frozenset[int] = frozenset()  # plots whose crop someone checked on the ground
    record: Optional[field_checks.Record] = None  # how the calls did on the checked plots
    picture_record: Optional[field_checks.Record] = None  # how they agree with the plots named from the pictures
    # Plots with a kept fingerprint (None: none kept yet; 0: kept, but no plot holds a whole square), and the job
    # making them while none are kept. With the survey off they are how a plot nobody checked gets a crop.
    fingerprinted: Optional[int] = None
    fingerprint_job: Optional[background_jobs.Job] = None


async def plot_looks(s3: Any, bucket: str, metadata: dict[str, Any], plots: drone_plots.PlotSet,
                     project_id: Optional[str], *, place: Optional[str], scope: str, start: bool,
                     retry_failed: bool) -> PlotLooks:
    """The looks at a plot set, the one way the cards and Sage take a plot's crop. With `start`, the vision survey
    and the fingerprints are started in the background when they have not run yet. With the survey switched off
    (drone_vision.survey_enabled), no plot is looked at: the crops come from the fingerprint calls and the checks,
    and until fingerprints are kept, their job says how naming the other plots stands."""
    if drone_vision.survey_enabled():
        survey, survey_job = await _survey(s3, bucket, metadata, plots, place, scope=scope, start=start,
                                           retry_failed=retry_failed)
    else:
        survey, survey_job = drone_vision.no_looks(plots), None
    plot_set = field_checks.plot_set_id(photo_plots.photo_key(metadata), plots)
    checks = await field_checks.load_checks(s3, bucket, project_id, plot_set)
    # The crop of each plot comes from its fingerprint and the checked plots when there are any; the looks
    # keep stage, weeds and problems. Checks then override both, and the record compares the calls (each made
    # with its own check hidden) with the checks.
    prints, fingerprint_job = await _fingerprints(s3, bucket, metadata, plots, start=start, retry_failed=retry_failed)
    examples = await crop_fingerprints.load_examples(s3, bucket, project_id, plot_set)
    looked = crop_fingerprints.looked(survey, prints, {**examples, **{n: c.crop for n, c in checks.items()}})
    return PlotLooks(survey=field_checks.apply(looked, checks), survey_job=survey_job, checked=frozenset(checks),
                     record=field_checks.model_record(looked, checks) if checks else None,
                     picture_record=_picture_record(looked, examples, checks) if prints else None,
                     fingerprinted=None if prints is None else len(prints), fingerprint_job=fingerprint_job)


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
    looks = (await plot_looks(s3, bucket, metadata, plots, project_id, place=analysis.look.place,
                              scope=drone_vision.reference_scope(org_id, user_id), start=start_jobs,
                              retry_failed=retry_failed)
             if plots is not None else PlotLooks())
    here = drone_cards.Here(photos=max(1, int(photos_here or 0)), plots=plots, plot_job=plot_job,
                            plot_maps=tuple(m for m, _ in maps), plot_map=plot_map, plot_map_error=map_error,
                            survey=looks.survey, survey_job=looks.survey_job,
                            vision_looks=drone_vision.survey_enabled(), seed=seed,
                            records=tuple(records), checked=looks.checked, record=looks.record,
                            picture_record=looks.picture_record, fingerprinted=looks.fingerprinted,
                            fingerprint_job=looks.fingerprint_job)
    return PhotoContext(analysis=analysis, here=here, reader=reader)
