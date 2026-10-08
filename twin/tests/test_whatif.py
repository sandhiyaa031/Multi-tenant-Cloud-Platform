"""Tier T1 on the twin source: planner what-if with hypothetical indexes."""
import pytest

from agent import pg, whatif

from .conftest import PRIMARY_PORT, scalar

pytestmark = pytest.mark.usefixtures("cluster")

INDEX_FOR_T_A = {"type": "create_index", "table": "order_line", "columns": ["ol_i_id"], "tenant_role": "t_a"}
LOOKUP = "SELECT * FROM ch.order_line_t_a WHERE ol_i_id = $1"
UNRELATED = "SELECT count(*) FROM ch.tenant_map"


def real_indexes(port: int) -> int:
    return scalar(port, "SELECT count(*) FROM pg_indexes WHERE schemaname = 'ch' AND indexname LIKE 'dbp_%%'")


def test_whatif_reports_which_observed_queries_the_planner_would_speed_up():
    result = whatif.index_whatif(INDEX_FOR_T_A, [LOOKUP, UNRELATED])
    assert result["supported"] and result["explained"] == 2 and result["improved"] == 1
    assert result["estimated_index_bytes"] > 0
    by_query = {q["query"]: q for q in result["queries"]}
    assert by_query[LOOKUP]["ratio"] < 0.9 and by_query[LOOKUP]["cost_after"] < by_query[LOOKUP]["cost_before"]
    assert by_query[UNRELATED]["ratio"] == 1.0
    # Best improvement first.
    assert result["queries"][0]["query"] == LOOKUP


def test_a_tenants_index_is_judged_on_that_tenants_partition():
    """Tenants name the shared table. Planned across every tenant's partition, an index on one
    partition saves only that partition's share, and a larger neighbour hides the benefit."""
    shared = "SELECT * FROM ch.order_line WHERE ol_i_id = $1"
    result = whatif.index_whatif(INDEX_FOR_T_A, [shared])
    assert result["improved"] == 1 and result["queries"][0]["query"] == shared
    assert result["queries"][0]["ratio"] < 0.9
    # Exactly what the same lookup costs when written against the tenant's partition.
    direct = whatif.index_whatif(INDEX_FOR_T_A, [LOOKUP])["queries"][0]
    assert result["queries"][0]["cost_before"] == direct["cost_before"]
    assert result["queries"][0]["cost_after"] == direct["cost_after"]
    # An index for every tenant is still judged on the shared table as written.
    everyone = whatif.index_whatif({**INDEX_FOR_T_A, "tenant_role": None}, [shared])
    assert everyone["improved"] == 1


def test_whatif_builds_nothing_anywhere():
    whatif.index_whatif(INDEX_FOR_T_A, [LOOKUP])
    assert real_indexes(pg.SOURCE_PORT) == 0 and real_indexes(PRIMARY_PORT) == 0
    # The hypothetical index lived in the what-if's own session only.
    assert scalar(pg.SOURCE_PORT, "SELECT count(*) FROM hypopg_list_indexes") == 0


def test_whatif_for_an_index_nothing_would_use_reports_no_improvement():
    result = whatif.index_whatif({**INDEX_FOR_T_A, "columns": ["ol_amount"]}, [LOOKUP])
    assert result["explained"] == 1 and result["improved"] == 0


def test_whatif_skips_statements_that_cannot_be_planned():
    result = whatif.index_whatif(INDEX_FOR_T_A, [LOOKUP, "VACUUM ch.order_line", "SELECT * FROM ch.no_such_table"])
    assert result["explained"] == 1 and result["improved"] == 1


def test_whatif_applies_to_indexes_only_and_rejects_what_is_not_an_action():
    result = whatif.index_whatif({"type": "analyze", "table": "order_line"}, [LOOKUP])
    assert result == {"supported": False, "reason": "planner what-if does not apply to analyze"}
    with pytest.raises(ValueError):
        whatif.index_whatif({"type": "create_index", "table": "order_line", "columns": ["ol_i_id); DROP TABLE x; --"]},
                            [LOOKUP])


def test_explain_returns_a_plan_or_the_reason_there_is_none():
    plan = whatif.explain(LOOKUP)
    assert plan["explained"] and "order_line_t_a" in plan["plan"]
    refused = whatif.explain("SELECT * FROM ch.no_such_table")
    assert refused["explained"] is False and "does not exist" in refused["error"]
