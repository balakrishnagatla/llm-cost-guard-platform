"""Tracing and metrics.

Every request gets a Trace: an ID plus the steps it went through (auth, route,
budget, cache, each upstream attempt) with timings. Traces are stored with the
call, shown on the dashboard, exported as Prometheus metrics and, when
OTEL_EXPORTER_OTLP_ENDPOINT is set, sent as OpenTelemetry spans (e.g. to Jaeger).
"""
import logging
import os
import time
import uuid
from dataclasses import dataclass, field

from prometheus_client import CollectorRegistry, Counter, Histogram
from prometheus_client.core import GaugeMetricFamily

log = logging.getLogger("cost_guard")


# ---------- per-request trace ----------

@dataclass
class Trace:
    request_id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])
    start_ns: int = field(default_factory=time.time_ns)
    _t0: float = field(default_factory=time.perf_counter)
    steps: list[dict] = field(default_factory=list)

    def now_ms(self) -> float:
        return (time.perf_counter() - self._t0) * 1000

    def add(self, name: str, detail: str, ok: bool = True, started_ms: float | None = None) -> None:
        """Record a step. Pass started_ms (from now_ms()) to time it; otherwise it's an instant."""
        end = self.now_ms()
        start = end if started_ms is None else started_ms
        self.steps.append({"step": name, "detail": detail, "ok": ok,
                           "at_ms": round(start, 2), "ms": round(end - start, 2)})

    @property
    def total_ms(self) -> float:
        return round(self.now_ms(), 2)


# ---------- Prometheus ----------

REGISTRY = CollectorRegistry(auto_describe=True)
LATENCY_BUCKETS = (0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 60, 120)

REQUESTS = Counter("costguard_requests", "LLM calls handled by the gateway",
                   ["team", "feature", "model", "upstream", "status", "cache"], registry=REGISTRY)
COST = Counter("costguard_cost_usd", "Spend in USD", ["team", "feature", "model"], registry=REGISTRY)
SAVED = Counter("costguard_saved_usd", "Spend avoided in USD", ["team", "kind"], registry=REGISTRY)
TOKENS = Counter("costguard_tokens", "Tokens processed", ["model", "kind"], registry=REGISTRY)
LATENCY = Histogram("costguard_request_duration_seconds", "End-to-end call latency",
                    ["upstream"], buckets=LATENCY_BUCKETS, registry=REGISTRY)
UPSTREAM_ERRORS = Counter("costguard_upstream_errors", "Failed upstream attempts (before any failover)",
                          ["upstream", "reason"], registry=REGISTRY)


class BudgetCollector:
    """Reads budgets and today's spend at scrape time."""

    def __init__(self, budgets_fn, spent_fn):
        self.budgets_fn, self.spent_fn = budgets_fn, spent_fn

    def collect(self):
        budget = GaugeMetricFamily("costguard_budget_usd", "Daily budget per team", labels=["team"])
        remaining = GaugeMetricFamily("costguard_budget_remaining_usd", "Budget left today", labels=["team"])
        spent = self.spent_fn()
        for team, amount in self.budgets_fn().items():
            budget.add_metric([team], amount)
            remaining.add_metric([team], max(amount - spent.get(team, 0.0), 0.0))
        yield budget
        yield remaining


_budget_collector: BudgetCollector | None = None


def register_budget_collector(budgets_fn, spent_fn) -> None:
    global _budget_collector
    if _budget_collector is None:
        _budget_collector = BudgetCollector(budgets_fn, spent_fn)
        REGISTRY.register(_budget_collector)
    else:
        _budget_collector.budgets_fn, _budget_collector.spent_fn = budgets_fn, spent_fn


def seed_counters(rows: list[dict]) -> None:
    """Start counters from the all-time totals in the database, so Grafana keeps its history across
    restarts (the counters keep growing instead of resetting to zero)."""
    for r in rows:
        upstream = r["upstream"]
        cache = "hit" if r["cache_hit"] else "miss"
        REQUESTS.labels(r["team"], r["feature"], r["model"], upstream, str(r["status"]), cache).inc(r["calls"])
        if r["cost_usd"]:
            COST.labels(r["team"], r["feature"], r["model"]).inc(r["cost_usd"])
        if r["saved_usd"]:
            SAVED.labels(r["team"], "cache").inc(r["saved_usd"])
        if r["routing_saved_usd"]:
            SAVED.labels(r["team"], "routing").inc(r["routing_saved_usd"])
        for kind in ("input", "output", "reasoning"):
            if r[f"{kind}_tokens"]:
                TOKENS.labels(r["model"], kind).inc(r[f"{kind}_tokens"])


