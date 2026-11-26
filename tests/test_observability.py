"""Tracing, recent calls, insights, Prometheus metrics and OpenTelemetry export."""
import base64

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from app import observability
from app.observability import REGISTRY
from tests.conftest import chat

OPENAI = "/openai/v1/chat/completions"


def steps(client, request_id):
    return [(s["step"], s["ok"]) for s in client.get(f"/calls/{request_id}").json()["trace"]]


# ---------- request IDs and traces ----------

def test_every_response_has_a_request_id_and_stored_trace(client):
    r = client.post(OPENAI, json=chat("hello", model="auto"))
    rid = r.headers["x-request-id"]
    assert len(rid) == 16
    call = client.get(f"/calls/{rid}").json()
    assert call["team"] == "unknown" and call["model"] == "gpt-4o-mini"
    names = [s["step"] for s in call["trace"]]
    assert names == ["auth", "route", "estimate", "cache", "upstream", "result"]
    route = call["trace"][1]["detail"]
    assert "cheap" in route and "reasoning_effort=low" in route


def test_cache_hit_trace(client):
    body = chat("hello", model="gpt-4o-mini", temperature=0)
    client.post(OPENAI, json=body)
    rid = client.post(OPENAI, json=body).headers["x-request-id"]
    trace = client.get(f"/calls/{rid}").json()["trace"]
    assert [s["step"] for s in trace] == ["auth", "route", "estimate", "cache", "result"]
    assert trace[3]["detail"].startswith("hit")


def test_failover_trace_shows_both_attempts(client, upstream, monkeypatch):
    monkeypatch.setenv("BACKUP_API_KEY", "x")
    upstream.fail["api.groq.com"] = 429
    rid = client.post(OPENAI, json=chat("hello", model="auto")).headers["x-request-id"]
    trace = client.get(f"/calls/{rid}").json()["trace"]
    attempts = [s for s in trace if s["step"] == "upstream"]
    assert [a["ok"] for a in attempts] == [False, True]
    assert "429" in attempts[0]["detail"] and "backup" in attempts[1]["detail"]


def test_budget_refusal_is_traced_and_counted(client):
    client.app.state.budgets = {"search": 0.00001}
    try:
        r = client.post(OPENAI, headers={"X-Team": "search"}, json=chat("hi", model="gpt-4o-mini"))
        assert r.status_code == 429
        call = client.get(f"/calls/{r.headers['x-request-id']}").json()
        assert call["upstream"] == "blocked" and call["status"] == 429
        assert ("budget", False) in [(s["step"], s["ok"]) for s in call["trace"]]
        assert client.get("/stats").json()["insights"]["blocked_calls"] == 1
    finally:
        client.app.state.budgets = {}


def test_unknown_request_id_is_404(client):
    assert client.get("/calls/doesnotexist").status_code == 404


def test_recent_calls_newest_first(client):
    for text in ("a", "b", "c"):
        client.post(OPENAI, json=chat(text, model="gpt-4o-mini"), headers={"X-Feature": text})
    calls = client.get("/calls?limit=2").json()["calls"]
    assert [c["feature"] for c in calls] == ["c", "b"]


# ---------- failure paths ----------

def test_all_upstreams_down_returns_502_with_request_id(client, upstream, monkeypatch):
    monkeypatch.setenv("BACKUP_API_KEY", "x")
    upstream.down |= {"api.groq.com", "backup.example"}
    r = client.post(OPENAI, json=chat("hello", model="gpt-4o-mini"))
    assert r.status_code == 502
    call = client.get(f"/calls/{r.headers['x-request-id']}").json()
    assert call["status"] == 502 and call["upstream"] == "none"
    assert [s["ok"] for s in call["trace"] if s["step"] == "upstream"] == [False, False]


def test_stream_all_upstreams_down_returns_502(client, upstream):
    upstream.down.add("api.groq.com")
    with client.stream("POST", OPENAI, json=chat("hi", model="gpt-4o-mini", stream=True)) as r:
        assert r.status_code == 502


def test_stream_upstream_error_is_passed_through(client, upstream):
    upstream.fail["api.groq.com"] = 400
    with client.stream("POST", OPENAI, json=chat("hi", model="gpt-4o-mini", stream=True)) as r:
        assert r.status_code == 400
        rid = r.headers["x-request-id"]
    assert client.get(f"/calls/{rid}").json()["status"] == 400


# ---------- insights ----------

def test_insights(client, upstream, monkeypatch):
    upstream.reported_reasoning = 400
    body = chat("hello", model="gpt-4o-mini", temperature=0)
    client.post(OPENAI, json=body, headers={"X-Feature": "chat"})
    client.post(OPENAI, json=body, headers={"X-Feature": "chat"})  # cache hit
    client.post(OPENAI, json=chat("analyze this step by step", model="auto"), headers={"X-Feature": "analyze"})
    i = client.get("/stats").json()["insights"]

    # spend: 0.00045 (mini) + 0.0075 (gpt-4o); saved: 0.00045 by cache
    assert abs(i["savings_rate"] - 0.00045 / (0.00045 + 0.0075 + 0.00045)) < 1e-4  # rounded to 4 places
    assert [c["feature"] for c in i["top_calls"]] == ["analyze", "chat"]
    assert [f["feature"] for f in i["cost_per_call_by_feature"]] == ["analyze", "chat"]
    assert all(m["share"] == 0.8 for m in i["reasoning_share_by_model"])  # 400 of 500
    assert i["latency_by_upstream"][0]["upstream"] == "primary"
    assert i["latency_by_upstream"][0]["calls"] == 2  # cache hits excluded
    tl = i["timeline"]
    assert tl["bucket_seconds"] == 30  # brand-new data -> 30-second detail over the last 15 minutes
    assert len(tl["points"]) == 30  # empty buckets included
    assert sum(p["calls"] for p in tl["points"]) == 3
    assert i["strong_model_share"] == 1.0  # the one routed, non-cached call went to the strong tier
    assert i["projected_monthly_usd"] > 0
    assert i["cost_per_1k_tokens_usd"] > 0
    chat_cache = next(f for f in i["cache_hit_rate_by_feature"] if f["feature"] == "chat")
    assert chat_cache == {"feature": "chat", "calls": 2, "hit_rate": 0.5}


