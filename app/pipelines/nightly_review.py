from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Callable

from app.config import load_config
from app.cost import append_token_usage_report
from app.daily import daily_path, read_daily, write_daily_section
from app.inbox import (
    clear_tomorrow_plan,
    has_leading_markdown_heading,
    read_today_tasks,
    read_tomorrow_plan,
    remove_added_today_tasks_heading,
    write_today_tasks,
)
from app.llm.provider import LLMProvider
from app.llm.usage import append_llm_call_event
from app.logger import EventLogger


REQUIRED_ROLLOVER_KEYS = ("review", "next_today_tasks")


class NightlyReviewParseError(ValueError):
    """Raised when the rollover review response is not the strict JSON shape."""


@dataclass(frozen=True)
class NightlyReviewResult:
    date: str
    path: Path
    review: str
    today_tasks_path: Path | None = None
    tomorrow_plan_path: Path | None = None
    next_today_tasks: str | None = None
    rolled_over: bool = False


@dataclass(frozen=True)
class ParsedNightlyReview:
    review: str
    next_today_tasks: str


def generate_nightly_review(
    provider: LLMProvider,
    config: dict[str, Any] | None = None,
    target_date: date | None = None,
    logger: EventLogger | None = None,
    cancel_check: Callable[[], None] | None = None,
) -> NightlyReviewResult:
    cfg = config or load_config()
    today = date.today()
    current_date = target_date or today
    date_text = current_date.isoformat()
    day_text = read_daily(cfg, date_text)
    event_logger = logger or EventLogger(config=cfg)
    events = _events_for_date(event_logger, date_text)
    rolled_over = current_date == today

    if rolled_over:
        today_tasks = read_today_tasks(cfg)
        tomorrow_plan = read_tomorrow_plan(cfg)
        prompt = _build_rollover_prompt(
            date_text=date_text,
            daily_text=day_text,
            events=events,
            today_tasks=today_tasks,
            tomorrow_plan=tomorrow_plan,
        )
        raw_reply = provider.chat(prompt).strip()
        if cancel_check is not None:
            cancel_check()
        append_llm_call_event(event_logger, provider, "nightly_review")
        parsed = parse_nightly_review_response(raw_reply)
        review = parsed.review
        next_today_tasks = remove_added_today_tasks_heading(
            parsed.next_today_tasks,
            preserve_heading=has_leading_markdown_heading(today_tasks),
        )

        path = write_daily_section("复盘", review, cfg, date_text, mode="replace")
        next_tasks_path = write_today_tasks(next_today_tasks, cfg)
        next_tomorrow_plan_path = clear_tomorrow_plan(cfg)
        append_token_usage_report(cfg, event_logger, current_date)
    else:
        prompt = _build_prompt(date_text, day_text, events)
        review = provider.chat(prompt).strip()
        if cancel_check is not None:
            cancel_check()
        append_llm_call_event(event_logger, provider, "nightly_review")

        path = write_daily_section("复盘", review, cfg, date_text, mode="replace")
        next_today_tasks = None
        next_tasks_path = None
        next_tomorrow_plan_path = None

    event_logger.append_event(
        "review_generated",
        f"生成 {date_text} 的复盘",
        {
            "date": date_text,
            "daily_path": str(daily_path(cfg, date_text)),
            "daily_chars": len(day_text),
            "events_count": len(events),
            "rolled_over": rolled_over,
            "today_tasks_path": str(next_tasks_path) if next_tasks_path else None,
            "tomorrow_plan_path": str(next_tomorrow_plan_path) if next_tomorrow_plan_path else None,
        },
    )

    return NightlyReviewResult(
        date=date_text,
        path=path,
        review=review,
        today_tasks_path=next_tasks_path,
        tomorrow_plan_path=next_tomorrow_plan_path,
        next_today_tasks=next_today_tasks,
        rolled_over=rolled_over,
    )


