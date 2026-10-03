-- 007_proposals.sql
-- The optimisation lifecycle as data: proposal -> verification steps -> twin run
-- -> canary -> outcome. Every row is kept; together they are the outcome ledger.

CREATE TABLE cp.proposals (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id            uuid NOT NULL,
    cluster_id        uuid NOT NULL,
    target_tenant_id  uuid,                      -- NULL for instance-wide actions
    source            text NOT NULL CHECK (source IN ('manual', 'rule', 'agent')),
    action            jsonb NOT NULL,            -- a validated typed action; never SQL
    rationale         text NOT NULL DEFAULT '',
    evidence          jsonb NOT NULL DEFAULT '{}'::jsonb,
    gate_mode         text NOT NULL DEFAULT 'per_tenant' CHECK (gate_mode IN ('per_tenant', 'aggregate')),
    -- 'full' runs every tier; the others exist so that experiments can measure
    -- what each tier contributes.
    verification      text NOT NULL DEFAULT 'full' CHECK (verification IN ('full', 'canary_only', 'none')),
    -- Whether a verified proposal proceeds to canary without waiting for a person.
    auto_approve      boolean NOT NULL DEFAULT false,
    state             text NOT NULL DEFAULT 'PROPOSED',
    state_reason      text NOT NULL DEFAULT '',
    created_by        uuid,
    decided_by        uuid,
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now(),
    UNIQUE (id, org_id),
    FOREIGN KEY (cluster_id, org_id) REFERENCES cp.clusters (id, org_id) ON DELETE CASCADE,
    FOREIGN KEY (target_tenant_id, org_id) REFERENCES cp.tenants (id, org_id) ON DELETE CASCADE
);
CREATE INDEX proposals_cluster_idx ON cp.proposals (cluster_id, created_at DESC);
-- The engine's work queue: only rows it can act on are indexed.
CREATE INDEX proposals_queue_idx ON cp.proposals (created_at)
    WHERE state IN ('PROPOSED', 'APPROVED', 'ROLLBACK_REQUESTED');
-- At most one change may be in canary on a cluster at a time, so that any
-- regression observed can be attributed to it.
CREATE UNIQUE INDEX proposals_one_canary_per_cluster ON cp.proposals (cluster_id) WHERE state = 'CANARY';

-- The lifecycle is a state machine. Which moves are legal is data, checked by a trigger,
-- so no code path (API, engine, or manual SQL) can skip verification or resurrect a
-- rejected proposal.
CREATE TABLE cp.proposal_transitions (
    from_state  text NOT NULL,
    to_state    text NOT NULL,
    PRIMARY KEY (from_state, to_state)
);
INSERT INTO cp.proposal_transitions VALUES
    ('PROPOSED', 'VERIFYING'), ('VERIFYING', 'ADVISORY'),
    ('VERIFYING', 'REJECTED'), ('VERIFYING', 'INCONCLUSIVE'), ('VERIFYING', 'AWAITING_APPROVAL'),
    ('VERIFYING', 'APPROVED'), ('VERIFYING', 'FAILED'),
    ('INCONCLUSIVE', 'APPROVED'), ('INCONCLUSIVE', 'REJECTED'),          -- a human decides
    ('AWAITING_APPROVAL', 'APPROVED'), ('AWAITING_APPROVAL', 'REJECTED'),
    ('APPROVED', 'CANARY'), ('APPROVED', 'FAILED'),
    ('CANARY', 'APPLIED'), ('CANARY', 'ROLLED_BACK'), ('CANARY', 'FAILED'),
    ('APPLIED', 'ROLLBACK_REQUESTED'), ('ROLLBACK_REQUESTED', 'ROLLED_BACK'),
    ('ROLLBACK_REQUESTED', 'FAILED');

