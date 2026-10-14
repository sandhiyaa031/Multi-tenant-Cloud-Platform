"""One Chen-notation ER diagram for the whole DBPilot database design.
Sources: controlplane/migrations/001..008 (schema cp) and dataplane/postgres/initdb/02_schema.sql (schema ch).
Run: python make_chen.py
"""
import os

from chen_lib import Chen

OUT = os.path.dirname(os.path.abspath(__file__))


def A(*names):
    """'id*' = primary key (underlined), 'x~' = derived/generated (dashed), else plain."""
    out = []
    for n in names:
        out.append((n[:-1], "k") if n.endswith("*") else (n[:-1], "d") if n.endswith("~") else (n, "n"))
    return out


W, H = 92, 73.6
DY = 5.5   # control-plane block sits this much higher than drawn below
c = Chen(W, H, "DBPilot: entity-relationship diagram of the whole database design",
         "Two PostgreSQL databases: the control plane (DBPilot's own memory: 16 entities, plus memberships as the relationship MEMBER OF = 17 tables) and the data plane (the shared database the tenants use)")

# ── control-plane band ─────────────────────────────────────────────────────────────────────────
c.band(0.6, 31.4, 90.8, 38.6, "CONTROL-PLANE DATABASE  (schema cp)", "#047857", "#ecfdf5")


def ce(key, x, y, *a, **k):
    c.entity(key, x, y + DY, *a, **k)


def cr(key, x, y, *a, **k):
    c.rel(key, x, y + DY, *a, **k)

# identity row
ce("USER", 9, 59.5, "USER", A("id*", "email", "password_hash", "full_name", "is_active", "has_password~"), "T")
cr("MEMBER", 20.5, 59.5, "MEMBER\nOF", A("role", "created_at"), "T")
ce("ORG", 32, 59.5, "ORGANIZATION", A("id*", "slug", "name", "archived_at"), "T", w=4.2)
cr("RECORDS", 44, 59.5, "RECORDS")
ce("AUDIT", 56, 59.5, "AUDIT_LOG", A("id*", "action", "entity_type", "detail", "created_at"), "T")
cr("RECEIVES", 9, 54.5, "RECEIVES")
ce("TOKEN", 9, 49.8, "PASSWORD_\nRESET_TOKEN", A("id*", "token_hash", "purpose", "expires_at"), "L", size=13)
cr("OWNS", 32, 54.5, "OWNS")
ce("CLUSTER", 32, 49.8, "CLUSTER", A("id*", "name", "pooler_host", "database_name", "status", "primary_host"), "L", shift=2.3)
cr("HOSTS", 44, 49.8, "HOSTS")
ce("TENANT", 56, 49.8, "TENANT", A("id*", "db_role", "warehouse_lo", "warehouse_hi", "profile"), "T")

# tenant children (right column)
cr("HASOBJ", 65, 55.5, "HAS\nOBJECTIVE")
ce("SLO", 73, 55.5, "SLO", A("id*", "query_class", "percentile", "threshold_ms"), "R")
cr("GEN", 65, 49.8, "GENERATES")
ce("QSTATS", 73, 49.8, "QUERY_STATS", A("queryid*", "window_end*", "calls", "total_exec_ms", "wal_bytes"), "R")
cr("MEAS", 65, 42.6, "MEASURED\nBY")
ce("LAT", 73, 42.6, "LATENCY_STATS", A("query_class*", "window_end*", "p50_ms", "p95_ms", "p99_ms"), "R", size=14)

# cluster children
cr("SEES", 20, 44.2, "SEES")
ce("FPRINT", 20, 39.6, "QUERY_\nFINGERPRINT", A("queryid*", "query", "first_seen"), "L", size=13)
cr("REPORTS", 32, 44.2, "REPORTS")
ce("INST", 32, 39.6, "INSTANCE_STATS", A("window_end*", "xact_commit", "deadlocks", "wal_bytes", "database_bytes"), "B", size=14)

