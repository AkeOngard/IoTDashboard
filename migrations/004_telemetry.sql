-- Devices and telemetry (doc §7). Plain PostgreSQL only, so this applies
-- everywhere; 005 adds the TimescaleDB machinery where the extension exists.

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

-- Everything numeric, booleans as 0/1: it keeps the table narrow and lets
-- avg() over a switch double as a duty-cycle reading.
CREATE TABLE IF NOT EXISTS telemetry (
    ts         TIMESTAMPTZ      NOT NULL,
    device_id  TEXT             NOT NULL,
    capability TEXT             NOT NULL,
    value      DOUBLE PRECISION NOT NULL
);

-- Carries every history query: one device, one capability, a time range.
-- On plain PostgreSQL it is also what keeps a year-long chart cheap, since
-- there are no chunks to exclude.
CREATE INDEX IF NOT EXISTS telemetry_device_cap_ts_idx
    ON telemetry (device_id, capability, ts DESC);
