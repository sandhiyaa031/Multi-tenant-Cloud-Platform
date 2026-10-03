#!/bin/bash
# Creates the executor's least-privilege role. See executor.sql for what it may do.
set -euo pipefail

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
     -v executor_pw="$EXECUTOR_PASSWORD" -f /usr/local/share/dbpilot/executor.sql
