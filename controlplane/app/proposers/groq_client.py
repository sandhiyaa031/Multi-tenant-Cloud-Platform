"""Runs the agent loop of agent.py against a model served by Groq.

Groq speaks the OpenAI chat-completions format. This adapter exposes the one call
the loop makes (`client.beta.messages.create`) and translates in both directions,
so the loop, the tools and the validation are exactly the ones used with any other
model. Nothing here can propose or apply anything by itself.

A free Groq account allows a few thousand tokens per minute, and every request
resends the whole conversation. So the adapter does two things the loop does not
know about: it waits when the API says the minute's budget is spent, and it sends
older tool results in shortened form. The trace stored with the proposal keeps what
each tool returned; what the model was shown is shorter, and `evidence` says so.
"""
import json
import os
import time
from types import SimpleNamespace

import httpx

URL = "https://api.groq.com/openai/v1/chat/completions"
DEFAULT_MODEL = "openai/gpt-oss-120b"
MAX_OUTPUT_TOKENS = 1500
LATEST_RESULT_CHARS = 2500     # tool results of the most recent turn, as shown to the model
OLDER_RESULT_CHARS = 300       # tool results of earlier turns
MAX_WAITS = 8                  # times one request may wait for the rate limit before giving up
MAX_WAIT_S = 65.0


class GroqError(RuntimeError):
    """The model API could not be used: transport failure, refusal by the API, or rate limit not clearing."""


def _shorten(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + f"... [shortened from {len(text)} characters to fit the rate limit]"


def to_openai_messages(system: str, messages: list[dict]) -> list[dict]:
    """The loop's conversation (Anthropic message shapes) as OpenAI chat messages."""
    last_results = max((i for i, m in enumerate(messages) if m["role"] == "user" and isinstance(m["content"], list)), default=-1)
    out = [{"role": "system", "content": system}]
    for i, message in enumerate(messages):
        content = message["content"]
        if isinstance(content, str):
            out.append({"role": message["role"], "content": content})
        elif message["role"] == "assistant":
            text = "\n".join(b.text for b in content if b.type == "text" and b.text.strip())
            calls = [{"id": b.id, "type": "function", "function": {"name": b.name, "arguments": json.dumps(b.input)}}
                     for b in content if b.type == "tool_use"]
            out.append({"role": "assistant", "content": text or None, **({"tool_calls": calls} if calls else {})})
        else:
            limit = LATEST_RESULT_CHARS if i == last_results else OLDER_RESULT_CHARS
            for result in content:
                out.append({"role": "tool", "tool_call_id": result["tool_use_id"], "content": _shorten(str(result["content"]), limit)})
    return out


def to_openai_tools(tools: list[dict]) -> list[dict]:
    return [{"type": "function", "function": {"name": t["name"], "description": t["description"], "parameters": t["input_schema"]}}
            for t in tools]


def from_openai_response(body: dict) -> SimpleNamespace:
    choice = body["choices"][0]
    message = choice["message"]
    content = []
    if (message.get("content") or "").strip():
        content.append(SimpleNamespace(type="text", text=message["content"]))
    for call in message.get("tool_calls") or []:
        try:
            arguments = json.loads(call["function"].get("arguments") or "{}")
        except json.JSONDecodeError:
            arguments = {}          # the loop answers a malformed call with a validation error the model can read
        content.append(SimpleNamespace(type="tool_use", id=call["id"], name=call["function"]["name"],
                                       input=arguments if isinstance(arguments, dict) else {}))
    usage = body.get("usage") or {}
    return SimpleNamespace(
        content=content, stop_reason="max_tokens" if choice.get("finish_reason") == "length" else choice.get("finish_reason"),
        usage=SimpleNamespace(input_tokens=usage.get("prompt_tokens", 0), output_tokens=usage.get("completion_tokens", 0),
                              cache_read_input_tokens=0))


class GroqClient:
    def __init__(self, api_key: str | None = None, model: str | None = None, http: httpx.Client | None = None,
                 sleep=time.sleep):
        self.model = model or os.environ.get("GROQ_MODEL") or DEFAULT_MODEL
        self.provider = "groq"
        self.waited_s = 0.0
        self._sleep = sleep
        self._http = http or httpx.Client(timeout=120, headers={
            "Authorization": f"Bearer {api_key or os.environ['GROQ_API_KEY']}"})
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, *, system, tools, messages, **_ignored) -> SimpleNamespace:
        payload = {"model": self.model, "max_completion_tokens": MAX_OUTPUT_TOKENS, "tool_choice": "auto",
                   "tools": to_openai_tools(tools),
                   "messages": to_openai_messages("".join(part["text"] for part in system), messages)}
        for _ in range(MAX_WAITS + 1):
            try:
                response = self._http.post(URL, json=payload)
            except httpx.HTTPError as exc:
                raise GroqError(f"could not reach the model API: {type(exc).__name__}") from exc
            if response.status_code == 429:
                try:
                    wait = float(response.headers.get("retry-after", "20"))
                except ValueError:
                    wait = 20.0
                if wait > MAX_WAIT_S:
                    # Not the per-minute budget: a daily quota is spent. Waiting inside a request would not help.
                    try:
                        said = response.json()["error"]["message"]
                    except (ValueError, KeyError, TypeError):
                        said = response.text
                    raise GroqError(f"the model API's quota is used up: {said[:240]}")
                wait += 1.0
                self.waited_s += wait
                self._sleep(wait)
                continue
            if response.status_code >= 400:
                raise GroqError(f"the model API answered {response.status_code}: {response.text[:300]}")
            return from_openai_response(response.json())
        raise GroqError("the model API's rate limit did not clear")
