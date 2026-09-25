"""
LLM order assistant for the Transaction Processing API.

Answers a logged-in customer's questions about their orders and the store's
inventory by letting an LLM call the API's own read-only endpoints as tools.

Layout:
    tools.py    -- tool schemas the LLM sees + the code that runs them (HTTP calls
                   to the API, made with the customer's own JWT)
    llm.py      -- a small LLM interface, a Groq implementation, and a scripted
                   fake used by the tests
    agent.py    -- the tool-calling loop, structured final answer, guardrails
    tracing.py  -- per-run JSONL trace of every LLM call and tool call
    cli.py      -- `python -m assistant.cli ...` to ask a question from a terminal
    evaluate.py -- seeded evaluation set + scoring
"""