# ---------- Prometheus ----------

def metric(name, **labels):
    return REGISTRY.get_sample_value(name, labels) or 0.0


def test_metrics_endpoint_and_counters(client):
    labels = {"team": "metrics-team", "feature": "unknown", "model": "gpt-4o-mini"}
    before = metric("costguard_cost_usd_total", **labels)
    client.post(OPENAI, json=chat("hi", model="gpt-4o-mini"), headers={"X-Team": "metrics-team"})
    assert abs(metric("costguard_cost_usd_total", **labels) - before - 0.00045) < 1e-9

    text = client.get("/metrics").text
    for name in ("costguard_requests_total", "costguard_cost_usd_total", "costguard_tokens_total",
                 "costguard_request_duration_seconds_bucket"):
        assert name in text


def test_budget_gauges(client):
    client.app.state.budgets = {"gauge-team": 1.0}
    try:
        client.post(OPENAI, json=chat("hi", model="gpt-4o-mini"), headers={"X-Team": "gauge-team"})
        text = client.get("/metrics").text
        assert 'costguard_budget_usd{team="gauge-team"} 1.0' in text
        assert 'costguard_budget_remaining_usd{team="gauge-team"} 0.99955' in text
    finally:
        client.app.state.budgets = {}


def test_upstream_errors_are_counted(client, upstream, monkeypatch):
    monkeypatch.setenv("BACKUP_API_KEY", "x")
    before = metric("costguard_upstream_errors_total", upstream="primary", reason="503")
    upstream.fail["api.groq.com"] = 503
    client.post(OPENAI, json=chat("hi", model="gpt-4o-mini"))
    assert metric("costguard_upstream_errors_total", upstream="primary", reason="503") == before + 1


def test_metrics_token(client, monkeypatch):
    monkeypatch.setenv("METRICS_TOKEN", "scrape-secret")
    assert client.get("/metrics").status_code == 401
    assert client.get("/metrics", headers={"Authorization": "Bearer scrape-secret"}).status_code == 200


def test_tokens_from_secret_files_ignore_trailing_newline(client, monkeypatch):
    # Kubernetes Secrets created from files often keep the file's trailing newline
    monkeypatch.setenv("METRICS_TOKEN", "scrape-secret\n")
    assert client.get("/metrics", headers={"Authorization": "Bearer scrape-secret"}).status_code == 200
    monkeypatch.setenv("DASHBOARD_PASSWORD", "pw-123456\n")
    good = "Basic " + base64.b64encode(b"admin:pw-123456").decode()
    assert client.get("/calls", headers={"Authorization": good}).status_code == 200


def test_new_endpoints_need_dashboard_login(client, monkeypatch):
    monkeypatch.setenv("DASHBOARD_PASSWORD", "pw-123456")
    assert client.get("/calls").status_code == 401
    good = "Basic " + base64.b64encode(b"admin:pw-123456").decode()
    assert client.get("/calls", headers={"Authorization": good}).status_code == 200


# ---------- OpenTelemetry ----------

@pytest.fixture()
def spans():
    exporter = InMemorySpanExporter()
    observability.init_tracing(exporter=exporter)
    yield exporter
    observability._tracer = None


def test_spans_are_exported_with_genai_attributes(client, spans, upstream, monkeypatch):
    monkeypatch.setenv("BACKUP_API_KEY", "x")
    upstream.fail["api.groq.com"] = 429
    rid = client.post(OPENAI, json=chat("hello", model="auto"), headers={"X-Team": "otel"}).headers["x-request-id"]

    finished = spans.get_finished_spans()
    root = next(s for s in finished if s.name == "llm.request")
    assert root.attributes["costguard.request_id"] == rid
    assert root.attributes["costguard.team"] == "otel"
    assert root.attributes["gen_ai.request.model"] == "auto"
    assert root.attributes["gen_ai.response.model"] == "backup-model"
    assert root.attributes["gen_ai.usage.output_tokens"] == 500

    children = [s for s in finished if s.parent and s.parent.span_id == root.context.span_id]
    assert [c.name for c in children] == ["auth", "route", "estimate", "cache", "upstream", "upstream", "result"]
    failed = children[4]
    assert failed.status.status_code.name == "ERROR"
    assert all(c.start_time >= root.start_time and c.end_time <= root.end_time for c in children)


def test_tracing_is_off_by_default(monkeypatch):
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    assert observability.init_tracing() is False


def test_counters_are_seeded_from_history(client):
    """After a restart, Prometheus counters continue from the database totals instead of zero."""
    from app import db
    from app.observability import seed_counters

    client.post(OPENAI, json=chat("hi", model="gpt-4o-mini"), headers={"X-Team": "seed-team"})
    rows = [r for r in db.totals_for_metrics() if r["team"] == "seed-team"]
    assert rows and rows[0]["calls"] == 1
    labels = {"team": "seed-team", "feature": "unknown", "model": "gpt-4o-mini"}
    before = metric("costguard_cost_usd_total", **labels)
    seed_counters(rows)  # what startup does with the stored history
    assert abs(metric("costguard_cost_usd_total", **labels) - before - 0.00045) < 1e-9
