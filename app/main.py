"""LLM Cost Guard: a gateway that measures what every LLM call costs and cuts it.

Point your app at this gateway instead of the provider:
  OpenAI-compatible:  POST http://localhost:8080/openai/v1/chat/completions
  Anthropic:          POST http://localhost:8080/anthropic/v1/messages
Set "model": "auto" to let the gateway pick a cheap or strong model.
"""
import codecs
import contextlib
import hashlib
import hmac
import json
import logging
import os
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response, StreamingResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from . import __version__, db
from .logging_setup import configure_logging
from .observability import (
    REGISTRY,
    UPSTREAM_ERRORS,
    Trace,
    emit_span,
    init_tracing,
    record_metrics,
    register_budget_collector,
    seed_counters,
)
from .pricing import cost_usd, estimate_tokens, is_priced, load_prices
from .router import Route, fallbacks_for, load_config, prompt_text, route
from .security import (
    clean_label,
    credential_fingerprint,
    parse_budgets,
    proxy_keys,
    require_dashboard_login,
    resolve_team,
)

configure_logging()
log = logging.getLogger("cost_guard")

def _env_int(name: str, default: int) -> int:
    """Read an integer setting; accepts forms like "1e+06" that some templating tools produce."""
    return int(float(os.getenv(name, str(default))))


OPENAI_BASE = os.getenv("OPENAI_BASE_URL", "https://api.groq.com/openai/v1").rstrip("/")
ANTHROPIC_BASE = os.getenv("ANTHROPIC_BASE_URL", "https://api.anthropic.com").rstrip("/")
# deterministic = cache only temperature 0 calls; all = cache everything; off = no cache
CACHE_MODE = os.getenv("CACHE_MODE", "deterministic").lower()
CACHE_TTL_SECONDS = float(os.getenv("CACHE_TTL_SECONDS", "86400"))
UPSTREAM_TIMEOUT = float(os.getenv("UPSTREAM_TIMEOUT", "120"))
MAX_BODY_BYTES = _env_int("MAX_BODY_BYTES", 1_000_000)
# Output tokens assumed for the pre-call budget check when the request sets no limit
DEFAULT_OUTPUT_ESTIMATE = _env_int("DEFAULT_OUTPUT_ESTIMATE", 1024)
# Upstream answers that make the gateway try the next fallback provider
RETRYABLE_STATUS = {429, 500, 502, 503, 504}


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    # Fail fast on bad config instead of at the first request.
    keys = proxy_keys()
    app.state.budgets = parse_budgets(os.getenv("BUDGETS", ""))
    if not keys:
        log.warning("COST_GUARD_KEYS is not set: gateway is in open dev mode. Keep it on localhost.")
    if not os.getenv("DASHBOARD_PASSWORD"):
        log.warning("DASHBOARD_PASSWORD is not set: dashboard and /stats are not password protected.")
    register_budget_collector(lambda: app.state.budgets, db.spent_today_by_team)
    if not getattr(app.state, "counters_seeded", False):
        seed_counters(db.totals_for_metrics())
        app.state.counters_seeded = True
    init_tracing()
    app.state.client = httpx.AsyncClient(timeout=UPSTREAM_TIMEOUT)
    yield
    await app.state.client.aclose()


app = FastAPI(title="LLM Cost Guard", version=__version__, lifespan=lifespan,
              docs_url=None, redoc_url=None, openapi_url=None)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
        "frame-ancestors 'none'")
    return response


# ---------- token usage ----------

@dataclass
class Usage:
    input: int = 0
    output: int = 0
    reasoning: int = 0  # part of output; reported by the provider or estimated from reasoning text
    reasoning_chars: int = 0


def _reasoning_text_tokens(chars: int) -> int:
    return estimate_tokens("x" * chars) if chars else 0