CREATE FUNCTION cp.proposal_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.action IS DISTINCT FROM OLD.action OR NEW.cluster_id <> OLD.cluster_id
       OR NEW.gate_mode <> OLD.gate_mode OR NEW.verification <> OLD.verification
       OR NEW.auto_approve <> OLD.auto_approve THEN
        RAISE EXCEPTION 'a proposal''s action and verification settings cannot be changed' USING ERRCODE = 'DP003';
    END IF;
    IF NEW.state <> OLD.state THEN
        IF NOT EXISTS (SELECT 1 FROM cp.proposal_transitions
                       WHERE from_state = OLD.state AND to_state = NEW.state) THEN
            RAISE EXCEPTION 'illegal proposal transition % -> %', OLD.state, NEW.state USING ERRCODE = 'DP003';
        END IF;
        NEW.updated_at := now();
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER proposals_guard BEFORE UPDATE ON cp.proposals
    FOR EACH ROW EXECUTE FUNCTION cp.proposal_guard();
CREATE TRIGGER proposals_audit AFTER INSERT OR UPDATE ON cp.proposals
    FOR EACH ROW EXECUTE FUNCTION cp.audit_row();

CREATE TABLE cp.verification_steps (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    proposal_id  uuid NOT NULL,
    org_id       uuid NOT NULL,
    tier         text NOT NULL CHECK (tier IN ('T0', 'T1', 'T2', 'T3')),
    decision     text NOT NULL CHECK (decision IN ('APPROVE', 'REJECT', 'INCONCLUSIVE', 'SKIPPED')),
    summary      text NOT NULL,
    detail       jsonb NOT NULL DEFAULT '{}'::jsonb,
    seconds      double precision NOT NULL DEFAULT 0,
    created_at   timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (proposal_id, org_id) REFERENCES cp.proposals (id, org_id) ON DELETE CASCADE
);
CREATE INDEX verification_steps_proposal_idx ON cp.verification_steps (proposal_id, id);

CREATE TABLE cp.twin_runs (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    proposal_id   uuid NOT NULL,
    org_id        uuid NOT NULL,
    window_s      double precision NOT NULL,
    transactions  integer NOT NULL,
    repetitions   integer NOT NULL,
    replay_errors integer NOT NULL,
    wal_ratio     double precision,
    storage_delta_bytes bigint NOT NULL DEFAULT 0,
    apply_seconds double precision,
    verdict       jsonb NOT NULL,     -- decision, per-tenant effects with intervals, rollback contract
    seconds       double precision NOT NULL,
    created_at    timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (proposal_id, org_id) REFERENCES cp.proposals (id, org_id) ON DELETE CASCADE
);
CREATE INDEX twin_runs_proposal_idx ON cp.twin_runs (proposal_id);

CREATE TABLE cp.canaries (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    proposal_id   uuid NOT NULL UNIQUE,
    org_id        uuid NOT NULL,
    applied       jsonb NOT NULL,     -- statements run on production
    inverse       jsonb NOT NULL,     -- statements that undo them
    contract      jsonb NOT NULL,     -- "tenant/CLASS" -> largest acceptable p95 ratio
    baseline      jsonb NOT NULL,     -- "tenant/CLASS" -> p95 before the change
    observations  jsonb NOT NULL DEFAULT '[]'::jsonb,
    result        jsonb,              -- "tenant/CLASS" -> observed ratio over the whole canary
    outcome       text CHECK (outcome IN ('HELD', 'ROLLED_BACK')),
    outcome_reason text NOT NULL DEFAULT '',
    started_at    timestamptz NOT NULL DEFAULT now(),
    finished_at   timestamptz,
    FOREIGN KEY (proposal_id, org_id) REFERENCES cp.proposals (id, org_id) ON DELETE CASCADE
);

