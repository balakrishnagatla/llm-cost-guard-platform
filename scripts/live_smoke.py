"""End-to-end checks against a real provider through a running gateway.

Usage: start the gateway on localhost:8080, then run `python scripts/live_smoke.py`.
Prints a summary to stdout and to the GitHub Actions job summary when available.
"""
import base64
import json
import os
import sys
import urllib.request

BASE = os.getenv("GATEWAY_URL", "http://localhost:8080")
if not BASE.startswith(("http://", "https://")):
    sys.exit("GATEWAY_URL must be an http(s) URL")


def call(prompt: str, feature: str) -> tuple[dict, dict]:
    body = json.dumps({"model": "auto", "temperature": 0,
                       "messages": [{"role": "user", "content": prompt}]}).encode()
    req = urllib.request.Request(  # noqa: S310 (scheme checked above)
        f"{BASE}/openai/v1/chat/completions", data=body, method="POST",
        headers={"Content-Type": "application/json", "X-Team": "ci", "X-Feature": feature})
    with urllib.request.urlopen(req, timeout=120) as resp:  # noqa: S310 (scheme checked above)
        return dict(resp.headers), json.loads(resp.read())


def get(path: str) -> bytes:
    """GET a read endpoint, sending the dashboard login and metrics token when they're set."""
    headers = {}
    token = os.getenv("METRICS_TOKEN", "").strip()
    password = os.getenv("DASHBOARD_PASSWORD", "").strip()
    if path.startswith("/metrics") and token:
        headers["Authorization"] = f"Bearer {token}"
    elif password:
        login = f"{os.getenv('DASHBOARD_USER', 'admin')}:{password}"
        headers["Authorization"] = "Basic " + base64.b64encode(login.encode()).decode()
    req = urllib.request.Request(f"{BASE}{path}", headers=headers)  # noqa: S310 (scheme checked above)
    with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310
        return resp.read()


def main() -> int:
    failures = []
    calls_before = json.loads(get("/stats"))["totals"]["calls"]  # the database may hold earlier calls
    h1, b1 = call("Say hello in five words", "greeting")
    h2, _ = call("Say hello in five words", "greeting")
    h3, _ = call("Analyze the trade-offs of caching LLM responses step by step, briefly", "analysis")

    def check(name: str, ok: bool, detail: str) -> None:
        print(f"{'PASS' if ok else 'FAIL'}  {name}: {detail}")
        if not ok:
            failures.append(name)

    answer = str(b1.get("choices", [{}])[0].get("message", {}).get("content"))
    m1, m3 = h1.get("x-cost-guard-model", ""), h3.get("x-cost-guard-model", "")
    check("real answer", bool(b1.get("choices")), answer[:60])
    check("simple prompt -> cheap model", m1.endswith("20b"), m1)
    check("repeat -> cache hit", h2.get("x-cost-guard-cache") == "hit", h2.get("x-cost-guard-cache", ""))
    check("complex prompt -> strong model", m3.endswith("120b"), m3)

    # Reasoning control: a simple prompt on the cheap tier should stay short (it was ~227 output tokens
    # before low reasoning effort; ~63 after). A generous bound so normal variation doesn't flake.
    rid = h1.get("x-request-id", "")
    traced = json.loads(get(f"/calls/{rid}"))
    route = next((s["detail"] for s in traced["trace"] if s["step"] == "route"), "")
    check("trace recorded", len(traced["trace"]) >= 5, f"{len(traced['trace'])} steps")
    check("low reasoning effort applied", "reasoning_effort=low" in route, route)
    check("cheap answer stays short", traced["output_tokens"] < 200, f"{traced['output_tokens']} output tokens")

    sbody = json.dumps({"model": "auto", "stream": True,
                        "messages": [{"role": "user", "content": "Count to three."}]}).encode()
    sreq = urllib.request.Request(  # noqa: S310 (scheme checked above)
        f"{BASE}/openai/v1/chat/completions", data=sbody, method="POST",
        headers={"Content-Type": "application/json", "X-Team": "ci", "X-Feature": "stream"})
    with urllib.request.urlopen(sreq, timeout=120) as resp:  # noqa: S310
        stream_text = resp.read().decode()
        stream_rid = resp.headers.get("x-request-id", "")
    streamed = json.loads(get(f"/calls/{stream_rid}"))
    check("streaming relays chunks", stream_text.count("data:") > 2, f"{stream_text.count('data:')} chunks")
    check("streamed call priced", streamed["cost_usd"] > 0, f"${streamed['cost_usd']:.6f}")

    check("prometheus metrics", b"costguard_requests_total" in get("/metrics"), "/metrics")

    t = json.loads(get("/stats"))["totals"]
    check("calls logged", t["calls"] - calls_before == 4, f"{t['calls'] - calls_before} new")
    summary = (f"Spend ${t['cost_usd']:.6f} · saved by cache ${t['saved_usd']:.6f} · "
               f"saved by routing ${t['routing_saved_usd']:.6f} · output tokens {t['output_tokens']} "
               f"(reasoning {t['reasoning_tokens']})")
    print(summary)
    if os.getenv("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as f:
            status = "All checks passed" if not failures else "Failed: " + ", ".join(failures)
            f.write(f"### Live smoke test\n\n{status}\n\n{summary}\n")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
