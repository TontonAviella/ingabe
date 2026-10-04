"""Give every row saved without a partner to one organization (one-off).

Decided by Roger (2026-10-04): when the first partner organization is set up,
the data saved before partners existed (partner_id NULL) becomes that
partner's, so Partner #2 starts empty. Dry run by default; --apply changes
the rows in one transaction. Take a backup first:

    docker exec -u postgres mundiai-postgresdb-1 pg_dump -U mundi_admin -d mundidb -Fc > before-partner.dump
    python scripts/assign_unpartnered_data.py --org <organizations.id or WorkOS org_...>          # counts
    python scripts/assign_unpartnered_data.py --org <...> --apply

Needs POSTGRES_* env vars.
"""

from __future__ import annotations

import argparse
import asyncio
import os

import asyncpg


async def _tables(conn: asyncpg.Connection) -> list[str]:
    rows = await conn.fetch(
        "SELECT table_name FROM information_schema.columns "
        "WHERE table_schema = 'public' AND column_name = 'partner_id' AND data_type = 'uuid' "
        "ORDER BY table_name"
    )
    return [r["table_name"] for r in rows]


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--org", required=True, help="organizations.id (uuid) or WorkOS org id (org_...)")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    conn = await asyncpg.connect(
        host=os.environ.get("POSTGRES_HOST", "postgresdb"), port=int(os.environ.get("POSTGRES_PORT", "5432")),
        user=os.environ["POSTGRES_USER"], password=os.environ["POSTGRES_PASSWORD"],
        database=os.environ.get("POSTGRES_DB", "mundidb"),
    )
    try:
        org = await conn.fetchrow(
            "SELECT id::text AS id, name FROM organizations WHERE id::text = $1 OR workos_org_id = $1", args.org)
        if org is None:
            raise SystemExit(f"no organization {args.org!r}: sign in under it once so its row is created")
        tables = await _tables(conn)
        async with conn.transaction():
            for t in tables:
                n = await conn.fetchval(f"SELECT count(*) FROM {t} WHERE partner_id IS NULL")
                if args.apply and n:
                    await conn.execute(f"UPDATE {t} SET partner_id = $1::uuid WHERE partner_id IS NULL", org["id"])
                print(f"{t}: {n} rows {'assigned' if args.apply else 'would be assigned'} to {org['name']}")
        if not args.apply:
            print("dry run: nothing changed (add --apply)")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
