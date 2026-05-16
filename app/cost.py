from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from app.config import load_config
from app.daily import write_daily_section
from app.logger import EventLogger


@dataclass
class TokenStats:
    calls: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    total_tokens: int = 0
    estimated_calls: int = 0
    by_model: dict[str, int] = field(default_factory=dict)


@dataclass
class ImageGenerationStats:
    image_count: int = 0
    by_model: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class TokenUsageReportResult:
    path: Path
    report: str


def summarize_token_usage(
    logger: EventLogger | None = None,
    today: date | None = None,
) -> dict[str, TokenStats | ImageGenerationStats]:
    event_logger = logger or EventLogger()
    current_date = today or date.today()
    current_date_text = current_date.isoformat()
    current_month = current_date.strftime("%Y-%m")

    today_stats = TokenStats()
    month_stats = TokenStats()
    today_image_stats = ImageGenerationStats()
    month_image_stats = ImageGenerationStats()

    today_events = event_logger.read_events_for_date(current_date_text)
    month_events = event_logger.read_events_for_month(current_month)
    _add_llm_events(today_stats, today_events)
    _add_llm_events(month_stats, month_events)
    _add_image_generation_events(today_image_stats, today_events)
    _add_image_generation_events(month_image_stats, month_events)

    return {
        "today": today_stats,
        "month": month_stats,
        "today_images": today_image_stats,
        "month_images": month_image_stats,
    }


def format_token_report(stats: dict[str, TokenStats | ImageGenerationStats]) -> str:
    today_stats = _as_token_stats(stats["today"])
    month_stats = _as_token_stats(stats["month"])
    today_image_stats = _as_image_stats(stats["today_images"])
    month_image_stats = _as_image_stats(stats["month_images"])
    return "\n".join(
        [
            f"今日 LLM 调用：{today_stats.calls} 次",
            f"今日 token 数：{today_stats.total_tokens}",
            f"本月 LLM 调用：{month_stats.calls} 次",
            f"本月 token 数：{month_stats.total_tokens}",
            f"今日文生图图片数：{today_image_stats.image_count} 张",
            f"本月文生图图片数：{month_image_stats.image_count} 张",
        ]
    )


def append_token_usage_report(
    config: dict[str, Any] | None = None,
    logger: EventLogger | None = None,
    target_date: date | None = None,
) -> TokenUsageReportResult:
    cfg = config or load_config()
    current_date = target_date or date.today()
    event_logger = logger or EventLogger(config=cfg)
    stats = summarize_token_usage(event_logger, current_date)
    report = format_token_report(stats)
    path = write_daily_section("token 消耗统计", f"```text\n{report}\n```", cfg, current_date)
    return TokenUsageReportResult(path=path, report=report)


def _add_llm_events(stats: TokenStats, events: list[dict[str, Any]]) -> None:
    for event in events:
        if event.get("type") != "llm_call":
            continue
        detail = event.get("detail")
        if not isinstance(detail, dict):
            continue
        _add_event(stats, detail)


def _add_image_generation_events(stats: ImageGenerationStats, events: list[dict[str, Any]]) -> None:
    for event in events:
        if event.get("type") != "image_generation_usage":
            continue
        detail = event.get("detail")
        if not isinstance(detail, dict):
            continue
        image_count = int(detail.get("image_count") or 0)
        if image_count <= 0:
            continue
        model = str(detail.get("model") or "unknown")
        stats.image_count += image_count
        stats.by_model[model] = stats.by_model.get(model, 0) + image_count


def _add_event(stats: TokenStats, detail: dict[str, Any]) -> None:
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


def _as_token_stats(value: TokenStats | ImageGenerationStats) -> TokenStats:
    if not isinstance(value, TokenStats):
        raise TypeError("Expected TokenStats.")
    return value


def _as_image_stats(value: TokenStats | ImageGenerationStats) -> ImageGenerationStats:
    if not isinstance(value, ImageGenerationStats):
        raise TypeError("Expected ImageGenerationStats.")
    return value
