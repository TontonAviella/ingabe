"""Everything the question cards and Sage know about one drone photo: its analysis, its plots, the vision
model's looks with field checks applied, and the project's farm documents.

One home for this, so a card and Sage answer from the same facts. With `start_jobs`, plot finding and the
vision survey are started (or retried) in the background when they have not run yet; the survey only while
DRONE_VISION_SURVEY is on (drone_vision.survey_enabled).
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any, Optional

from src.services import (
    background_jobs,
    crop_fingerprints,
    drone_cards,
    drone_plots,
    drone_vision,
    farm_records,
    record_links,
    field_checks,
    field_facts,
    field_flights,
    field_health,
    photo_plots,
    reader_roles,
    satellite_view,
)
from src.services.remote_cog_layer import validate_remote_cog_bounds
from src.services.shown_maps import SHOWN_MAPS_SQL
from src.structures import async_read_conn

logger = logging.getLogger(__name__)

LONG_URL_SECONDS = 4 * 3600  # the plot search and the survey read the photo for many minutes

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

# Other photos in the layer's projects (their shown maps only) whose outline overlaps this layer's, newest first:
# where a thermal photo, band files or a multispectral file uploaded beside a colour photo are looked for
# (field_health.with_thermal, with_bands, with_spectral). Each says how many bands it has.
_ONE_BAND_PHOTOS_HERE_SQL = f"""
SELECT l.layer_id, l.name, l.metadata, l.bounds, l.created_on,
       ABS(EXTRACT(EPOCH FROM (l.created_on - (SELECT o.created_on FROM map_layers o WHERE o.layer_id = $1)))) / 3600
           AS hours_from_photo
  FROM map_layers l
 WHERE l.type = 'raster'
   AND l.layer_id <> $1
   AND l.bounds IS NOT NULL
   AND l.bounds[1] < $4 AND l.bounds[3] > $2 AND l.bounds[2] < $5 AND l.bounds[4] > $3
   AND l.metadata->>'cog_key' IS NOT NULL
   AND l.metadata->>'cog_status' = 'ready'
   AND COALESCE(l.metadata->>'crs_missing', 'false') <> 'true'
   AND CASE WHEN (l.metadata->>'band_count') ~ '^[0-9]+$'
            THEN (l.metadata->>'band_count')::int = 1 OR (l.metadata->>'band_count')::int >= 4
            ELSE false END
   AND l.layer_id IN (
       SELECT unnest(s.layers) FROM ({SHOWN_MAPS_SQL}) s
        WHERE s.project_id IN (SELECT s2.project_id FROM ({SHOWN_MAPS_SQL}) s2 WHERE $1 = ANY(s2.layers)))
 ORDER BY l.created_on DESC
 LIMIT 60
"""

# The other pictures of this layer's place in its projects (their shown maps only), newest first: the other flights a
# field is followed across (field_flights). Three bands or more; one-band files are a flight's parts, not a flight.
_FLIGHTS_HERE_SQL = f"""
SELECT l.layer_id, l.name, l.metadata, l.bounds, l.created_on
  FROM map_layers l
 WHERE l.type = 'raster'
   AND l.layer_id <> $1
   AND l.bounds IS NOT NULL
   AND l.bounds[1] < $4 AND l.bounds[3] > $2 AND l.bounds[2] < $5 AND l.bounds[4] > $3
   AND l.metadata->>'cog_key' IS NOT NULL
   AND l.metadata->>'cog_status' = 'ready'
   AND COALESCE(l.metadata->>'crs_missing', 'false') <> 'true'
   AND CASE WHEN (l.metadata->>'band_count') ~ '^[0-9]+$' THEN (l.metadata->>'band_count')::int >= 3 ELSE false END
   AND l.layer_id IN (
       SELECT unnest(s.layers) FROM ({SHOWN_MAPS_SQL}) s
        WHERE s.project_id IN (SELECT s2.project_id FROM ({SHOWN_MAPS_SQL}) s2 WHERE $1 = ANY(s2.layers)))
 ORDER BY l.created_on DESC
 LIMIT 40
"""

_PROJECT_SQL = """
SELECT m.project_id FROM user_mundiai_maps m
 WHERE $1 = ANY(m.layers) AND m.soft_deleted_at IS NULL
 ORDER BY m.last_edited DESC NULLS LAST LIMIT 1
"""

# The live projects whose shown maps (shown_maps: the latest of each line of versions) list the layer.
_PROJECTS_SQL = f"""
SELECT DISTINCT s.project_id FROM ({SHOWN_MAPS_SQL}) s
 WHERE $1 = ANY(s.layers)
 ORDER BY s.project_id
