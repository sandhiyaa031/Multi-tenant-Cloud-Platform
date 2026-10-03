"""The typed action space and its executor.

An action is data: a small validated object naming *what* to change. Nothing in
it is SQL. `plan()` turns an action into the statements that apply it and the
statements that undo it, reading the current state so the inverse restores
exactly what was there. The same code runs against the digital twin and against
production, so what was verified is what gets applied.

A proposer (rule-based or LLM) can only produce these objects. Anything that
does not validate is rejected before it reaches a database.
"""
import hashlib
import re
from dataclasses import dataclass, field
from typing import Annotated, Literal, Union

import psycopg
from pydantic import BaseModel, Field, TypeAdapter, model_validator

SCHEMA = "ch"
# table -> (warehouse-id column, other columns). The only tables and columns an action may name.
TABLES: dict[str, tuple[str, tuple[str, ...]]] = {
    "warehouse": ("w_id", ("w_name", "w_state", "w_zip", "w_tax", "w_ytd")),
    "district": ("d_w_id", ("d_id", "d_name", "d_tax", "d_ytd", "d_next_o_id")),
    "customer": ("c_w_id", ("c_d_id", "c_id", "c_first", "c_last", "c_state", "c_credit", "c_balance",
                            "c_since", "c_n_nationkey")),
    "history": ("h_w_id", ("h_c_id", "h_c_d_id", "h_c_w_id", "h_d_id", "h_date", "h_amount")),
    "orders": ("o_w_id", ("o_d_id", "o_id", "o_c_id", "o_entry_d", "o_carrier_id", "o_ol_cnt")),
    "new_order": ("no_w_id", ("no_d_id", "no_o_id")),
    "order_line": ("ol_w_id", ("ol_d_id", "ol_o_id", "ol_number", "ol_i_id", "ol_supply_w_id",
                               "ol_delivery_d", "ol_quantity", "ol_amount")),
    "stock": ("s_w_id", ("s_i_id", "s_quantity", "s_ytd", "s_order_cnt", "s_remote_cnt", "s_su_suppkey")),
}
PARTITIONED = {"customer", "history", "orders", "new_order", "order_line", "stock"}
ROLE_RE = re.compile(r"^[a-z][a-z0-9_]{2,62}$")
INDEX_RE = re.compile(r"^[a-z][a-z0-9_]{2,62}$")


@dataclass(frozen=True)
class Setting:
    """An allowlisted PostgreSQL setting and the range DBPilot may move it within."""

    kind: Literal["int", "float", "bool"]
    lo: float = 0
    hi: float = 0
    unit: str = ""

    def normalise(self, value: str) -> str:
        if self.kind == "bool":
            if value not in ("on", "off"):
                raise ValueError("must be 'on' or 'off'")
            return value
        # Accept an already-normalised value ("65536kB"), so stored actions re-validate.
        number = float(value.removesuffix(self.unit) if self.unit else value)
        if not self.lo <= number <= self.hi:
            raise ValueError(f"must be between {self.lo:g} and {self.hi:g}{self.unit}")
        return f"{int(number)}{self.unit}" if self.kind == "int" else f"{number:g}"


# Applied with ALTER ROLE ... SET: affects one tenant's new connections only.
ROLE_SETTINGS = {
    "work_mem": Setting("int", 1024, 262144, "kB"),
    "max_parallel_workers_per_gather": Setting("int", 0, 4),
    "statement_timeout": Setting("int", 0, 600000, "ms"),
    "random_page_cost": Setting("float", 1.0, 4.0),
    "jit": Setting("bool"),
}
# Applied with ALTER SYSTEM + reload: affects every tenant. None of these needs a restart.
INSTANCE_SETTINGS = {
    "work_mem": Setting("int", 1024, 65536, "kB"),
    "max_parallel_workers_per_gather": Setting("int", 0, 4),
    "random_page_cost": Setting("float", 1.0, 4.0),
    "effective_io_concurrency": Setting("int", 0, 256),
    "default_statistics_target": Setting("int", 10, 1000),
    "checkpoint_completion_target": Setting("float", 0.5, 0.9),
    "autovacuum_vacuum_scale_factor": Setting("float", 0.01, 0.2),
    "jit": Setting("bool"),
}


