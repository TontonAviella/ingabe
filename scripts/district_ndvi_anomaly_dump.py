"""Every district's NDVI anomaly as Sage's get_anomaly_alerts sees it, read cold and then warm.

Evidence for district_ndvi_anomaly: the values, the alerts, what is missing and how long a
first (cold) and a repeated (warm) request take. Needs the district boundaries in the database it
connects to (POSTGRES_* variables; use a copy, never mundidb).

    PYTHONPATH=. python scripts/district_ndvi_anomaly_dump.py 2026-10-07 > out.json
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from datetime import date

import asyncpg

from src.services import district_ndvi_anomaly


async def main(today: date) -> dict:
    conn = await asyncpg.connect(
        host=os.environ["POSTGRES_HOST"], port=int(os.environ.get("POSTGRES_PORT", 5432)),
        user=os.environ["POSTGRES_USER"], password=os.environ["POSTGRES_PASSWORD"],
        database=os.environ["POSTGRES_DB"])
    out: dict = {"today": today.isoformat(), "workers": district_ndvi_anomaly.WORKERS}
    # Cold without a deadline, to time every district; warm with the tool's deadline.
    for label, deadline in (("cold", 900.0), ("warm", district_ndvi_anomaly.DEADLINE_S)):
        started = time.monotonic()
        result = await district_ndvi_anomaly.district_ndvi_anomalies(conn, today, deadline_s=deadline)
        out[label] = {"seconds": round(time.monotonic() - started, 1), **result}
    await conn.close()
    return out


if __name__ == "__main__":
    print(json.dumps(asyncio.run(main(date.fromisoformat(sys.argv[1]))), indent=1))
