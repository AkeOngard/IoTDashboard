"""History queries.

Short ranges read raw telemetry; anything longer reads the 5-minute continuous
aggregate, which is what keeps a 30-day chart from scanning a month of rows.
Either way the result is re-bucketed to a fixed number of points so the browser
always gets a chart-sized payload rather than whatever the retention happens to
hold.
"""
from __future__ import annotations

from datetime import timedelta
from typing import Any

from app.db import Database
from app.models import Capability
from app.telemetry import BOOLEAN_CAPS

#: Ranges up to this read raw rows; beyond it, the rollup.
RAW_MAX_HOURS = 6.0
#: Rollup granularity, from migration 004.
AGGREGATE_SECONDS = 300.0
#: Raw samples closer together than this are not worth separate points.
MIN_RAW_BUCKET_SECONDS = 10.0

RAW_SQL = """
SELECT extract(epoch FROM time_bucket($1::interval, ts)) AS t,
       avg(value) AS v,
       min(value) AS lo,
       max(value) AS hi
FROM telemetry
WHERE device_id = $2 AND capability = $3 AND ts >= now() - $4::interval
GROUP BY 1
ORDER BY 1
"""

AGGREGATE_SQL = """
SELECT extract(epoch FROM time_bucket($1::interval, bucket)) AS t,
       avg(avg_value) AS v,
       min(min_value) AS lo,
       max(max_value) AS hi
FROM telemetry_5m
WHERE device_id = $2 AND capability = $3 AND bucket >= now() - $4::interval
GROUP BY 1
ORDER BY 1
"""


async def series(
    db: Database,
    device_id: str,
    capability: str,
    hours: float,
    points: int = 240,
) -> dict[str, Any]:
    cap = Capability(capability)
    hours = max(0.05, min(hours, 24 * 365.0))
    points = max(10, min(points, 1000))

    use_raw = hours <= RAW_MAX_HOURS
    floor = MIN_RAW_BUCKET_SECONDS if use_raw else AGGREGATE_SECONDS
    bucket_seconds = max(floor, (hours * 3600.0) / points)

    rows = await db.fetch(
        RAW_SQL if use_raw else AGGREGATE_SQL,
        timedelta(seconds=bucket_seconds),
        device_id,
        cap.value,
        timedelta(hours=hours),
    )

    boolean = cap in BOOLEAN_CAPS
    series_points = [
        {
            "t": round(float(row["t"])),
            "v": _round(row["v"], boolean),
            "lo": _round(row["lo"], boolean),
            "hi": _round(row["hi"], boolean),
        }
        for row in rows
    ]
    return {
        "device_id": device_id,
        "capability": cap.value,
        "hours": hours,
        "source": "telemetry" if use_raw else "telemetry_5m",
        "bucket_seconds": round(bucket_seconds),
        "boolean": boolean,
        "points": series_points,
    }


def _round(value: Any, boolean: bool) -> float | None:
    if value is None:
        return None
    # A boolean averaged over a bucket is a duty cycle; report it as a percent.
    return round(float(value) * 100, 1) if boolean else round(float(value), 2)