def record_metrics(row: dict) -> None:
    upstream = row.get("upstream", "primary")
    cache = "hit" if row.get("cache_hit") else "miss"
    REQUESTS.labels(row["team"], row["feature"], row["model"], upstream, str(row["status"]), cache).inc()
    if row.get("cost_usd"):
        COST.labels(row["team"], row["feature"], row["model"]).inc(row["cost_usd"])
    if row.get("saved_usd"):
        SAVED.labels(row["team"], "cache").inc(row["saved_usd"])
    if row.get("routing_saved_usd"):
        SAVED.labels(row["team"], "routing").inc(row["routing_saved_usd"])
    for kind, key in (("input", "input_tokens"), ("output", "output_tokens"), ("reasoning", "reasoning_tokens")):
        if row.get(key):
            TOKENS.labels(row["model"], kind).inc(row[key])
    if upstream != "blocked":
        LATENCY.labels(upstream).observe(row.get("latency_ms", 0) / 1000)


# ---------- OpenTelemetry (optional) ----------

_tracer = None


def init_tracing(exporter=None) -> bool:
    """Enable OpenTelemetry export. Uses OTEL_EXPORTER_OTLP_ENDPOINT unless an exporter is given."""
    global _tracer
    if exporter is None and not os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT"):
        return False
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor

    from . import __version__

    provider = TracerProvider(resource=Resource.create({
        "service.name": os.getenv("OTEL_SERVICE_NAME", "llm-cost-guard"), "service.version": __version__}))
    if exporter is None:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
        log.info("OpenTelemetry export enabled to %s", os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT"))
    else:
        provider.add_span_processor(SimpleSpanProcessor(exporter))
    _tracer = provider.get_tracer("llm-cost-guard")
    return True


def emit_span(trace: Trace, row: dict) -> None:
    """Turn a finished Trace into one OpenTelemetry span with a child span per step."""
    if _tracer is None:
        return
    from opentelemetry.trace import Status, StatusCode, set_span_in_context

    status = int(row.get("status", 0))
    root = _tracer.start_span("llm.request", start_time=trace.start_ns, attributes={
        "costguard.request_id": trace.request_id,
        "costguard.team": row.get("team", ""),
        "costguard.feature": row.get("feature", ""),
        "costguard.upstream": row.get("upstream", ""),
        "costguard.cache_hit": bool(row.get("cache_hit")),
        "costguard.cost_usd": round(float(row.get("cost_usd", 0.0)), 8),
        "costguard.saved_usd": round(float(row.get("saved_usd", 0.0)) + float(row.get("routing_saved_usd", 0.0)), 8),
        "gen_ai.system": row.get("provider", ""),
        "gen_ai.request.model": row.get("requested_model", ""),
        "gen_ai.response.model": row.get("model", ""),
        "gen_ai.usage.input_tokens": int(row.get("input_tokens", 0)),
        "gen_ai.usage.output_tokens": int(row.get("output_tokens", 0)),
        "gen_ai.usage.reasoning_tokens": int(row.get("reasoning_tokens", 0)),
        "http.response.status_code": status,
    })
    ctx = set_span_in_context(root)
    for s in trace.steps:
        start = trace.start_ns + int(s["at_ms"] * 1e6)
        child = _tracer.start_span(s["step"], context=ctx, start_time=start,
                                   attributes={"costguard.detail": s["detail"]})
        if not s["ok"]:
            child.set_status(Status(StatusCode.ERROR, s["detail"]))
        child.end(end_time=start + int(s["ms"] * 1e6))
    if status >= 400:
        root.set_status(Status(StatusCode.ERROR, f"HTTP {status}"))
    root.end(end_time=trace.start_ns + int(trace.total_ms * 1e6))
