"""Checks the properties the rest of DBPilot relies on: tenant isolation,
per-tenant attribution of statistics, per-tenant partitions, replication, and
that the pooler only admits tenants."""
import os
import time

import psycopg
import pytest

POOLER = os.environ["DP_POOLER_HOST"]
PRIMARY = os.environ["DP_PRIMARY_HOST"]
TENANT_PW = os.environ["DP_TENANT_PASSWORD"]
RANGES = {"t_steady": (1, 2), "t_bursty": (3, 4), "t_analytic": (5, 8), "t_mixed": (9, 12)}
PARTITIONED = ["customer", "history", "orders", "new_order", "order_line", "stock"]


def tenant(role: str, database: str = "app") -> psycopg.Connection:
    """Connects the way a tenant application does: through PgBouncer, as its own role."""
    return psycopg.connect(
        host=POOLER, port=6432, dbname=database, user=role, password=TENANT_PW, autocommit=True
    )


@pytest.fixture
def owner():
    with psycopg.connect(os.environ["DP_OWNER_URL"], autocommit=True) as conn:
        yield conn


@pytest.fixture
def replica_owner():
    with psycopg.connect(os.environ["DP_REPLICA_OWNER_URL"], autocommit=True) as conn:
        yield conn


def test_population_follows_the_specification_and_stays_consistent_under_load(owner):
    warehouses = owner.execute("SELECT count(*) FROM ch.warehouse").fetchone()[0]
    assert warehouses == 12
    assert owner.execute("SELECT count(*) FROM ch.district").fetchone()[0] == warehouses * 10
    items = owner.execute("SELECT count(*) FROM ch.item").fetchone()[0]
    assert owner.execute("SELECT count(*) FROM ch.stock").fetchone()[0] == warehouses * items
    customers = owner.execute("SELECT count(*) FROM ch.customer").fetchone()[0]
    # One initial order per customer; the workload only ever adds orders.
    assert owner.execute("SELECT count(*) FROM ch.orders").fetchone()[0] >= customers
    # Undelivered orders are exactly the new_order rows, and only they lack a carrier.
    assert owner.execute("SELECT count(*) FROM ch.new_order").fetchone()[0] == owner.execute(
        "SELECT count(*) FROM ch.orders WHERE o_carrier_id IS NULL"
    ).fetchone()[0]
    # Every order has exactly o_ol_cnt lines.
    assert owner.execute("SELECT sum(o_ol_cnt) FROM ch.orders").fetchone()[0] == owner.execute(
        "SELECT count(*) FROM ch.order_line"
    ).fetchone()[0]


@pytest.mark.parametrize("role", RANGES)
def test_tenant_sees_only_its_warehouses(role):
    lo, hi = RANGES[role]
    with tenant(role) as conn:
        assert [r[0] for r in conn.execute("SELECT w_id FROM ch.warehouse ORDER BY 1")] == list(range(lo, hi + 1))
        for table, col in [("customer", "c_w_id"), ("stock", "s_w_id"), ("order_line", "ol_w_id")]:
            assert conn.execute(f"SELECT min({col}), max({col}) FROM ch.{table}").fetchone() == (lo, hi)


def test_tenant_cannot_read_or_write_another_tenants_rows():
    with tenant("t_steady") as conn:
        assert conn.execute("SELECT count(*) FROM ch.customer WHERE c_w_id = 5").fetchone()[0] == 0
        assert conn.execute("UPDATE ch.warehouse SET w_ytd = 0 WHERE w_id = 5").rowcount == 0
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute(
                "INSERT INTO ch.new_order (no_w_id, no_d_id, no_o_id) VALUES (5, 1, 999999)"
            )


def test_tenant_cannot_bypass_the_policy_through_a_partition():
    with tenant("t_steady") as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("SELECT count(*) FROM ch.customer_t_analytic")


def test_each_tenant_has_its_own_partitions(owner):
    for table in PARTITIONED:
        children = {
            r[0]
            for r in owner.execute(
                "SELECT c.relname FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid"
                " JOIN pg_class p ON p.oid = i.inhparent WHERE p.relname = %s",
                (table,),
            )
        }
        assert children == {f"{table}_{role}" for role in RANGES}


