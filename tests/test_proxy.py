from tests.conftest import chat

OPENAI = "/openai/v1/chat/completions"


def test_logs_cost_and_caches(client, upstream):
    body = chat("hello", model="gpt-4o-mini", temperature=0)
    headers = {"X-Team": "search", "X-Feature": "summarize", "Authorization": "Bearer test"}

    r1 = client.post(OPENAI, json=body, headers=headers)
    assert r1.status_code == 200
    assert r1.headers["x-cost-guard-cache"] == "miss"
    # 1000 * 0.15/1M + 500 * 0.60/1M = 0.00045
    assert abs(float(r1.headers["x-cost-guard-cost-usd"]) - 0.00045) < 1e-9

    r2 = client.post(OPENAI, json=body, headers=headers)
    assert r2.headers["x-cost-guard-cache"] == "hit"
    assert len(upstream.seen) == 1  # second call never reached the provider

    client.post("/anthropic/v1/messages", headers={"X-Team": "support", "x-api-key": "test"},
                json=chat("hello", model="claude-haiku-4-5", max_tokens=100))

    s = client.get("/stats").json()
    assert s["totals"]["calls"] == 3
    assert s["totals"]["cache_hits"] == 1
    assert abs(s["totals"]["saved_usd"] - 0.00045) < 1e-9
    assert {t["name"] for t in s["by_team"]} == {"search", "support"}


def test_auto_routes_simple_prompt_to_cheap_model(client, upstream):
    r = client.post(OPENAI, json=chat("What's the capital of France?", model="auto"))
    assert r.status_code == 200
    assert r.headers["x-cost-guard-model"] == "gpt-4o-mini"
    assert upstream.seen[-1]["model"] == "gpt-4o-mini"
    # strong would cost 1000*2.5/1M + 500*10/1M = 0.0075; cheap costs 0.00045
    s = client.get("/stats").json()["totals"]
    assert s["routed_calls"] == 1
    assert abs(s["routing_saved_usd"] - (0.0075 - 0.00045)) < 1e-9


def test_auto_routes_complex_prompt_to_strong_model(client):
    r = client.post(OPENAI, json=chat("Debug this stack trace and explain why it fails", model="auto"))
    assert r.headers["x-cost-guard-model"] == "gpt-4o"
    assert "keyword" in r.headers["x-cost-guard-route-reason"]

    r = client.post(OPENAI, json=chat("x" * 5000, model="auto"))
    assert r.headers["x-cost-guard-model"] == "gpt-4o"
    assert r.headers["x-cost-guard-route-reason"] == "long prompt"

    r = client.post(OPENAI, json=chat("hi", model="auto", tools=[{"type": "function"}]))
    assert r.headers["x-cost-guard-model"] == "gpt-4o"


def test_explicit_model_is_never_rerouted(client, upstream):
    r = client.post(OPENAI, json=chat("hi", model="gpt-4o"))
    assert r.headers["x-cost-guard-model"] == "gpt-4o"
    assert upstream.seen[-1]["model"] == "gpt-4o"
    assert "reasoning_effort" not in upstream.seen[-1]


def test_openai_streaming_passes_through_and_is_priced(client, upstream):
    with client.stream("POST", OPENAI, json=chat("hello", model="gpt-4o-mini", stream=True),
                       headers={"X-Team": "chat"}) as r:
        assert r.status_code == 200
        text = "".join(r.iter_text())
    assert '"content": "hi"' in text and "[DONE]" in text
    assert upstream.seen[-1]["stream_options"] == {"include_usage": True}

    s = client.get("/stats").json()["totals"]
    assert s["streamed_calls"] == 1
    assert abs(s["cost_usd"] - 0.00045) < 1e-9


def test_anthropic_streaming_is_priced(client):
    with client.stream("POST", "/anthropic/v1/messages",
                       json=chat("hello", model="claude-haiku-4-5", max_tokens=100, stream=True)) as r:
        assert r.status_code == 200
        "".join(r.iter_text())
    s = client.get("/stats").json()["totals"]
    # 1000 * 1.00/1M + 500 * 5.00/1M = 0.0035
    assert abs(s["cost_usd"] - 0.0035) < 1e-9


def test_streams_are_not_cached(client, upstream):
    for _ in range(2):
        with client.stream("POST", OPENAI,
                           json=chat("hello", model="gpt-4o-mini", temperature=0, stream=True)) as r:
            "".join(r.iter_text())
    assert len(upstream.seen) == 2


def test_health_reports_version(client):
    assert client.get("/health").json() == {"ok": True, "version": "0.6.1"}
