"""Proposers are tested against a scripted Observer, and the agent against a
scripted model, so the tests need neither a data plane nor an API key."""
from types import SimpleNamespace

from app.proposers import agent, rules


class FakeObserver:
    """Returns canned observations; records which tools were used."""

    def __init__(self, **overrides):
        self.calls = []
        self.data = {
            "tenants": [{"name": "analytic", "db_role": "t_analytic"}, {"name": "steady", "db_role": "t_steady"}],
            "top_queries": [], "slo_status": [], "tenant_load": [], "history": [],
            "settings": {"instance": {}, "tenants": {"t_analytic": {"overrides": [], "connection_limit": -1},
                                                      "t_steady": {"overrides": [], "connection_limit": -1},
                                                      "t_bursty": {"overrides": [], "connection_limit": -1}}},
            "whatif": {"queries": [], "estimated_index_bytes": 0},
            "profile": {"relations": [], "indexes": []},
        }
        self.data.update(overrides)

    def tenants(self): return self.data["tenants"]
    def slo_status(self, minutes=15): self.calls.append("slo_status"); return self.data["slo_status"]
    def latency(self, minutes=15): return []
    def top_queries(self, minutes=15, tenant_role=None, limit=15): self.calls.append("top_queries"); return self.data["top_queries"]
    def tenant_load(self, minutes=30): return self.data["tenant_load"]
    def history(self, limit=20): return self.data["history"]
    def settings(self): return self.data["settings"]
    def explain(self, queryid): return {"explained": True, "plan": "Seq Scan on order_line_t_analytic"}
    def table_profile(self, table): return {"table": table, **self.data["profile"]}

    def whatif_index(self, table, columns, tenant_role=None, minutes=60):
        self.calls.append(("whatif", table, tuple(columns), tenant_role))
        return self.data["whatif"]


def query(role, text, total_ms, temp=0, queryid="1"):
    return {"tenant_role": role, "queryid": queryid, "query": text, "calls": 10, "total_exec_ms": total_ms,
            "mean_exec_ms": total_ms / 10, "rows": 1, "blocks_read_from_disk": 0, "temp_blocks_written": temp, "wal_bytes": 0}


ITEM_LOOKUP = "SELECT ol_w_id, count(*) FROM ch.order_line WHERE ol_i_id = $1 GROUP BY ol_w_id"


# ── rule-based proposer ──────────────────────────────────────────────────────

def test_rules_propose_tenant_scoped_memory_for_a_spilling_tenant():
    obs = FakeObserver(top_queries=[query("t_analytic", "SELECT 1 FROM ch.orders ORDER BY o_entry_d", 5000, temp=9000)])
    found = rules.propose(obs)
    assert [p.action for p in found] == [
        {"type": "role_setting", "tenant_role": "t_analytic", "name": "work_mem", "value": "65536"}]
    assert "9000" in found[0].rationale


def test_rules_do_not_repeat_a_memory_override_that_already_exists():
    obs = FakeObserver(top_queries=[query("t_analytic", "SELECT 1 FROM ch.orders", 5000, temp=9000)])
    obs.data["settings"]["tenants"]["t_analytic"]["overrides"] = ["work_mem=65536kB"]
    assert rules.propose(obs) == []


def test_rules_propose_an_index_only_when_the_planner_agrees():
    helpful = {"queries": [{"query": ITEM_LOOKUP, "ratio": 0.02}], "estimated_index_bytes": 10_000_000}
    obs = FakeObserver(top_queries=[query("t_analytic", ITEM_LOOKUP, 4000)], whatif=helpful)
    found = rules.propose(obs)
    assert found[0].action == {"type": "create_index", "table": "order_line", "columns": ["ol_i_id"],
                               "tenant_role": "t_analytic"}        # scoped to the tenant that needs it
    assert ("whatif", "order_line", ("ol_i_id",), "t_analytic") in obs.calls

    useless = {"queries": [{"query": ITEM_LOOKUP, "ratio": 0.97}], "estimated_index_bytes": 10_000_000}
    assert rules.propose(FakeObserver(top_queries=[query("t_analytic", ITEM_LOOKUP, 4000)], whatif=useless)) == []


def test_rules_ignore_columns_the_primary_key_already_serves_and_non_selects():
    by_key = "SELECT * FROM ch.order_line WHERE ol_d_id = $1 AND ol_o_id = $2"
    update = "UPDATE ch.order_line SET ol_amount = $1 WHERE ol_i_id = $2"
    obs = FakeObserver(top_queries=[query("t_steady", by_key, 4000), query("t_steady", update, 4000, queryid="2")])
    assert rules.propose(obs) == []
    assert not any(c[0] == "whatif" for c in obs.calls if isinstance(c, tuple))


def test_rules_propose_analyze_for_stale_partitions():
    stale = {"relation": "orders_t_mixed", "rows_changed_since_analyze": 90000, "live_rows": 120000}
    fresh = {"relation": "orders_t_steady", "rows_changed_since_analyze": 500, "live_rows": 120000}

    class Obs(FakeObserver):
        def table_profile(self, table):
            return {"relations": [stale, fresh] if table == "orders" else [], "indexes": []}

    assert [p.action for p in rules.propose(Obs())] == [{"type": "analyze", "table": "orders", "tenant_role": "t_mixed"}]


