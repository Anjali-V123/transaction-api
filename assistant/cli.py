"""
Ask the assistant a question against a running API.

    export GROQ_API_KEY=...
    python -m assistant.cli --email you@example.com --password yourpass "Which of my orders are unpaid?"

--base-url defaults to the nginx load balancer from docker-compose (port 8080).
Every run is appended to traces/assistant.jsonl.
"""

import argparse
import sys

import httpx

from .agent import ask
from .llm import GroqLLM
from .tools import ToolRunner
from .tracing import Tracer


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Ask the order assistant a question.")
    p.add_argument("question")
    p.add_argument("--email", required=True)
    p.add_argument("--password", required=True)
    p.add_argument("--base-url", default="http://localhost:8080")
    args = p.parse_args(argv)

    with httpx.Client(base_url=args.base_url, timeout=10) as http:
        login = http.post("/auth/login", json={"email": args.email, "password": args.password})
        if login.status_code != 200:
            print(f"Login failed ({login.status_code}): {login.text}", file=sys.stderr)
            return 1
        token = login.json()["access_token"]

        result = ask(args.question, GroqLLM(), ToolRunner(http, token), Tracer())

    if not result.ok:
        print(f"Sorry, I couldn't answer that ({result.error}).")
        return 2
    print(result.answer.answer)
    if result.answer.order_ids:
        print("Orders:", ", ".join(result.answer.order_ids))
    print(
        f"[run {result.run_id}: {result.steps} steps, tools={result.tools_called}, "
        f"{result.prompt_tokens + result.completion_tokens} tokens, {result.latency_ms} ms]"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
