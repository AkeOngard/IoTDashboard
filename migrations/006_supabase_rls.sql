-- Close Supabase's Data API over our tables.
--
-- Supabase publishes every table in `public` over its REST/GraphQL API to the
-- `anon` and `authenticated` roles, and grants them full rights by default.
-- The key that API takes is the project's anon key, which is meant to be
-- public. Without row-level security, anyone holding it could read the
-- device list and history, write fake readings, or delete them all. That is
-- what the "RLS disabled" warning in the dashboard is about.
--
-- This app never uses that API: it connects to Postgres directly, as the role
-- that ran these migrations and owns the tables. A table's owner is not subject
-- to its row-level security (FORCE ROW LEVEL SECURITY would change that; it is
-- deliberately not used), so turning RLS on with *no policies* shuts the API
-- out completely and changes nothing for the app, its backups, or restores.
-- No policy is missing here: a policy would be a way back in.
--
-- The REVOKE goes further than RLS on its own: RLS does not cover TRUNCATE, and
-- without any privilege the tables stop appearing in the API's schema at all.
--
-- Only on Supabase, recognised by its `anon` role. A self-hosted Postgres has
-- no such API to close, and TimescaleDB's compressed hypertables do not
-- support row-level security.

DO $$
DECLARE
    tbl    text;
    bypass boolean;
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon')
       OR NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
        RAISE NOTICE 'not Supabase (no anon/authenticated roles): nothing to lock down';
        RETURN;
    END IF;

    SELECT rolsuper OR rolbypassrls INTO bypass
      FROM pg_roles WHERE rolname = current_user;

    FOREACH tbl IN ARRAY ARRAY['devices', 'telemetry', 'schema_migrations'] LOOP
        CONTINUE WHEN to_regclass('public.' || tbl) IS NULL;

        -- Enabling RLS on a table the app does not own would lock the app out
        -- of it: no policy lets anyone else in. Refuse instead, so the
        -- migration fails loudly and nothing changes.
        IF NOT bypass AND NOT EXISTS (
            SELECT 1 FROM pg_class
             WHERE oid = to_regclass('public.' || tbl)
               AND pg_has_role(current_user, relowner, 'USAGE')
        ) THEN
            RAISE EXCEPTION
                'table % is not owned by %, so enabling row-level security would lock this app out of it',
                tbl, current_user;
        END IF;

        EXECUTE format('ALTER TABLE public.%I ENABLE ROW LEVEL SECURITY', tbl);
        EXECUTE format('REVOKE ALL ON TABLE public.%I FROM anon, authenticated', tbl);
    END LOOP;
END
$$;

-- Supabase's other warning on this schema ("function search_path mutable"):
-- pin it, so the trigger helper cannot be redirected to a look-alike function
-- by whoever controls the caller's search_path. It only calls now(), which
-- lives in pg_catalog. Harmless everywhere, so not conditional.
ALTER FUNCTION touch_updated_at() SET search_path = pg_catalog;
