"""Model routing: send simple prompts to a cheap model, complex ones to a strong model.

Routing only happens when the caller sets "model": "auto", so existing apps are
never rerouted by surprise. Rules, per-tier request settings (such as reasoning
effort and output caps) and fallback providers live in routing.json.
"""
import json
import os
from dataclasses import dataclass, field
from functools import lru_cache

ROUTING_PATH = os.getenv("ROUTING_PATH", os.path.join(os.path.dirname(__file__), "..", "routing.json"))


@dataclass
class Route:
    model: str
    baseline: str | None = None  # strong model, used to estimate routing savings
    reason: str | None = None
    tier: str | None = None
    params: dict = field(default_factory=dict)  # tier defaults to add to the request


@lru_cache(maxsize=1)
def load_config() -> dict:
    with open(ROUTING_PATH) as f:
        return json.load(f)


def _text_of(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(p.get("text", "") for p in content if isinstance(p, dict))
    return ""


def prompt_text(body: dict) -> str:
    parts = [_text_of(body.get("system", ""))]
    parts += [_text_of(m.get("content", "")) for m in body.get("messages", []) if isinstance(m, dict)]
    return "\n".join(p for p in parts if p)


def last_user_text(body: dict) -> str:
    for m in reversed(body.get("messages", [])):
        if isinstance(m, dict) and m.get("role") == "user":
            return _text_of(m.get("content", ""))
    return ""


def decide(body: dict, cfg: dict) -> tuple[str, str]:
    """Return (tier, reason) where tier is 'cheap' or 'strong'."""
    if body.get("tools") or body.get("functions"):
        return "strong", "uses tools"
    if len(prompt_text(body)) > cfg.get("max_cheap_prompt_chars", 2000):
        return "strong", "long prompt"
    last = last_user_text(body).lower()
    for kw in cfg.get("strong_keywords", []):
        if kw.lower() in last:
            return "strong", f"keyword '{kw}'"
    return "cheap", "short, simple prompt"


def route(provider: str, body: dict) -> Route:
    requested = body.get("model", "unknown")
    if requested != "auto":
        return Route(model=requested)
    cfg = load_config()
    models = cfg.get("providers", {}).get(provider)
    if not models:
        raise ValueError(f"No routing config for provider '{provider}' in routing.json")
    tier, reason = decide(body, cfg)
    params = (models.get("params") or {}).get(tier, {})
    return Route(model=models[tier], baseline=models["strong"], reason=reason, tier=tier, params=dict(params))


def fallbacks_for(provider: str) -> list[dict]:
    """Backup upstreams tried, in order, when the primary is rate-limited or down."""
    return list(load_config().get("fallbacks", {}).get(provider, []))
