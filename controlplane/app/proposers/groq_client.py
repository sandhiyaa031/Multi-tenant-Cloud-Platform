"""Runs the agent loop of agent.py against a model behind an OpenAI-style chat-completions API:
one served by Groq, or Google's Gemini through its OpenAI-compatible endpoint.

The adapter exposes the one call the loop makes (`client.beta.messages.create`) and
translates in both directions, so the loop, the tools and the validation are exactly
the ones used with any other model. Nothing here can propose or apply anything by itself.

A free Groq account allows a few thousand tokens per minute, and every request
resends the whole conversation. So for Groq the adapter does two things the loop does
not know about: it waits when the API says the minute's budget is spent, and it keeps
all tool results together within a size budget, shortening the oldest first. The
trace stored with the proposal keeps what each tool returned; what the model was
shown can be shorter, and `evidence` says so. Gemini's limits are wide enough that
its client shows results whole.

The first live run (2026-10-12) cut every earlier result to 300 characters. The
model then asked for the same observations again and again and used up its turns
before deciding. Hence the shared budget: recent results stay readable.
"""
import json
import os
import re
import time
from types import SimpleNamespace

import httpx

URL = "https://api.groq.com/openai/v1/chat/completions"
DEFAULT_MODEL = "openai/gpt-oss-120b"
MAX_OUTPUT_TOKENS = 1500
RESULT_CHARS = 2500            # the most of any one tool result the model is shown
CONTEXT_CHARS = 9000           # all tool results in one request together; the newest are kept whole first
STUB_CHARS = 200               # what is left of a result once the budget is spent: enough to see it was asked
MAX_WAITS = 8                  # times one request may wait for the rate limit before giving up
MAX_WAIT_S = 65.0
BUSY_WAIT_S = 15.0             # between tries when the provider says it is overloaded
_RETRY_IN = re.compile(r'retry(?:Delay"?:\s*"| in )(\d+(?:\.\d+)?)s')


class GroqError(RuntimeError):
    """The model API could not be used: transport failure, refusal by the API, or rate limit not clearing."""


def _shorten(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + f"... [shortened from {len(text)} characters to fit the rate limit]"


def _allowances(messages: list[dict], result_chars: int, context_chars: int) -> dict[str, int]:
    """Characters of each tool result to show, by tool_use_id: the budget is spent from the newest result back."""
    results = [r for m in messages if m["role"] == "user" and isinstance(m["content"], list)
               for r in m["content"] if r.get("type") == "tool_result"]
    remaining, allowed = context_chars, {}
    for result in reversed(results):
        allowed[result["tool_use_id"]] = max(STUB_CHARS, min(result_chars, remaining))
        remaining -= min(len(str(result["content"])), allowed[result["tool_use_id"]])
    return allowed


def to_openai_messages(system: str, messages: list[dict], result_chars: int = RESULT_CHARS,
                       context_chars: int = CONTEXT_CHARS) -> list[dict]:
    """The loop's conversation (Anthropic message shapes) as OpenAI chat messages."""
    allowed = _allowances(messages, result_chars, context_chars)
    out = [{"role": "system", "content": system}]
    for message in messages:
        content = message["content"]
        if isinstance(content, str):
            out.append({"role": message["role"], "content": content})
        elif message["role"] == "assistant":
            text = "\n".join(b.text for b in content if b.type == "text" and b.text.strip())
            calls = [{"id": b.id, "type": "function", "function": {"name": b.name, "arguments": json.dumps(b.input)},
                      # What the provider attached to its own call and wants back unchanged (Gemini's thought signature).
                      **({"extra_content": b.extra} if getattr(b, "extra", None) else {})}
                     for b in content if b.type == "tool_use"]
            out.append({"role": "assistant", "content": text or None, **({"tool_calls": calls} if calls else {})})
        else:
            for result in content:
                if result.get("type") == "tool_result":
                    out.append({"role": "tool", "tool_call_id": result["tool_use_id"],
                                "content": _shorten(str(result["content"]), allowed[result["tool_use_id"]])})
            # Anything the loop says beside the results (the last-turn notice) follows them as a user message.
            said = "\n".join(part["text"] for part in content if part.get("type") == "text")
            if said:
                out.append({"role": "user", "content": said})
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
                                       input=arguments if isinstance(arguments, dict) else {},
                                       extra=call.get("extra_content")))
    usage = body.get("usage") or {}
    return SimpleNamespace(
        content=content, stop_reason="max_tokens" if choice.get("finish_reason") == "length" else choice.get("finish_reason"),
        usage=SimpleNamespace(input_tokens=usage.get("prompt_tokens", 0), output_tokens=usage.get("completion_tokens", 0),
                              cache_read_input_tokens=0))


