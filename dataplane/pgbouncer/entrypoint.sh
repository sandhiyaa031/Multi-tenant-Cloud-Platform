#!/bin/sh
# Writes the configuration from environment variables, then starts PgBouncer.
set -eu

cat > /etc/pgbouncer/userlist.txt <<USERLIST
"pgbouncer_auth" "${PGBOUNCER_AUTH_PASSWORD}"
USERLIST

cat > /etc/pgbouncer/pgbouncer.ini <<INI
[databases]
; "app" is the read-write path; "app_ro" is the replica, used by replica routing (action A6).
app    = host=${PRIMARY_HOST} port=5432 dbname=app
app_ro = host=${REPLICA_HOST} port=5432 dbname=app

[pgbouncer]
listen_addr = 0.0.0.0
listen_port = 6432
auth_type = scram-sha-256
auth_file = /etc/pgbouncer/userlist.txt
; Tenant roles are created at runtime, so their verifiers are looked up in
; PostgreSQL instead of being listed here. The function only answers for tenant roles.
auth_user = pgbouncer_auth
auth_query = SELECT usename, passwd FROM pgbouncer.get_auth(\$1)
; Transaction pooling: a server connection is held only for one transaction.
pool_mode = transaction
max_prepared_statements = 200
max_client_conn = 2000
default_pool_size = 20
ignore_startup_parameters = extra_float_digits,options
; Lets the engine ask for server connections to be recycled after a role-level change.
admin_users = pgbouncer_auth
INI

exec pgbouncer /etc/pgbouncer/pgbouncer.ini
