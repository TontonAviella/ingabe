"""Rwanda Brain ingestion package.

Continuous-ops fetchers, not a pilot batch. Modules:

- models:      pydantic SourceConfig / FetchedContent / FetchResult
- base:        BaseFetcher abstract class (retry, backoff, structured logs)
- concurrency: OCR semaphore + rate limits + per-source daily cap +
               pre-enqueue cost projection
- registry:    source-tier table writer (brain_sources) + ToS state

The fetcher framework is the input side of the retrieval loop. Its output
feeds brain_pages (via brain_service.put_page) and brain_page_versions
(append-only history). Embeddings are backfilled by the wsgi brain hook
loop (brain_hook_processor.run_hook_processor_once → embed_all_stale).
"""
