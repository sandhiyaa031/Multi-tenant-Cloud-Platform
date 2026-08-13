-- 00_schema_and_extensions.sql
--
-- PURPOSE: Creates the namespace ("dbpilot") that all platform tables live
-- in, kept separate from the "public" schema on purpose — this DB is
-- physically a different Postgres database from "cartnova" (the workload
-- subsystem's target), but namespacing the platform tables under their own
-- schema still keeps things tidy if this ever needs to live alongside
-- other schemas (e.g. a future reporting schema) in the same database.
--
-- DEPENDENCIES: none. Must run first, once, as a superuser/owner role.
--
-- WHAT BREAKS IF REMOVED: every other file references "dbpilot.<table>";
-- without this schema existing first, every CREATE TABLE fails.

CREATE SCHEMA IF NOT EXISTS dbpilot;

-- gen_random_uuid() is used nowhere yet (we're using BIGSERIAL/IDENTITY
-- surrogate keys throughout, consistent with the workload subsystem's
-- style in dbpilot_meta.workload_runs). pgcrypto is enabled anyway
-- because AuditLog / password-adjacent work later in the project will
-- likely want it, and enabling it once now avoids a mid-project ALTER.
CREATE EXTENSION IF NOT EXISTS pgcrypto;

SET search_path TO dbpilot, public;
