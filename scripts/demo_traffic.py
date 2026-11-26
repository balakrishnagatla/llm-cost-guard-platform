"""Fill the dashboard with realistic traffic.

  python scripts/demo_traffic.py               # fake mode: free, no API key, starts its own gateway on :8090
  python scripts/demo_traffic.py --mode real   # sends traffic to your running gateway (default :8080)

Fake mode runs a simulated provider (with realistic token counts, reasoning, a backup
provider and occasional rate limits) and a separate gateway with its own database, so
your real data is never touched. It covers several teams and features, cheap and strong
routing, cache hits, streaming, failover and a team that runs out of budget.
"""
import argparse
import json
import os
import random
import subprocess
import sys
import tempfile
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path

import httpx

try:
    from scripts.mock_provider import FakeProvider
except ImportError:  # run as a plain script
    from mock_provider import FakeProvider  # type: ignore[no-redef]

ROOT = Path(__file__).resolve().parent.parent

# (team, feature, prompts). Prompts with "analyze"/"debug"/"step by step" route to the strong model.
SCENARIOS = [
    ("support", "chat", ["How do I reset my password?", "Where can I download my invoice?",
                         "Can I change my billing date?", "How do I add a team member?"]),
    ("support", "classify", ["Classify this ticket: 'App crashes on login'",
                             "Classify this ticket: 'Refund request for order 1182'"]),
    ("search", "summarize", ["Summarize: quarterly revenue grew 12% driven by enterprise renewals.",
                             "Summarize: the outage lasted 14 minutes and affected EU customers."]),
    ("search", "rewrite", ["Rewrite as a search query: cheap flights to Lisbon in May"]),
    ("analytics", "analyze", ["Analyze churn drivers in this cohort step by step: 18% churn, mostly month 2.",
                              "Analyze the trade-offs between caching and fine-tuning step by step."]),
    ("engineering", "code", ["Debug this: TypeError: 'NoneType' object is not subscriptable in parse()",
                             "Explain why this SQL query is slow and suggest an index."]),
    ("marketing", "copy", ["Write a tagline for a budgeting app.", "Write a subject line for a spring sale.",
                           "Write a tweet about our new dashboard.", "Write a short product description for a mug."]),
    ("sales", "email", ["Draft a two-line follow-up email after a demo call.",
                        "Write a polite reminder about an unsigned quote."]),
    ("finance", "forecast", ["Analyze next quarter's cash flow step by step: revenue $1.2M, costs up 8%.",
                             "Explain why gross margin dropped 3 points this quarter."]),
    ("legal", "review", ["Analyze this clause for risks step by step: 'Either party may terminate at any time.'"]),
    ("hr", "policy", ["How many vacation days do new employees get?", "What is the parental leave policy?"]),
    ("product", "feedback", ["Summarize this feedback: 'Love the app, but exports are slow and search is confusing.'",
                             "Tag this feedback by theme: 'Pricing page is unclear.'"]),
    ("data", "sql", ["Debug this SQL: SELECT * FROM orders WHERE created_at > NOW() GROUP BY customer_id",
                     "Write a SQL query for monthly active users."]),
]
WEIGHTS = [5, 2, 3, 1, 2, 2, 3, 2, 2, 1, 2, 2, 2]  # how often each scenario appears
BUDGETS = "marketing:0.0004"  # small on purpose: the pre-call estimate starts refusing calls after a few


def start_fake_stack(port: int, provider_port: int) -> tuple[subprocess.Popen, str]:
    server = ThreadingHTTPServer(("127.0.0.1", provider_port), FakeProvider)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    tmp = Path(tempfile.mkdtemp(prefix="costguard-demo-"))
    routing = json.loads((ROOT / "routing.json").read_text())
    routing["fallbacks"] = {"openai": [{
        "name": "backup", "base_url": f"http://127.0.0.1:{provider_port}/backup/v1", "api_key_env": "DEMO_BACKUP_KEY",
        "models": {"*": "demo/backup-model"}, "drop_params": ["reasoning_effort"]}]}
    (tmp / "routing.json").write_text(json.dumps(routing))
    pricing = json.loads((ROOT / "pricing.json").read_text())
    pricing["demo/backup-model"] = {"input_per_mtok": 0.10, "output_per_mtok": 0.40}
    (tmp / "pricing.json").write_text(json.dumps(pricing))

    env = {**os.environ, "OPENAI_BASE_URL": f"http://127.0.0.1:{provider_port}/primary/v1",
           "OPENAI_API_KEY": "demo", "DEMO_BACKUP_KEY": "demo", "DB_PATH": str(tmp / "demo.db"),
           "ROUTING_PATH": str(tmp / "routing.json"), "PRICING_PATH": str(tmp / "pricing.json"),
           "BUDGETS": BUDGETS, "COST_GUARD_KEYS": "", "DASHBOARD_PASSWORD": ""}
    proc = subprocess.Popen(  # noqa: S603 (fixed command, our own app)
        [sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(port), "--log-level", "warning"],
        cwd=ROOT, env=env)
    url = f"http://127.0.0.1:{port}"
    for _ in range(50):
        try:
            if httpx.get(url + "/health", timeout=1).status_code == 200:
                return proc, url
        except httpx.HTTPError:
            time.sleep(0.2)
    proc.terminate()
    sys.exit("Demo gateway didn't start")


