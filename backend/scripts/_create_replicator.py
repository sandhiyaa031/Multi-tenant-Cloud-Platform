"""Create replicator role with a fixed password for this local experiment.
The password is defined here only in this setup script (not in any committed config file).
"""
import psycopg

REPL_PWD = "Repl!cator2026"   # local experiment only — change before any shared deployment

conn = psycopg.connect("postgresql://postgres:100978@localhost:5432/postgres", autocommit=True)
cur = conn.cursor()

cur.execute("SELECT rolname FROM pg_roles WHERE rolname='replicator'")
if cur.fetchone():
    print("Role 'replicator' already exists — skipping creation.")
else:
    cur.execute(f"CREATE ROLE replicator WITH REPLICATION LOGIN PASSWORD '{REPL_PWD}'")
    print("Role 'replicator' created with REPLICATION + LOGIN (SCRAM-SHA-256).")

conn.close()
print("Done.")
