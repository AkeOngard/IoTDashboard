-- migrate:no-transaction
-- migrate:requires-extension timescaledb
-- Hypertable, compression, retention and the 5-minute continuous aggregate.
--
-- Skipped wholesale on managed Postgres without TimescaleDB; history then
-- reads raw rows with date_bin() and the app prunes on a timer instead.
--
-- Runs outside a transaction because TimescaleDB refuses to create a
-- continuous aggregate inside one. Every statement is individually idempotent,
-- so a failure part-way through is fixed by running `migrate.py up` again.

SELECT create_hypertable(
    'telemetry', 'ts',
    chunk_time_interval => INTERVAL '1 day',
    if_not_exists       => TRUE,
    migrate_data        => TRUE
);

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
