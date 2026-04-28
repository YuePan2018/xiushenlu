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
    daily_records = daily_text.strip() or "（今天还没有 daily 记录）"
    return f"""你是一个帮助用户复盘学习和工作的个人执行助手。

请根据 {date_text} 的 daily 记录，生成review。

要求：
- 重点分析学习工作的安排和工程经验，而不是每个目标的技术实现!
- 输出“完成了什么”“改进建议”“值得肯定的行为”三部分。
- daily记录的“##记录”标题下，没有的内容，说明没有完成。记录的内容，可能包含不是原定计划。
- 最后基于事实，给予一句话的表扬。

Daily 记录：
{daily_records}
"""


def _format_event(event: dict[str, Any]) -> str:
    return f"- {event.get('ts', '')} [{event.get('type', '')}] {event.get('summary', '')}"
