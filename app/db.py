"""SQLite storage for call logs and the response cache."""
import json
import os
import sqlite3
import threading
import time

DB_PATH = os.getenv("DB_PATH", "data/costguard.db")
_lock = threading.Lock()
_conn = None


def _db() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
        _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
    return _conn


def init_db() -> None:
    with _lock:
        _db().executescript(
            """
            CREATE TABLE IF NOT EXISTS calls (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL NOT NULL,
                provider TEXT NOT NULL,
                model TEXT NOT NULL,
                team TEXT NOT NULL,
                feature TEXT NOT NULL,
                input_tokens INTEGER NOT NULL,
                output_tokens INTEGER NOT NULL,
                cost_usd REAL NOT NULL,
                saved_usd REAL NOT NULL,
                latency_ms REAL NOT NULL,
                cache_hit INTEGER NOT NULL,
                status INTEGER NOT NULL,
                priced INTEGER NOT NULL,
                requested_model TEXT NOT NULL DEFAULT '',
                routing_saved_usd REAL NOT NULL DEFAULT 0,
                streamed INTEGER NOT NULL DEFAULT 0,
                reasoning_tokens INTEGER NOT NULL DEFAULT 0,
                upstream TEXT NOT NULL DEFAULT 'primary',
                request_id TEXT NOT NULL DEFAULT '',
                trace TEXT NOT NULL DEFAULT '[]'
            );
            CREATE TABLE IF NOT EXISTS cache (
                key TEXT PRIMARY KEY,
                response TEXT NOT NULL,
                created_at REAL NOT NULL
            );
            """
        )
        # Upgrade databases created by v0.1
        existing = {r["name"] for r in _db().execute("PRAGMA table_info(calls)")}
        for col, ddl in [
            ("requested_model", "TEXT NOT NULL DEFAULT ''"),
            ("routing_saved_usd", "REAL NOT NULL DEFAULT 0"),
            ("streamed", "INTEGER NOT NULL DEFAULT 0"),
            ("reasoning_tokens", "INTEGER NOT NULL DEFAULT 0"),
            ("upstream", "TEXT NOT NULL DEFAULT 'primary'"),
            ("request_id", "TEXT NOT NULL DEFAULT ''"),
            ("trace", "TEXT NOT NULL DEFAULT '[]'"),
        ]:
            if col not in existing:
                _db().execute(f"ALTER TABLE calls ADD COLUMN {col} {ddl}")
        _db().execute("CREATE INDEX IF NOT EXISTS idx_calls_team_ts ON calls (team, ts)")
        _db().execute("CREATE INDEX IF NOT EXISTS idx_calls_request_id ON calls (request_id)")
        _db().commit()


def _start_of_today_utc() -> float:
    now = time.time()
    return now - (now % 86400)


def spent_today(team: str) -> float:
    with _lock:
        r = _db().execute("SELECT COALESCE(SUM(cost_usd), 0) AS s FROM calls WHERE team = ? AND ts >= ?",
                          (team, _start_of_today_utc())).fetchone()
    return float(r["s"])


def spent_today_by_team() -> dict:
    with _lock:
        rows = _db().execute("SELECT team, SUM(cost_usd) AS s FROM calls WHERE ts >= ? GROUP BY team",
                             (_start_of_today_utc(),)).fetchall()
    return {r["team"]: float(r["s"]) for r in rows}


CALL_COLUMNS = frozenset({
    "ts", "provider", "model", "team", "feature", "input_tokens", "output_tokens", "cost_usd",
    "saved_usd", "latency_ms", "cache_hit", "status", "priced", "requested_model",
    "routing_saved_usd", "streamed", "reasoning_tokens", "upstream", "request_id", "trace",
})
GROUP_COLUMNS = frozenset({"team", "feature", "model"})


def ping() -> None:
    """Cheap query used by the readiness probe."""
    with _lock:
        _db().execute("SELECT 1").fetchone()


def log_call(**row) -> None:
    row.setdefault("ts", time.time())
    unknown = set(row) - CALL_COLUMNS
    if unknown:
        raise ValueError(f"Unknown columns: {sorted(unknown)}")
    cols = ", ".join(row)  # only names from CALL_COLUMNS; values are bound parameters
    marks = ", ".join("?" for _ in row)
    with _lock:
        _db().execute(f"INSERT INTO calls ({cols}) VALUES ({marks})", tuple(row.values()))  # nosec B608
        _db().commit()


def get_cached(key: str, ttl_seconds: float):
    with _lock:
        r = _db().execute("SELECT response, created_at FROM cache WHERE key = ?", (key,)).fetchone()
    if r is None or (ttl_seconds > 0 and time.time() - r["created_at"] > ttl_seconds):
        return None
    return json.loads(r["response"])


def put_cache(key: str, response: dict) -> None:
    with _lock:
        _db().execute(
            "INSERT OR REPLACE INTO cache (key, response, created_at) VALUES (?, ?, ?)",
            (key, json.dumps(response), time.time()),
        )
        _db().commit()


