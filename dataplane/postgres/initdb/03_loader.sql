-- 03_loader.sql
-- Set-based data generator following the TPC-C population rules (cardinalities,
-- value ranges, 10% bad-credit customers, last 30% of orders undelivered) with
-- the CH-benCHmark additions (nation/region/supplier, c_n_nationkey, s_su_suppkey).
--
-- p_scale shrinks the per-district population for fast development runs:
-- 1.0 is the specification's 3,000 customers and 3,000 orders per district.

CREATE FUNCTION ch.rnd_str(p_len integer) RETURNS text
LANGUAGE sql VOLATILE AS
$$ SELECT substr(string_agg(md5(random()::text), ''), 1, p_len) FROM generate_series(1, p_len / 32 + 1) $$;

-- TPC-C customer last name: three syllables chosen by the digits of a number 0..999.
CREATE FUNCTION ch.last_name(p_num integer) RETURNS text
LANGUAGE sql IMMUTABLE AS $$
    SELECT s[p_num / 100 + 1] || s[(p_num / 10) % 10 + 1] || s[p_num % 10 + 1]
    FROM (SELECT ARRAY['BAR', 'OUGHT', 'ABLE', 'PRI', 'PRES', 'ESE', 'ANTI', 'CALLY', 'ATION', 'EING'] AS s) x
$$;

CREATE FUNCTION ch.rnd_int(p_lo integer, p_hi integer) RETURNS integer
LANGUAGE sql VOLATILE AS
$$ SELECT p_lo + floor(random() * (p_hi - p_lo + 1))::integer $$;

-- Reference data shared by all tenants. Idempotent: does nothing if already loaded.
CREATE PROCEDURE ch.load_reference(p_items integer DEFAULT 100000)
LANGUAGE plpgsql AS $$
BEGIN
    IF EXISTS (SELECT 1 FROM ch.item) THEN
        RETURN;
    END IF;

    INSERT INTO ch.region
    SELECT r, (ARRAY['AFRICA', 'AMERICA', 'ASIA', 'EUROPE', 'MIDDLE EAST'])[r + 1], ch.rnd_str(60)
    FROM generate_series(0, 4) r;

    INSERT INTO ch.nation
    SELECT n, 'NATION' || lpad(n::text, 2, '0'), n % 5, ch.rnd_str(60)
    FROM generate_series(0, 61) n;

    INSERT INTO ch.supplier
    SELECT s, 'Supplier#' || lpad(s::text, 9, '0'), ch.rnd_str(25), ch.rnd_int(0, 61),
           lpad(ch.rnd_int(1, 999999999)::text, 15, '0'), ch.rnd_int(-99999, 999999) / 100.0, ch.rnd_str(60)
    FROM generate_series(0, 9999) s;

    INSERT INTO ch.item
    SELECT i, ch.rnd_int(1, 10000), ch.rnd_str(ch.rnd_int(14, 24)), ch.rnd_int(100, 10000) / 100.0,
           CASE WHEN random() < 0.1 THEN 'ORIGINAL' || ch.rnd_str(20) ELSE ch.rnd_str(ch.rnd_int(26, 50)) END
    FROM generate_series(1, p_items) i;

    ANALYZE ch.region, ch.nation, ch.supplier, ch.item;
END $$;

-- Populates warehouses p_w_lo..p_w_hi. One transaction per warehouse, so a large
-- load does not hold one giant transaction and can be watched as it progresses.
CREATE PROCEDURE ch.load_warehouses(p_w_lo integer, p_w_hi integer, p_scale numeric DEFAULT 1.0)
LANGUAGE plpgsql AS $$
DECLARE
    v_w         integer;
    v_items     integer;
    v_cust      integer := greatest(30, round(3000 * p_scale))::integer;  -- customers = orders per district
    v_delivered integer;