def _openai_usage(data: dict) -> Usage:
    u = data.get("usage") or {}
    usage = Usage(int(u.get("prompt_tokens", 0)), int(u.get("completion_tokens", 0)))
    details = u.get("completion_tokens_details") or {}
    usage.reasoning = int(details.get("reasoning_tokens") or u.get("reasoning_tokens") or 0)
    if not usage.reasoning:
        # Some providers (e.g. Groq) return the reasoning text but report 0 reasoning tokens.
        chars = sum(len((c.get("message") or {}).get("reasoning") or "") for c in data.get("choices", []))
        usage.reasoning = min(_reasoning_text_tokens(chars), usage.output)
    return usage


def _anthropic_usage(data: dict) -> Usage:
    u = data.get("usage") or {}
    usage = Usage(int(u.get("input_tokens", 0)), int(u.get("output_tokens", 0)))
    chars = sum(len(b.get("thinking", "")) for b in data.get("content", []) if b.get("type") == "thinking")
    usage.reasoning = min(_reasoning_text_tokens(chars), usage.output)
    return usage


def _openai_stream_event(event: dict, usage: Usage) -> None:
    for choice in event.get("choices") or []:
        usage.reasoning_chars += len((choice.get("delta") or {}).get("reasoning") or "")
    # Usage arrives in the last chunk (OpenAI with include_usage) or under x_groq.usage (Groq).
    u = event.get("usage") or (event.get("x_groq") or {}).get("usage")
    if u:
        usage.input = int(u.get("prompt_tokens", usage.input))
        usage.output = int(u.get("completion_tokens", usage.output))
        details = u.get("completion_tokens_details") or {}
        usage.reasoning = int(details.get("reasoning_tokens") or usage.reasoning)


def _anthropic_stream_event(event: dict, usage: Usage) -> None:
    kind = event.get("type")
    if kind == "message_start":
        u = (event.get("message") or {}).get("usage") or {}
        usage.input = int(u.get("input_tokens", 0))
        usage.output = int(u.get("output_tokens", 0))
    elif kind == "message_delta":
        usage.output = int((event.get("usage") or {}).get("output_tokens", usage.output))  # cumulative
    elif kind == "content_block_delta":
        usage.reasoning_chars += len((event.get("delta") or {}).get("thinking") or "")


@dataclass
class Provider:
    name: str
    usage: Callable[[dict], Usage]
    stream_event: Callable[[dict, Usage], None]


PROVIDERS = {
    "openai": Provider("openai", _openai_usage, _openai_stream_event),
    "anthropic": Provider("anthropic", _anthropic_usage, _anthropic_stream_event),
}


# ---------- helpers ----------

def _cacheable(body: dict) -> bool:
    if body.get("stream") or CACHE_MODE == "off":
        return False
    if CACHE_MODE == "all":
        return True
    return body.get("temperature") == 0


def _costs(model: str, baseline: str | None, usage: Usage) -> tuple[float, float]:
    cost = cost_usd(model, usage.input, usage.output)
    routing_saved = 0.0
    if baseline and baseline != model:
        # Estimate: what the same tokens would have cost on the strong model.
        routing_saved = max(cost_usd(baseline, usage.input, usage.output) - cost, 0.0)
    return cost, routing_saved


def estimate_call_cost(model: str, body: dict) -> float:
    """Upper-bound-ish cost of a call before it's sent, for the budget check."""
    in_t = estimate_tokens(prompt_text(body))
    out_t = int(body.get("max_completion_tokens") or body.get("max_tokens") or DEFAULT_OUTPUT_ESTIMATE)
    return cost_usd(model, in_t, out_t)


def _guard_headers(trace: Trace, rt: Route, cache: str, upstream: str, cost: float | None = None,
                   estimate: float | None = None) -> dict[str, str]:
    h = {"x-request-id": trace.request_id, "x-cost-guard-model": rt.model, "x-cost-guard-cache": cache,
         "x-cost-guard-upstream": upstream}
    if rt.reason:
        h["x-cost-guard-route-reason"] = rt.reason
    if cost is not None:
        h["x-cost-guard-cost-usd"] = f"{cost:.6f}"
    if estimate is not None:
        h["x-cost-guard-estimate-usd"] = f"{estimate:.6f}"
    return h


