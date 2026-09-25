"""
The tool-calling loop.

    question -> LLM -> (tool calls -> results -> LLM)* -> final_answer tool call -> validate

Guardrails, in the order they apply:

1. Step limit: at most MAX_STEPS model calls per question, so a confused
   model can't loop forever (or run up the bill).
2. Tool errors are returned to the model as data ("Not found."), not raised,
   so it can recover, e.g. tell the user the order doesn't exist.
3. Structured output: the model gives its answer by calling a `final_answer`
   tool whose arguments must match AssistantAnswer. Using a tool for the
   answer (instead of asking for "only JSON" in plain text) means the
   provider checks the shape before we even see it, and it's the format
   tool-calling models handle most reliably. Invalid answers are sent back
   with the exact error, up to MAX_FORMAT_RETRIES times.
4. Grounding check: every order id in the answer must have appeared in a
   tool result during this run. An id the model made up (or copied from the
   question) is treated like invalid output and sent back for correction.
5. Malformed tool calls: if the provider rejects a tool call the model
   generated (wrong argument types, a tool that doesn't exist), the model is
   told what was wrong and tries again, instead of the question failing.
"""

import json
import re
import time
from dataclasses import dataclass, field

from pydantic import BaseModel, Field, ValidationError

from .llm import LLMResponse
from .tools import TOOL_SCHEMAS, ToolError, ToolRunner
from .tracing import Tracer

MAX_STEPS = 6
MAX_FORMAT_RETRIES = 2

FINAL_ANSWER_TOOL = "final_answer"
FINAL_ANSWER_SCHEMA = {
    "type": "function",
    "function": {
        "name": FINAL_ANSWER_TOOL,
        "description": "Give your final answer to the customer. Call this exactly once, when you are done.",
        "parameters": {
            "type": "object",
            "properties": {
                "answer": {"type": "string", "description": "A short, direct answer for the customer."},
                "order_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Ids of every order the answer refers to; empty if none.",
                },
            },
            "required": ["answer", "order_ids"],
        },
    },
}
ALL_TOOLS = TOOL_SCHEMAS + [FINAL_ANSWER_SCHEMA]

SYSTEM_PROMPT = """You are an assistant for a small online store. You help ONE logged-in customer with questions about their own orders and about the store's inventory.

Rules:
- Use the tools to look things up. Never guess order details, prices, stock levels or totals; if the tools don't give you the answer, say you don't know.
- You can only see this customer's orders. If asked about other customers' orders, say you can't access them.
- You cannot place, pay for, cancel or change orders. If asked to, say so.
- Money amounts are in the same units as the data; don't add a currency symbol.
- To answer, call the final_answer tool. Put the ids of every order your answer refers to in order_ids (empty list if none)."""


class AssistantAnswer(BaseModel):
    answer: str = Field(min_length=1)
    order_ids: list[str] = Field(default_factory=list)


class InvalidAnswer(Exception):
    pass


@dataclass
class AgentResult:
    ok: bool
    answer: AssistantAnswer | None
    tools_called: list[str] = field(default_factory=list)
    steps: int = 0
    format_retries: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: int = 0
    run_id: str = ""
    error: str | None = None


def parse_answer(content: str | None, allowed_order_ids: set[str]) -> AssistantAnswer:
    """Turn the model's answer (final_answer arguments, or a plain-text JSON reply) into a validated AssistantAnswer."""
    if not content or not content.strip():
        raise InvalidAnswer("The answer was empty.")
    # Plain-text replies sometimes wrap the JSON in ```json fences or a
    # sentence; take the outermost {...} rather than failing on that alone.
    match = re.search(r"\{.*\}", content, re.DOTALL)
    if not match:
        raise InvalidAnswer("The answer did not contain a JSON object.")
    try:
        answer = AssistantAnswer.model_validate(json.loads(match.group(0)))
    except json.JSONDecodeError as e:
        raise InvalidAnswer(f"The JSON could not be parsed: {e.msg}.")
    except ValidationError as e:
        raise InvalidAnswer(f"The JSON did not match the required shape: {e.errors()[0]['msg']}.")

    ungrounded = [oid for oid in answer.order_ids if oid not in allowed_order_ids]
    if ungrounded:
        raise InvalidAnswer(
            f"order_ids {ungrounded} did not come from any tool result. "
            "Only include ids returned by the tools."
        )
    return answer


def _order_ids_in(result) -> set[str]:
    """Order ids present in a tool result (orders are the only objects with an idempotency_key)."""
    items = result if isinstance(result, list) else [result]
    return {i["id"] for i in items if isinstance(i, dict) and "id" in i and "idempotency_key" in i}


def _is_rejected_tool_call(error: Exception) -> bool:
    # Groq returns HTTP 400 with code "tool_use_failed" when the model generates
    # a tool call that doesn't match the schema or names a tool that doesn't exist.
    return "tool_use_failed" in str(error)