def _role(value: str) -> str:
    if not ROLE_RE.match(value):
        raise ValueError("invalid role name")
    return value


class NoAction(BaseModel):
    type: Literal["no_action"] = "no_action"
    reason: str = Field(min_length=1, max_length=2000)
    escalate: bool = False


class CreateIndex(BaseModel):
    type: Literal["create_index"] = "create_index"
    table: str
    columns: list[str] = Field(min_length=1, max_length=4)
    include: list[str] = Field(default_factory=list, max_length=4)
    # One tenant's partition, or None for every tenant's partition.
    tenant_role: str | None = None

    @model_validator(mode="after")
    def _check(self):
        if self.table not in TABLES:
            raise ValueError(f"unknown table {self.table!r}")
        w_col, others = TABLES[self.table]
        allowed = {w_col, *others}
        named = self.columns + self.include
        if set(named) - allowed:
            raise ValueError(f"unknown column(s) for {self.table}: {sorted(set(named) - allowed)}")
        if len(set(named)) != len(named):
            raise ValueError("a column may appear only once")
        if self.tenant_role is not None:
            _role(self.tenant_role)
            if self.table not in PARTITIONED:
                raise ValueError(f"{self.table} is not partitioned per tenant; tenant_role must be omitted")
        return self

    def index_name(self, partition_role: str | None) -> str:
        digest = hashlib.sha1("|".join([self.table, *self.columns, "+", *self.include]).encode()).hexdigest()[:8]
        suffix = f"_{partition_role}" if partition_role else ""
        return f"dbp_{self.table}_{digest}{suffix}"[:63]


class DropIndex(BaseModel):
    type: Literal["drop_index"] = "drop_index"
    index_name: str

    @model_validator(mode="after")
    def _check(self):
        if not INDEX_RE.match(self.index_name):
            raise ValueError("invalid index name")
        return self


class RoleSetting(BaseModel):
    type: Literal["role_setting"] = "role_setting"
    tenant_role: str
    name: str
    value: str

    @model_validator(mode="after")
    def _check(self):
        _role(self.tenant_role)
        if self.name not in ROLE_SETTINGS:
            raise ValueError(f"{self.name!r} is not an allowed role-level setting")
        self.value = ROLE_SETTINGS[self.name].normalise(self.value)
        return self


class InstanceSetting(BaseModel):
    type: Literal["instance_setting"] = "instance_setting"
    name: str
    value: str

    @model_validator(mode="after")
    def _check(self):
        if self.name not in INSTANCE_SETTINGS:
            raise ValueError(f"{self.name!r} is not an allowed instance-level setting")
        self.value = INSTANCE_SETTINGS[self.name].normalise(self.value)
        return self


class Analyze(BaseModel):
    type: Literal["analyze"] = "analyze"
    table: str
    tenant_role: str | None = None

    @model_validator(mode="after")
    def _check(self):
        if self.table not in TABLES:
            raise ValueError(f"unknown table {self.table!r}")
        if self.tenant_role is not None:
            _role(self.tenant_role)
            if self.table not in PARTITIONED:
                raise ValueError(f"{self.table} is not partitioned per tenant; tenant_role must be omitted")
        return self


class ConcurrencyCap(BaseModel):
    """Caps how many database connections a tenant may hold at once."""

    type: Literal["concurrency_cap"] = "concurrency_cap"
    tenant_role: str
    max_connections: int = Field(ge=1, le=200)

    @model_validator(mode="after")
    def _check(self):
        _role(self.tenant_role)
        return self