def test_partition_pruning_under_rls():
    """A tenant's query must touch only its own partition, not scan all four."""
    with tenant("t_steady") as conn:
        plan = "\n".join(r[0] for r in conn.execute("EXPLAIN (ANALYZE, COSTS OFF) SELECT count(*) FROM ch.orders"))
    assert "orders_t_steady" in plan
    for other in ("t_bursty", "t_analytic", "t_mixed"):
        assert f"orders_{other}" not in plan or "never executed" in plan


def test_index_can_be_built_for_one_tenant_only(owner):
    """What action A1's tenant scope depends on."""
    owner.execute("CREATE INDEX CONCURRENTLY dbpilot_test_idx ON ch.order_line_t_steady (ol_i_id)")
    try:
        indexed = {
            r[0] for r in owner.execute("SELECT tablename FROM pg_indexes WHERE indexname = 'dbpilot_test_idx'")
        }
        assert indexed == {"order_line_t_steady"}
    finally:
        owner.execute("DROP INDEX CONCURRENTLY ch.dbpilot_test_idx")


def test_pg_stat_statements_attributes_queries_to_tenants(owner):
    owner.execute("SELECT pg_stat_statements_reset()")
    marker = "SELECT count(*) FROM ch.district WHERE d_id = %s"
    with tenant("t_steady") as a, tenant("t_bursty") as b:
        for _ in range(3):
            a.execute(marker, (1,))
        b.execute(marker, (2,))
    rows = dict(
        owner.execute(
            "SELECT r.rolname, s.calls FROM pg_stat_statements s JOIN pg_roles r ON r.oid = s.userid"
            " WHERE s.query LIKE 'SELECT count(*) FROM ch.district WHERE d_id = %'"
        ).fetchall()
    )
    assert rows == {"t_steady": 3, "t_bursty": 1}


def test_role_level_setting_applies_to_that_tenant_only(owner):
    """What action A3 depends on. Role defaults are read when a server connection
    starts, so this connects straight to the primary to get a fresh one."""

    def work_mem(role: str) -> str:
        with psycopg.connect(
            host=PRIMARY, port=5432, dbname="app", user=role, password=TENANT_PW, autocommit=True
        ) as conn:
            return conn.execute("SHOW work_mem").fetchone()[0]

    owner.execute("ALTER ROLE t_analytic SET work_mem = '77MB'")
    try:
        assert work_mem("t_analytic") == "77MB"
        assert work_mem("t_steady") != "77MB"
    finally:
        owner.execute("ALTER ROLE t_analytic RESET work_mem")


def test_pooler_refuses_non_tenant_roles():
    with pytest.raises(psycopg.OperationalError):
        psycopg.connect(host=POOLER, port=6432, dbname="app", user="postgres", password="anything", connect_timeout=5)


def test_pooler_refuses_wrong_password():
    with pytest.raises(psycopg.OperationalError):
        psycopg.connect(host=POOLER, port=6432, dbname="app", user="t_steady", password="wrong", connect_timeout=5)


def test_replica_is_streaming_and_read_only(owner, replica_owner):
    assert replica_owner.execute("SELECT pg_is_in_recovery()").fetchone()[0] is True
    state = owner.execute("SELECT state FROM pg_stat_replication").fetchall()
    assert ("streaming",) in state
    with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
        replica_owner.execute("UPDATE ch.warehouse SET w_ytd = w_ytd")


def test_write_on_primary_reaches_replica(owner, replica_owner):
    before = owner.execute("SELECT w_ytd FROM ch.warehouse WHERE w_id = 1").fetchone()[0]
    owner.execute("UPDATE ch.warehouse SET w_ytd = w_ytd + 1 WHERE w_id = 1")
    try:
        deadline = time.time() + 10
        while time.time() < deadline:
            if replica_owner.execute("SELECT w_ytd FROM ch.warehouse WHERE w_id = 1").fetchone()[0] == before + 1:
                break
            time.sleep(0.1)
        else:
            pytest.fail("update did not reach the replica within 10 s")
    finally:
        owner.execute("UPDATE ch.warehouse SET w_ytd = %s WHERE w_id = 1", (before,))


