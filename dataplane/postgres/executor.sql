-- The role DBPilot's executor logs in as, and nothing more than it needs.
--
-- Idempotent: run at first start by initdb/04_executor.sh, and by hand against a
-- database created before this file existed:
--   psql -U postgres -d app -v executor_pw=... -f /usr/local/share/dbpilot/executor.sql
--
-- What each typed action needs, and how it is granted:
--   create/drop index, analyze   table ownership. PostgreSQL has no narrower
--                                privilege for CREATE INDEX, so the tables belong
--                                to a group role (ch_owner) the executor is in.
--   role setting, concurrency    CREATEROLE plus ADMIN on each tenant role (and on
--   cap                          nothing else).
--   instance setting             ALTER SYSTEM on the allowlisted parameters only.
--
-- What it is not: a superuser. It cannot change a parameter outside the
-- allowlist, read other databases, or load code. An event trigger refuses every
-- DDL command from it except CREATE INDEX and DROP INDEX, so table ownership
-- cannot be used to drop or alter a table, or to grant anything.
--
-- What remains, and is stated openly: as a member of the owning role it could
-- read and change rows, and with ADMIN on a tenant role it could reset that
-- tenant's password. PostgreSQL offers no privilege that separates these.

DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'ch_owner') THEN
        CREATE ROLE ch_owner NOLOGIN;
    END IF;
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'dbpilot_executor') THEN
        CREATE ROLE dbpilot_executor LOGIN CREATEROLE;
    END IF;
END $$;
ALTER ROLE dbpilot_executor PASSWORD :'executor_pw';

GRANT ch_owner TO dbpilot_executor;
GRANT USAGE, CREATE ON SCHEMA ch TO ch_owner;
-- Replication lag and deadlock counters, read during a canary.
GRANT pg_read_all_stats TO dbpilot_executor;

-- Must match INSTANCE_SETTINGS in core/dbpilot_core/actions.py (a test checks it).
GRANT ALTER SYSTEM ON PARAMETER
    work_mem, max_parallel_workers_per_gather, random_page_cost, effective_io_concurrency,
    default_statistics_target, checkpoint_completion_target, autovacuum_vacuum_scale_factor, jit
    TO dbpilot_executor;
GRANT EXECUTE ON FUNCTION pg_reload_conf() TO dbpilot_executor;
-- The inverse of an instance setting is its previous value in postgresql.auto.conf.
GRANT SELECT ON pg_file_settings TO dbpilot_executor;
GRANT EXECUTE ON FUNCTION pg_show_all_file_settings() TO dbpilot_executor;

-- Called after a tenant is provisioned: hands new partitions to ch_owner and
-- gives the executor ADMIN (not membership) on the new tenant role.
CREATE OR REPLACE FUNCTION ch.sync_executor() RETURNS void
LANGUAGE plpgsql AS $$
DECLARE
    r record;
BEGIN
    FOR r IN SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
             WHERE n.nspname = 'ch' AND c.relkind IN ('r', 'p') AND c.relowner <> 'ch_owner'::regrole
    LOOP
        EXECUTE format('ALTER TABLE ch.%I OWNER TO ch_owner', r.relname);
    END LOOP;
    FOR r IN SELECT m.db_role FROM ch.tenant_map m
             WHERE NOT EXISTS (SELECT FROM pg_auth_members a
                               WHERE a.roleid = m.db_role::regrole AND a.member = 'dbpilot_executor'::regrole)
    LOOP
        EXECUTE format('GRANT %I TO dbpilot_executor WITH ADMIN TRUE, INHERIT FALSE, SET FALSE', r.db_role);
    END LOOP;
END $$;
REVOKE ALL ON FUNCTION ch.sync_executor() FROM PUBLIC;

CREATE OR REPLACE FUNCTION ch.executor_ddl_guard() RETURNS event_trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF session_user = 'dbpilot_executor' AND tg_tag NOT IN ('CREATE INDEX', 'DROP INDEX') THEN
        RAISE EXCEPTION 'dbpilot_executor may not run %', tg_tag USING ERRCODE = '42501';
    END IF;
END $$;
DROP EVENT TRIGGER IF EXISTS executor_ddl_guard;
CREATE EVENT TRIGGER executor_ddl_guard ON ddl_command_start EXECUTE FUNCTION ch.executor_ddl_guard();

SELECT ch.sync_executor();
