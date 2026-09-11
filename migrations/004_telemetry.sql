-- migrate:no-transaction
-- Devices, telemetry hypertable and the 5-minute continuous aggregate (doc §7).
--
-- The whole file runs outside a transaction because TimescaleDB refuses to
-- create a continuous aggregate inside one. Every statement is therefore
-- written to be individually idempotent, so a failure part-way through is
-- fixed by simply running `migrate.py up` again.

-- Device inventory. Survives a hub going offline so history stays attributable
-- to a readable name rather than a bare "matter:1:7".
CREATE TABLE IF NOT EXISTS devices (
    id           TEXT        PRIMARY KEY,
    name         TEXT        NOT NULL,
    room         TEXT        NOT NULL DEFAULT 'Unassigned',
    adapter      TEXT        NOT NULL,
    native_id    TEXT        NOT NULL,
    kind         TEXT        NOT NULL,
    capabilities TEXT[]      NOT NULL DEFAULT '{}',
    first_seen   TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Everything numeric, booleans as 0/1: it keeps the hypertable narrow and lets
-- avg() over a switch double as a duty-cycle reading.
CREATE TABLE IF NOT EXISTS telemetry (
    ts         TIMESTAMPTZ      NOT NULL,
    device_id  TEXT             NOT NULL,
    capability TEXT             NOT NULL,
    value      DOUBLE PRECISION NOT NULL
);

SELECT create_hypertable(
    'telemetry', 'ts',
    chunk_time_interval => INTERVAL '1 day',
    if_not_exists       => TRUE
);

CREATE INDEX IF NOT EXISTS telemetry_device_cap_ts_idx
    ON telemetry (device_id, capability, ts DESC);

ALTER TABLE telemetry SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'device_id, capability',
    timescaledb.compress_orderby   = 'ts DESC'
);

-- Doc §7: compress after 7 days, keep raw for 400 days.
SELECT add_compression_policy('telemetry', INTERVAL '7 days',   if_not_exists => TRUE);
SELECT add_retention_policy( 'telemetry', INTERVAL '400 days', if_not_exists => TRUE);

-- 5-minute rollups, so a 30-day chart reads a few hundred rows instead of a
-- few hundred thousand.
CREATE MATERIALIZED VIEW IF NOT EXISTS telemetry_5m
WITH (timescaledb.continuous) AS
SELECT time_bucket(INTERVAL '5 minutes', ts) AS bucket,
       device_id,
       capability,
       avg(value)   AS avg_value,
       min(value)   AS min_value,
       max(value)   AS max_value,
       count(*)     AS samples
FROM telemetry
GROUP BY bucket, device_id, capability
WITH NO DATA;

-- Real-time aggregation: unions not-yet-materialised raw rows into the view, so
-- a long-range chart still ends at "now" instead of at the last refresh.
ALTER MATERIALIZED VIEW telemetry_5m SET (timescaledb.materialized_only = false);

SELECT add_continuous_aggregate_policy('telemetry_5m',
    start_offset      => INTERVAL '7 days',
    end_offset        => INTERVAL '5 minutes',
    schedule_interval => INTERVAL '5 minutes',
    if_not_exists     => TRUE);

-- Rollups outlive the raw data they came from.
SELECT add_retention_policy('telemetry_5m', INTERVAL '2 years', if_not_exists => TRUE);
