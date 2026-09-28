import psycopg, uuid

conn = psycopg.connect("postgresql://postgres:100978@localhost:5432/postgres", autocommit=True)
cur = conn.cursor()

# Check ALL constraints on app.organizations
print("=== Constraints on app.organizations ===")
cur.execute("""
    SELECT conname, contype, pg_get_constraintdef(oid)
    FROM pg_constraint
    WHERE conrelid = 'app.organizations'::regclass
""")
for row in cur.fetchall():
    print(row)

# Check row count
cur.execute("SELECT count(*) FROM app.organizations")
print("\nTotal rows in organizations:", cur.fetchone()[0])

# Check last 5 rows
cur.execute("SELECT org_id, name FROM app.organizations ORDER BY org_id DESC LIMIT 5")
print("Last 5 orgs:", cur.fetchall())

# Try insert without ON CONFLICT
try:
    tid = str(uuid.uuid4())
    cur.execute("INSERT INTO app.organizations (org_id, name) VALUES (%s, %s)", (tid, 'Test Direct'))
    print(f"\nDirect insert succeeded, rowcount={cur.rowcount}")
    cur.execute("SELECT org_id, name FROM app.organizations WHERE org_id = %s", (tid,))
    print("Row found:", cur.fetchone())
except Exception as e:
    print(f"\nDirect insert FAILED: {e}")

conn.close()
