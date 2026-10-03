-- Development seed: four tenants with different sizes, matching the four workload
-- profiles in the architecture specification. Safe to re-run: it does nothing
-- once the tenants exist.
\set ON_ERROR_STOP on

SELECT count(*) = 0 AS fresh FROM ch.tenant_map \gset
\if :fresh
    SELECT ch.provision_tenant('t_steady',   :'tenant_pw', 1, 2);    -- steady OLTP, SLO-critical
    SELECT ch.provision_tenant('t_bursty',   :'tenant_pw', 3, 4);    -- bursty OLTP
    SELECT ch.provision_tenant('t_analytic', :'tenant_pw', 5, 8);    -- analytical
    SELECT ch.provision_tenant('t_mixed',    :'tenant_pw', 9, 12);   -- mixed, growing

    CALL ch.load_reference(:items);
    CALL ch.load_warehouses(1, 12, :scale);
    ANALYZE;
    SELECT pg_stat_statements_reset();
\else
    \echo 'tenants already provisioned; nothing to do'
\endif
