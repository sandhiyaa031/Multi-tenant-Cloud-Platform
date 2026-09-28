"""
Focused debug of test_tenant_isolation_on_replica
"""
import psycopg, uuid, traceback, time

PRIMARY_DSN = "postgresql://postgres:100978@localhost:5432/postgres"
REPLICA_DSN = "postgresql://postgres:100978@localhost:5433/postgres"

tid_a = str(uuid.uuid4())
tid_b = str(uuid.uuid4())

print(f"tid_a={tid_a}")
print(f"tid_b={tid_b}")

# Step 1: Insert orgs with autocommit
print("\n--- Inserting orgs ---")
p_conn = psycopg.connect(PRIMARY_DSN, autocommit=True)
cur = p_conn.cursor()

try:
    cur.execute("INSERT INTO app.organizations (org_id, name) VALUES (%s, %s) ON CONFLICT DO NOTHING", (tid_a, 'Mock A'))
    print(f"org A: rowcount={cur.rowcount}")
    cur.execute("INSERT INTO app.organizations (org_id, name) VALUES (%s, %s) ON CONFLICT DO NOTHING", (tid_b, 'Mock B'))
    print(f"org B: rowcount={cur.rowcount}")
except Exception as e:
    print("Org insert failed:", e); traceback.print_exc()

# Verify orgs exist
cur.execute("SELECT count(*) FROM app.organizations WHERE org_id IN (%s, %s)", (tid_a, tid_b))
print(f"orgs visible in primary: {cur.fetchone()[0]}")

# Step 2: Insert security events
print("\n--- Inserting security_events ---")
try:
    cur.execute("""
        INSERT INTO app.security_events (org_id, ts, uid, id_orig_h, id_orig_p, id_resp_h, id_resp_p, proto)
        VALUES (%s, NOW(), %s, '1.2.3.4', 1234, '5.6.7.8', 443, 'tcp')
    """, (tid_a, str(uuid.uuid4())))
    print(f"event A: {cur.rowcount}")
    cur.execute("""
        INSERT INTO app.security_events (org_id, ts, uid, id_orig_h, id_orig_p, id_resp_h, id_resp_p, proto)
        VALUES (%s, NOW(), %s, '1.2.3.4', 1234, '5.6.7.8', 443, 'tcp')
    """, (tid_b, str(uuid.uuid4())))
    print(f"event B: {cur.rowcount}")
except Exception as e:
    print("Security event insert failed:", e); traceback.print_exc()

p_conn.close()

print("\n--- Waiting 2s for replication ---")
time.sleep(2)

# Step 3: Read on replica
print("\n--- Reading on replica ---")
r_conn = psycopg.connect(REPLICA_DSN)
try:
    with r_conn.transaction():
        cur_r = r_conn.cursor()
        cur_r.execute("SELECT current_user, pg_is_in_recovery()")
        print("Before SET ROLE:", cur_r.fetchone())

        cur_r.execute("SET LOCAL ROLE dbpilot_app")
        cur_r.execute(f"SET LOCAL app.tenant_id = '{tid_a}'")

        cur_r.execute("SELECT current_user, current_setting('app.tenant_id', true)")
        print("After SET ROLE:", cur_r.fetchone())

        cur_r.execute("SELECT count(*) FROM app.security_events WHERE org_id = %s", (tid_b,))
        count_b = cur_r.fetchone()[0]
        print(f"Tenant B rows visible to Tenant A: {count_b}")
except Exception as e:
    print("Replica read failed:", e); traceback.print_exc()
finally:
    r_conn.close()
