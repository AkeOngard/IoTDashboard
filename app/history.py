"""History queries, on TimescaleDB or on plain PostgreSQL.

With TimescaleDB, short ranges read raw telemetry and longer ones read the
5-minute continuous aggregate, which is what keeps a 30-day chart from scanning
a month of rows. Managed free tiers do not offer TimescaleDB, so there the same
query runs against raw rows with date_bin() -- affordable because the recorder's
deadband and 5-minute heartbeat mean a capability writes a few hundred rows a
day, not a few hundred thousand, and the (device, capability, ts) index covers
exactly this shape of query.

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

#: Ranges up to this read raw rows; beyond it, the rollup (when there is one).
RAW_MAX_HOURS = 6.0
#: Rollup granularity, from migration 005.
AGGREGATE_SECONDS = 300.0
#: Raw samples closer together than this are not worth separate points.
MIN_RAW_BUCKET_SECONDS = 10.0

#: date_bin() needs an explicit origin; time_bucket() defaults to the same one.
ORIGIN = "TIMESTAMPTZ '2000-01-01'"


def _raw_sql(bucket: str) -> str:
    return """
SELECT extract(epoch FROM {bucket}) AS t,
       avg(value) AS v,
       min(value) AS lo,
       max(value) AS hi
FROM telemetry
WHERE device_id = $2 AND capability = $3 AND ts >= now() - $4::interval
GROUP BY 1
ORDER BY 1
""".format(bucket=bucket)


#: One query per backend, built once. The bucket function is the only
#: difference: time_bucket is TimescaleDB's, date_bin is core PostgreSQL 14+.
RAW_TIMESCALE_SQL = _raw_sql("time_bucket($1::interval, ts)")
RAW_PLAIN_SQL = _raw_sql("date_bin($1::interval, ts, %s)" % ORIGIN)

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

    # The rollup only exists where TimescaleDB does; everywhere else a long
    # range simply reads more raw rows.
    use_rollup = db.rollup and hours > RAW_MAX_HOURS
    floor = AGGREGATE_SECONDS if use_rollup else MIN_RAW_BUCKET_SECONDS
    bucket_seconds = max(floor, (hours * 3600.0) / points)

    if use_rollup:
        sql, source = AGGREGATE_SQL, "telemetry_5m"
    elif db.timescale:
        sql, source = RAW_TIMESCALE_SQL, "telemetry"
    else:
        sql, source = RAW_PLAIN_SQL, "telemetry"

    rows = await db.fetch(
        sql,
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
        "source": source,
        "bucket_seconds": round(bucket_seconds),
        "boolean": boolean,
        "points": series_points,
    }


def _round(value: Any, boolean: bool) -> float | None:
    if value is None:
        return None
    # A boolean averaged over a bucket is a duty cycle; report it as a percent.
    return round(float(value) * 100, 1) if boolean else round(float(value), 2)
