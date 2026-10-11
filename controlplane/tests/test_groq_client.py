"""The Groq adapter, against a scripted HTTP server: no network and no key."""
import json

import httpx
import pytest

from app.proposers import agent
from app.proposers.groq_client import (LATEST_RESULT_CHARS, OLDER_RESULT_CHARS, GroqClient, GroqError)
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


def test_older_tool_results_are_sent_shortened_and_the_latest_in_fuller_form():
    long_rows = [{"tenant_role": "t_analytic", "query": "SELECT " + "x" * 200, "n": i} for i in range(60)]
    obs = FakeObserver(top_queries=long_rows, slo_status=long_rows)
    server = Server(completion(calls=[("c1", "get_top_queries", "{}")]), completion(calls=[("c2", "get_slo_status", "{}")]),
                    completion(calls=[PROPOSE]))
    result = agent.propose(obs, client=server.client())
    tools_sent = [m["content"] for m in server.requests[2]["messages"] if m["role"] == "tool"]
    assert len(tools_sent[0]) < OLDER_RESULT_CHARS + 100 < len(tools_sent[1]) < LATEST_RESULT_CHARS + 100
    assert "shortened" in tools_sent[0]
    # What the tool returned is still in the stored trace, in the trace's own length.
    assert len(result.evidence["trace"][0]["result"]) == 3000


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