def stats() -> dict:
    def group(col: str):
        # col is checked against an allowlist, so it is safe to put in the query
        if col not in GROUP_COLUMNS:
            raise ValueError(f"Can't group by {col}")
        with _lock:
            rows = _db().execute(
                f"""SELECT {col} AS name, COUNT(*) AS calls,
                           SUM(input_tokens) AS input_tokens, SUM(output_tokens) AS output_tokens,
                           SUM(reasoning_tokens) AS reasoning_tokens,
                           ROUND(SUM(cost_usd), 6) AS cost_usd, ROUND(SUM(saved_usd), 6) AS saved_usd,
                           ROUND(SUM(routing_saved_usd), 6) AS routing_saved_usd
                    FROM calls GROUP BY {col} ORDER BY cost_usd DESC"""  # nosec B608
            ).fetchall()
        return [dict(r) for r in rows]

    with _lock:
        t = _db().execute(
            """SELECT COUNT(*) AS calls,
                      COALESCE(SUM(cache_hit), 0) AS cache_hits,
                      ROUND(COALESCE(SUM(cost_usd), 0), 6) AS cost_usd,
                      ROUND(COALESCE(SUM(saved_usd), 0), 6) AS saved_usd,
                      ROUND(COALESCE(SUM(routing_saved_usd), 0), 6) AS routing_saved_usd,
                      COALESCE(SUM(CASE WHEN requested_model = 'auto' THEN 1 ELSE 0 END), 0) AS routed_calls,
                      COALESCE(SUM(streamed), 0) AS streamed_calls,
                      COALESCE(SUM(output_tokens), 0) AS output_tokens,
                      COALESCE(SUM(reasoning_tokens), 0) AS reasoning_tokens,
                      COALESCE(SUM(CASE WHEN upstream NOT IN ('primary', 'cache', 'blocked', 'none')
                                        THEN 1 ELSE 0 END), 0) AS fallback_calls,
                      ROUND(COALESCE(AVG(latency_ms), 0), 1) AS avg_latency_ms,
                      COALESCE(SUM(CASE WHEN priced = 0 THEN 1 ELSE 0 END), 0) AS unpriced_calls
               FROM calls"""
        ).fetchone()
    return {"totals": dict(t), "by_team": group("team"), "by_feature": group("feature"), "by_model": group("model")}


# ---------- tracing and insights ----------

_SUMMARY_COLS = ("ts, request_id, team, feature, model, requested_model, upstream, status, cache_hit, streamed, "
                 "input_tokens, output_tokens, reasoning_tokens, cost_usd, saved_usd, routing_saved_usd, latency_ms")


def recent_calls(limit: int = 50) -> list[dict]:
    with _lock:
        rows = _db().execute(f"SELECT {_SUMMARY_COLS} FROM calls ORDER BY id DESC LIMIT ?",  # nosec B608
                             (max(1, min(limit, 500)),)).fetchall()
    return [dict(r) for r in rows]


def get_call(request_id: str) -> dict | None:
    with _lock:
        r = _db().execute(f"SELECT {_SUMMARY_COLS}, trace FROM calls WHERE request_id = ?",  # nosec B608
                          (request_id,)).fetchone()
    if r is None:
        return None
    row = dict(r)
    row["trace"] = json.loads(row["trace"] or "[]")
    return row


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    values = sorted(values)
    k = max(0, min(len(values) - 1, round(pct / 100 * (len(values) - 1))))
    return round(values[k], 1)


