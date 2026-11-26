import base64

import pytest

from tests.conftest import chat

KEY_SEARCH = "k_search_0123456789abcdef"
KEY_SUPPORT = "k_support_0123456789abcdef"


@pytest.fixture()
def locked(monkeypatch):
    monkeypatch.setenv("COST_GUARD_KEYS", f"search:{KEY_SEARCH},support:{KEY_SUPPORT}")


def post(client, key=None, team=None, **body):
    headers = {}
    if key:
        headers["X-Cost-Guard-Key"] = key
    if team:
        headers["X-Team"] = team
    return client.post("/openai/v1/chat/completions", headers=headers,
                       json=chat("hello", model="gpt-4o-mini", **body))


def test_proxy_key_required_when_configured(client, upstream, locked):
    assert post(client).status_code == 401
    assert post(client, key="k_wrong_0123456789abcdef").status_code == 401
    assert post(client, key=KEY_SEARCH).status_code == 200


def test_team_comes_from_key_not_header(client, upstream, locked):
    post(client, key=KEY_SEARCH, team="support")  # tries to bill another team
    teams = {t["name"] for t in client.get("/stats").json()["by_team"]}
    assert teams == {"search"}


def test_bad_labels_are_neutralised(client, upstream):
    client.post("/openai/v1/chat/completions",
                headers={"X-Team": "<img src=x onerror=alert(1)>", "X-Feature": "ok-feature"},
                json=chat("hello", model="gpt-4o-mini"))
    s = client.get("/stats").json()
    assert {t["name"] for t in s["by_team"]} == {"invalid-label"}
    assert {f["name"] for f in s["by_feature"]} == {"ok-feature"}


def test_cache_is_scoped_per_api_key(client, upstream):
    body = chat("hello", model="gpt-4o-mini", temperature=0)
    for token in ("Bearer user-a", "Bearer user-a", "Bearer user-b"):
        r = client.post("/openai/v1/chat/completions", headers={"Authorization": token}, json=body)
    assert r.headers["x-cost-guard-cache"] == "miss"  # user-b doesn't get user-a's cached answer
    assert len(upstream.seen) == 2


def test_daily_budget_blocks_calls(client, upstream):
    client.app.state.budgets = {"search": 0.0006}
    try:
        h = {"X-Team": "search"}
        def call(text):
            return client.post("/openai/v1/chat/completions", headers=h,
                               json=chat(text, model="gpt-4o-mini", max_tokens=100))
        assert call("a").status_code == 200 and call("b").status_code == 200
        r3 = call("c")
        assert r3.status_code == 429
        assert "$0.0009" in r3.json()["detail"] and "$0.0006" in r3.json()["detail"]
        assert len(upstream.seen) == 2  # blocked call never reached the provider
        b = client.get("/stats").json()["budgets"]
        assert b == [{"team": "search", "budget_usd": 0.0006, "spent_today_usd": 0.0009}]
        # Other teams are unaffected
        assert client.post("/openai/v1/chat/completions", headers={"X-Team": "other"},
                           json=chat("d", model="gpt-4o-mini")).status_code == 200
    finally:
        client.app.state.budgets = {}


def test_dashboard_password(client, upstream, monkeypatch):
    monkeypatch.setenv("DASHBOARD_PASSWORD", "s3cret-pass")
    assert client.get("/").status_code == 401
    assert client.get("/stats").status_code == 401
    good = "Basic " + base64.b64encode(b"admin:s3cret-pass").decode()
    bad = "Basic " + base64.b64encode(b"admin:nope").decode()
    assert client.get("/stats", headers={"Authorization": bad}).status_code == 401
    assert client.get("/stats", headers={"Authorization": good}).status_code == 200
    assert client.get("/health").status_code == 200  # health stays open for Docker


def test_body_size_limit(client, upstream, monkeypatch):
    import app.main as m
    monkeypatch.setattr(m, "MAX_BODY_BYTES", 100)
    r = client.post("/openai/v1/chat/completions", json=chat("x" * 500, model="gpt-4o-mini"))
    assert r.status_code == 413


def test_non_object_body_rejected(client, upstream):
    r = client.post("/openai/v1/chat/completions", json=["not", "an", "object"])
    assert r.status_code == 400


def test_security_headers(client, upstream):
    r = client.get("/health")
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]


def test_docs_endpoints_disabled(client, upstream):
    assert client.get("/docs").status_code == 404
    assert client.get("/openapi.json").status_code == 404


def test_credential_fingerprint_is_salted_and_slow(monkeypatch, tmp_path):
    import hashlib
    import stat

    from app import security

    monkeypatch.setenv("DB_PATH", str(tmp_path / "x.db"))
    monkeypatch.setattr(security, "_salt", None)
    security._derive.cache_clear()
    a1 = security.credential_fingerprint({"Authorization": "Bearer sk-aaa"})
    a2 = security.credential_fingerprint({"Authorization": "Bearer sk-aaa"})
    b = security.credential_fingerprint({"x-api-key": "sk-bbb"})
    assert a1 == a2 and a1 != b
    assert security.credential_fingerprint({}) == "none"
    # Not a plain (unsalted) hash of the credential, and doesn't contain it
    assert a1 not in hashlib.sha256(b"Bearer sk-aaa").hexdigest()
    assert "sk-aaa" not in a1
    # The salt is stored next to the database, readable by the owner only, and reused
    salt_file = tmp_path / "fingerprint.salt"
    assert len(salt_file.read_bytes()) == 32
    assert stat.S_IMODE(salt_file.stat().st_mode) == 0o600
    monkeypatch.setattr(security, "_salt", None)
    security._derive.cache_clear()
    assert security.credential_fingerprint({"Authorization": "Bearer sk-aaa"}) == a1
    # A different deployment (different salt) gets a different fingerprint
    monkeypatch.setenv("DB_PATH", str(tmp_path / "other" / "x.db"))
    monkeypatch.setattr(security, "_salt", None)
    security._derive.cache_clear()
    assert security.credential_fingerprint({"Authorization": "Bearer sk-aaa"}) != a1
    monkeypatch.setattr(security, "_salt", None)
    security._derive.cache_clear()
