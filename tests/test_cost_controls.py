"""Reasoning control, pre-call budget estimates and provider failover."""
from tests.conftest import chat

OPENAI = "/openai/v1/chat/completions"


# ---------- reasoning control ----------

def test_cheap_tier_gets_low_reasoning_and_output_cap(client, upstream):
    client.post(OPENAI, json=chat("hello", model="auto"))
    sent = upstream.seen[-1]
    assert sent["reasoning_effort"] == "low"
    assert sent["max_completion_tokens"] == 256


def test_strong_tier_is_left_alone(client, upstream):
    client.post(OPENAI, json=chat("analyze this step by step", model="auto"))
    sent = upstream.seen[-1]
    assert "reasoning_effort" not in sent and "max_completion_tokens" not in sent


def test_callers_own_settings_win(client, upstream):
    client.post(OPENAI, json=chat("hello", model="auto", reasoning_effort="high", max_completion_tokens=50))
    sent = upstream.seen[-1]
    assert sent["reasoning_effort"] == "high"
    assert sent["max_completion_tokens"] == 50


def test_reported_reasoning_tokens_are_tracked(client, upstream):
    upstream.reported_reasoning = 420
    client.post(OPENAI, json=chat("hello", model="gpt-4o-mini"))
    s = client.get("/stats").json()
    assert s["totals"]["reasoning_tokens"] == 420
    assert s["by_model"][0]["reasoning_tokens"] == 420


def test_reasoning_estimated_from_text_when_not_reported(client, upstream):
    upstream.reasoning_text = "x" * 800  # ~200 tokens
    client.post(OPENAI, json=chat("hello", model="gpt-4o-mini"))
    assert client.get("/stats").json()["totals"]["reasoning_tokens"] == 200


def test_reasoning_estimated_in_streams(client, upstream):
    upstream.reasoning_text = "x" * 400  # ~100 tokens
    with client.stream("POST", OPENAI, json=chat("hello", model="gpt-4o-mini", stream=True)) as r:
        "".join(r.iter_text())
    assert client.get("/stats").json()["totals"]["reasoning_tokens"] == 100


# ---------- pre-call budget estimate ----------

def test_budget_blocks_before_overspending(client, upstream):
    # Each call: ~1 input token + 100 output max on gpt-4o-mini = ~0.00006 estimated; actual 0.00045.
    client.app.state.budgets = {"search": 0.0006}
    try:
        h = {"X-Team": "search"}
        r1 = client.post(OPENAI, headers=h, json=chat("a", model="gpt-4o-mini", max_tokens=100))
        assert r1.status_code == 200
        assert float(r1.headers["x-cost-guard-estimate-usd"]) > 0
        # 0.00045 spent; next estimate 0.00006 -> 0.00051 fits
        assert client.post(OPENAI, headers=h, json=chat("b", model="gpt-4o-mini", max_tokens=100)).status_code == 200
        # 0.00090 spent: already over, blocked before reaching the provider
        r3 = client.post(OPENAI, headers=h, json=chat("c", model="gpt-4o-mini", max_tokens=100))
        assert r3.status_code == 429
        assert "would be exceeded" in r3.json()["detail"]
        assert len(upstream.seen) == 2
    finally:
        client.app.state.budgets = {}


def test_big_estimate_is_blocked_up_front(client, upstream):
    client.app.state.budgets = {"search": 0.001}
    try:
        # 100k output tokens on gpt-4o would cost ~$1: refused without spending anything
        r = client.post(OPENAI, headers={"X-Team": "search"},
                        json=chat("hi", model="gpt-4o", max_completion_tokens=100_000))
        assert r.status_code == 429
        assert upstream.seen == []
    finally:
        client.app.state.budgets = {}


# ---------- failover ----------

def test_fails_over_when_primary_is_rate_limited(client, upstream, monkeypatch):
    monkeypatch.setenv("BACKUP_API_KEY", "backup-secret")
    upstream.fail["api.groq.com"] = 429
    r = client.post(OPENAI, json=chat("hello", model="auto"))
    assert r.status_code == 200
    assert r.headers["x-cost-guard-upstream"] == "backup"
    assert r.headers["x-cost-guard-model"] == "backup-model"
    assert upstream.hosts == ["api.groq.com", "backup.example"]
    assert "reasoning_effort" not in upstream.seen[-1]  # dropped for the backup
    s = client.get("/stats").json()
    assert s["totals"]["fallback_calls"] == 1
    # priced at the backup model's rate: 1000*0.10/1M + 500*0.40/1M
    assert abs(s["totals"]["cost_usd"] - 0.0003) < 1e-9


def test_fails_over_in_streams(client, upstream, monkeypatch):
    monkeypatch.setenv("BACKUP_API_KEY", "backup-secret")
    upstream.fail["api.groq.com"] = 503
    with client.stream("POST", OPENAI, json=chat("hello", model="gpt-4o-mini", stream=True)) as r:
        assert r.headers["x-cost-guard-upstream"] == "backup"
        "".join(r.iter_text())
    assert upstream.hosts == ["api.groq.com", "backup.example"]


def test_no_failover_without_backup_key(client, upstream, monkeypatch):
    monkeypatch.delenv("BACKUP_API_KEY", raising=False)
    upstream.fail["api.groq.com"] = 429
    r = client.post(OPENAI, json=chat("hello", model="gpt-4o-mini"))
    assert r.status_code == 429  # passed through: nothing to fail over to
    assert upstream.hosts == ["api.groq.com"]


def test_client_errors_do_not_fail_over(client, upstream, monkeypatch):
    monkeypatch.setenv("BACKUP_API_KEY", "backup-secret")
    upstream.fail["api.groq.com"] = 400
    r = client.post(OPENAI, json=chat("hello", model="gpt-4o-mini"))
    assert r.status_code == 400
    assert upstream.hosts == ["api.groq.com"]


def test_failover_answers_are_not_cached(client, upstream, monkeypatch):
    monkeypatch.setenv("BACKUP_API_KEY", "backup-secret")
    upstream.fail["api.groq.com"] = 429
    body = chat("hello", model="gpt-4o-mini", temperature=0)
    client.post(OPENAI, json=body)
    upstream.fail.clear()
    r = client.post(OPENAI, json=body)
    assert r.headers["x-cost-guard-cache"] == "miss"
    assert r.headers["x-cost-guard-upstream"] == "primary"
