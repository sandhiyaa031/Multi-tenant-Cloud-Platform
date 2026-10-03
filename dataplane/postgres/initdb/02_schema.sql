-- 02_schema.sql
-- The managed workload database: a CH-benCHmark-derived schema (TPC-C tables plus
-- the three reference tables the CH analytical queries join against), laid out
-- for pooled multi-tenancy.
--
--   * Tenants share every table.
--   * A tenant owns a contiguous range of warehouse ids.
--   * The six large tables are range-partitioned on warehouse id, one partition
--     per tenant, so an index can be built for one tenant only (action A1) and
--     its write cost falls on that tenant's rows.
--
-- This schema follows the published specifications but is not an audited TPC
-- implementation; results obtained with it are not TPC results.

CREATE EXTENSION IF NOT EXISTS pg_stat_statements;
CREATE EXTENSION IF NOT EXISTS hypopg;

CREATE SCHEMA ch;

-- Global, read-only reference data.
CREATE TABLE ch.item (
    i_id     integer PRIMARY KEY,
    i_im_id  integer NOT NULL,
    i_name   varchar(24) NOT NULL,
    i_price  numeric(5, 2) NOT NULL,
    i_data   varchar(50) NOT NULL
);
CREATE TABLE ch.region (
    r_regionkey  integer PRIMARY KEY,
    r_name       char(25) NOT NULL,
    r_comment    varchar(152)
);
CREATE TABLE ch.nation (
    n_nationkey  integer PRIMARY KEY,
    n_name       char(25) NOT NULL,
    n_regionkey  integer NOT NULL REFERENCES ch.region,
    n_comment    varchar(152)
);
CREATE TABLE ch.supplier (
    su_suppkey    integer PRIMARY KEY,
    su_name       char(25) NOT NULL,
    su_address    varchar(40) NOT NULL,
    su_nationkey  integer NOT NULL REFERENCES ch.nation,
    su_phone      char(15) NOT NULL,
    su_acctbal    numeric(12, 2) NOT NULL,
    su_comment    char(101) NOT NULL
);

-- Small per-tenant tables: not worth partitioning, isolated by RLS alone.
CREATE TABLE ch.warehouse (
    w_id        integer PRIMARY KEY,
    w_name      varchar(10) NOT NULL,
    w_street_1  varchar(20) NOT NULL,
    w_street_2  varchar(20) NOT NULL,
    w_city      varchar(20) NOT NULL,
    w_state     char(2) NOT NULL,
    w_zip       char(9) NOT NULL,
    w_tax       numeric(4, 4) NOT NULL,
    w_ytd       numeric(12, 2) NOT NULL
);
CREATE TABLE ch.district (
    d_w_id       integer NOT NULL REFERENCES ch.warehouse,
    d_id         smallint NOT NULL,
    d_name       varchar(10) NOT NULL,
    d_street_1   varchar(20) NOT NULL,
    d_street_2   varchar(20) NOT NULL,
    d_city       varchar(20) NOT NULL,
    d_state      char(2) NOT NULL,
    d_zip        char(9) NOT NULL,
    d_tax        numeric(4, 4) NOT NULL,
    d_ytd        numeric(12, 2) NOT NULL,
    d_next_o_id  integer NOT NULL,
    PRIMARY KEY (d_w_id, d_id)
);

-- Large tables: partitioned by warehouse id. PostgreSQL requires the partition
-- key in every unique constraint, which the TPC-C keys already satisfy.
CREATE TABLE ch.customer (
    c_w_id          integer NOT NULL,
    c_d_id          smallint NOT NULL,
    c_id            integer NOT NULL,
    c_first         varchar(16) NOT NULL,
    c_middle        char(2) NOT NULL,
    c_last          varchar(16) NOT NULL,
    c_street_1      varchar(20) NOT NULL,
    c_street_2      varchar(20) NOT NULL,
    c_city          varchar(20) NOT NULL,
    c_state         char(2) NOT NULL,
    c_zip           char(9) NOT NULL,
    c_phone         char(16) NOT NULL,
    c_since         timestamptz NOT NULL,
    c_credit        char(2) NOT NULL,
    c_credit_lim    numeric(12, 2) NOT NULL,
    c_discount      numeric(4, 4) NOT NULL,
    c_balance       numeric(12, 2) NOT NULL,
    c_ytd_payment   numeric(12, 2) NOT NULL,
    c_payment_cnt   integer NOT NULL,
    c_delivery_cnt  integer NOT NULL,
    c_data          varchar(500) NOT NULL,
    c_n_nationkey   integer NOT NULL,
    PRIMARY KEY (c_w_id, c_d_id, c_id)
) PARTITION BY RANGE (c_w_id);
CREATE INDEX customer_name_idx ON ch.customer (c_w_id, c_d_id, c_last, c_first);

