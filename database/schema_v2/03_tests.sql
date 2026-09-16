-- 03_tests.sql
-- Seed tests for constraints and tenant isolation.

-- Need to run this as superuser/owner initially
DO $$ 
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'dbpilot_app') THEN
        CREATE ROLE dbpilot_app LOGIN;
    END IF;
    
    -- Strip any accidental superuser privileges, securing RLS enforcement
    ALTER ROLE dbpilot_app NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;

    -- Grant access
    GRANT USAGE ON SCHEMA app TO dbpilot_app;
    GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA app TO dbpilot_app;
    GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA app TO dbpilot_app;
    
    GRANT USAGE ON SCHEMA research TO dbpilot_app;
    GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA research TO dbpilot_app;
    GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA research TO dbpilot_app;
END
$$;

-- Seed Organizations
INSERT INTO app.organizations (org_id, name) VALUES 
('11111111-1111-1111-1111-111111111111', 'CyberGuard MSSP') ON CONFLICT DO NOTHING;
INSERT INTO app.organizations (org_id, name) VALUES 
('22222222-2222-2222-2222-222222222222', 'Sentinel Dynamics') ON CONFLICT DO NOTHING;

-- Seed Users mapped to db roles
INSERT INTO app.users (user_id, org_id, email, db_role) VALUES 
('aaaa1111-1111-1111-1111-111111111111', '11111111-1111-1111-1111-111111111111', 'admin@cyberguard.com', 'none') ON CONFLICT DO NOTHING;
INSERT INTO app.users (user_id, org_id, email, db_role) VALUES 
('bbbb2222-2222-2222-2222-222222222222', '22222222-2222-2222-2222-222222222222', 'admin@sentinel.com', 'none') ON CONFLICT DO NOTHING;

-- Insert Seed Events
INSERT INTO app.security_events (org_id, uid, id_orig_h, id_resp_h, id_orig_p, id_resp_p, proto, ts, duration, orig_bytes, resp_bytes) VALUES
('11111111-1111-1111-1111-111111111111', 'F-ALPHA-1', '192.168.1.10', '10.0.0.5', 1234, 80, 'tcp', '2017-07-05 10:00:00', 0.1, 500, 2000),
('11111111-1111-1111-1111-111111111111', 'F-ALPHA-2', '192.168.1.11', '10.0.0.5', 1235, 80, 'tcp', '2017-07-05 10:05:00', 0.5, 400, 100),
('22222222-2222-2222-2222-222222222222', 'F-BETA-1', '172.16.0.50', '8.8.8.8', 53, 53, 'udp', '2017-07-05 11:00:00', 0.05, 50, 50) ON CONFLICT DO NOTHING;

-- Insert into research schema to verify it works without context
INSERT INTO research.workload_runs (phase) VALUES ('phase1_test');

-- Swap to the non-privileged application role to test RLS
SET ROLE dbpilot_app;

-- Transaction 1: Org A Context
BEGIN;
SELECT set_config('app.tenant_id', '11111111-1111-1111-1111-111111111111', true);
DO $$
DECLARE cnt INT;
BEGIN
    SELECT count(*) INTO cnt FROM app.security_events;
    IF cnt != 2 THEN RAISE EXCEPTION 'Org A got % rows, expected 2', cnt; END IF;
    
    SELECT count(*) INTO cnt FROM app.security_events WHERE uid = 'F-BETA-1';
    IF cnt != 0 THEN RAISE EXCEPTION 'Org A can see Org B!'; END IF;
    
    BEGIN
        INSERT INTO app.security_events (org_id, uid, id_orig_h, id_resp_h, id_orig_p, id_resp_p, proto, ts) 
        VALUES ('22222222-2222-2222-2222-222222222222', 'F-BETA-ATTEMPT', '1.1.1.1', '8.8.8.8', 80, 80, 'tcp', '2017-07-05');
        RAISE EXCEPTION 'Org A was able to insert Org B rows!';
    EXCEPTION WHEN insufficient_privilege THEN NULL;
    END;
END $$;
COMMIT;

-- Transaction 2: No context (Ensure state was destroyed by previous COMMIT)
BEGIN;
DO $$
DECLARE cnt INT;
BEGIN
    SELECT count(*) INTO cnt FROM app.security_events;
    IF cnt != 0 THEN RAISE EXCEPTION 'Context leak! 0 rows expected, got %', cnt; END IF;
END $$;
COMMIT;

-- Transaction 3: Org B Context
BEGIN;
SELECT set_config('app.tenant_id', '22222222-2222-2222-2222-222222222222', true);
DO $$
DECLARE cnt INT;
BEGIN
    SELECT count(*) INTO cnt FROM app.security_events;
    IF cnt != 1 THEN RAISE EXCEPTION 'Org B got % rows, expected 1', cnt; END IF;
END $$;
COMMIT;

-- Transaction 4: Verify research schema ignores tenant boundaries completely
BEGIN;
DO $$
DECLARE cnt INT;
BEGIN
    SELECT count(*) INTO cnt FROM research.workload_runs;
    IF cnt = 0 THEN RAISE EXCEPTION 'Research schema blocked unexpectedly!'; END IF;
    RAISE NOTICE 'SUCCESS: RLS implementation completely secure and context safe!';
END $$;
COMMIT;