def parse_nightly_review_response(text: str) -> ParsedNightlyReview:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise NightlyReviewParseError(f"LLM response is not valid JSON: {exc}") from exc

    if not isinstance(data, dict):
        raise NightlyReviewParseError("LLM response must be a JSON object.")

    missing = [key for key in REQUIRED_ROLLOVER_KEYS if key not in data]
    if missing:
        raise NightlyReviewParseError(f"LLM response is missing keys: {', '.join(missing)}")

    values: dict[str, str] = {}
    for key in REQUIRED_ROLLOVER_KEYS:
        value = data[key]
        if not isinstance(value, str) or not value.strip():
            raise NightlyReviewParseError(f"LLM response key must be a non-empty string: {key}")
        values[key] = value.strip()

    return ParsedNightlyReview(review=values["review"], next_today_tasks=values["next_today_tasks"])


def _events_for_date(logger: EventLogger, date_text: str) -> list[dict[str, Any]]:
    return logger.read_events_for_date(date_text)


def _build_prompt(date_text: str, daily_text: str, events: list[dict[str, Any]]) -> str:
    daily_records = daily_text.strip() or "（今天还没有 daily 记录）"
    event_records = _format_events(events)
    return f"""你是一个帮助用户复盘学习和工作的个人执行助手。

请根据 {date_text} 的 daily 记录，生成review。

要求：
- 重点分析学习工作的安排和工程经验，而不是每个目标的技术实现!
- 输出“完成了什么”“改进建议”“值得肯定的行为”三部分。
- daily记录的“##记录”标题下，没有的内容，说明没有完成。记录的内容，可能包含不是原定计划。
- 最后基于事实，给予一句话的表扬。

“记录”的标签：
“临时：”代表临时插入的任务，不在当天任务计划内
“经验：”代表学习和工作中产生的心得，需要在review中总结归纳。

Daily 记录：
{daily_records}

事件日志：
{event_records}
"""


def _build_rollover_prompt(
    date_text: str,
    daily_text: str,
    events: list[dict[str, Any]],
    today_tasks: str,
    tomorrow_plan: str,
) -> str:
    daily_records = daily_text.strip() or "（今天还没有 daily 记录）"
    event_records = _format_events(events)
    today_tasks_text = today_tasks.strip() or "（当前 today_tasks.md 为空）"
    tomorrow_plan_text = tomorrow_plan.strip() or "（明日计划.md 为空）"
    return f"""你是一个帮助用户复盘学习和工作的个人执行助手。

请根据 {date_text} 的 daily 记录生成晚间复盘，并为明天滚动生成完整的 today_tasks.md 内容。

你必须只输出一个严格 JSON 对象，不要使用代码块，不要输出解释文字。
JSON 必须包含且只需要包含这些字符串字段：
- review：晚间复盘正文。输出“完成了什么”“改进建议”“值得肯定的行为”三部分；重点分析学习工作的安排和工程经验；最后基于事实给一句话表扬。
- next_today_tasks：新的完整 today_tasks.md 内容。保持当前待办文件的风格，包括一级标题是否存在、口号、中文小标题、列表/编号习惯；如果当前 today_tasks.md 没有一级标题，不要新增“# 今日待办”；根据 daily 的计划与记录判断今天未完成任务，优先按原小标题归类；再叠加“明日计划.md”的内容；去掉明显重复项。

判断未完成任务的规则：
- 记录中没有出现完成证据的计划任务，视为未完成。
- 只记录了开始、研究、优化中但没有明确完成的任务，视为未完成。
- 临时任务不要自动滚入明天，除非它在记录中仍明显待完成。
- 如果今天没有未完成任务且明日计划也为空，next_today_tasks 仍输出完整 today_tasks.md，至少保留原有标题/无标题状态和口号风格。

当前 today_tasks.md：
{today_tasks_text}

明日计划.md：
{tomorrow_plan_text}

Daily 记录：
{daily_records}

事件日志：
{event_records}
"""


def _format_event(event: dict[str, Any]) -> str:
    return f"- {event.get('ts', '')} [{event.get('type', '')}] {event.get('summary', '')}"


def _format_events(events: list[dict[str, Any]]) -> str:
    if not events:
        return "（当天没有事件日志）"
    return "\n".join(_format_event(event) for event in events)