CREATE TABLE ch.history (
    h_c_id    integer NOT NULL,
    h_c_d_id  smallint NOT NULL,
    h_c_w_id  integer NOT NULL,
    h_d_id    smallint NOT NULL,
    h_w_id    integer NOT NULL,
    h_date    timestamptz NOT NULL,
    h_amount  numeric(6, 2) NOT NULL,
    h_data    varchar(24) NOT NULL
) PARTITION BY RANGE (h_w_id);

CREATE TABLE ch.orders (
    o_w_id        integer NOT NULL,
    o_d_id        smallint NOT NULL,
    o_id          integer NOT NULL,
    o_c_id        integer NOT NULL,
    o_entry_d     timestamptz NOT NULL,
    o_carrier_id  smallint,
    o_ol_cnt      smallint NOT NULL,
    o_all_local   smallint NOT NULL,
    PRIMARY KEY (o_w_id, o_d_id, o_id)
) PARTITION BY RANGE (o_w_id);
CREATE INDEX orders_customer_idx ON ch.orders (o_w_id, o_d_id, o_c_id, o_id);

CREATE TABLE ch.new_order (
    no_w_id  integer NOT NULL,
    no_d_id  smallint NOT NULL,
    no_o_id  integer NOT NULL,
    PRIMARY KEY (no_w_id, no_d_id, no_o_id)
) PARTITION BY RANGE (no_w_id);

CREATE TABLE ch.order_line (
    ol_w_id         integer NOT NULL,
    ol_d_id         smallint NOT NULL,
    ol_o_id         integer NOT NULL,
    ol_number       smallint NOT NULL,
    ol_i_id         integer NOT NULL,
    ol_supply_w_id  integer NOT NULL,
    ol_delivery_d   timestamptz,
    ol_quantity     smallint NOT NULL,
    ol_amount       numeric(6, 2) NOT NULL,
    ol_dist_info    char(24) NOT NULL,
    PRIMARY KEY (ol_w_id, ol_d_id, ol_o_id, ol_number)
) PARTITION BY RANGE (ol_w_id);

CREATE TABLE ch.stock (
    s_w_id        integer NOT NULL,
    s_i_id        integer NOT NULL,
    s_quantity    smallint NOT NULL,
    s_dist_01     char(24) NOT NULL,
    s_dist_02     char(24) NOT NULL,
    s_dist_03     char(24) NOT NULL,
    s_dist_04     char(24) NOT NULL,
    s_dist_05     char(24) NOT NULL,
    s_dist_06     char(24) NOT NULL,
    s_dist_07     char(24) NOT NULL,
    s_dist_08     char(24) NOT NULL,
    s_dist_09     char(24) NOT NULL,
    s_dist_10     char(24) NOT NULL,
    s_ytd         integer NOT NULL,
    s_order_cnt   integer NOT NULL,
    s_remote_cnt  integer NOT NULL,
    s_data        varchar(50) NOT NULL,
    s_su_suppkey  integer NOT NULL,
    PRIMARY KEY (s_w_id, s_i_id)
) PARTITION BY RANGE (s_w_id);

-- ── Tenancy ──────────────────────────────────────────────────────────────────

-- Which warehouse range each tenant role owns. The data-plane mirror of the
-- control plane's cp.tenants row.
CREATE TABLE ch.tenant_map (
    db_role  name PRIMARY KEY,
    w_lo     integer NOT NULL CHECK (w_lo >= 1),
    w_hi     integer NOT NULL,
    CHECK (w_hi >= w_lo),
    EXCLUDE USING gist (int4range(w_lo, w_hi, '[]') WITH &&)
);

-- session_user, not current_user: inside a SECURITY DEFINER function current_user
-- is the function owner, while session_user is still the tenant that logged in.
-- STABLE lets the planner evaluate these once per query and prune partitions.
CREATE FUNCTION ch.my_w_lo() RETURNS integer
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = ch, pg_temp AS
$$ SELECT w_lo FROM tenant_map WHERE db_role = session_user $$;

