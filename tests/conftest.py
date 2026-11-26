import json
import os
import tempfile
from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures"
os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "test.db")
os.environ["ROUTING_PATH"] = str(FIXTURES / "routing.json")
os.environ["PRICING_PATH"] = str(FIXTURES / "pricing.json")

import httpx  # noqa: E402
import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import db  # noqa: E402
from app.main import app  # noqa: E402


def _sse(events, done=True) -> bytes:
    out = "".join(f"data: {json.dumps(e)}\n\n" for e in events)
    if done:
        out += "data: [DONE]\n\n"
    return out.encode()


class FakeUpstream:
    """Stands in for every provider. Records requests; can be told to fail or to 'reason'."""

    def __init__(self):
        self.seen: list[dict] = []
        self.hosts: list[str] = []
        self.fail: dict[str, int] = {}  # host -> status code to return
        self.down: set[str] = set()  # hosts that can't be reached at all
        self.reasoning_text = ""
        self.reported_reasoning: int | None = None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        host = request.url.host
        self.seen.append(body)
        self.hosts.append(host)
        if host in self.down:
            raise httpx.ConnectError("simulated outage", request=request)
        if host in self.fail:
            return httpx.Response(self.fail[host], json={"error": "simulated"})
        anthropic = "anthropic" in host
        usage = {"prompt_tokens": 1000, "completion_tokens": 500}
        if self.reported_reasoning is not None:
            usage["completion_tokens_details"] = {"reasoning_tokens": self.reported_reasoning}
        if body.get("stream"):
            if anthropic:
                events = [
                    {"type": "message_start", "message": {"usage": {"input_tokens": 1000, "output_tokens": 1}}},
                    {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "hi"}},
                    {"type": "message_delta", "usage": {"output_tokens": 500}},
                    {"type": "message_stop"},
                ]
                return httpx.Response(200, content=_sse(events, done=False),
                                      headers={"content-type": "text/event-stream"})
            events = [
                {"choices": [{"delta": {"reasoning": self.reasoning_text}}]},
                {"choices": [{"delta": {"content": "hi"}}]},
                {"choices": [], "usage": usage},
            ]
            return httpx.Response(200, content=_sse(events), headers={"content-type": "text/event-stream"})
        if anthropic:
            return httpx.Response(200, json={"content": [{"type": "text", "text": "hi"}],
                                             "usage": {"input_tokens": 1000, "output_tokens": 500}})
        message = {"content": "hi"}
        if self.reasoning_text:
            message["reasoning"] = self.reasoning_text
        return httpx.Response(200, json={"model": body["model"], "choices": [{"message": message}],
                                         "usage": usage})


@pytest.fixture()
def upstream():
    return FakeUpstream()


@pytest.fixture()
def client(upstream):
    with TestClient(app) as c:
        app.state.client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        with db._lock:
            db._db().execute("DELETE FROM calls")
            db._db().execute("DELETE FROM cache")
            db._db().commit()
        yield c


def chat(content, **extra):
    return {"messages": [{"role": "user", "content": content}], **extra}