@dataclass
class Attempt:
    name: str
    url: str
    headers: dict
    body: dict


def _attempts(provider: str, url: str, headers: dict, body: dict) -> list[Attempt]:
    attempts = [Attempt("primary", url, headers, body)]
    for fb in fallbacks_for(provider):
        key = os.getenv(fb.get("api_key_env", ""), "")
        models = fb.get("models") or {}
        model = models.get(body.get("model")) or models.get("*")
        if not key or not model:
            continue  # not configured on this machine
        fb_body = {k: v for k, v in body.items() if k not in set(fb.get("drop_params", []))}
        fb_body["model"] = model
        attempts.append(Attempt(clean_label(fb.get("name"), "fallback"),
                                fb["base_url"].rstrip("/") + "/chat/completions",
                                {"Authorization": f"Bearer {key}"}, fb_body))
    return attempts


class UpstreamsFailed(Exception):
    pass


async def _send(client: httpx.AsyncClient, attempts: list[Attempt], stream: bool,
                trace: Trace) -> tuple[Attempt, httpx.Response]:
    """Try each upstream in order; move on when one is rate-limited, failing or unreachable."""
    last_error = "no upstream configured"
    for i, a in enumerate(attempts):
        is_last = i == len(attempts) - 1
        started = trace.now_ms()
        try:
            resp = await client.send(client.build_request("POST", a.url, json=a.body, headers=a.headers),
                                     stream=stream)
        except httpx.HTTPError as e:
            last_error = f"{a.name} unreachable: {type(e).__name__}"
            log.warning("Upstream %s unreachable: %s", a.name, e)
            UPSTREAM_ERRORS.labels(a.name, "unreachable").inc()
            trace.add("upstream", f"{a.name} ({a.body['model']}) unreachable", ok=False, started_ms=started)
            continue
        ok = resp.status_code < 400
        trace.add("upstream", f"{a.name} ({a.body['model']}) -> HTTP {resp.status_code}", ok=ok, started_ms=started)
        if resp.status_code in RETRYABLE_STATUS:
            UPSTREAM_ERRORS.labels(a.name, str(resp.status_code)).inc()
            if not is_last:
                log.warning("Upstream %s returned %s, trying next", a.name, resp.status_code)
                await resp.aclose()
                continue
        return a, resp
    raise UpstreamsFailed(last_error)


def _record(trace: Trace, **row) -> None:
    """Store a call with its trace, update metrics and export the span."""
    row["request_id"] = trace.request_id
    row["trace"] = json.dumps(trace.steps)
    db.log_call(**row)
    record_metrics(row)
    emit_span(trace, row)
    # One structured line per call: searchable by request_id in any log pipeline (no prompts or answers).
    log.info("llm call", extra={
        "request_id": trace.request_id, "team": row["team"], "feature": row["feature"], "model": row["model"],
        "upstream": row.get("upstream", "primary"), "status": row["status"], "cache_hit": bool(row.get("cache_hit")),
        "cost_usd": round(row.get("cost_usd", 0.0), 8), "input_tokens": row.get("input_tokens", 0),
        "output_tokens": row.get("output_tokens", 0), "latency_ms": round(row.get("latency_ms", 0.0), 1)})


async def _read_json(request: Request) -> dict:
    if int(request.headers.get("content-length") or 0) > MAX_BODY_BYTES:
        raise HTTPException(413, "Request body too large")
    raw = await request.body()
    if len(raw) > MAX_BODY_BYTES:
        raise HTTPException(413, "Request body too large")
    try:
        body = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise HTTPException(400, "Request body must be JSON")
    if not isinstance(body, dict):
        raise HTTPException(400, "Request body must be a JSON object")
    return body


# ---------- core ----------

def _usage_detail(u: Usage, cost: float) -> str:
    return f"{u.input} in / {u.output} out ({u.reasoning} reasoning) = ${cost:.6f}"


def _error(trace: Trace, status: int, detail: str) -> HTTPException:
    return HTTPException(status, detail, headers={"x-request-id": trace.request_id})