CREATE FUNCTION ch.my_w_hi() RETURNS integer
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = ch, pg_temp AS
$$ SELECT w_hi FROM tenant_map WHERE db_role = session_user $$;

-- A tenant sees and writes only rows of its own warehouses. A role with no
-- tenant_map row gets NULL bounds, so every comparison is NULL and it sees nothing.
DO $$
DECLARE
    t record;
BEGIN
    FOR t IN SELECT * FROM (VALUES
        ('warehouse', 'w_id'), ('district', 'd_w_id'), ('customer', 'c_w_id'), ('history', 'h_w_id'),
        ('orders', 'o_w_id'), ('new_order', 'no_w_id'), ('order_line', 'ol_w_id'), ('stock', 's_w_id')
    ) AS v (tbl, col)
    LOOP
        EXECUTE format('ALTER TABLE ch.%I ENABLE ROW LEVEL SECURITY', t.tbl);
        EXECUTE format(
            'CREATE POLICY tenant_rows ON ch.%1$I TO ch_tenant
                 USING (%2$I >= ch.my_w_lo() AND %2$I <= ch.my_w_hi())
                 WITH CHECK (%2$I >= ch.my_w_lo() AND %2$I <= ch.my_w_hi())', t.tbl, t.col);
        EXECUTE format('GRANT SELECT, INSERT, UPDATE, DELETE ON ch.%I TO ch_tenant', t.tbl);
    END LOOP;
END $$;

GRANT USAGE ON SCHEMA ch TO ch_tenant;
GRANT SELECT ON ch.item, ch.region, ch.nation, ch.supplier TO ch_tenant;
GRANT EXECUTE ON FUNCTION ch.my_w_lo(), ch.my_w_hi() TO ch_tenant;

-- Creates a tenant: its login role, its row in tenant_map, and its partition of
-- each large table. Tenants are never granted anything on the partitions
-- themselves, so the only way in is through the parent table and its policy.
CREATE OR REPLACE FUNCTION ch.provision_tenant(p_role name, p_password text, p_w_lo integer, p_w_hi integer)
RETURNS void
LANGUAGE plpgsql AS $$
DECLARE
    t text;
BEGIN
    IF p_role !~ '^[a-z][a-z0-9_]{2,62}$' THEN
        RAISE EXCEPTION 'invalid tenant role name: %', p_role;
    END IF;

    EXECUTE format('CREATE ROLE %I LOGIN PASSWORD %L IN ROLE ch_tenant', p_role, p_password);
    INSERT INTO ch.tenant_map (db_role, w_lo, w_hi) VALUES (p_role, p_w_lo, p_w_hi);

    FOREACH t IN ARRAY ARRAY['customer', 'history', 'orders', 'new_order', 'order_line', 'stock']
    LOOP
        EXECUTE format('CREATE TABLE ch.%I PARTITION OF ch.%I FOR VALUES FROM (%s) TO (%s)',
                       t || '_' || p_role, t, p_w_lo, p_w_hi + 1);
    END LOOP;
    -- New partitions and the new role become manageable by DBPilot's executor (executor.sql).
    IF to_regprocedure('ch.sync_executor()') IS NOT NULL THEN
        PERFORM ch.sync_executor();
    END IF;
END $$;

-- ── PgBouncer authentication lookup ──────────────────────────────────────────

CREATE SCHEMA pgbouncer;
-- Returns the SCRAM verifier for a tenant role, and for nothing else: the owner
-- and service roles cannot be reached through the pooler.
CREATE FUNCTION pgbouncer.get_auth(p_usename text)
RETURNS TABLE (usename name, passwd text)
LANGUAGE sql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
    SELECT s.usename, s.passwd
    FROM pg_shadow s
    WHERE s.usename = p_usename AND pg_has_role(s.usename, 'ch_tenant', 'MEMBER')
$$;
REVOKE ALL ON FUNCTION pgbouncer.get_auth(text) FROM PUBLIC;
GRANT USAGE ON SCHEMA pgbouncer TO pgbouncer_auth;
GRANT EXECUTE ON FUNCTION pgbouncer.get_auth(text) TO pgbouncer_auth;