"""


@dataclass(frozen=True)
class PhotoContext:
    analysis: drone_cards.PhotoAnalysis
    here: drone_cards.Here
    reader: str  # the reader's role (reader_roles): the questions are worded for it, the answers in its voice


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


async def one_band_photos_here(conn: Any, layer_id: str, bounds: Any,
                               own_cog_key: Optional[str] = None) -> list[dict[str, Any]]:
    """The project's other photos over this layer's place ({"name", "metadata", "bounds", "uploaded", "bands"}),
    newest first, as `conn` may see them: one-band files (a thermal photo, band files) and files of four bands or
    more (a multispectral photo beside the colour one). Ordinary three-band pictures are left out. [] when the
    layer has no bounds."""
    if not bounds or len(bounds) != 4:
        return []
    west, south, east, north = bounds
    rows = await conn.fetch(_ONE_BAND_PHOTOS_HERE_SQL, layer_id, west, south, east, north)
    out: list[dict[str, Any]] = []
    seen: set[Any] = {own_cog_key}  # a copy of this very photo under another layer is not another photo
    for row in rows:
        metadata = row["metadata"]
        metadata = json.loads(metadata) if isinstance(metadata, str) else dict(metadata or {})
        if metadata.get("cog_key") in seen:  # copies of one layer are one photo
            continue
        seen.add(metadata.get("cog_key"))
        bands = int(metadata.get("band_count") or 0)
        if bands == 1 or bands >= 4:
            out.append({"name": row["name"], "metadata": metadata, "bounds": list(row["bounds"] or []),
                        "uploaded": row["created_on"], "bands": bands,
                        "hours_from_photo": (float(row["hours_from_photo"])
                                             if row["hours_from_photo"] is not None else None)})
    return out


async def flights_here(conn: Any, layer_id: str, bounds: Any,
                       own_cog_key: Optional[str] = None) -> tuple[Any, list[dict[str, Any]]]:
    """(When this layer was added, the project's other pictures over its place as `conn` may see them, newest
    first: {"name", "metadata", "bounds", "uploaded", "bands"}). Copies of one stored photo are one photo, and a
    copy of this very photo is not another flight. (None, []) when the layer has no bounds."""
    if not bounds or len(bounds) != 4:
        return None, []
    west, south, east, north = bounds
    own = await conn.fetchval("SELECT created_on FROM map_layers WHERE layer_id = $1", layer_id)
    out: list[dict[str, Any]] = []
    own_metadata = await conn.fetchval("SELECT metadata FROM map_layers WHERE layer_id = $1", layer_id)
    own_metadata = json.loads(own_metadata) if isinstance(own_metadata, str) else dict(own_metadata or {})
    # this photo, and the file a named-bands copy of it was made from, are not other flights
    seen: set[Any] = {k for k in (own_cog_key, own_metadata.get("cog_key"), own_metadata.get("cog_key_before_names"),
                                  own_metadata.get("upload_etag")) if k}
    for row in await conn.fetch(_FLIGHTS_HERE_SQL, layer_id, west, south, east, north):
        metadata = row["metadata"]
        metadata = json.loads(metadata) if isinstance(metadata, str) else dict(metadata or {})
        # a copy the reader named the bands of is still the same flight as the file it was made from
        marks = [k for k in (metadata.get("cog_key"), metadata.get("cog_key_before_names"), metadata.get("upload_etag")) if k]
        if not metadata.get("cog_key") or any(mark in seen for mark in marks):  # the same file added twice is one photo
            continue
        seen.update(marks)
        out.append({"name": row["name"], "metadata": metadata, "bounds": list(row["bounds"] or []),
                    "uploaded": row["created_on"], "bands": int(metadata.get("band_count") or 0)})
    return own, out


def satellite_job_key(layer_id: str) -> str:
    """The background job that makes a photo's satellite view when its cards are opened before the night's run."""
    return f"satellite-view:{layer_id}"


async def satellite_of_fields(bounds: Any, features: list[dict[str, Any]]
                              ) -> dict[str, tuple[Optional[str], Optional[str]]]:
    """What the photo's kept satellite view says of each field outline, by field_health.key_of; {} when no view is
    kept yet or it cannot be read (the fields are then said without it). Reads no satellite and starts no work."""
    if not bounds or not features:
        return {}
    try:
        outlines = {field_health.key_of(f["geometry"]): f["geometry"] for f in features}
        return await asyncio.to_thread(satellite_view.of_fields, [float(v) for v in bounds], outlines)
    except Exception:
        logger.exception("photo context: the satellite figures of the fields could not be read")
        return {}


async def long_cog_url(s3: Any, bucket: str, metadata: dict[str, Any]) -> str:
    """A link to a photo that lasts as long as a measurement beside the request may run."""
    return await cog_url(s3, bucket, metadata, LONG_URL_SECONDS)


async def project_of(conn: Any, layer_id: str) -> Optional[str]:
    return await conn.fetchval(_PROJECT_SQL, layer_id)


async def projects_of(conn: Any, layer_id: str) -> list[str]:
    """Every live project whose shown maps (shown_maps) list the layer, as `conn` may see them: more than one when the
    layer was added to another project's map and is still on it."""
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
        job = drone_plots.start_finding(s3, bucket, key, await cog_url(s3, bucket, metadata, LONG_URL_SECONDS))
    return plots, (None if plots is not None else job)


