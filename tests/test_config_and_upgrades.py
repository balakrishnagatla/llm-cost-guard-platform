"""Startup validation, database upgrades and the demo traffic plan."""
import sqlite3

import pytest
from fastapi.testclient import TestClient

from app import db
from app.main import app


@pytest.mark.parametrize("env, value", [
    ("COST_GUARD_KEYS", "search:short"),          # key too short
    ("COST_GUARD_KEYS", "no-colon-here-0123456789"),
    ("BUDGETS", "search:lots"),                   # not a number
])
def test_bad_config_fails_at_startup(monkeypatch, env, value):
    monkeypatch.setenv(env, value)
    with pytest.raises(ValueError), TestClient(app):
        pass


V01_SCHEMA = """
CREATE TABLE calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, provider TEXT NOT NULL, model TEXT NOT NULL,
    team TEXT NOT NULL, feature TEXT NOT NULL, input_tokens INTEGER NOT NULL, output_tokens INTEGER NOT NULL,
    cost_usd REAL NOT NULL, saved_usd REAL NOT NULL, latency_ms REAL NOT NULL, cache_hit INTEGER NOT NULL,
    status INTEGER NOT NULL, priced INTEGER NOT NULL
);
CREATE TABLE cache (key TEXT PRIMARY KEY, response TEXT NOT NULL, created_at REAL NOT NULL);
INSERT INTO calls (ts, provider, model, team, feature, input_tokens, output_tokens, cost_usd, saved_usd,
                   latency_ms, cache_hit, status, priced)
VALUES (1, 'openai', 'gpt-4o-mini', 'old-team', 'chat', 10, 20, 0.5, 0, 100, 0, 200, 1);
"""


def test_v01_database_upgrades_and_keeps_data(tmp_path, monkeypatch):
    conn = sqlite3.connect(tmp_path / "old.db", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(V01_SCHEMA)
    monkeypatch.setattr(db, "_conn", conn)

    db.init_db()
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(calls)")}
    assert {"requested_model", "routing_saved_usd", "streamed", "reasoning_tokens", "upstream",
            "request_id", "trace"} <= cols

    db.log_call(provider="openai", model="m", team="new-team", feature="f", input_tokens=1, output_tokens=1,
                cost_usd=0.25, saved_usd=0, latency_ms=1, cache_hit=0, status=200, priced=1, request_id="abc")
    totals = db.stats()["totals"]
    assert totals["calls"] == 2 and totals["cost_usd"] == 0.75
    assert db.get_call("abc")["team"] == "new-team"


def test_log_call_rejects_unknown_columns():
    with pytest.raises(ValueError):
        db.log_call(provider="x", model="m", team="t", feature="f", input_tokens=0, output_tokens=0, cost_usd=0,
                    saved_usd=0, latency_ms=0, cache_hit=0, status=200, priced=1, evil="1; DROP TABLE calls")


def test_demo_traffic_plan_is_varied_and_reproducible():
    from scripts.demo_traffic import plan

    calls = plan(80)
    assert calls == plan(80)  # same seed, same traffic
    assert len({c["team"] for c in calls}) >= 10
    assert any(c["stream"] for c in calls)
    prompts = [c["prompt"] for c in calls]
    assert len(set(prompts)) < len(prompts)  # some repeats, for cache hits
    assert any("step by step" in p or "Debug" in p for p in prompts)  # some strong-tier prompts