class ReplicaRouting(BaseModel):
    """Send a tenant's read-only class to the replica. Applied at the pooler, so it
    cannot be reproduced on a single twin instance and is never auto-approved."""

    type: Literal["replica_routing"] = "replica_routing"
    tenant_role: str
    enabled: bool
    max_staleness_ms: int = Field(default=2000, ge=0, le=600000)

    @model_validator(mode="after")
    def _check(self):
        _role(self.tenant_role)
        return self


class QueryRewrite(BaseModel):
    """Advice for the tenant's developers. DBPilot does not own application SQL."""

    type: Literal["query_rewrite"] = "query_rewrite"
    queryid: str
    suggestion: str = Field(min_length=1, max_length=4000)


Action = Annotated[
    Union[NoAction, CreateIndex, DropIndex, RoleSetting, InstanceSetting, Analyze, ConcurrencyCap,
          ReplicaRouting, QueryRewrite],
    Field(discriminator="type"),
]
_adapter = TypeAdapter(Action)

# Actions the executor can apply to a database. The rest are advisory or need a human.
EXECUTABLE = (CreateIndex, DropIndex, RoleSetting, InstanceSetting, Analyze, ConcurrencyCap)
# Scope decides how wide the blast radius is and therefore how the canary stages it.
INSTANCE_WIDE = (InstanceSetting,)


def parse_action(data: dict) -> Action:
    """Validates untrusted input (from an LLM, an API call, a stored row) into an action."""
    return _adapter.validate_python(data)


def action_json_schema() -> dict:
    return _adapter.json_schema()


def target_tenant(action: Action) -> str | None:
    return getattr(action, "tenant_role", None)


@dataclass
class Plan:
    apply: list[str]
    inverse: list[str]
    # False when undoing does not restore the previous state exactly (ANALYZE) or is expensive (index rebuild).
    cheap_to_undo: bool = True
    notes: list[str] = field(default_factory=list)


class ActionRefused(Exception):
    """The action is valid in form but must not be applied to this database."""