async def _proxy(request: Request, provider_name: str, url: str, headers: dict) -> Response:
    trace = Trace()
    t = trace.now_ms()
    team = resolve_team(request)  # 401 if a key is required and missing
    trace.add("auth", f"team {team}", started_ms=t)
    body = await _read_json(request)
    provider = PROVIDERS[provider_name]

    requested = body.get("model", "unknown")
    try:
        rt = route(provider_name, body)
    except ValueError as e:
        raise _error(trace, 400, str(e))
    body["model"] = rt.model
    added = {k: v for k, v in rt.params.items() if k not in body}
    body.update(added)  # tier defaults; the caller's own settings win
    if rt.tier:
        extras = ", ".join(f"{k}={v}" for k, v in added.items())
        trace.add("route", f"auto -> {rt.model} ({rt.tier}: {rt.reason})" + (f"; {extras}" if extras else ""))
    else:
        trace.add("route", f"{rt.model} (requested explicitly)")

    meta = {"provider": provider_name, "requested_model": requested, "team": team,
            "feature": clean_label(request.headers.get("x-feature"))}

    estimate = estimate_call_cost(rt.model, body)
    budget = request.app.state.budgets.get(team)
    if budget is not None:
        spent = db.spent_today(team)
        if spent + estimate > budget:
            detail = (f"Daily budget for team '{team}' would be exceeded (spent ${spent:.6g}, "
                      f"this call up to ${estimate:.6g}, budget ${budget:.6g}). Resets at 00:00 UTC.")
            trace.add("budget", f"blocked: ${spent:.6g} spent + ${estimate:.6g} est > ${budget:.6g}", ok=False)
            _record(trace, **meta, model=rt.model, priced=int(is_priced(rt.model)), input_tokens=0,
                    output_tokens=0, cost_usd=0.0, saved_usd=0.0, routing_saved_usd=0.0,
                    latency_ms=trace.total_ms, cache_hit=0, status=429, upstream="blocked")
            raise _error(trace, 429, detail)
        trace.add("budget", f"ok: ${spent:.6g} spent + ${estimate:.6g} est <= ${budget:.6g}")
    else:
        trace.add("estimate", f"up to ${estimate:.6g} (no budget set)")

    if body.get("stream"):
        return await _proxy_stream(request, provider, url, headers, body, rt, meta, trace, estimate)

    # Cache entries are scoped to the upstream credential so answers never leak across API keys.
    key = hashlib.sha256(
        (provider_name + credential_fingerprint(headers) + json.dumps(body, sort_keys=True)).encode()).hexdigest()
    cacheable = _cacheable(body)
    if cacheable:
        t = trace.now_ms()
        cached = db.get_cached(key, CACHE_TTL_SECONDS)
        if cached is not None:
            u = provider.usage(cached)
            saved = cost_usd(rt.model, u.input, u.output)
            trace.add("cache", f"hit: saved ${saved:.6f}", started_ms=t)
            trace.add("result", _usage_detail(u, 0.0))
            _record(trace, **meta, model=rt.model, priced=int(is_priced(rt.model)), input_tokens=u.input,
                    output_tokens=u.output, reasoning_tokens=u.reasoning, cost_usd=0.0, saved_usd=saved,
                    routing_saved_usd=0.0, latency_ms=trace.total_ms, cache_hit=1, status=200, upstream="cache")
            return JSONResponse(cached, headers=_guard_headers(trace, rt, "hit", "cache", 0.0, estimate))
        trace.add("cache", "miss", started_ms=t)
    else:
        trace.add("cache", "skipped (not deterministic)")

    try:
        attempt, resp = await _send(request.app.state.client, _attempts(provider_name, url, headers, body),
                                    False, trace)
    except UpstreamsFailed as e:
        _record(trace, **meta, model=rt.model, priced=int(is_priced(rt.model)), input_tokens=0, output_tokens=0,
                cost_usd=0.0, saved_usd=0.0, routing_saved_usd=0.0, latency_ms=trace.total_ms, cache_hit=0,
                status=502, upstream="none")
        raise _error(trace, 502, f"All upstreams failed: {e}")
    try:
        data = resp.json()
    except ValueError:
        data = {"error": resp.text}

    used_model = attempt.body["model"]
    usage, cost, routing_saved = Usage(), 0.0, 0.0
    if resp.status_code == 200:
        usage = provider.usage(data)
        # Routing savings compare cheap vs strong tier on the same provider; not meaningful after failover.
        cost, routing_saved = _costs(used_model, rt.baseline if attempt.name == "primary" else None, usage)
        if cacheable and attempt.name == "primary":
            db.put_cache(key, data)
        detail = _usage_detail(usage, cost)
        if routing_saved:
            detail += f"; routing saved ${routing_saved:.6f}"
        trace.add("result", detail)
    else:
        trace.add("result", f"HTTP {resp.status_code} from {attempt.name}", ok=False)

    _record(trace, **meta, model=used_model, priced=int(is_priced(used_model)), input_tokens=usage.input,
            output_tokens=usage.output, reasoning_tokens=usage.reasoning, cost_usd=cost, saved_usd=0.0,
            routing_saved_usd=routing_saved, latency_ms=trace.total_ms, cache_hit=0, status=resp.status_code,
            upstream=attempt.name)
    out_rt = Route(model=used_model, reason=rt.reason)
    return JSONResponse(data, status_code=resp.status_code,
                        headers=_guard_headers(trace, out_rt, "miss", attempt.name, cost, estimate))