async def _survey(s3: Any, bucket: str, metadata: dict[str, Any], plots: drone_plots.PlotSet, place: Optional[str], *,
                  scope: str, start: bool,
                  retry_failed: bool) -> tuple[Optional[drone_vision.Survey], Optional[background_jobs.Job]]:
    key = drone_vision.survey_key(photo_plots.photo_key(metadata), plots, scope)
    survey = await drone_vision.load_survey(s3, bucket, key)
    job = drone_vision.job(key)
    if start and survey is None and (job is None or (retry_failed and job.state == "failed")):
        job = drone_vision.start_survey(s3, bucket, key, await cog_url(s3, bucket, metadata, LONG_URL_SECONDS),
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
                     retry_failed: bool, layer_id: Optional[str] = None) -> PlotLooks:
    """The looks at a plot set, the one way the cards and Sage take a plot's crop. With `start`, the vision survey
    and the fingerprints are started in the background when they have not run yet. With the survey switched off
    (drone_vision.survey_enabled), no plot is looked at: the crops come from the fingerprint calls and the checks,
    and until fingerprints are kept, their job says how naming the other plots stands."""
    if drone_vision.survey_enabled():
        survey, survey_job = await _survey(s3, bucket, metadata, plots, place, scope=scope, start=start,
                                           retry_failed=retry_failed)
    else:
        survey, survey_job = drone_vision.no_looks(plots), None
    plot_set = field_checks.checks_set_id(photo_plots.photo_key(metadata), plots, layer_id)
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
        reader = await reader_roles.role_in(conn, "agriculture", audience, user_id, org_id)
        maps = await photo_plots.plot_sources(conn, s3, bucket, layer_id, analysis.bounds, metadata)
        project_id = await project_of(conn, layer_id)
        thermal_photos: list[dict[str, Any]] = []
        try:
            thermal_photos = await one_band_photos_here(conn, layer_id, analysis.bounds, metadata.get("cog_key"))
        except Exception:  # the cards still answer, without a temperature from another photo
            logger.exception("photo context: the thermal photos of layer %s's place could not be listed", layer_id)
        added, other_flights = None, []
        try:
            added, other_flights = await flights_here(conn, layer_id, analysis.bounds, metadata.get("cog_key"))
        except Exception:  # the cards still answer, each field on this photo alone
            logger.exception("photo context: the other flights of layer %s's place could not be listed", layer_id)
    records = await farm_records.load_records(s3, bucket, project_id) if project_id else []
    plots, plot_map, map_error = await photo_plots.plots_from_chosen_map(
        s3, bucket, layer_id, metadata, user_id, url, maps)
    # Finding plots, fingerprints and looks all need a true-colour picture; a photo without one starts none of them.
    start_jobs = start_jobs and analysis.look.bands.picture
    plot_job = None
    if plots is None:
        plots, plot_job = await _found_plots(s3, bucket, metadata, start=start_jobs, retry_failed=retry_failed)
    looks = (await plot_looks(s3, bucket, metadata, plots, project_id, place=analysis.look.place,
                              scope=drone_vision.reference_scope(org_id, user_id), start=start_jobs,
                              retry_failed=retry_failed, layer_id=layer_id)
             if plots is not None else PlotLooks())
    # the documents' rows on the fields they belong to, by place: read here once, for the cards, Sage and the fields
    linked: list[record_links.Linked] = []
    if project_id and records:
        try:
            linked = record_links.resolve(records, await record_links.load(s3, bucket, project_id),
                                          plots.geojson["features"] if plots is not None else [])
        except Exception:  # the cards still answer; the records are then shown as not linked to a field
            logger.exception("photo context: the documents could not be placed on the fields of layer %s", layer_id)
            linked = record_links.resolve(records, {}, [])
    fields: list[field_facts.FieldFacts] = []
    zones: Optional[dict[str, Any]] = None  # the fields' zones from a near-infrared photo, once measured
    if plots is not None:
        try:
            health: dict[str, field_health.FieldHealth] = {}
            if analysis.look.bands.nir is not None or analysis.look.bands.thermal is not None:  # else nothing to measure
                try:
                    health = await field_health.of_features(
                        s3, bucket, photo_plots.photo_key(metadata),
                        await cog_url(s3, bucket, metadata, LONG_URL_SECONDS),  # measured beside the request
                        plots.geojson["features"])
                except Exception:  # the fields are still said, without their health index
                    logger.exception("photo context: the fields' health could not be measured for layer %s", layer_id)
            if thermal_photos:  # the bands of a flight uploaded one band a file
                health = await field_health.with_bands(
                    s3, bucket, health, plots.geojson["features"], thermal_photos,
                    lambda companion: long_cog_url(s3, bucket, companion))
                health = await field_health.with_spectral(
                    s3, bucket, health, plots.geojson["features"], thermal_photos,
                    lambda companion: long_cog_url(s3, bucket, companion),
                    # a colour photo is known to have no bands of its own to wait for
                    photo_plots.photo_key(metadata)
                    if analysis.look.bands.nir is not None or analysis.look.bands.thermal is not None else None)
            if thermal_photos:  # a thermal photo of this place flown as its own file
                health = await field_health.with_thermal(
                    s3, bucket, health, plots.geojson["features"], thermal_photos,
                    lambda companion: long_cog_url(s3, bucket, companion))
            flights: dict[str, list[dict[str, Any]]] = {}
            this_photo = {"name": name, "metadata": metadata, "uploaded": added}
            apart, _ = field_flights.other_flights(this_photo, other_flights)
            if apart:  # the same outlines on the other flights of this place
                try:
                    flights = await field_flights.of_features(
                        s3, bucket, plots.geojson["features"], apart,
                        lambda companion: long_cog_url(s3, bucket, companion))
                except Exception:
                    logger.exception("photo context: the other flights could not be read for layer %s", layer_id)
            fields = await field_facts.of_plots(s3, bucket, photo_plots.photo_key(metadata), layer_id, plots,
                                                record_links.by_field(linked), health, flights, this_photo,
                                                await satellite_of_fields(bounds, plots.geojson["features"]))
            of_outlined = field_facts.outlined(plots)
            zoned = [(f["properties"]["number"],
                      field_facts.name(f["properties"]["number"], of_outlined, field_facts.own_name(f["properties"])),
                      health[field_health.key_of(f["geometry"])])
                     for f in plots.geojson["features"] if field_health.key_of(f["geometry"]) in health]
            if any(h.zones is not None for _, _, h in zoned):
                zones = {"geojson": field_health.zone_features(zoned),
                         "bare_m2": sum(h.bare_m2 or 0 for _, _, h in zoned),
                         "even": sum(1 for _, _, h in zoned if h.zones == ()),
                         "zoned": sum(1 for _, _, h in zoned if h.zones),
                         "by": next((h.zone_index for _, _, h in zoned if h.zone_index), None)}
        except Exception:  # the cards still answer, without the per-field facts gathered from the other surfaces
            logger.exception("photo context: the fields' facts could not be read for layer %s", layer_id)
    satellite = None
    if bounds:
        try:  # the view already kept for this photo: a small file read, no satellite
            satellite = await asyncio.to_thread(satellite_view.ready, [float(v) for v in bounds])
        except Exception:  # the other cards still answer; this one says it is not ready
            logger.exception("photo context: the kept satellite view of layer %s could not be read", layer_id)
    here = drone_cards.Here(satellite=satellite, satellite_job=background_jobs.status(satellite_job_key(layer_id)),
                            zones=zones, fields=tuple(fields), photos=max(1, int(photos_here or 0)), plots=plots, plot_job=plot_job,
                            plot_maps=tuple(m for m, _ in maps), plot_map=plot_map, plot_map_error=map_error,
                            survey=looks.survey, survey_job=looks.survey_job,
                            vision_looks=drone_vision.survey_enabled(), seed=seed,
                            records=tuple(records), linked=tuple(linked), checked=looks.checked, record=looks.record,
                            picture_record=looks.picture_record, fingerprinted=looks.fingerprinted,
                            fingerprint_job=looks.fingerprint_job)
    return PhotoContext(analysis=analysis, here=here, reader=reader)
