"""
A deliberately small LLM interface.

The agent only needs one operation -- "given these messages and tools, what
does the model say next?" -- so that's the whole interface. Keeping it this
narrow means:

* the Groq SDK is used in exactly one place (GroqLLM), so switching provider
  is a change to one class, and
* tests can swap in ScriptedLLM, which replays fixed responses. The agent's
  logic (tool loop, retries, guardrails) is then tested deterministically,
  with no network, no API key and no cost.
"""

import os
import time
from dataclasses import dataclass, field


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: str  # JSON string, exactly as the model produced it


@dataclass
class LLMResponse:
    content: str | None
    tool_calls: list[ToolCall] = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0


class GroqLLM:
    def __init__(self, model: str | None = None, api_key: str | None = None):
        from groq import Groq  # imported here so tests don't need the package configured

        key = api_key or os.getenv("GROQ_API_KEY")
        if not key:
            raise RuntimeError("GROQ_API_KEY is not set.")
        self._client = Groq(api_key=key)
        self.model = model or os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")

    def complete(self, messages: list[dict], tools: list[dict]) -> LLMResponse:
        from groq import RateLimitError

        # Groq's free tier has a low requests-per-minute limit; back off and
        # retry a few times instead of failing the whole question.
        for attempt in range(4):
            try:
                resp = self._client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    tools=tools,
                    # "required": every turn must be a tool call. The answer
                    # itself is a tool (final_answer), so the model can never
                    # end a turn with an empty or free-text reply -- which
                    # gpt-oss-120b did on most questions with "auto".
                    tool_choice="required",
                    temperature=0,  # repeatable answers; this is a data lookup, not creative writing
                )
                break
            except RateLimitError:
                if attempt == 3:
                    raise
                time.sleep(2 ** (attempt + 2))  # 4s, 8s, 16s
        msg = resp.choices[0].message
        calls = [
            ToolCall(id=tc.id, name=tc.function.name, arguments=tc.function.arguments)
            for tc in (msg.tool_calls or [])
        ]
        usage = resp.usage
        return LLMResponse(
            content=msg.content,
            tool_calls=calls,
            prompt_tokens=getattr(usage, "prompt_tokens", 0) or 0,
            completion_tokens=getattr(usage, "completion_tokens", 0) or 0,
        )


class ScriptedLLM:
    """Test double: returns the given responses in order and records what it was sent."""

    def __init__(self, responses: list[LLMResponse]):
        self._responses = list(responses)
        self.calls: list[list[dict]] = []
        self.model = "scripted"

    def complete(self, messages: list[dict], tools: list[dict]) -> LLMResponse:
        self.calls.append([dict(m) for m in messages])
        if not self._responses:
            raise AssertionError("ScriptedLLM ran out of responses")
        response = self._responses.pop(0)
        if isinstance(response, Exception):  # lets tests simulate a provider error
            raise response
        return response
