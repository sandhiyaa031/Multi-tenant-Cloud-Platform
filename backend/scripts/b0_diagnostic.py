"""
B0 Diagnostic Script — DBPilot
All output goes to a file only. No stdout printing.
Run: python backend/scripts/b0_diagnostic.py
"""
import os, sys
import psycopg
from pathlib import Path
from dotenv import load_dotenv

# Disable any pager interference
os.environ["PAGER"] = ""
os.environ["PSQL_PAGER"] = ""

PROJECT_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(PROJECT_ROOT / "backend" / ".env")

PG_USER     = os.getenv("PG_USER", "postgres")
PG_PASSWORD = os.getenv("PG_PASSWORD", "postgres")
PG_HOST     = os.getenv("PG_HOST", "localhost")
PG_PORT     = os.getenv("PG_PORT", "5432")
PG_DB       = os.getenv("PG_DB", "postgres")
CONN_STR    = f"postgresql://{PG_USER}:{PG_PASSWORD}@{PG_HOST}:{PG_PORT}/{PG_DB}"

OUT_FILE = PROJECT_ROOT / "results" / "b0" / "diagnostic_clean.txt"

ANALYTICAL_QUERY = (
    "SELECT id_resp_p, sum(orig_ip_bytes), count(*) "
    "FROM research.ctu_conn_log "
    "GROUP BY id_resp_p "
    "ORDER BY count DESC LIMIT 10"
)


def rq(conn, sql):
    with conn.cursor() as cur:
        cur.execute(sql)
        return cur.fetchall()


def main():
    out = []

    def p(msg=""):
        out.append(str(msg))

    def sec(title):
        p()
        p("=" * 72)
        p(f"  {title}")
        p("=" * 72)

    with psycopg.connect(CONN_STR) as conn:

        # ── A) Environment ────────────────────────────────────────
        sec("A) PostgreSQL Environment")
        ver = rq(conn, "SELECT version()")[0][0]
        p(f"  version:  {ver}")

        for param in [
            "server_version", "shared_buffers", "effective_cache_size",
            "work_mem", "max_connections",
            "max_worker_processes", "max_parallel_workers",
            "max_parallel_workers_per_gather",
            "wal_buffers", "random_page_cost", "seq_page_cost",
        ]:
            val = rq(conn, f"SHOW {param}")[0][0]
            p(f"  {param:<45} {val}")

        # ── B) Footprint ──────────────────────────────────────────
        sec("B) DB Footprint: research.ctu_conn_log")
        r = rq(conn, """
            SELECT
              pg_size_pretty(pg_table_size('research.ctu_conn_log')),
              pg_size_pretty(pg_indexes_size('research.ctu_conn_log')),
              pg_size_pretty(pg_total_relation_size('research.ctu_conn_log')),
              (SELECT reltuples::bigint FROM pg_class c
               JOIN pg_namespace n ON n.oid=c.relnamespace
               WHERE n.nspname='research' AND c.relname='ctu_conn_log'),
              (SELECT relpages FROM pg_class c
               JOIN pg_namespace n ON n.oid=c.relnamespace
               WHERE n.nspname='research' AND c.relname='ctu_conn_log')
        """)[0]
        tbl_sz, idx_sz, tot_sz, est_rows, pages = r
        p(f"  table_size:          {tbl_sz}")
        p(f"  index_size:          {idx_sz}")
        p(f"  total_size:          {tot_sz}")
        p(f"  estimated_rows:      {est_rows:,}")
        p(f"  pages (8 KB each):   {pages:,}  = {pages*8/1024:.1f} MB")

        sb_pages = rq(conn, "SELECT setting::int FROM pg_settings WHERE name='shared_buffers'")[0][0]
        p(f"  shared_buffers:      {sb_pages:,} pages = {sb_pages*8/1024:.0f} MB")
        pct = pages / sb_pages * 100 if sb_pages else 0
        p(f"  table % of shbuf:    {pct:.1f}%  (<100 = fits in cache)")

        s = rq(conn, """
            SELECT seq_scan, seq_tup_read, idx_scan, idx_tup_fetch, n_live_tup
            FROM pg_stat_user_tables
            WHERE schemaname='research' AND relname='ctu_conn_log'
        """)
        if s:
            s = s[0]
            p(f"  seq_scan:            {s[0]:,}")
            p(f"  seq_tup_read:        {s[1]:,}")
            p(f"  idx_scan:            {s[2]:,}")
            p(f"  idx_tup_fetch:       {s[3]:,}")
            p(f"  n_live_tup:          {s[4]:,}")

        # ── B2) Indexes ───────────────────────────────────────────
        sec("B2) Indexes")
        idxs = rq(conn, """
            SELECT indexname, indexdef
            FROM pg_indexes
            WHERE schemaname='research' AND tablename='ctu_conn_log'
        """)
        for idx in idxs:
            p(f"  [{idx[0]}]  {idx[1]}")

        # ── C) Analytical EXPLAIN ─────────────────────────────────
        sec("C) EXPLAIN ANALYZE — Analytical Query")
        p(f"  SQL: {ANALYTICAL_QUERY}")
        plan = rq(conn, f"EXPLAIN (ANALYZE, BUFFERS, FORMAT TEXT) {ANALYTICAL_QUERY}")
        for row in plan:
            p(f"  {row[0]}")

        # ── D) Interactive EXPLAIN ────────────────────────────────
        sec("D) EXPLAIN ANALYZE — Interactive Query")
        # Get the most common source IP
        top5 = rq(conn, """
            SELECT id_orig_h, count(*) AS cnt
            FROM research.ctu_conn_log
            GROUP BY id_orig_h ORDER BY cnt DESC LIMIT 5
        """)
        p(f"  Top 5 source IPs by row count:")
        for ip, cnt in top5:
            p(f"    {ip}  ({cnt:,} rows)")

        real_ip = top5[0][0]
        int_sql = f"SELECT uid, conn_state FROM research.ctu_conn_log WHERE id_orig_h = '{real_ip}'"
        p(f"\n  EXPLAIN for: {int_sql}")
        iplan = rq(conn, f"EXPLAIN (ANALYZE, BUFFERS, FORMAT TEXT) {int_sql}")
        for row in iplan:
            p(f"  {row[0]}")

        # ── E) Timestamp coverage ─────────────────────────────────
        sec("E) Timestamp Coverage")
        cov = rq(conn, "SELECT min(ts), max(ts), count(*) FROM research.ctu_conn_log")[0]
        p(f"  min_ts:      {cov[0]}")
        p(f"  max_ts:      {cov[1]}")
        p(f"  total_rows:  {cov[2]:,}")

        dist = rq(conn, """
            SELECT date_trunc('day', ts)::date AS day, count(*)
            FROM research.ctu_conn_log
            GROUP BY day ORDER BY day
        """)
        p("\n  Daily distribution:")
        for d in dist:
            p(f"    {d[0]}    {d[1]:>10,}")

        p()
        p("=== DIAGNOSTIC COMPLETE ===")

    result = "\n".join(out)
    with open(OUT_FILE, "w", encoding="utf-8") as f:
        f.write(result)
    # Only write a done marker to stdout
    sys.stdout.write(f"Done. Output written to: {OUT_FILE}\n")


if __name__ == "__main__":
    main()
