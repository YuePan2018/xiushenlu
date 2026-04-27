from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from app.config import load_config
from app.daily import daily_path, read_daily, write_daily_section
from app.llm.provider import LLMProvider
from app.llm.usage import append_llm_call_event
from app.logger import EventLogger


@dataclass(frozen=True)
class NightlyReviewResult:
    date: str
    path: Path
    review: str


def generate_nightly_review(
    provider: LLMProvider,
    config: dict[str, Any] | None = None,
    target_date: date | None = None,
    logger: EventLogger | None = None,
) -> NightlyReviewResult:
    cfg = config or load_config()
    current_date = target_date or date.today()
    date_text = current_date.isoformat()
    day_text = read_daily(cfg, date_text)
    event_logger = logger or EventLogger()
    events = _events_for_date(event_logger, date_text)
    prompt = _build_prompt(date_text, day_text, events)

    review = provider.chat(prompt).strip()
    path = write_daily_section("复盘", review, cfg, date_text, mode="replace")

    append_llm_call_event(event_logger, provider, "nightly_review")
    event_logger.append_event(
        "review_generated",
        f"生成 {date_text} 的复盘",
        {
            "date": date_text,
            "daily_path": str(daily_path(cfg, date_text)),
            "daily_chars": len(day_text),
            "events_count": len(events),
        },
    )

    return NightlyReviewResult(date=date_text, path=path, review=review)


def _events_for_date(logger: EventLogger, date_text: str) -> list[dict[str, Any]]:
    return logger.read_events_for_date(date_text)


def _build_prompt(date_text: str, daily_text: str, events: list[dict[str, Any]]) -> str:
    daily = daily_text.strip() or "（今天还没有 daily 记录）"
    event_lines = "\n".join(_format_event(event) for event in events) or "（今天还没有事件日志）"
    return f"""你是修身炉，一个帮助用户复盘学习和工作的个人执行助手。

请根据 {date_text} 的 daily 记录和事件日志，生成晚间复盘。

要求：
- 用中文输出。
- 必须基于事实记录，不要空泛表扬。
- 输出“完成了什么”“能力增长/值得肯定的行为”“卡点或风险”“明日建议”四部分。
- 如果记录很少，要先指出资料不足，再给出温和、具体的补记录建议。
- 不要使用 emoji。

Daily 记录：
{daily}

事件日志：
{event_lines}
"""


def _format_event(event: dict[str, Any]) -> str:
    return f"- {event.get('ts', '')} [{event.get('type', '')}] {event.get('summary', '')}"
