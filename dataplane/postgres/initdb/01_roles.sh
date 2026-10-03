#!/bin/bash
# Runs once, when the primary's data directory is first created.
set -euo pipefail

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
     -v repl_pw="$REPLICATION_PASSWORD" -v bouncer_pw="$PGBOUNCER_AUTH_PASSWORD" <<'SQL'
-- Streams WAL to standbys. Nothing else.
CREATE ROLE replicator WITH REPLICATION LOGIN PASSWORD :'repl_pw';
-- PgBouncer logs in as this role only to look up a connecting tenant's password verifier.
CREATE ROLE pgbouncer_auth WITH LOGIN PASSWORD :'bouncer_pw';
-- Group role: every tenant role is a member; privileges are granted to the group once.
CREATE ROLE ch_tenant NOLOGIN;
SQL

echo "host replication replicator all scram-sha-256" >> "$PGDATA/pg_hba.conf"