-- LEARN: one row per proposal joining what was predicted with what happened.
-- security_invoker makes the view obey the caller's row-level security.
CREATE VIEW cp.outcome_ledger WITH (security_invoker = true) AS
SELECT p.id AS proposal_id, p.org_id, p.cluster_id, p.target_tenant_id, p.source, p.action,
       p.action ->> 'type' AS action_type, p.gate_mode, p.verification, p.state, p.state_reason, p.created_at,
       t.verdict ->> 'decision' AS twin_decision,
       t.verdict -> 'effects'   AS twin_effects,
       t.replay_errors, t.transactions AS twin_transactions,
       c.outcome AS canary_outcome, c.outcome_reason AS canary_reason,
       c.contract, c.result AS production_ratios
FROM cp.proposals p
LEFT JOIN LATERAL (SELECT * FROM cp.twin_runs r WHERE r.proposal_id = p.id ORDER BY r.created_at DESC LIMIT 1) t ON true
LEFT JOIN cp.canaries c ON c.proposal_id = p.id;

-- ── Access ───────────────────────────────────────────────────────────────────

ALTER TABLE cp.proposals          ENABLE ROW LEVEL SECURITY;
ALTER TABLE cp.verification_steps ENABLE ROW LEVEL SECURITY;
ALTER TABLE cp.twin_runs          ENABLE ROW LEVEL SECURITY;
ALTER TABLE cp.canaries           ENABLE ROW LEVEL SECURITY;

CREATE POLICY proposals_read ON cp.proposals FOR SELECT TO dbpilot_api
    USING (org_id = cp.current_org() AND cp.has_role('VIEWER'));
CREATE POLICY proposals_insert ON cp.proposals FOR INSERT TO dbpilot_api
    WITH CHECK (org_id = cp.current_org() AND cp.has_role('OPERATOR'));
CREATE POLICY proposals_update ON cp.proposals FOR UPDATE TO dbpilot_api
    USING (org_id = cp.current_org() AND cp.has_role('OPERATOR'))
    WITH CHECK (org_id = cp.current_org() AND cp.has_role('OPERATOR'));
GRANT SELECT, INSERT ON cp.proposals TO dbpilot_api;
-- Members can move a proposal between states (approve, reject); they cannot edit anything else.
GRANT UPDATE (state, state_reason, decided_by) ON cp.proposals TO dbpilot_api;
GRANT SELECT ON cp.proposal_transitions TO dbpilot_api, dbpilot_engine;

DO $$
DECLARE
    t text;
BEGIN
    FOREACH t IN ARRAY ARRAY['verification_steps', 'twin_runs', 'canaries'] LOOP
        EXECUTE format('CREATE POLICY %1$s_read ON cp.%1$I FOR SELECT TO dbpilot_api
                            USING (org_id = cp.current_org() AND cp.has_role(''VIEWER''))', t);
        EXECUTE format('GRANT SELECT ON cp.%I TO dbpilot_api', t);
    END LOOP;
    -- The engine works across organizations, like the collector, with its own role.
    FOREACH t IN ARRAY ARRAY['proposals', 'verification_steps', 'twin_runs', 'canaries'] LOOP
        EXECUTE format('CREATE POLICY %1$s_engine ON cp.%1$I FOR ALL TO dbpilot_engine USING (true) WITH CHECK (true)', t);
        EXECUTE format('GRANT SELECT, INSERT, UPDATE ON cp.%I TO dbpilot_engine', t);
    END LOOP;
    FOREACH t IN ARRAY ARRAY['clusters', 'tenants', 'slos', 'latency_stats', 'query_stats', 'query_fingerprints'] LOOP
        EXECUTE format('CREATE POLICY %1$s_engine_read ON cp.%1$I FOR SELECT TO dbpilot_engine USING (true)', t);
        EXECUTE format('GRANT SELECT ON cp.%I TO dbpilot_engine', t);
    END LOOP;
END $$;
GRANT USAGE ON SCHEMA cp TO dbpilot_engine;
GRANT SELECT ON cp.outcome_ledger TO dbpilot_api, dbpilot_engine;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA cp TO dbpilot_engine;
