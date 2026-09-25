"""
Append-only JSONL tracing: one line per event, one run_id per question.

Every LLM call (latency, token counts, which tools it asked for) and every
tool call (arguments, latency, success or error, size of the result) is
recorded, so a wrong answer can be traced back to the step that caused it:
did the model pick the wrong tool, pass the wrong arguments, or misread a
correct result?

The customer's JWT is never passed to the tracer, so it can't end up in a
log file. Tool results are truncated rather than logged in full.
"""

import json
import time
import uuid
from pathlib import Path

MAX_LOGGED_CHARS = 500


class Tracer:
    def __init__(self, path: str | Path | None = "traces/assistant.jsonl"):
        self.path = Path(path) if path else None
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.run_id = uuid.uuid4().hex[:12]
        self.events: list[dict] = []

    def log(self, event_type: str, **fields):
        event = {"run_id": self.run_id, "ts": round(time.time(), 3), "type": event_type, **fields}
        self.events.append(event)
        if self.path:
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(event, default=str) + "\n")

    @staticmethod
    def preview(value) -> str:
        text = value if isinstance(value, str) else json.dumps(value, default=str)
        return text if len(text) <= MAX_LOGGED_CHARS else text[:MAX_LOGGED_CHARS] + "...(truncated)"