async def _proxy_stream(request: Request, provider: Provider, url: str, headers: dict, body: dict,
                        rt: Route, meta: dict, trace: Trace, estimate: float) -> Response:
    if provider.name == "openai":
        # Ask for token usage in the final chunk so we can price the call.
        body.setdefault("stream_options", {"include_usage": True})
    trace.add("cache", "skipped (streaming)")

    try:
        attempt, resp = await _send(request.app.state.client, _attempts(provider.name, url, headers, body),
                                    True, trace)
    except UpstreamsFailed as e:
        _record(trace, **meta, model=rt.model, priced=int(is_priced(rt.model)), input_tokens=0, output_tokens=0,
                cost_usd=0.0, saved_usd=0.0, routing_saved_usd=0.0, latency_ms=trace.total_ms, cache_hit=0,
                status=502, upstream="none", streamed=1)
        raise _error(trace, 502, f"All upstreams failed: {e}")
    used_model = attempt.body["model"]
    log_base = dict(**meta, model=used_model, priced=int(is_priced(used_model)), upstream=attempt.name, streamed=1)

    if resp.status_code != 200:
        content = await resp.aread()
        await resp.aclose()
        trace.add("result", f"HTTP {resp.status_code} from {attempt.name}", ok=False)
        _record(trace, **log_base, input_tokens=0, output_tokens=0, cost_usd=0.0, saved_usd=0.0,
                routing_saved_usd=0.0, latency_ms=trace.total_ms, cache_hit=0, status=resp.status_code)
        return Response(content, status_code=resp.status_code, headers={"x-request-id": trace.request_id},
                        media_type=resp.headers.get("content-type", "application/json"))

    usage = Usage()

    async def relay() -> AsyncIterator[bytes]:
        decoder = codecs.getincrementaldecoder("utf-8")()
        buf = ""
        started = trace.now_ms()
        try:
            async for chunk in resp.aiter_bytes():
                yield chunk  # pass bytes through untouched
                buf += decoder.decode(chunk)
                while "\n" in buf:
                    line, buf = buf.split("\n", 1)
                    line = line.strip()
                    if line.startswith("data:"):
                        payload = line[5:].strip()
                        if payload and payload != "[DONE]":
                            with contextlib.suppress(json.JSONDecodeError, AttributeError, TypeError):
                                provider.stream_event(json.loads(payload), usage)
        finally:
            await resp.aclose()
            if not usage.reasoning:
                usage.reasoning = min(_reasoning_text_tokens(usage.reasoning_chars), usage.output)
            cost, routing_saved = _costs(used_model, rt.baseline if attempt.name == "primary" else None, usage)
            trace.add("stream", "relayed to caller", started_ms=started)
            trace.add("result", _usage_detail(usage, cost))
            _record(trace, **log_base, input_tokens=usage.input, output_tokens=usage.output,
                    reasoning_tokens=usage.reasoning, cost_usd=cost, saved_usd=0.0,
                    routing_saved_usd=routing_saved, latency_ms=trace.total_ms, cache_hit=0, status=200)

    out_rt = Route(model=used_model, reason=rt.reason)
    return StreamingResponse(relay(), media_type="text/event-stream",
                             headers=_guard_headers(trace, out_rt, "bypass", attempt.name, estimate=estimate))


