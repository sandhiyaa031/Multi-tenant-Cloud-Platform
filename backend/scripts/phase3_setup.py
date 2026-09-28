"""
Phase 3 replica setup utility.
Run this script interactively or via command line to:
  1. Check primary wal_level, max_wal_senders, hot_standby settings
  2. Create the 'replicator' role with SCRAM-SHA-256 authentication
  3. Check pg_hba.conf entry
  4. Bootstrap the replica via pg_basebackup
  5. Start the replica on port 5433

Usage:
  python backend/scripts/phase3_setup.py check      — print primary PG settings
  python backend/scripts/phase3_setup.py create_role — create replicator role
  python backend/scripts/phase3_setup.py bootstrap   — run pg_basebackup
  python backend/scripts/phase3_setup.py start       — start replica pg_ctl
  python backend/scripts/phase3_setup.py verify      — verify replica connectivity
"""

import sys
import os
import subprocess
import getpass

PG_BIN = r"C:\Program Files\PostgreSQL\18\bin"
PRIMARY_DSN = "postgresql://postgres:100978@localhost:5432/postgres"
REPLICA_DATA = r"D:\PostgreSQL_Replica"
REPLICA_PORT = 5433

def get_conn():
    import psycopg
    return psycopg.connect(PRIMARY_DSN, autocommit=True)

def cmd_check():
    conn = get_conn()
    cur = conn.cursor()
    params = ['wal_level', 'max_wal_senders', 'hot_standby', 'max_replication_slots']
    for p in params:
        cur.execute(f"SHOW {p}")
        print(f"  {p} = {cur.fetchone()[0]}")
    cur.execute("SELECT rolname FROM pg_roles WHERE rolname='replicator'")
    row = cur.fetchone()
    print(f"  replicator role exists: {bool(row)}")
    cur.execute("SELECT * FROM pg_stat_replication")
    rows = cur.fetchall()
    print(f"  active replication slots: {len(rows)}")
    conn.close()

def cmd_create_role():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT rolname FROM pg_roles WHERE rolname='replicator'")
    if cur.fetchone():
        print("Role 'replicator' already exists.")
    else:
        pwd = getpass.getpass("Set password for replicator role: ")
        cur.execute(f"CREATE ROLE replicator WITH REPLICATION LOGIN PASSWORD '{pwd}'")
        print("Role 'replicator' created with REPLICATION LOGIN.")
        print("\nNow add this line to D:/PostgreSQL_Data/pg_hba.conf:")
        print("  host    replication     replicator      127.0.0.1/32            scram-sha-256")
        print("Then run: SELECT pg_reload_conf(); on the primary.")
    conn.close()

def cmd_bootstrap():
    repl_pwd = os.environ.get("PG_REPLICATOR_PASSWORD")
    if not repl_pwd:
        repl_pwd = getpass.getpass("Replicator password (or set PG_REPLICATOR_PASSWORD env): ")

    if os.path.exists(REPLICA_DATA):
        print(f"WARNING: {REPLICA_DATA} already exists. Remove it first to re-bootstrap.")
        sys.exit(1)

    env = os.environ.copy()
    env["PGPASSWORD"] = repl_pwd

    print(f"Running pg_basebackup to {REPLICA_DATA}...")
    result = subprocess.run([
        os.path.join(PG_BIN, "pg_basebackup"),
        "-h", "127.0.0.1", "-p", "5432", "-U", "replicator",
        "-D", REPLICA_DATA,
        "-R", "-P", "--wal-method=stream"
    ], env=env)

    if result.returncode != 0:
        print("pg_basebackup FAILED.")
        sys.exit(1)

    # Patch port
    conf_path = os.path.join(REPLICA_DATA, "postgresql.conf")
    with open(conf_path, "a") as f:
        f.write("\nport = 5433\n")
        f.write("hot_standby = on\n")
    print(f"Set port=5433 in {conf_path}")
    print("Bootstrap complete. Run: python phase3_setup.py start")

def cmd_start():
    pg_ctl = os.path.join(PG_BIN, "pg_ctl")
    result = subprocess.run([pg_ctl, "-D", REPLICA_DATA, "start"], capture_output=False)
    if result.returncode == 0:
        print("Replica started on port 5433.")
    else:
        print("pg_ctl start failed.")

def cmd_verify():
    import psycopg
    try:
        conn = psycopg.connect("postgresql://postgres:100978@localhost:5433/postgres")
        cur = conn.cursor()
        cur.execute("SELECT pg_is_in_recovery()")
        in_recovery = cur.fetchone()[0]
        print(f"Replica connected on port 5433. in_recovery={in_recovery}")
        cur.execute("SELECT count(*) FROM app.security_events")
        count = cur.fetchone()[0]
        print(f"Replica row count in app.security_events: {count}")
        conn.close()

        # Compare with primary
        p_conn = psycopg.connect(PRIMARY_DSN)
        p_cur = p_conn.cursor()
        p_cur.execute("SELECT count(*) FROM app.security_events")
        p_count = p_cur.fetchone()[0]
        print(f"Primary row count in app.security_events: {p_count}")
        print(f"Row count match: {count == p_count}")
        p_conn.close()
    except Exception as e:
        print(f"Verify FAILED: {e}")

if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "check"
    {"check": cmd_check, "create_role": cmd_create_role,
     "bootstrap": cmd_bootstrap, "start": cmd_start, "verify": cmd_verify}.get(cmd, cmd_check)()