def _lit(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _tenant_roles(conn: psycopg.Connection) -> list[str]:
    return [r[0] for r in conn.execute("SELECT db_role FROM ch.tenant_map ORDER BY w_lo")]


def _require_tenant(conn: psycopg.Connection, role: str) -> None:
    if role not in _tenant_roles(conn):
        raise ActionRefused(f"{role} is not a tenant of this cluster")


def plan(action: Action, conn: psycopg.Connection) -> Plan:
    """Builds apply and inverse statements for `action` against the database behind `conn`."""
    if isinstance(action, CreateIndex):
        cols = ", ".join(action.columns)
        include = f" INCLUDE ({', '.join(action.include)})" if action.include else ""
        if action.table in PARTITIONED:
            roles = [action.tenant_role] if action.tenant_role else _tenant_roles(conn)
            if action.tenant_role:
                _require_tenant(conn, action.tenant_role)
            targets = [(f"{action.table}_{r}", action.index_name(r)) for r in roles]
        else:
            targets = [(action.table, action.index_name(None))]
        # CONCURRENTLY: builds without blocking writes, at the cost of a slower build.
        return Plan(
            apply=[f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {name} ON {SCHEMA}.{rel} ({cols}){include}"
                   for rel, name in targets],
            inverse=[f"DROP INDEX CONCURRENTLY IF EXISTS {SCHEMA}.{name}" for _, name in targets],
        )

    if isinstance(action, DropIndex):
        row = conn.execute(
            "SELECT pg_get_indexdef(i.indexrelid),"
            "       EXISTS (SELECT 1 FROM pg_constraint c WHERE c.conindid = i.indexrelid),"
            "       EXISTS (SELECT 1 FROM pg_inherits h WHERE h.inhrelid = i.indexrelid)"
            " FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid JOIN pg_namespace n ON n.oid = c.relnamespace"
            " WHERE n.nspname = %s AND c.relname = %s",
            (SCHEMA, action.index_name),
        ).fetchone()
        if row is None:
            raise ActionRefused(f"index {action.index_name} does not exist")
        definition, backs_constraint, is_partition_of_index = row
        if backs_constraint:
            raise ActionRefused("index enforces a constraint and cannot be dropped")
        if is_partition_of_index:
            raise ActionRefused("index is part of a partitioned index and cannot be dropped on its own")
        return Plan(
            apply=[f"DROP INDEX CONCURRENTLY IF EXISTS {SCHEMA}.{action.index_name}"],
            inverse=[definition.replace("CREATE INDEX", "CREATE INDEX CONCURRENTLY IF NOT EXISTS", 1)],
            cheap_to_undo=False,
            notes=["undoing requires rebuilding the index"],
        )

    if isinstance(action, RoleSetting):
        _require_tenant(conn, action.tenant_role)
        row = conn.execute("SELECT rolconfig FROM pg_roles WHERE rolname = %s", (action.tenant_role,)).fetchone()
        previous = dict(item.split("=", 1) for item in (row[0] or []))
        undo = (f"ALTER ROLE {action.tenant_role} SET {action.name} = {_lit(previous[action.name])}"
                if action.name in previous else f"ALTER ROLE {action.tenant_role} RESET {action.name}")
        return Plan(
            apply=[f"ALTER ROLE {action.tenant_role} SET {action.name} = {_lit(action.value)}"],
            inverse=[undo],
            notes=["takes effect on the tenant's new connections"],
        )

    if isinstance(action, InstanceSetting):
        row = conn.execute(
            "SELECT s.context, (SELECT f.setting FROM pg_file_settings f WHERE f.name = s.name"
            "   AND f.sourcefile LIKE '%%postgresql.auto.conf' ORDER BY f.seqno DESC LIMIT 1)"
            " FROM pg_settings s WHERE s.name = %s",
            (action.name,),
        ).fetchone()
        if row is None or row[0] == "postmaster":
            raise ActionRefused(f"{action.name} cannot be changed without a restart")
        undo = (f"ALTER SYSTEM SET {action.name} = {_lit(row[1])}" if row[1] is not None
                else f"ALTER SYSTEM RESET {action.name}")
        return Plan(
            apply=[f"ALTER SYSTEM SET {action.name} = {_lit(action.value)}", "SELECT pg_reload_conf()"],
            inverse=[undo, "SELECT pg_reload_conf()"],
        )

    if isinstance(action, Analyze):
        if action.tenant_role:
            _require_tenant(conn, action.tenant_role)
            relation = f"{action.table}_{action.tenant_role}"
        else:
            relation = action.table
        return Plan(apply=[f"ANALYZE {SCHEMA}.{relation}"], inverse=[], cheap_to_undo=False,
                    notes=["refreshes planner statistics; there is nothing to undo"])

    if isinstance(action, ConcurrencyCap):
        _require_tenant(conn, action.tenant_role)
        previous = conn.execute(
            "SELECT rolconnlimit FROM pg_roles WHERE rolname = %s", (action.tenant_role,)
        ).fetchone()[0]
        return Plan(
            apply=[f"ALTER ROLE {action.tenant_role} CONNECTION LIMIT {action.max_connections}"],
            inverse=[f"ALTER ROLE {action.tenant_role} CONNECTION LIMIT {previous}"],
        )

    raise ActionRefused(f"{action.type} is not applied by the executor")


def run(statements: list[str], conn: psycopg.Connection) -> None:
    """Executes plan statements. The connection must be in autocommit mode, because
    CREATE/DROP INDEX CONCURRENTLY and ALTER SYSTEM cannot run inside a transaction."""
    if not conn.autocommit:
        raise RuntimeError("executor requires an autocommit connection")
    for statement in statements:
        conn.execute(statement)
