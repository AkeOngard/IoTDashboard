-- Extensions and shared helpers (doc §12).
-- Versions 002 (auth/audit) and 003 (user management) are reserved for the
-- security phase; the runner tolerates the gap.

-- TimescaleDB where it exists, plain PostgreSQL where it does not. Managed
-- free tiers (Supabase, Neon) do not offer it, and history is designed to work
-- either way -- so a missing extension is a notice, not a failure.
DO $$
BEGIN
    EXECUTE 'CREATE EXTENSION IF NOT EXISTS timescaledb';
EXCEPTION WHEN OTHERS THEN
    RAISE NOTICE 'timescaledb unavailable (%); history will use plain PostgreSQL', SQLERRM;
END
$$;

-- Used by any table that wants a self-maintaining updated_at column.
CREATE OR REPLACE FUNCTION touch_updated_at() RETURNS trigger AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