BEGIN
    SELECT count(*) INTO v_items FROM ch.item;
    IF v_items = 0 THEN
        RAISE EXCEPTION 'load reference data first: CALL ch.load_reference()';
    END IF;
    v_delivered := (v_cust * 0.7)::integer;

    FOR v_w IN p_w_lo..p_w_hi LOOP
        INSERT INTO ch.warehouse
        VALUES (v_w, ch.rnd_str(8), ch.rnd_str(15), ch.rnd_str(15), ch.rnd_str(15), upper(ch.rnd_str(2)),
                lpad(ch.rnd_int(0, 9999)::text, 4, '0') || '11111', ch.rnd_int(0, 2000) / 10000.0, 300000);

        INSERT INTO ch.district
        SELECT v_w, d, ch.rnd_str(8), ch.rnd_str(15), ch.rnd_str(15), ch.rnd_str(15), upper(ch.rnd_str(2)),
               lpad(ch.rnd_int(0, 9999)::text, 4, '0') || '11111', ch.rnd_int(0, 2000) / 10000.0, 30000, v_cust + 1
        FROM generate_series(1, 10) d;

        INSERT INTO ch.stock
        SELECT v_w, i, ch.rnd_int(10, 100),
               ch.rnd_str(24), ch.rnd_str(24), ch.rnd_str(24), ch.rnd_str(24), ch.rnd_str(24),
               ch.rnd_str(24), ch.rnd_str(24), ch.rnd_str(24), ch.rnd_str(24), ch.rnd_str(24),
               0, 0, 0,
               CASE WHEN random() < 0.1 THEN 'ORIGINAL' || ch.rnd_str(20) ELSE ch.rnd_str(ch.rnd_int(26, 50)) END,
               (v_w * i) % 10000
        FROM generate_series(1, v_items) i;

        INSERT INTO ch.customer
        SELECT v_w, d, c, ch.rnd_str(ch.rnd_int(8, 16)), 'OE',
               ch.last_name(CASE WHEN c <= 1000 THEN c - 1 ELSE ch.rnd_int(0, 999) END),
               ch.rnd_str(15), ch.rnd_str(15), ch.rnd_str(15), upper(ch.rnd_str(2)),
               lpad(ch.rnd_int(0, 9999)::text, 4, '0') || '11111', lpad(ch.rnd_int(1, 999999999)::text, 16, '0'),
               now(), CASE WHEN random() < 0.1 THEN 'BC' ELSE 'GC' END, 50000, ch.rnd_int(0, 5000) / 10000.0,
               -10, 10, 1, 0, ch.rnd_str(ch.rnd_int(300, 500)), ch.rnd_int(0, 61)
        FROM generate_series(1, 10) d, generate_series(1, v_cust) c;

        INSERT INTO ch.history
        SELECT c_id, c_d_id, c_w_id, c_d_id, c_w_id, now(), 10, ch.rnd_str(ch.rnd_int(12, 24))
        FROM ch.customer WHERE c_w_id = v_w;

        -- Each district's orders are placed by a random permutation of its customers.
        INSERT INTO ch.orders
        SELECT v_w, d, o_id, c_id, now(),
               CASE WHEN o_id <= v_delivered THEN ch.rnd_int(1, 10) END, ch.rnd_int(5, 15), 1
        FROM generate_series(1, 10) d,
             LATERAL (SELECT c AS c_id, row_number() OVER (ORDER BY random(), d) AS o_id
                      FROM generate_series(1, v_cust) c) perm;

        INSERT INTO ch.new_order
        SELECT o_w_id, o_d_id, o_id FROM ch.orders WHERE o_w_id = v_w AND o_carrier_id IS NULL;

        INSERT INTO ch.order_line
        SELECT o_w_id, o_d_id, o_id, n, ch.rnd_int(1, v_items), o_w_id,
               CASE WHEN o_carrier_id IS NOT NULL THEN o_entry_d END, 5,
               CASE WHEN o_carrier_id IS NOT NULL THEN 0 ELSE ch.rnd_int(1, 999999) / 100.0 END,
               ch.rnd_str(24)
        FROM ch.orders, generate_series(1, o_ol_cnt) n
        WHERE o_w_id = v_w;

        COMMIT;
        RAISE NOTICE 'loaded warehouse %', v_w;
    END LOOP;
END $$;