def test_rules_cap_a_bursting_tenant_only_when_someone_else_is_suffering():
    load = [{"tenant_role": "t_bursty", "calls": c} for c in (100, 110, 95, 105, 100, 900)]
    suffering = [{"tenant_role": "t_steady", "query_class": "OLTP", "windows": 4, "windows_violating": 3}]
    found = rules.propose(FakeObserver(tenant_load=load, slo_status=suffering))
    assert found[0].action == {"type": "concurrency_cap", "tenant_role": "t_bursty", "max_connections": 4}
    # Same burst, nobody hurt: leave it alone.
    assert rules.propose(FakeObserver(tenant_load=load, slo_status=[])) == []


def test_every_rule_output_is_inside_the_action_space():
    from dbpilot_core.actions import parse_action
    obs = FakeObserver(top_queries=[query("t_analytic", ITEM_LOOKUP, 4000, temp=9000)],
                       whatif={"queries": [{"query": ITEM_LOOKUP, "ratio": 0.1}], "estimated_index_bytes": 1})
    for p in rules.propose(obs):
        parse_action(p.action)


# ── LLM agent, against a scripted model ──────────────────────────────────────

def text(t): return SimpleNamespace(type="text", text=t)
def tool(name, id_, **inp): return SimpleNamespace(type="tool_use", name=name, id=id_, input=inp)


def reply(*blocks, stop="tool_use"):
    return SimpleNamespace(content=list(blocks), stop_reason=stop,
                           usage=SimpleNamespace(input_tokens=100, output_tokens=20, cache_read_input_tokens=0))


class FakeModel:
    def __init__(self, *replies):
        self.replies, self.requests = list(replies), []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        # The agent keeps appending to its message list; snapshot what was actually sent.
        self.requests.append({**kwargs, "messages": list(kwargs["messages"])})
        return self.replies.pop(0)


def test_agent_investigates_then_proposes_and_records_its_trace():
    model = FakeModel(
        reply(text("Checking who is missing their objective."), tool("get_slo_status", "a"), tool("get_top_queries", "b")),
        reply(tool("whatif_index", "c", table="order_line", columns=["ol_i_id"], tenant_role="t_analytic")),
        reply(tool("propose_action", "d", rationale="Item lookups scan the partition; t_analytic does not write it.",
                   action={"type": "create_index", "table": "order_line", "columns": ["ol_i_id"], "tenant_role": "t_analytic"})),
    )
    obs = FakeObserver()
    result = agent.propose(obs, client=model)

    assert result.action["type"] == "create_index" and result.action["include"] == []
    assert "t_analytic does not write" in result.rationale
    assert [t["name"] for t in result.evidence["trace"] if t["kind"] == "tool"] == [
        "get_slo_status", "get_top_queries", "whatif_index", "propose_action"]
    assert result.evidence["usage"] == {"input_tokens": 300, "output_tokens": 60, "cache_read_input_tokens": 0, "requests": 3}
    # Both tool results of the first turn went back in one user message.
    assert len(model.requests[1]["messages"][-1]["content"]) == 2
    # Forced tool choice is never used; the model decides.
    assert all("tool_choice" not in r for r in model.requests)


def test_agent_output_outside_the_action_space_is_bounced_back_not_executed():
    model = FakeModel(
        reply(tool("propose_action", "a", rationale="quick fix", action={"type": "run_sql", "sql": "DROP INDEX x"})),
        reply(tool("propose_action", "b", rationale="nothing is wrong", action={"type": "no_action", "reason": "transient spike"})),
    )
    result = agent.propose(FakeObserver(), client=model)
    assert result.action == {"type": "no_action", "reason": "transient spike", "escalate": False}
    bounced = model.requests[1]["messages"][-1]["content"][0]
    assert bounced["is_error"] and "not a valid action" in bounced["content"]


def test_agent_tool_errors_are_reported_to_the_model_not_raised():
    model = FakeModel(
        reply(tool("get_table_profile", "a", table="pg_authid")),
        reply(tool("propose_action", "b", rationale="x", action={"type": "no_action", "reason": "nothing to do"})),
    )

    class Obs(FakeObserver):
        def table_profile(self, table):
            raise ValueError(f"unknown table {table!r}")

    result = agent.propose(Obs(), client=model)
    assert result.action["type"] == "no_action"
    assert model.requests[1]["messages"][-1]["content"][0]["is_error"]


def test_agent_refusal_and_silence_escalate_to_a_human():
    refused = agent.propose(FakeObserver(), client=FakeModel(reply(stop="refusal")))
    assert refused.action["escalate"] and refused.evidence["stop_reason"] == "refusal"
    silent = agent.propose(FakeObserver(), client=FakeModel(reply(text("All looks fine."), stop="end_turn")))
    assert silent.action["type"] == "no_action" and silent.action["escalate"]


def test_agent_cannot_loop_forever():
    model = FakeModel(*[reply(tool("get_slo_status", str(i))) for i in range(agent.MAX_TURNS + 5)])
    result = agent.propose(FakeObserver(), client=model)
    assert result.action["escalate"] and len(model.requests) == agent.MAX_TURNS


def test_agent_has_no_tool_that_writes():
    names = {t["name"] for t in agent.TOOLS}
    assert names == {"get_tenants", "get_slo_status", "get_latency", "get_top_queries", "get_tenant_load",
                     "explain_query", "get_table_profile", "get_settings", "whatif_index", "get_history", "propose_action"}