def _said(response: httpx.Response) -> str:
    """The provider's own error message, whichever of the shapes in use it comes in."""
    try:
        body = response.json()
        body = body[0] if isinstance(body, list) else body
        return str(body["error"]["message"])
    except (ValueError, KeyError, TypeError, IndexError):
        return response.text


class GroqClient:
    url = URL
    provider = "groq"
    key_env, model_env, default_model = "GROQ_API_KEY", "GROQ_MODEL", DEFAULT_MODEL
    max_output_tokens = MAX_OUTPUT_TOKENS
    result_chars, context_chars = RESULT_CHARS, CONTEXT_CHARS
    note = "tool results were kept within a size budget; the oldest were shortened before being shown to the model"

    def __init__(self, api_key: str | None = None, model: str | None = None, http: httpx.Client | None = None,
                 sleep=time.sleep):
        self.model = model or os.environ.get(self.model_env) or self.default_model
        self.waited_s = 0.0
        self._sleep = sleep
        self._http = http or httpx.Client(timeout=180, headers={
            "Authorization": f"Bearer {api_key or os.environ[self.key_env]}"})
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, *, system, tools, messages, **_ignored) -> SimpleNamespace:
        payload = {"model": self.model, "max_completion_tokens": self.max_output_tokens, "tool_choice": "auto",
                   "tools": to_openai_tools(tools),
                   "messages": to_openai_messages("".join(part["text"] for part in system), messages,
                                                  self.result_chars, self.context_chars)}
        last = ""
        for _ in range(MAX_WAITS + 1):
            try:
                response = self._http.post(self.url, json=payload)
            except httpx.HTTPError as exc:
                raise GroqError(f"could not reach the model API: {type(exc).__name__}") from exc
            if response.status_code == 429:
                told = _RETRY_IN.search(response.text)
                try:
                    wait = float(response.headers.get("retry-after") or (told.group(1) if told else "20"))
                except ValueError:
                    wait = 20.0
                if wait > MAX_WAIT_S:
                    # Not the per-minute budget: a daily quota is spent. Waiting inside a request would not help.
                    raise GroqError(f"the model API's quota is used up: {_said(response)[:240]}")
                wait += 1.0
                self.waited_s += wait
                self._sleep(wait)
                continue
            if response.status_code in (500, 502, 503, 504):
                # The provider is overloaded or briefly down. Seen on the first Gemini run, one call in.
                last = f"the model API answered {response.status_code}: {_said(response)[:300]}"
                self.waited_s += BUSY_WAIT_S
                self._sleep(BUSY_WAIT_S)
                continue
            if response.status_code >= 400:
                raise GroqError(f"the model API answered {response.status_code}: {_said(response)[:300]}")
            return from_openai_response(response.json())
        raise GroqError(last or "the model API's rate limit did not clear")


class GeminiClient(GroqClient):
    """Google's Gemini through its OpenAI-compatible endpoint. Its limits do not call for shortening tool results."""
    url = "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
    provider = "gemini"
    key_env, model_env, default_model = "GEMINI_API_KEY", "GEMINI_MODEL", "gemini-3.8-flash"
    max_output_tokens = 8000
    result_chars, context_chars = 12000, 10**9
    note = "tool results were shown to the model whole"
