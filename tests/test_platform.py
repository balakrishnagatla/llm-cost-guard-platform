"""Kubernetes-facing behaviour: health probes and structured logs."""
import json
import logging

from app import db, logging_setup
from tests.conftest import chat


def test_liveness(client):
    assert client.get("/health/live").json() == {"ok": True}


def test_readiness_ok(client):
    r = client.get("/health/ready")
    assert r.status_code == 200 and r.json()["ok"] is True


def test_readiness_fails_when_database_is_down(client, monkeypatch):
    def broken():
        raise RuntimeError("db down")
    monkeypatch.setattr(db, "ping", broken)
    r = client.get("/health/ready")
    assert r.status_code == 503
    assert r.json() == {"ok": False, "problems": ["database: RuntimeError"]}


def test_readiness_fails_on_bad_config(client, monkeypatch):
    import app.main as m

    def broken():
        raise FileNotFoundError("routing.json")
    monkeypatch.setattr(m, "load_config", broken)
    r = client.get("/health/ready")
    assert r.status_code == 503 and r.json()["problems"] == ["config: FileNotFoundError"]


def test_json_log_lines(monkeypatch, capsys):
    monkeypatch.setenv("LOG_FORMAT", "json")
    logging_setup.configure_logging()
    logging.getLogger("cost_guard").info("llm call", extra={"request_id": "abc123", "cost_usd": 0.0001})
    line = json.loads(capsys.readouterr().err.strip().splitlines()[-1])
    assert line["msg"] == "llm call" and line["level"] == "info"
    assert line["request_id"] == "abc123" and line["cost_usd"] == 0.0001
    assert line["ts"].endswith("Z")
    monkeypatch.setenv("LOG_FORMAT", "text")
    logging_setup.configure_logging()


def test_every_call_is_logged_without_prompt(client, caplog):
    logger = logging.getLogger("cost_guard")
    logger.addHandler(caplog.handler)
    try:
        r = client.post("/openai/v1/chat/completions", json=chat("my secret prompt", model="gpt-4o-mini"),
                        headers={"X-Team": "logs"})
    finally:
        logger.removeHandler(caplog.handler)
    rec = next(r for r in caplog.records if r.getMessage() == "llm call")
    assert rec.request_id == r.headers["x-request-id"] and rec.team == "logs" and rec.status == 200
    assert "my secret prompt" not in json.dumps({k: str(v) for k, v in vars(rec).items()})


def test_integer_settings_accept_scientific_notation(monkeypatch):
    from app.main import _env_int
    monkeypatch.setenv("SOME_LIMIT", "1e+06")
    assert _env_int("SOME_LIMIT", 5) == 1_000_000
    monkeypatch.delenv("SOME_LIMIT")
    assert _env_int("SOME_LIMIT", 5) == 5


def test_no_api_key_means_no_auth_header(client, upstream, monkeypatch):
    """Regression: with no key configured the gateway sent 'Authorization: Bearer ' (invalid) and every call 502'd."""
    import httpx

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    sent = {}
    real = upstream.__call__

    def capture(request: httpx.Request):
        sent.update(request.headers)
        return real(request)

    client.app.state.client = httpx.AsyncClient(transport=httpx.MockTransport(capture))
    r = client.post("/openai/v1/chat/completions", json=chat("hi", model="gpt-4o-mini"))
    assert r.status_code == 200
    assert "authorization" not in sent

    monkeypatch.setenv("OPENAI_API_KEY", "server-key")
    client.post("/openai/v1/chat/completions", json=chat("hi again", model="gpt-4o-mini"))
    assert sent["authorization"] == "Bearer server-key"
