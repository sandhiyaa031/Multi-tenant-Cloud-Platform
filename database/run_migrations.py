import os
import psycopg
import sys

def run_migrations():
    # Use environment variables securely, avoiding hardcoded secrets
    host = os.environ.get("PG_HOST", "localhost")
    port = os.environ.get("PG_PORT", "5432")
    dbname = os.environ.get("PG_DB", "postgres")
    user = os.environ.get("PG_USER", "postgres")
    password = os.environ.get("PG_PASSWORD", "postgres")  # Default placeholder

    conn_str = f"host={host} port={port} dbname={dbname} user={user} password={password}"
    print(f"Connecting to database '{dbname}' at {host}:{port} as user '{user}'...")

    try:
        # Autocommit must be ON or we manually manage transactions strictly
        with psycopg.connect(conn_str, autocommit=True) as conn:
            print("[PASS] Connected successfully.\n")
            
            # Retrieve script paths
            base_dir = os.path.join(os.path.dirname(__file__), "schema_v2")
            scripts = ["01_schema.sql", "02_rls.sql", "03_tests.sql"]
            
            # Listen for RAISE NOTICE messages specifically for test outputs
            conn.add_notice_handler(lambda n: print(f"    -> {n.message_primary}"))
            
            with conn.cursor() as cur:
                for script in scripts:
                    path = os.path.join(base_dir, script)
                    print(f"Executing: {script}")
                    with open(path, "r", encoding="utf-8") as f:
                        sql = f.read()
                        try:
                            cur.execute(sql)
                            print(f"[PASS] {script} executed cleanly.\n")
                        except Exception as e:
                            print(f"\n[FAIL] Error executing {script}")
                            print(f"Details: {e}")
                            sys.exit(1)
                            
            print("========================================")
            print("All migrations and RLS tests PASSED!")
            print("Phase 1 Database Verification: SUCCESS")
            print("Phase 1 Checkpoint: GREEN")

    except psycopg.OperationalError as e:
        print(f"\n[FAIL] Could not authenticate or connect to PostgreSQL.")
        print(f"Database response: {e}")
        print("Please ensure the database service is running and credentials are correct.")
        
        # Display clear cross-platform usage instructions without exposing passwords
        print("\nTo run these migrations, export your environment variables:")
        print("Windows (PowerShell):")
        print("  $env:PG_PASSWORD='your_actual_password'; python run_migrations.py")
        print("Linux/Mac (Bash):")
        print("  PG_PASSWORD='your_actual_password' python run_migrations.py")
        sys.exit(1)

if __name__ == "__main__":
    run_migrations()