# ---------- routes ----------

@app.post("/openai/v1/chat/completions")
async def openai_chat(request: Request):
    # Caller's own key wins; otherwise the server's key. Send no header at all when there's neither
    # (keyless upstreams such as a local Ollama or a test provider): "Bearer " alone is an invalid header.
    auth = request.headers.get("authorization") or (f"Bearer {key}" if (key := os.getenv("OPENAI_API_KEY")) else "")
    return await _proxy(request, "openai", f"{OPENAI_BASE}/chat/completions",
                        {"Authorization": auth} if auth else {})


@app.post("/anthropic/v1/messages")
async def anthropic_messages(request: Request):
    headers = {"anthropic-version": request.headers.get("anthropic-version") or "2023-06-01"}
    if api_key := request.headers.get("x-api-key") or os.environ.get("ANTHROPIC_API_KEY"):
        headers["x-api-key"] = api_key
    return await _proxy(request, "anthropic", f"{ANTHROPIC_BASE}/v1/messages", headers)


@app.get("/stats", dependencies=[Depends(require_dashboard_login)])
def get_stats(request: Request):
    s = db.stats()
    spent = db.spent_today_by_team()
    s["budgets"] = [
        {"team": t, "budget_usd": b, "spent_today_usd": round(spent.get(t, 0.0), 6)}
        for t, b in sorted(request.app.state.budgets.items())
    ]
    s["insights"] = db.insights()
    return s


@app.get("/calls", dependencies=[Depends(require_dashboard_login)])
def list_calls(limit: int = 50):
    return {"calls": db.recent_calls(limit)}


@app.get("/calls/{request_id}", dependencies=[Depends(require_dashboard_login)])
def get_call(request_id: str):
    call = db.get_call(request_id)
    if call is None:
        raise HTTPException(404, "No call with that request ID")
    return call


@app.get("/metrics", include_in_schema=False)
def metrics(request: Request):
    token = os.getenv("METRICS_TOKEN", "")
    if token:
        supplied = request.headers.get("authorization", "")
        if not hmac.compare_digest(supplied.encode(), f"Bearer {token}".encode()):
            raise HTTPException(401, "Missing or invalid metrics token")
    return PlainTextResponse(generate_latest(REGISTRY), media_type=CONTENT_TYPE_LATEST)


@app.get("/health")
def health():
    return {"ok": True, "version": __version__}


@app.get("/health/live")
def live():
    """Liveness: the process is up. Kubernetes restarts the pod if this fails."""
    return {"ok": True}


@app.get("/health/ready")
def ready():
    """Readiness: config loads and the database answers. Kubernetes stops sending traffic if this fails."""
    problems = []
    try:
        db.ping()
    except Exception as e:  # noqa: BLE001 (report any failure as not ready)
        problems.append(f"database: {type(e).__name__}")
    try:
        load_prices()
        load_config()
    except Exception as e:  # noqa: BLE001
        problems.append(f"config: {type(e).__name__}")
    if problems:
        return JSONResponse({"ok": False, "problems": problems}, status_code=503)
    return {"ok": True, "version": __version__}


@app.get("/", response_class=HTMLResponse, dependencies=[Depends(require_dashboard_login)])
@app.get("/dashboard", response_class=HTMLResponse, dependencies=[Depends(require_dashboard_login)])
def dashboard():
    return (Path(__file__).parent / "dashboard.html").read_text()
