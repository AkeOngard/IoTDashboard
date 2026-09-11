-- Extensions and shared helpers (doc §12).
-- Versions 002 (auth/audit) and 003 (user management) are reserved for the
-- security phase; the runner tolerates the gap.

CREATE EXTENSION IF NOT EXISTS timescaledb;

-- Used by any table that wants a self-maintaining updated_at column.
CREATE OR REPLACE FUNCTION touch_updated_at() RETURNS trigger AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
