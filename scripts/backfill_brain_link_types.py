"""One-shot backfill for brain_links.link_type.

Before the typed-edge inference landed, every auto-extracted link wrote
`link_type='auto', context=''`. This script walks every brain_links row
whose link_type is 'auto' (or empty/null) and recomputes the type via
`src.services.brain_edge_inference.infer_link_type`, then runs the
PostGIS geometric refinement once per source page.

Idempotent. Safe to re-run. Reads the source page's compiled_truth to
rebuild the same context window the live extractor would produce.

Usage (inside the mundi-app container):
    docker exec -i mundi-app python3 /app/scripts/backfill_brain_link_types.py

Options (env):
    BACKFILL_DRY_RUN=1   Print the proposed updates without writing.
    BACKFILL_LIMIT=N     Process at most N rows (default: unlimited).
    BACKFILL_BATCH=N     Commit every N rows (default: 200).

Output:
    Summary printed at end:
        - rows scanned
        - rows updated (link_type changed)
        - rows unchanged (already correct)
        - rows skipped (orphan: source or target page missing)
        - distribution of resulting link_types
"""
from __future__ import annotations

import asyncio
import os
import sys
from collections import Counter
from typing import Optional

import asyncpg

# Allow running from anywhere inside the container — fix up sys.path so
# `from src.services...` resolves regardless of CWD.
sys.path.insert(0, "/app")
from src.services.brain_edge_inference import (  # noqa: E402
    context_window,
    geometric_refinement_sql,
    infer_link_type,
)


DRY_RUN = os.environ.get("BACKFILL_DRY_RUN", "").strip() in {"1", "true", "yes"}
LIMIT: Optional[int] = (
    int(os.environ["BACKFILL_LIMIT"])
    if os.environ.get("BACKFILL_LIMIT", "").strip().isdigit()
    else None
)
BATCH = int(os.environ.get("BACKFILL_BATCH", "200"))


async def _dsn() -> str:
    user = os.environ["POSTGRES_USER"]
    pw = os.environ["POSTGRES_PASSWORD"]
    host = os.environ["POSTGRES_HOST"]
    port = os.environ.get("POSTGRES_PORT", "5432")
    db = os.environ["POSTGRES_DB"]
    return f"postgresql://{user}:{pw}@{host}:{port}/{db}"


# Match the same wikilink regex the live extractor uses (brain_service.py:185)
import re  # noqa: E402

_WIKILINK_RE = re.compile(r"\[\[([^\]|]+?)(?:\|[^\]]+?)?\]\]")


def _context_for_link(source_compiled_truth: str, target_slug: str) -> str:
    """Find the first wikilink to `target_slug` in source and return its window.

    Returns "" when no link is found (the row was frontmatter-only, or
    the target slug shows up only in narrative without a wikilink).
    """
    if not source_compiled_truth or not target_slug:
        return ""
    for m in _WIKILINK_RE.finditer(source_compiled_truth):
        slug = m.group(1).strip()
        if slug == target_slug:
            return context_window(source_compiled_truth, m.start(), m.end(), 100)
    return ""


async def main() -> int:
    dsn = await _dsn()
    conn = await asyncpg.connect(dsn)

    # Bypass RLS for the backfill — we want to recompute every row
    # regardless of partner. Requires the connecting role to be
    # SUPERUSER or rolbypassrls. mundiuser is neither (per memory
    # project_mundiuser_bypassrls); this script must run as the
    # bootstrap role or via SET ROLE.
    print(f"[backfill] DRY_RUN={DRY_RUN}  LIMIT={LIMIT}  BATCH={BATCH}")

    target_filter = "(link_type = 'auto' OR link_type IS NULL OR link_type = '')"
    limit_sql = f" LIMIT {LIMIT}" if LIMIT else ""
    rows = await conn.fetch(
        f"""
        SELECT bl.from_page_id, bl.to_page_id,
               s.slug AS source_slug, s.type AS source_type,
               s.compiled_truth AS source_truth,
               t.slug AS target_slug, t.type AS target_type
        FROM brain_links bl
        LEFT JOIN brain_pages s ON s.id = bl.from_page_id
        LEFT JOIN brain_pages t ON t.id = bl.to_page_id
        WHERE {target_filter}
        ORDER BY bl.from_page_id, bl.to_page_id
        {limit_sql}
        """
    )
    print(f"[backfill] scanned {len(rows)} rows needing backfill")

    updated = 0
    skipped_orphan = 0
    distribution: Counter = Counter()
    batch_queue: list[tuple[int, int, str, str]] = []

    async def flush_batch():
        nonlocal updated
        if not batch_queue or DRY_RUN:
            batch_queue.clear()
            return
        async with conn.transaction():
            for from_id, to_id, edge_type, link_ctx in batch_queue:
                await conn.execute(
                    """
                    UPDATE brain_links
                    SET link_type = $3, context = COALESCE(NULLIF(context, ''), $4)
                    WHERE from_page_id = $1 AND to_page_id = $2
                    """,
                    from_id, to_id, edge_type, link_ctx,
                )
                updated += 1
        batch_queue.clear()

    for r in rows:
        if r["source_slug"] is None or r["target_slug"] is None:
            skipped_orphan += 1
            continue
        link_ctx = _context_for_link(r["source_truth"] or "", r["target_slug"])
        edge_type = infer_link_type(
            r["source_type"] or "",
            r["target_type"] or "",
            link_context=link_ctx,
            page_content=r["source_truth"] or "",
        )
        distribution[edge_type] += 1
        batch_queue.append((r["from_page_id"], r["to_page_id"], edge_type, link_ctx))
        if len(batch_queue) >= BATCH:
            await flush_batch()
    await flush_batch()

    # Pass 2: PostGIS geometric refinement, one query per distinct source page.
    # Idempotent — only changes rows where containment is verified AND the
    # current link_type differs.
    source_pages = sorted({r["from_page_id"] for r in rows if r["from_page_id"]})
    geom_promotions = 0
    if not DRY_RUN:
        sql = geometric_refinement_sql()
        for from_id in source_pages:
            result = await conn.execute(sql, from_id)
            # result is "UPDATE N"; parse the N to track promotions
            try:
                n = int(result.rsplit(" ", 1)[-1])
            except (ValueError, IndexError):
                n = 0
            geom_promotions += n

    # Final distribution after geometric refinement
    final_dist = await conn.fetch(
        "SELECT link_type, COUNT(*) AS n FROM brain_links GROUP BY link_type ORDER BY n DESC"
    )

    print(f"[backfill] updated:        {updated}")
    print(f"[backfill] skipped_orphan: {skipped_orphan}")
    print(f"[backfill] geom promotions: {geom_promotions} (pages {len(source_pages)})")
    print("[backfill] inference distribution (this run):")
    for et, n in distribution.most_common():
        print(f"    {n:5d}  {et}")
    print("[backfill] final link_type distribution (full table):")
    for r in final_dist:
        print(f"    {r['n']:5d}  {r['link_type']!r}")

    await conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
