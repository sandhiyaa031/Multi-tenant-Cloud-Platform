import psycopg
conn = psycopg.connect("postgresql://postgres:100978@localhost:5432/postgres", autocommit=True)
cur = conn.cursor()
cur.execute("SELECT column_name, is_nullable FROM information_schema.columns WHERE table_schema='app' AND table_name='security_events'")
for row in cur.fetchall():
    print(row)
conn.close()