def test_replica_route_enforces_the_same_isolation():
    """Reads sent to the replica through the pooler (action A6) are still tenant-scoped."""
    with tenant("t_analytic", database="app_ro") as conn:
        assert conn.execute("SELECT pg_is_in_recovery()").fetchone()[0] is True
        assert conn.execute("SELECT min(w_id), max(w_id) FROM ch.warehouse").fetchone() == RANGES["t_analytic"]


def test_hypopg_is_available(owner):
    """What tier T1 depends on: a hypothetical index changes the plan without being built."""
    owner.execute("SELECT hypopg_reset()")
    owner.execute("SELECT * FROM hypopg_create_index('CREATE INDEX ON ch.order_line_t_steady (ol_i_id)')")
    try:
        plan = "\n".join(
            r[0] for r in owner.execute("EXPLAIN SELECT * FROM ch.order_line_t_steady WHERE ol_i_id = 42")
        )
        assert "<" in plan and "btree" in plan  # hypothetical indexes are named <oid>btree_...
    finally:
        owner.execute("SELECT hypopg_reset()")


# ── The executor's role ──────────────────────────────────────────────────────

@pytest.fixture
def executor():
    with psycopg.connect(os.environ["DP_EXECUTOR_URL"], autocommit=True) as conn:
        yield conn


def test_executor_is_not_a_superuser_and_owns_nothing_itself(owner):
    row = owner.execute("SELECT rolsuper, rolreplication, rolbypassrls FROM pg_roles"
                        " WHERE rolname = 'dbpilot_executor'").fetchone()
    assert row == (False, False, False)


@pytest.mark.parametrize("data", [
    {"type": "create_index", "table": "order_line", "columns": ["ol_supply_w_id"], "tenant_role": "t_steady"},
    {"type": "role_setting", "tenant_role": "t_steady", "name": "work_mem", "value": "8192"},
    {"type": "concurrency_cap", "tenant_role": "t_steady", "max_connections": 50},
    {"type": "instance_setting", "name": "default_statistics_target", "value": "150"},
    {"type": "analyze", "table": "new_order", "tenant_role": "t_steady"},
])
def test_executor_role_can_apply_and_undo_every_executable_action(executor, data):
    """The same plan() and run() the engine uses, as the role the engine logs in as."""
    from dbpilot_core import actions

    plan = actions.plan(actions.parse_action(data), executor)
    try:
        actions.run(plan.apply, executor)
    finally:
        actions.run(plan.inverse, executor)


def test_executor_grants_cover_exactly_the_instance_allowlist(owner):
    from dbpilot_core import actions

    granted = {r[0] for r in owner.execute(
        "SELECT p.parname FROM pg_parameter_acl p, aclexplode(p.paracl) a"
        " WHERE a.grantee = 'dbpilot_executor'::regrole AND a.privilege_type = 'ALTER SYSTEM'")}
    assert granted == set(actions.INSTANCE_SETTINGS)


@pytest.mark.parametrize("statement", [
    "DROP TABLE ch.history_t_steady",
    "ALTER TABLE ch.item ADD COLUMN x int",
    "CREATE TABLE ch.x (a int)",
    "GRANT SELECT ON ch.item TO PUBLIC",
    "ALTER SYSTEM SET shared_preload_libraries = ''",
    "ALTER SYSTEM SET fsync = off",
    "ALTER ROLE postgres PASSWORD 'x'",
    "ALTER ROLE dbpilot_monitor SET work_mem = '1GB'",
    "CREATE ROLE evil SUPERUSER",
    "SET ROLE t_steady",
    "COPY ch.item TO PROGRAM 'cat'",
])
def test_executor_role_is_refused_everything_outside_the_action_space(executor, statement):
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        executor.execute(statement)
