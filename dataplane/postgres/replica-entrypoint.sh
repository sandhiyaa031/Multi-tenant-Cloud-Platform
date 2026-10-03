#!/bin/bash
# Starts a streaming standby. On first start the data directory is empty, so it
# is filled with a base backup of the primary; -R writes standby.signal and
# primary_conninfo, which is what makes PostgreSQL start in recovery and follow.
set -euo pipefail

if [ ! -s "$PGDATA/PG_VERSION" ]; then
    mkdir -p "$PGDATA"
    chown -R postgres:postgres "$(dirname "$PGDATA")"
    chmod 700 "$PGDATA"
    until pg_isready -h "$PRIMARY_HOST" -p 5432 -q; do
        echo "waiting for primary at $PRIMARY_HOST"
        sleep 2
    done
    PGPASSWORD="$REPLICATION_PASSWORD" gosu postgres pg_basebackup \
        -h "$PRIMARY_HOST" -p 5432 -U replicator -D "$PGDATA" -R -X stream -P
fi

exec docker-entrypoint.sh "$@"
