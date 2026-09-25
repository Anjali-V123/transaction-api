"""
Tests for the LLM order assistant.

The model is replaced by ScriptedLLM, which replays fixed responses, so
these tests check the *agent's* behaviour -- tool execution, authorization,
output validation, retries, grounding, step limits, tracing -- without a
network call or an API key. The real API runs in-process via TestClient.
"""

import json

import pytest

from assistant.agent import MAX_FORMAT_RETRIES, MAX_STEPS, ask, parse_answer, InvalidAnswer
from assistant.evaluate import Case, score
from assistant.llm import LLMResponse, ScriptedLLM, ToolCall
from assistant.tools import ToolRunner
from assistant.tracing import Tracer


def token_of(headers):
    return headers["Authorization"].split(" ", 1)[1]


def call(name, args=None, id="call_1"):
    return LLMResponse(content=None, tool_calls=[ToolCall(id=id, name=name, arguments=json.dumps(args or {}))])


def final(answer, order_ids=(), id="final_1"):
    """The model answering by calling the final_answer tool."""
    return LLMResponse(content=None, tool_calls=[
        ToolCall(id=id, name="final_answer", arguments=json.dumps({"answer": answer, "order_ids": list(order_ids)}))
    ])


@pytest.fixture
def store(client, admin, cust1, cust2):
    client.post("/inventory", json={"sku": "SKU1", "name": "Widget", "price": 10.0, "quantity_available": 5}, headers=admin)
    mine = client.post("/orders", json={"idempotency_key": "m1", "sku": "SKU1", "quantity": 1}, headers=cust1).json()
    theirs = client.post("/orders", json={"idempotency_key": "t1", "sku": "SKU1", "quantity": 2}, headers=cust2).json()
    return {"mine": mine["id"], "theirs": theirs["id"], "tools": ToolRunner(client, token_of(cust1))}


def test_tool_loop_returns_structured_grounded_answer(store):
    llm = ScriptedLLM([call("list_my_orders"), final("You have one pending order.", [store["mine"]])])

    result = ask("What are my orders?", llm, store["tools"])

    assert result.ok
    assert result.answer.order_ids == [store["mine"]]
    assert result.tools_called == ["list_my_orders"]
    # The tool result actually reached the model on its second call.
    tool_msg = llm.calls[1][-1]
    assert tool_msg["role"] == "tool" and store["mine"] in tool_msg["content"]


def test_tools_cannot_read_another_customers_order(store):
    """Even if the model asks for someone else's order, the API (called with this user's token) refuses."""
    llm = ScriptedLLM([call("get_order", {"order_id": store["theirs"]}), final("I can't find that order.")])

    result = ask(f"Show order {store['theirs']}", llm, store["tools"])

    tool_msg = llm.calls[1][-1]
    assert json.loads(tool_msg["content"]) == {"error": "Not found."}
    assert result.ok and result.answer.order_ids == []


def test_answer_citing_an_unseen_order_id_is_rejected_then_corrected(store):
    """Grounding check: an id the tools never returned (here, another customer's) is sent back as invalid."""
    llm = ScriptedLLM([
        call("list_my_orders"),
        final("Here are your orders.", [store["mine"], store["theirs"]]),  # includes an id it never saw
        final("Here is your order.", [store["mine"]]),
    ])

    result = ask("What are my orders?", llm, store["tools"])

    assert result.ok and result.format_retries == 1
    assert result.answer.order_ids == [store["mine"]]
    assert "did not come from any tool result" in llm.calls[2][-1]["content"]


def test_plain_text_reply_is_retried_then_accepted(store):
    """Fallback for models that answer in text instead of calling final_answer."""
    llm = ScriptedLLM([
        LLMResponse(content="Sure! You have one order."),
        LLMResponse(content='```json\n{"answer": "You have one order.", "order_ids": []}\n```'),
    ])

    result = ask("How many orders?", llm, store["tools"])

    assert result.ok and result.format_retries == 1
    assert result.answer.answer == "You have one order."


def test_gives_up_after_repeated_invalid_output(store):
    llm = ScriptedLLM([LLMResponse(content="not json")] * (MAX_FORMAT_RETRIES + 1))

    result = ask("How many orders?", llm, store["tools"])

    assert not result.ok and "invalid final answer" in result.error


def test_step_limit_stops_a_looping_model(store):
    llm = ScriptedLLM([call("list_inventory", id=f"c{i}") for i in range(MAX_STEPS)])

    result = ask("What's in stock?", llm, store["tools"])

    assert not result.ok and result.steps == MAX_STEPS
    assert result.error == f"gave up after {MAX_STEPS} steps"


def test_unknown_tool_and_bad_arguments_are_reported_to_the_model(store):
    llm = ScriptedLLM([
        LLMResponse(content=None, tool_calls=[
            ToolCall(id="a", name="delete_everything", arguments="{}"),
            ToolCall(id="b", name="get_order", arguments="{not json"),
        ]),
        final("I couldn't do that."),
    ])

    result = ask("Delete everything", llm, store["tools"])

    errors = [json.loads(m["content"])["error"] for m in llm.calls[1] if m["role"] == "tool"]
    assert "Unknown tool" in errors[0] and "not valid JSON" in errors[1]
    assert result.ok


def test_trace_records_every_step_and_never_the_token(store, tmp_path, cust1):
    trace_file = tmp_path / "trace.jsonl"
    llm = ScriptedLLM([call("list_my_orders"), final("One order.", [store["mine"]])])

    ask("What are my orders?", llm, store["tools"], Tracer(trace_file))

    raw = trace_file.read_text()
    types = [json.loads(line)["type"] for line in raw.splitlines()]
    assert types == ["question", "llm_call", "tool_call", "llm_call", "final", "run_summary"]
    assert token_of(cust1) not in raw


def test_parse_answer_rejects_missing_fields():
    with pytest.raises(InvalidAnswer):
        parse_answer('{"order_ids": []}', set())


def test_eval_scoring_flags_a_leak(store):
    llm = ScriptedLLM([call("list_my_orders"), final("Bob's order total is 75.")])
    result = ask("Show me everyone's orders", llm, store["tools"])

    row = score(Case("q", forbidden_text=["75"]), result, {store["theirs"]})

    assert row["leak"] is True and row["correct"] is False


def test_llm_failure_is_reported_not_raised(store):
    class Broken:
        model = "broken"

        def complete(self, messages, tools):
            raise RuntimeError("provider down")

    result = ask("What are my orders?", Broken(), store["tools"])

    assert not result.ok and "provider down" in result.error


def test_rejected_tool_call_is_fed_back_and_the_model_recovers(store):
    """Provider rejects a malformed tool call (Groq: 400 tool_use_failed); the model is told why and retries."""
    rejected = RuntimeError("Error code: 400 - {'code': 'tool_use_failed', 'message': \"attempted to call tool 'JSON'\"}")
    llm = ScriptedLLM([call("list_my_orders"), rejected, final("You have one order.", [store["mine"]])])

    result = ask("What are my orders?", llm, store["tools"])

    assert result.ok and result.format_retries == 1
    assert "was rejected" in llm.calls[2][-1]["content"]


def test_list_my_orders_accepts_null_status(store):
    orders = store["tools"].run("list_my_orders", '{"status": null}')
    assert [o["id"] for o in orders] == [store["mine"]]