def insights(hours: int = 24) -> dict:
    """Derived numbers for the dashboard: savings rate, top calls, trends, latency."""
    since = time.time() - hours * 3600
    with _lock:
        rows = [dict(r) for r in _db().execute(
            "SELECT ts, request_id, team, feature, model, upstream, status, cost_usd, saved_usd, routing_saved_usd, "
            "input_tokens, output_tokens, reasoning_tokens, latency_ms, cache_hit, requested_model "
            "FROM calls WHERE ts >= ?", (since,)).fetchall()]

    served = [r for r in rows if r["upstream"] != "blocked"]
    spend = sum(r["cost_usd"] for r in served)
    saved = sum(r["saved_usd"] + r["routing_saved_usd"] for r in served)

    by_feature: dict[str, list[float]] = {}
    by_model_tokens: dict[str, list[int]] = {}
    latency: dict[str, list[float]] = {}
    for r in served:
        if r["status"] == 200 and not r["cache_hit"]:
            by_feature.setdefault(r["feature"], []).append(r["cost_usd"])
            t = by_model_tokens.setdefault(r["model"], [0, 0])
            t[0] += r["output_tokens"]
            t[1] += r["reasoning_tokens"]
            latency.setdefault(r["upstream"], []).append(r["latency_ms"])

    timeline = _timeline(served, now=time.time(), hours=hours)
    routed = [r for r in served if r["requested_model"] == "auto" and r["status"] == 200 and not r["cache_hit"]]
    strong = sum(1 for r in routed if _is_strong(r["model"]))
    tokens = sum(r["input_tokens"] + r["output_tokens"] for r in served if not r["cache_hit"])
    cache_by_feature: dict[str, list[int]] = {}
    for r in served:
        c = cache_by_feature.setdefault(r["feature"], [0, 0])
        c[0] += 1
        c[1] += r["cache_hit"]
    first_ts = min((r["ts"] for r in served), default=None)
    elapsed_h = max((time.time() - first_ts) / 3600, 1.0) if first_ts else 1.0

    top = sorted((r for r in served if r["cost_usd"] > 0), key=lambda r: r["cost_usd"], reverse=True)[:5]
    return {
        "window_hours": hours,
        "savings_rate": round(saved / (spend + saved), 4) if spend + saved else 0.0,
        "blocked_calls": sum(1 for r in rows if r["upstream"] == "blocked"),
        "error_calls": sum(1 for r in served if r["status"] >= 400),
        "top_calls": [{k: r[k] for k in ("request_id", "team", "feature", "model", "cost_usd", "output_tokens")}
                      for r in top],
        "cost_per_call_by_feature": sorted(
            ({"feature": f, "calls": len(v), "avg_cost_usd": round(sum(v) / len(v), 8)} for f, v in by_feature.items()),
            key=lambda x: x["avg_cost_usd"], reverse=True),
        "reasoning_share_by_model": sorted(
            ({"model": m, "output_tokens": o, "reasoning_tokens": rt, "share": round(rt / o, 4) if o else 0.0}
             for m, (o, rt) in by_model_tokens.items()),
            key=lambda x: x["share"], reverse=True),
        "latency_by_upstream": [
            {"upstream": u, "calls": len(v), "p50_ms": _percentile(v, 50), "p95_ms": _percentile(v, 95)}
            for u, v in sorted(latency.items())],
        "timeline": timeline,
        # Run-rate: spend so far in the window, scaled to 30 days (at least one hour of data assumed)
        "projected_monthly_usd": round(spend / elapsed_h * 24 * 30, 6),
        "strong_model_share": round(strong / len(routed), 4) if routed else 0.0,
        "cost_per_1k_tokens_usd": round(spend / tokens * 1000, 8) if tokens else 0.0,
        "cache_hit_rate_by_feature": sorted(
            ({"feature": f, "calls": n, "hit_rate": round(h / n, 4)} for f, (n, h) in cache_by_feature.items()),
            key=lambda x: x["hit_rate"], reverse=True),
    }


def _is_strong(model: str) -> bool:
    """Is this the strong tier of any provider in routing.json?"""
    from .router import load_config  # local import: router has no db dependency
    return any(model == p.get("strong") for p in load_config().get("providers", {}).values())


def _timeline(rows: list[dict], now: float, hours: int) -> dict:
    """Spend and savings in evenly spaced buckets. The bucket size adapts to how much history exists,
    so a fresh install shows 5-minute detail instead of one giant hourly bar."""
    first = min((r["ts"] for r in rows), default=now)
    span = now - first
    # (history up to, bucket size, window shown): use the first that fits
    scales = ((900, 30, 900), (3600, 60, 3600), (3 * 3600, 300, 3 * 3600),
              (12 * 3600, 900, 12 * 3600), (float("inf"), 3600, hours * 3600))
    bucket, window = next((b, w) for limit, b, w in scales if span <= limit)
    start = int((now - window) // bucket * bucket) + bucket
    buckets = {t: {"t": t, "calls": 0, "cost_usd": 0.0, "saved_usd": 0.0}
               for t in range(start, int(now) + 1, bucket)}
    for r in rows:
        t = int(r["ts"] // bucket * bucket)
        if t in buckets:
            b = buckets[t]
            b["calls"] += 1
            b["cost_usd"] += r["cost_usd"]
            b["saved_usd"] += r["saved_usd"] + r["routing_saved_usd"]
    return {"bucket_seconds": bucket, "points": [
        {**b, "cost_usd": round(b["cost_usd"], 8), "saved_usd": round(b["saved_usd"], 8)}
        for _, b in sorted(buckets.items())]}


def totals_for_metrics() -> list[dict]:
    """All-time aggregates used to seed Prometheus counters at startup, so they survive restarts."""
    with _lock:
        rows = _db().execute(
            """SELECT team, feature, model, upstream, status, cache_hit, COUNT(*) AS calls,
                      SUM(cost_usd) AS cost_usd, SUM(saved_usd) AS saved_usd,
                      SUM(routing_saved_usd) AS routing_saved_usd,
                      SUM(input_tokens) AS input_tokens, SUM(output_tokens) AS output_tokens,
                      SUM(reasoning_tokens) AS reasoning_tokens
               FROM calls GROUP BY team, feature, model, upstream, status, cache_hit"""
        ).fetchall()
    return [dict(r) for r in rows]
