"""The Groq adapter, against a scripted HTTP server: no network and no key."""
import json

import httpx
import pytest

from app.proposers import agent
from app.proposers.groq_client import CONTEXT_CHARS, RESULT_CHARS, STUB_CHARS, GroqClient, GroqError
from tests.test_proposers import INDEX, FakeObserver, diagnose

# `diagnosable` is a fixture of test_proposers; importing it makes it available here.
from tests.test_proposers import diagnosable  # noqa: F401


def completion(*, text=None, calls=(), finish="tool_calls"):
    tool_calls = [{"id": id_, "type": "function", "function": {"name": name, "arguments": arguments}}
                  for id_, name, arguments in calls]
    return httpx.Response(200, json={
        "choices": [{"finish_reason": finish, "message": {"role": "assistant", "content": text, "tool_calls": tool_calls}}],
        "usage": {"prompt_tokens": 900, "completion_tokens": 40}})


class Server:
    """Answers each request with the next scripted response and keeps what it was sent."""

    def __init__(self, *responses):
        self.responses, self.requests, self.slept = list(responses), [], []

    def client(self, **kwargs) -> GroqClient:
        def handle(request: httpx.Request) -> httpx.Response:
            self.requests.append(json.loads(request.content))
            return self.responses.pop(0)
        return GroqClient(api_key="not-a-real-key", http=httpx.Client(transport=httpx.MockTransport(handle)),
                          sleep=self.slept.append, **kwargs)


PROPOSE = ("c2", "propose_action", json.dumps({"action": INDEX, "rationale": "Item lookups scan the partition."}))


def test_the_same_agent_loop_runs_against_groq_and_records_the_model_that_answered():
    server = Server(completion(text="Checking objectives first.", calls=[("c1", "get_slo_status", "{}")]),
                    completion(calls=[PROPOSE]))
    result = agent.propose(FakeObserver(), client=server.client(model="some/model"))
    assert result.action == {**INDEX, "include": []}
    assert result.evidence["model"] == "some/model" and result.evidence["provider"] == "groq"
    assert [t.get("name", "note") for t in result.evidence["trace"]] == ["note", "get_slo_status", "propose_action"]
    assert result.evidence["usage"] == {"input_tokens": 1800, "output_tokens": 80, "cache_read_input_tokens": 0, "requests": 2}

    first, second = server.requests
    assert first["model"] == "some/model"
    assert {t["function"]["name"] for t in first["tools"]} == {t["name"] for t in agent.TOOLS}
    assert [m["role"] for m in first["messages"]] == ["system", "user"]
    # The second request carries the model's own call and the tool's answer, linked by id.
    assert [m["role"] for m in second["messages"]] == ["system", "user", "assistant", "tool"]
    assert second["messages"][2]["tool_calls"][0]["id"] == second["messages"][3]["tool_call_id"] == "c1"


def test_an_action_outside_the_action_space_is_bounced_back_to_the_model_here_too():
    bad = ("c1", "propose_action", json.dumps({"action": {"type": "run_sql", "sql": "DROP TABLE ch.orders"}, "rationale": "x"}))
    server = Server(completion(calls=[bad]), completion(calls=[PROPOSE]))
    result = agent.propose(FakeObserver(), client=server.client())
    assert result.action["type"] == "create_index"
    assert "Rejected, not a valid action" in server.requests[1]["messages"][-1]["content"]


def test_arguments_that_are_not_json_do_not_crash_the_loop():
    server = Server(completion(calls=[("c1", "propose_action", "{not json")]), completion(calls=[PROPOSE]))
    assert agent.propose(FakeObserver(), client=server.client()).action["type"] == "create_index"


def test_a_rate_limit_is_waited_out_and_the_wait_is_recorded():
    limited = httpx.Response(429, headers={"retry-after": "12"}, json={"error": {"message": "rate limit"}})
    server = Server(limited, completion(calls=[PROPOSE]))
    result = agent.propose(FakeObserver(), client=server.client())
    assert server.slept == [13.0] and result.evidence["waited_for_rate_limit_s"] == 13.0
    assert result.action["type"] == "create_index"


def test_a_rate_limit_that_never_clears_and_an_api_error_are_reported_not_hidden():
    limited = [httpx.Response(429, headers={"retry-after": "1"}, json={}) for _ in range(20)]
    with pytest.raises(GroqError, match="rate limit did not clear"):
        agent.propose(FakeObserver(), client=Server(*limited).client())
    with pytest.raises(GroqError, match="answered 401"):
        agent.propose(FakeObserver(), client=Server(httpx.Response(401, json={"error": "bad key"})).client())


def test_a_spent_daily_quota_fails_at_once_with_what_the_api_said():
    spent = httpx.Response(429, headers={"retry-after": "1023"},
                           json={"error": {"message": "Rate limit reached on tokens per day (TPD): Limit 200000, Used 199577"}})
    server = Server(spent)
    with pytest.raises(GroqError, match="quota is used up: Rate limit reached on tokens per day"):
        agent.propose(FakeObserver(), client=server.client())
    assert server.slept == [] and len(server.requests) == 1


