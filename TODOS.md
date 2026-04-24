# TODOS

## Deferred from Insurance Intelligence Engine (2026-04-24)

### accuracy_components not serialized in InsuranceReport.to_dict()
- **What**: `src/services/insurance_engine.py:156-193` — `to_dict()` serializes 25 of 27 dataclass fields but omits `accuracy_components` (POD/FAR/HSS/CSI metrics from `compute_insurance_accuracy_safe`) and `geometry` (GeoJSON used for spatial queries). The data is computed and stored on the dataclass but silently dropped during serialization.
- **Why deferred**: New code, not a regression. Requires design decision on whether accuracy metrics belong in all audience views or only scientist. The geometry field is partially handled (extracted from top-level result in message_routes.py, not from `data`).
- **Depends on**: Nothing. Straightforward to add to `to_dict()`.
- **When to revisit**: Before the BK Insurance demo, or when scientist view accuracy metrics are requested.

### SPI simplified from gamma-fit to z-score approximation
- **What**: `_compute_spi()` uses `(rainfall - mean) / std` with hardcoded national normals instead of the planned gamma distribution fit (scipy.stats.gamma) on 20-year CHIRPS monthly totals. No SPI-1/SPI-3 distinction.
- **Why deferred**: The z-score approximation is sufficient for trigger evaluation at current scale. Proper gamma-fit SPI requires historical CHIRPS data aggregation pipeline that doesn't exist yet.
- **Depends on**: Historical CHIRPS monthly aggregation (20+ years per district).
- **When to revisit**: When BNR requests WMO-standard SPI for regulatory compliance, or when district-level thresholds need calibration.

## Deferred from gbrain Adoption (2026-04-13)

### Dream Cycles (Phase 2)
- **What**: Nightly batch rewrite of compiled_truth via LLM. For each brain_page, aggregate recent timeline entries + raw_data, call LLM to produce updated compiled_truth, store old version in brain_page_versions.
- **Why deferred**: Effort L. Core brain needs to prove value first. No point rewriting compiled truth if no one is reading it yet.
- **Depends on**: brain_pages, brain_timeline_entries, brain_page_versions all shipping in Phase 1.
- **When to revisit**: After Phase 1 ships and at least 100 brain pages exist with timeline entries.

### Tool Dispatch Registry Refactor
- **What**: Refactor `src/routes/message_routes.py` tool dispatch from 40+ elif chain (lines 1941-5452, 5,500+ lines) to a registry/dispatch pattern. Each tool becomes a handler function registered in a dict. Dispatch becomes `handlers[function_name](tool_args, ctx)`.
- **Why deferred**: File works fine. Every new tool adds ~25 lines. Not blocking development, but the file is getting harder to navigate and review.
- **Depends on**: Nothing. Can be done anytime.
- **When to revisit**: When the elif chain exceeds 50 tools or someone needs to do a structural change to tool dispatch (e.g., adding middleware, auth per tool, rate limiting per tool).

## Deferred from Brain Ingestion Phase 0 (2026-04-17)

### _seconds_until_midnight_utc bug in concurrency.py
- **What**: `src/services/brain_ingestion/concurrency.py:106-113` has broken TTL math. `tomorrow.replace(day=tomorrow.day)` is a no-op, so the fallback branch always fires and the TTL computation is wrong on every call. Fix: compute `(now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0) - now`.
- **Why deferred**: Phase 0 is HTML-only, the OCR budget path that consumes this TTL never runs in production yet.
- **Depends on**: Nothing, trivial fix.
- **When to revisit**: Before the PDF fetcher lands in Phase 1, or sooner if anyone wires up `reserve_ocr_budget`.

### URL credential leak in last_error
- **What**: When a fetch fails, `record_fetch_failure` stores `repr(e)[:500]` which can include the full URL with query params. If partner sources ever embed an API key in the URL, that key ends up in `brain_sources.last_error` (visible to anyone with source-table read). Fix: strip query strings and basic-auth userinfo before logging.
- **Why deferred**: Phase 0 has no partner_internal sources with URL-embedded credentials. Public HTML fetches only.
- **Depends on**: partner_internal fetcher onboarding.
- **When to revisit**: Before the first partner_internal source is registered in brain_sources.

### Source hot-reload
- **What**: Scheduler only reads `brain_sources` at startup. Adding/pausing/retiring a source requires an app restart before the change takes effect.
- **Why deferred**: Operators already restart the app on deploys (daily cadence is fine). Not blocking Phase 0's single-source ops.
- **Depends on**: Nothing.
- **When to revisit**: When operators start managing sources via an admin UI instead of SQL + redeploy, or when source churn exceeds ~1/week.

### Cron-tick jitter across replicas
- **What**: All workers fire cron ticks at the same UTC second; only one wins the advisory lock but 5 lose a DB round-trip every tick. Adding `jitter=30` to `CronTrigger` spreads the thundering herd.
- **Why deferred**: Noise only, no correctness issue. Five `pg_try_advisory_lock` calls/tick is cheap.
- **Depends on**: Nothing.
- **When to revisit**: If cron-tick contention ever shows up in pg_stat_activity, or the source count grows past ~50.