# proposal and its children
cr("RECVP", 43.5, 44.2, "RECEIVES")
cr("TARGETS", 51.5, 44.2, "TARGETS")
ce("PROP", 47.5, 39.6, "PROPOSAL", A("id*", "source", "action", "state", "gate_mode", "verification"), "L", w=7.4, shift=1.4)
cr("VERIFBY", 36.5, 34.6, "VERIFIED\nBY")
cr("TWINBY", 47.5, 34.6, "REPLAYED\nON TWIN")
cr("CANBY", 58.5, 34.6, "WATCHED\nBY")
ce("VERIF", 36.5, 30.2, "VERIFICATION_\nSTEP", A("id*", "tier", "decision", "summary", "seconds"), "B", size=12)
ce("TWIN", 47.5, 30.2, "TWIN_RUN", A("id*", "window_s", "transactions", "replay_errors", "verdict"), "B", size=14)
ce("CANARY", 58.5, 30.2, "CANARY", A("id*", "applied", "inverse", "contract", "outcome"), "B", size=14)
cr("GOVERNS", 62, 41, "GOVERNS\n(trigger)", dashed=True)
ce("TRANS", 71, 37.6, "PROPOSAL_\nTRANSITION", A("from_state*", "to_state*"), "R", size=12)

# relationship lines (control plane)
c.line("USER", "MEMBER", "m", "")
c.line("MEMBER", "ORG", "", "n")
c.line("USER", "RECEIVES", "1", "")
c.line("RECEIVES", "TOKEN", "", "n")
c.line("ORG", "RECORDS", "1", "")
c.line("RECORDS", "AUDIT", "", "n")
c.line("ORG", "OWNS", "1", "")
c.line("OWNS", "CLUSTER", "", "n")
c.line("CLUSTER", "HOSTS", "1", "")
c.line("HOSTS", "TENANT", "", "n")
c.line("TENANT", "HASOBJ", "1", "")
c.line("HASOBJ", "SLO", "", "n")
c.line("TENANT", "GEN", "1", "")
c.line("GEN", "QSTATS", "", "n")
c.line("TENANT", "MEAS", "1", "")
c.line("MEAS", "LAT", "", "n")
c.line("CLUSTER", "SEES", "1", "")
c.line("SEES", "FPRINT", "", "n")
c.line("CLUSTER", "REPORTS", "1", "")
c.line("REPORTS", "INST", "", "n")
c.line("CLUSTER", "RECVP", "1", "")
c.line("RECVP", "PROP", "", "n", via=[(43.5, 41.2 + DY)])
c.line("TENANT", "TARGETS", "0..1", "")
c.line("TARGETS", "PROP", "", "n", via=[(51.5, 41.2 + DY)])
c.line("PROP", "VERIFBY", "1", "")
c.line("VERIFBY", "VERIF", "", "n")
c.line("PROP", "TWINBY", "1", "")
c.line("TWINBY", "TWIN", "", "n")
c.line("PROP", "CANBY", "1", "")
c.line("CANBY", "CANARY", "", "1")
c.line("PROP", "GOVERNS", "", "", dashed=True)
c.line("GOVERNS", "TRANS", "", "n", dashed=True)

# ── data-plane band ────────────────────────────────────────────────────────────────────────────
c.band(0.6, 0.8, 90.8, 29.6, "DATA-PLANE DATABASE  (schema ch)", "#be123c", "#fff1f2")
c.entity("OL", 6, 12.4, "ORDER_LINE", A("ol_w_id*", "ol_d_id*", "ol_o_id*", "ol_number*", "ol_i_id"), "T")
c.rel("CONT", 14.5, 12.4, "CONTAINS", dashed=True)
c.entity("ORD", 23, 12.4, "ORDERS", A("o_w_id*", "o_d_id*", "o_id*", "o_c_id"), "B")
c.rel("PLACES", 31, 12.4, "PLACES", dashed=True)
c.entity("CUST", 39, 12.4, "CUSTOMER", A("c_w_id*", "c_d_id*", "c_id*", "c_balance"), "B")
c.rel("SERVES", 47, 12.4, "SERVES", dashed=True)
c.entity("DIST", 55, 12.4, "DISTRICT", A("d_w_id*", "d_id*", "d_next_o_id"), "T")
c.rel("HASD", 63, 12.4, "HAS")
c.entity("WH", 71, 12.4, "WAREHOUSE", A("w_id*", "w_ytd", "w_tax"), "T")
c.rel("RANGE", 79, 12.4, "OWNS\nRANGE", dashed=True)
c.entity("TMAP", 86, 12.4, "TENANT_MAP", A("db_role*", "w_lo", "w_hi"), "B", w=3.6, size=13)