def test_a_run_cut_short_by_the_api_keeps_what_the_agent_had_done():
    spent = httpx.Response(429, headers={"retry-after": "900"}, json={"error": {"message": "tokens per day (TPD): Limit 200000"}})
    server = Server(completion(calls=[("r0", "get_slo_status", "{}")]), completion(calls=[("r1", "get_tenants", "{}")]), spent)
    with pytest.raises(GroqError) as failure:
        agent.propose(FakeObserver(), client=server.client(model="some/model"))
    partial = failure.value.partial_evidence
    assert [t["name"] for t in partial["trace"]] == ["get_slo_status", "get_tenants"]
    assert partial["stop_reason"] == "model_api_error" and partial["usage"]["requests"] == 2 and partial["model"] == "some/model"


def test_the_api_logs_a_cut_short_run_and_still_stores_nothing(client, org, diagnosable, monkeypatch, caplog):  # noqa: F811
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    monkeypatch.setenv("GROQ_API_KEY", "not-a-real-key")
    spent = httpx.Response(429, headers={"retry-after": "900"}, json={"error": {"message": "tokens per day (TPD): Limit 200000"}})
    server = Server(completion(calls=[("r0", "get_slo_status", "{}")]), spent)
    monkeypatch.setattr("app.proposers.groq_client.GroqClient", lambda: server.client())
    with caplog.at_level("WARNING", logger="dbpilot.api.proposals"):
        r = diagnose(client, org, diagnosable)
    assert r.status_code == 502 and "quota is used up" in r.json()["detail"]
    assert client.get(f"/api/v1/proposals?cluster_id={diagnosable}", headers=org.admin).json() == []
    assert any("agent run failed after 1 steps" in record.getMessage() and "get_slo_status" in record.getMessage()
               for record in caplog.records)


def test_recent_tool_results_stay_readable_and_only_the_oldest_are_shortened():
    """All results share one budget, spent from the newest back. The first live run shortened every
    earlier result, and the model kept asking for the same observations until its turns ran out."""
    long_rows = [{"tenant_role": "t_analytic", "query": "SELECT " + "x" * 200, "n": i} for i in range(60)]
    asks = [completion(calls=[(f"r{i}", "get_top_queries", "{}")]) for i in range(5)]
    server = Server(*asks, completion(calls=[PROPOSE]))
    result = agent.propose(FakeObserver(top_queries=long_rows), client=server.client())
    shown = [len(m["content"]) for m in server.requests[5]["messages"] if m["role"] == "tool"]     # oldest first
    note = 100                                                                                      # the "shortened from" suffix
    assert all(RESULT_CHARS <= n < RESULT_CHARS + note for n in shown[2:])          # the three newest: as much as any result gets
    assert STUB_CHARS <= shown[0] < STUB_CHARS + note < shown[1] < RESULT_CHARS     # then what is left, then a stub
    assert sum(shown) < CONTEXT_CHARS + STUB_CHARS + 5 * note
    # What the tool returned is still in the stored trace, in the trace's own length.
    assert len(result.evidence["trace"][0]["result"]) == 3000


def test_a_short_result_is_shown_whole_however_old_it_is_while_the_budget_lasts():
    objectives = [{"tenant_role": f"t_{i}", "query_class": "OLTP", "observed_ms": 20.0 + i, "threshold_ms": 100.0} for i in range(6)]
    asks = [completion(calls=[(f"r{i}", "get_slo_status", "{}")]) for i in range(4)]
    server = Server(*asks, completion(calls=[PROPOSE]))
    agent.propose(FakeObserver(slo_status=objectives), client=server.client())
    shown = [m["content"] for m in server.requests[4]["messages"] if m["role"] == "tool"]
    assert len(shown) == 4 and all(json.loads(s) == objectives for s in shown)


def test_the_last_turn_notice_reaches_the_model_after_the_tool_results():
    asks = [completion(calls=[(f"r{i}", "get_slo_status", "{}")]) for i in range(agent.MAX_TURNS - 1)]
    server = Server(*asks, completion(calls=[("p", "propose_action", json.dumps(
        {"action": {"type": "no_action", "reason": "every objective is met"}, "rationale": "Nothing is wrong."}))]))
    result = agent.propose(FakeObserver(), client=server.client())
    assert result.action["type"] == "no_action" and not result.action.get("escalate")
    final = server.requests[-1]["messages"]
    assert [m["role"] for m in final[-2:]] == ["tool", "user"] and final[-1]["content"] == agent.LAST_TURN
    assert all(m["content"] != agent.LAST_TURN for m in server.requests[-2]["messages"])


def test_the_api_uses_groq_only_when_no_anthropic_key_is_set(client, org, diagnosable, monkeypatch):  # noqa: F811
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    monkeypatch.setenv("GROQ_API_KEY", "not-a-real-key")
    server = Server(completion(calls=[PROPOSE]))
    monkeypatch.setattr("app.proposers.groq_client.GroqClient", lambda: server.client(model="some/model"))
    r = diagnose(client, org, diagnosable)
    assert r.status_code == 201, r.text
    [proposal] = r.json()
    # Stored like any other proposal: unapproved, to be verified, with the model named.
    assert proposal["state"] == "PROPOSED" and proposal["source"] == "agent"
    assert proposal["evidence"]["model"] == "some/model" and proposal["evidence"]["provider"] == "groq"