# ---------- traffic ----------

def plan(n: int, seed: int = 42) -> list[dict]:
    """A reproducible mix: ~20% repeats (cache hits), ~10% streaming, several teams."""
    rng = random.Random(seed)
    calls, history = [], []
    for _ in range(n):
        if history and rng.random() < 0.2:
            call = dict(rng.choice(history), stream=False)
        else:
            team, feature, prompts = rng.choices(SCENARIOS, weights=WEIGHTS)[0]
            # A ticket/order number makes each new prompt unique, so only true repeats hit the cache.
            prompt = f"{rng.choice(prompts)} (ref {rng.randint(1000, 9999)})"
            call = {"team": team, "feature": feature, "prompt": prompt, "stream": rng.random() < 0.1}
            if not call["stream"]:
                history.append(call)
        calls.append(call)
    return calls


def send(client: httpx.Client, url: str, call: dict) -> tuple[int, dict]:
    body = {"model": "auto", "temperature": 0, "messages": [{"role": "user", "content": call["prompt"]}]}
    headers = {"X-Team": call["team"], "X-Feature": call["feature"]}
    if call["stream"]:
        body["stream"] = True
        with client.stream("POST", url + "/openai/v1/chat/completions", json=body, headers=headers) as r:
            for _ in r.iter_bytes():
                pass
            return r.status_code, dict(r.headers)
    r = client.post(url + "/openai/v1/chat/completions", json=body, headers=headers)
    return r.status_code, dict(r.headers)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mode", choices=["fake", "real"], default="fake")
    ap.add_argument("--calls", type=int, help="number of calls (default 80 fake, 40 real)")
    ap.add_argument("--gateway", default="http://localhost:8080", help="real mode: your running gateway")
    ap.add_argument("--port", type=int, default=8090, help="fake mode: port for the demo gateway")
    ap.add_argument("--exit", action="store_true", help="fake mode: stop the demo gateway when done")
    args = ap.parse_args()

    proc = None
    if args.mode == "fake":
        proc, url = start_fake_stack(args.port, args.port + 1)
        n, pause = args.calls or 80, 0.0
    else:
        url, n, pause = args.gateway.rstrip("/"), args.calls or 40, 2.1  # stay under Groq's free 30 requests/min

    counts: dict[str, int] = {}
    with httpx.Client(timeout=120) as client:
        for i, call in enumerate(plan(n), 1):
            status, h = send(client, url, call)
            if status == 429 and h.get("x-cost-guard-upstream") is None:
                kind = "blocked by budget"
            elif status >= 400:
                kind = f"error {status}"
            elif h.get("x-cost-guard-cache") == "hit":
                kind = "cache hit"
            elif h.get("x-cost-guard-upstream") not in (None, "primary"):
                kind = "failover"
            else:
                kind = "strong model" if "120b" in h.get("x-cost-guard-model", "") else "cheap model"
            counts[kind] = counts.get(kind, 0) + 1
            print(f"[{i:>3}/{n}] {call['team']:<11} {call['feature']:<9} {kind}")
            time.sleep(pause)

    print("\nSummary:", ", ".join(f"{k}: {v}" for k, v in sorted(counts.items())))
    print(f"Dashboard: {url}")
    if proc:
        if args.exit:
            proc.terminate()
        else:
            print("Demo gateway is running. Press Ctrl+C to stop it.")
            try:
                proc.wait()
            except KeyboardInterrupt:
                proc.terminate()
    return 0


if __name__ == "__main__":
    sys.exit(main())
