from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

from app.config import load_config
from app.logger import EventLogger


@dataclass
class TokenStats:
    calls: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    total_tokens: int = 0
    estimated_calls: int = 0
    by_model: dict[str, int] = field(default_factory=dict)
    cost: float | None = None


def summarize_token_usage(
    config: dict[str, Any] | None = None,
    logger: EventLogger | None = None,
    today: date | None = None,
) -> dict[str, TokenStats]:
    cfg = config or load_config()
    event_logger = logger or EventLogger()
    current_date = today or date.today()
    current_date_text = current_date.isoformat()
    current_month = current_date.strftime("%Y-%m")

    today_stats = TokenStats()
    month_stats = TokenStats()

    _add_llm_events(today_stats, event_logger.read_events_for_date(current_date_text), cfg)
    _add_llm_events(month_stats, event_logger.read_events_for_month(current_month), cfg)

    return {"today": today_stats, "month": month_stats}


def _add_llm_events(stats: TokenStats, events: list[dict[str, Any]], config: dict[str, Any]) -> None:
    for event in events:
        if event.get("type") != "llm_call":
            continue
        detail = event.get("detail")
        if not isinstance(detail, dict):
            continue
        _add_event(stats, detail, config)


def _add_event(stats: TokenStats, detail: dict[str, Any], config: dict[str, Any]) -> None:
    tokens_in = int(detail.get("tokens_in") or 0)
    tokens_out = int(detail.get("tokens_out") or 0)
    total_tokens = int(detail.get("total_tokens") or (tokens_in + tokens_out))
    model = str(detail.get("model") or "unknown")

    stats.calls += 1
    stats.tokens_in += tokens_in
    stats.tokens_out += tokens_out
    stats.total_tokens += total_tokens
    stats.by_model[model] = stats.by_model.get(model, 0) + total_tokens
    if detail.get("estimated"):
        stats.estimated_calls += 1

    event_cost = _estimate_cost(model, tokens_in, tokens_out, config)
    if event_cost is not None:
        stats.cost = (stats.cost or 0) + event_cost


def _estimate_cost(model: str, tokens_in: int, tokens_out: int, config: dict[str, Any]) -> float | None:
    model_prices = config.get("cost", {}).get("model_prices", {})
    price = model_prices.get(model)
    if not isinstance(price, dict):
        return None
    input_per_1k = price.get("input_per_1k")
    output_per_1k = price.get("output_per_1k")
    if input_per_1k is None or output_per_1k is None:
        return None
    return tokens_in / 1000 * float(input_per_1k) + tokens_out / 1000 * float(output_per_1k)