def ask(question: str, llm, tools: ToolRunner, tracer: Tracer | None = None) -> AgentResult:
    tracer = tracer or Tracer(path=None)
    started = time.perf_counter()
    result = AgentResult(ok=False, answer=None, run_id=tracer.run_id)
    tracer.log("question", question=question, model=getattr(llm, "model", "?"))

    messages: list[dict] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": question},
    ]
    seen_order_ids: set[str] = set()

    def retry_or_fail(reason: str) -> bool:
        """Count a correction attempt. Returns False (and records the error) once retries are used up."""
        if result.format_retries >= MAX_FORMAT_RETRIES:
            result.error = f"invalid final answer: {reason}"
            return False
        result.format_retries += 1
        return True

    for step in range(1, MAX_STEPS + 1):
        result.steps = step
        t0 = time.perf_counter()
        try:
            response: LLMResponse = llm.complete(messages, ALL_TOOLS)
        except Exception as e:  # provider outage, rate limit after retries, malformed tool call...
            error = f"{type(e).__name__}: {e}"
            tracer.log("llm_error", step=step, error=error[:500])
            if _is_rejected_tool_call(e) and retry_or_fail(error):
                messages.append({
                    "role": "user",
                    "content": f"Your last tool call was rejected: {error[:400]} "
                               "Only call the tools you were given, with arguments matching their schemas.",
                })
                continue
            result.error = result.error or f"LLM call failed: {error}"
            break

        result.prompt_tokens += response.prompt_tokens
        result.completion_tokens += response.completion_tokens
        tracer.log(
            "llm_call",
            step=step,
            latency_ms=round((time.perf_counter() - t0) * 1000),
            prompt_tokens=response.prompt_tokens,
            completion_tokens=response.completion_tokens,
            requested_tools=[tc.name for tc in response.tool_calls],
        )

        if response.tool_calls:
            messages.append({
                "role": "assistant",
                "content": response.content or "",
                "tool_calls": [
                    {"id": tc.id, "type": "function", "function": {"name": tc.name, "arguments": tc.arguments}}
                    for tc in response.tool_calls
                ],
            })
            final_call = None
            for tc in response.tool_calls:
                if tc.name == FINAL_ANSWER_TOOL:
                    final_call = tc  # handled after any lookups in the same turn
                    continue
                result.tools_called.append(tc.name)
                t1 = time.perf_counter()
                try:
                    output = tools.run(tc.name, tc.arguments)
                    seen_order_ids |= _order_ids_in(output)
                    content, status = json.dumps(output, default=str), "ok"
                except ToolError as e:
                    content, status = json.dumps({"error": str(e)}), "error"
                tracer.log(
                    "tool_call",
                    step=step,
                    tool=tc.name,
                    arguments=tc.arguments,
                    status=status,
                    latency_ms=round((time.perf_counter() - t1) * 1000),
                    result_preview=Tracer.preview(content),
                )
                messages.append({"role": "tool", "tool_call_id": tc.id, "name": tc.name, "content": content})

            if final_call is None:
                continue
            try:
                result.answer = parse_answer(final_call.arguments, seen_order_ids)
                result.ok = True
                tracer.log("final", step=step, answer=result.answer.model_dump())
                break
            except InvalidAnswer as e:
                tracer.log("invalid_answer", step=step, reason=str(e), raw=Tracer.preview(final_call.arguments))
                messages.append({
                    "role": "tool",
                    "tool_call_id": final_call.id,
                    "name": FINAL_ANSWER_TOOL,
                    "content": json.dumps({"error": f"Answer rejected: {e} Call final_answer again."}),
                })
                if not retry_or_fail(str(e)):
                    break
                continue

        # Plain-text reply (some models answer in text instead of calling
        # final_answer): accept it if it's valid JSON, otherwise ask again.
        try:
            result.answer = parse_answer(response.content, seen_order_ids)
            result.ok = True
            tracer.log("final", step=step, answer=result.answer.model_dump())
            break
        except InvalidAnswer as e:
            tracer.log("invalid_answer", step=step, reason=str(e), raw=Tracer.preview(response.content or ""))
            if not retry_or_fail(str(e)):
                break
            messages.append({"role": "assistant", "content": response.content or ""})
            messages.append({
                "role": "user",
                "content": f"Your reply was invalid: {e} Give your answer by calling the final_answer tool.",
            })
    else:
        result.error = f"gave up after {MAX_STEPS} steps"

    result.latency_ms = round((time.perf_counter() - started) * 1000)
    tracer.log(
        "run_summary",
        ok=result.ok,
        error=result.error,
        steps=result.steps,
        format_retries=result.format_retries,
        tools_called=result.tools_called,
        prompt_tokens=result.prompt_tokens,
        completion_tokens=result.completion_tokens,
        latency_ms=result.latency_ms,
    )
    return result
