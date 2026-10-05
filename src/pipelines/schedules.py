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

"""Dagster schedules for the Rwanda pre-compute jobs.

Defines cron-style schedules for the Rwanda pre-compute jobs: nightly
district pre-warm, parcel NDVI and cache cleanup, weekly analytics, and the
daily weather ingest.
"""

from dagster import DefaultScheduleStatus, ScheduleDefinition


# ─── Rwanda pre-compute schedules (populate DuckDB cache for Sage) ────────

nightly_field_ndvi_schedule = ScheduleDefinition(
    name="nightly_field_ndvi",
    cron_schedule="0 2 * * *",  # Every night at 2 AM UTC
    job_name="nightly_field_ndvi_job",
    execution_timezone="UTC",
    description="Nightly: pre-warm district agri indices cache (30 PU, all 6 indices)",
    default_status=DefaultScheduleStatus.RUNNING,
)

weekly_anomaly_schedule = ScheduleDefinition(
    name="weekly_anomaly_scan",
    cron_schedule="0 1 * * 1",  # Every Monday at 1 AM UTC
    job_name="weekly_anomaly_scan_job",
    execution_timezone="UTC",
    description="Weekly NDVI anomaly detection → DuckDB alerts cache",
    default_status=DefaultScheduleStatus.RUNNING,
)

weekly_yield_risk_schedule = ScheduleDefinition(
    name="weekly_yield_risk",
    cron_schedule="0 2 * * 1",  # Every Monday at 2 AM UTC
    job_name="weekly_yield_risk_job",
    execution_timezone="UTC",
    description="Weekly yield risk prediction via Mann-Kendall → DuckDB cache",
    default_status=DefaultScheduleStatus.RUNNING,
)

weekly_drought_schedule = ScheduleDefinition(
    name="weekly_drought_scan",
    cron_schedule="0 3 * * 1",  # Every Monday at 3 AM UTC
    job_name="weekly_drought_scan_job",
    execution_timezone="UTC",
    description="Weekly drought detection via VCI + NDWI → DuckDB cache",
    default_status=DefaultScheduleStatus.RUNNING,
)

weekly_phenology_schedule = ScheduleDefinition(
    name="weekly_phenology",
    cron_schedule="0 4 * * 1",  # Every Monday at 4 AM UTC
    job_name="weekly_phenology_job",
    execution_timezone="UTC",
    description="Weekly crop phenology analysis → DuckDB cache",
    default_status=DefaultScheduleStatus.RUNNING,
)

# ─── Cache cleanup and parcel-level NDVI schedules ─────────────────────

nightly_cache_cleanup_schedule = ScheduleDefinition(
    name="nightly_cache_cleanup",
    cron_schedule="30 2 * * *",  # Every night at 2:30 AM UTC (after district pre-warm)
    job_name="nightly_cache_cleanup_job",
    execution_timezone="UTC",
    description="Nightly: purge stale DuckDB cache entries older than 30 days",
    default_status=DefaultScheduleStatus.RUNNING,
)

nightly_parcel_ndvi_schedule = ScheduleDefinition(
    name="nightly_parcel_ndvi",
    cron_schedule="0 5 * * *",  # Every night at 5 AM UTC
    job_name="nightly_parcel_ndvi_job",
    execution_timezone="UTC",
    description="Nightly parcel-level NDVI for user-uploaded fields → DuckDB cache",
    default_status=DefaultScheduleStatus.RUNNING,
)

# ─── Weather data schedule ──────────────────────────────────────────────

daily_weather_ingest_schedule = ScheduleDefinition(
    name="daily_weather_ingest",
    cron_schedule="0 6 * * *",  # Every day at 6 AM UTC
    job_name="daily_weather_ingest_job",
    execution_timezone="UTC",
    description="Daily AgERA5 weather data → district aggregation → DuckDB cache",
    default_status=DefaultScheduleStatus.RUNNING,
)
