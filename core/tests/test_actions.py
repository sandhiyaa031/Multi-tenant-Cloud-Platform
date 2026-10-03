"""Validation: what an untrusted proposer can and cannot express."""
import pytest
from pydantic import ValidationError

from dbpilot_core.actions import CreateIndex, RoleSetting, action_json_schema, parse_action


def test_valid_actions_parse():
    a = parse_action({"type": "create_index", "table": "order_line", "columns": ["ol_i_id"], "tenant_role": "t_analytic"})
    assert isinstance(a, CreateIndex)
    assert a.index_name("t_analytic").startswith("dbp_order_line_") and a.index_name("t_analytic").endswith("_t_analytic")
    r = parse_action({"type": "role_setting", "tenant_role": "t_analytic", "name": "work_mem", "value": "65536"})
    assert isinstance(r, RoleSetting) and r.value == "65536kB"
    assert parse_action({"type": "no_action", "reason": "transient spike"}).type == "no_action"


@pytest.mark.parametrize(
    "payload",
    [
        {"type": "run_sql", "sql": "DROP TABLE ch.customer"},                                  # no such action
        {"type": "create_index", "table": "pg_authid", "columns": ["rolname"]},                # not our table
        {"type": "create_index", "table": "orders", "columns": ["o_id); DROP TABLE x; --"]},   # injection as a column
        {"type": "create_index", "table": "orders", "columns": []},
        {"type": "create_index", "table": "item", "columns": ["i_id"]},                        # shared reference table
        {"type": "create_index", "table": "district", "columns": ["d_id"], "tenant_role": "t_steady"},
        {"type": "role_setting", "tenant_role": "t_a; DROP ROLE x", "name": "work_mem", "value": "4096"},
        {"type": "role_setting", "tenant_role": "t_steady", "name": "session_authorization", "value": "postgres"},
        {"type": "role_setting", "tenant_role": "t_steady", "name": "work_mem", "value": "99999999"},
        {"type": "instance_setting", "name": "shared_buffers", "value": "1"},                  # needs a restart
        {"type": "instance_setting", "name": "fsync", "value": "off"},                         # dangerous
        {"type": "instance_setting", "name": "max_parallel_workers_per_gather", "value": "64"},
        {"type": "concurrency_cap", "tenant_role": "t_steady", "max_connections": 0},
        {"type": "drop_index", "index_name": "x\"; DROP TABLE y; --"},
        {"type": "no_action"},
    ],
)
def test_anything_outside_the_action_space_is_rejected(payload):
    with pytest.raises(ValidationError):
        parse_action(payload)


@pytest.mark.parametrize("payload", [
    {"type": "role_setting", "tenant_role": "t_analytic", "name": "work_mem", "value": "65536"},
    {"type": "instance_setting", "name": "random_page_cost", "value": "1.1"},
    {"type": "create_index", "table": "order_line", "columns": ["ol_i_id"], "tenant_role": "t_analytic"},
    {"type": "concurrency_cap", "tenant_role": "t_bursty", "max_connections": 4},
])
def test_a_stored_action_parses_back_to_itself(payload):
    """Actions are stored normalised and re-validated by the engine before every use."""
    stored = parse_action(payload).model_dump()
    assert parse_action(stored).model_dump() == stored


def test_schema_is_exportable_for_tool_calling():
    schema = action_json_schema()
    assert "create_index" in str(schema) and "discriminator" in schema
