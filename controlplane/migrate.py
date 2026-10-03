"""Apply SQL migrations in filename order, each in its own transaction.

Runs as the database owner. The API never uses this connection: it logs in as
the unprivileged role created here, which is what makes row-level security bind.
"""
import os
import sys
from pathlib import Path

import psycopg
from psycopg import sql

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
API_ROLE = "dbpilot_api"
# Arbitrary constant: two migrators started together must not interleave.
ADVISORY_LOCK_KEY = 728_140_001


def ensure_api_role(conn: psycopg.Connection, password: str) -> None:
    exists = conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (API_ROLE,)).fetchone()
    verb = "ALTER" if exists else "CREATE"
    conn.execute(
        sql.SQL("{} ROLE {} LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE PASSWORD {}").format(
            sql.SQL(verb), sql.Identifier(API_ROLE), sql.Literal(password)
        )
    )


def main() -> int:
    owner_url = os.environ["CONTROL_DB_OWNER_URL"]
    api_password = os.environ["CONTROL_DB_API_PASSWORD"]

    with psycopg.connect(owner_url, autocommit=True) as conn:
        conn.execute("SELECT pg_advisory_lock(%s)", (ADVISORY_LOCK_KEY,))
        conn.execute(
            "CREATE TABLE IF NOT EXISTS public.schema_migrations ("
            " version text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
        )
        ensure_api_role(conn, api_password)
        applied = {row[0] for row in conn.execute("SELECT version FROM public.schema_migrations")}

        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            if path.name in applied:
                continue
            print(f"applying {path.name}", flush=True)
            with conn.transaction():
                conn.execute(path.read_text(encoding="utf-8"))
                conn.execute("INSERT INTO public.schema_migrations (version) VALUES (%s)", (path.name,))
        print("migrations up to date", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
