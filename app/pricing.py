"""Model pricing lookup.

Prices live in pricing.json (USD per 1M tokens) so you can update them
without touching code. Model names are matched exactly first, then by the
longest matching prefix (so "gpt-4o-mini-2024-07-18" matches "gpt-4o-mini").
"""
import json
import os
from functools import lru_cache

PRICING_PATH = os.getenv("PRICING_PATH", os.path.join(os.path.dirname(__file__), "..", "pricing.json"))


@lru_cache(maxsize=1)
def load_prices() -> dict:
    with open(PRICING_PATH) as f:
        data = json.load(f)
    return {k: v for k, v in data.items() if not k.startswith("_")}


def find_price(model: str) -> dict | None:
    prices = load_prices()
    if model in prices:
        return prices[model]
    matches = [name for name in prices if model.startswith(name)]
    if matches:
        return prices[max(matches, key=len)]
    return None


def cost_usd(model: str, input_tokens: int, output_tokens: int) -> float:
    price = find_price(model)
    if price is None:
        return 0.0
    return (input_tokens * price["input_per_mtok"] + output_tokens * price["output_per_mtok"]) / 1_000_000


def is_priced(model: str) -> bool:
    return find_price(model) is not None


def estimate_tokens(text: str) -> int:
    """Rough token count (about 4 characters per token) for estimates before a call."""
    return max(1, len(text) // 4) if text else 0