c.rel("QUEUED", 23, 17.2, "QUEUED\nAS", dashed=True)
c.entity("NORD", 23, 21.6, "NEW_ORDER", A("no_w_id*", "no_d_id*", "no_o_id*"), "T")
c.rel("PAYS", 39, 17.2, "PAYS", dashed=True)
c.entity("HIST", 39, 21.6, "HISTORY", A("h_c_id", "h_date", "h_amount"), "T")

c.entity("ITEM", 30, 3.6, "ITEM", A("i_id*", "i_name", "i_price"), "T")
c.rel("STOCKEDAS", 39.5, 3.6, "STOCKED\nAS", dashed=True)
c.entity("STOCK", 49, 3.6, "STOCK", A("s_w_id*", "s_i_id*", "s_quantity"), "T")
c.rel("APPEARS", 14.5, 6.8, "APPEARS\nIN", dashed=True)
c.rel("STOCKS", 62, 6.8, "STOCKS", dashed=True)

c.entity("REG", 28, 25.6, "REGION", A("r_regionkey*", "r_name"), "T")
c.rel("RCONT", 37, 25.6, "CONTAINS")
c.entity("NAT", 46, 25.6, "NATION", A("n_nationkey*", "n_name"), "T")
c.rel("HOME", 55, 25.6, "HOME OF")
c.entity("SUPP", 64, 25.6, "SUPPLIER", A("su_suppkey*", "su_name"), "T")

c.line("OL", "CONT", "n", "")
c.line("CONT", "ORD", "", "1")
c.line("ORD", "PLACES", "n", "")
c.line("PLACES", "CUST", "", "1")
c.line("CUST", "SERVES", "n", "")
c.line("SERVES", "DIST", "", "1")
c.line("DIST", "HASD", "n", "")
c.line("HASD", "WH", "", "1")
c.line("WH", "RANGE", "n", "")
c.line("RANGE", "TMAP", "", "1", dashed=True)
c.line("ORD", "QUEUED", "1", "")
c.line("QUEUED", "NORD", "", "0..1")
c.line("CUST", "PAYS", "1", "")
c.line("PAYS", "HIST", "", "n")
c.line("ITEM", "APPEARS", "1", "")
c.line("APPEARS", "OL", "", "n")
c.line("ITEM", "STOCKEDAS", "1", "")
c.line("STOCKEDAS", "STOCK", "", "n")
c.line("WH", "STOCKS", "1", "")
c.line("STOCKS", "STOCK", "", "n")
c.line("REG", "RCONT", "1", "")
c.line("RCONT", "NAT", "", "n")
c.line("NAT", "HOME", "1", "")
c.line("HOME", "SUPP", "", "n")

# ── the one link between the two databases ─────────────────────────────────────────────────────
c.rel("MIRROR", 86, 38 + 2.0, "MIRRORED\nAS", dashed=True)
c.line("TENANT", "MIRROR", "1", "", via=[(60, 46.2 + DY), (86, 46.2 + DY)], dashed=True)
c.line("MIRROR", "TMAP", "", "1", via=[(86, 14.5)], dashed=True)
c.text(84.2, 35.2, "the same tenant, kept in two databases\n(no cross-database foreign key)", size=14, ha="right")

# ── legend ─────────────────────────────────────────────────────────────────────────────────────
c.text(61, 69.0,
       "How to read it\n"
       "rectangle = entity (a table)      diamond = relationship (a foreign key)      oval = attribute\n"
       "underlined = primary key      dashed oval = generated column      m / n / 1 / 0..1 = cardinality\n"
       "dashed diamond = a relationship the database does NOT declare as a foreign key (logical, or kept by a trigger)\n"
       "jsonb columns (action, verdict, applied, inverse) hold structured documents inside one column",
       size=15)
c.text(1.6, 5.4,
       "Only 3 foreign keys are declared among the ch tables\n(nation>region, supplier>nation, district>warehouse).\nThe other TPC-C links are logical (dashed).",
       size=15)

c.save("DBPilot_ER_diagram", OUT, dpi=100)
print("done")
