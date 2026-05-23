from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Callable

from app.config import load_config
from app.cost import append_token_usage_report
from app.daily import daily_path, read_daily, write_daily_section
from app.inbox import (
    clear_tomorrow_plan,
    read_tomorrow_plan,
    write_today_tasks,
)
from app.llm.provider import LLMProvider
from app.llm.usage import append_llm_call_event
from app.logger import EventLogger
from app.pipelines.today_tasks_format import (
    EMPTY_TODAY_TASKS_PLACEHOLDER,
    format_today_tasks_snapshot,
)


REQUIRED_ROLLOVER_KEYS = ("review", "next_today_tasks")
_REVIEW_STRUCTURE_INSTRUCTION = "输出“完成了什么”“改进建议”“值得肯定的行为”三部分；重点分析学习工作的安排和工程经验，而不是每个目标的技术实现。"
_REVIEW_EVIDENCE_INSTRUCTION = "daily记录的“##记录”标题下，没有的内容，说明没有完成。记录的内容，可能包含不是原定计划。"
_REVIEW_PRAISE_INSTRUCTION = "最后另起一个独立段落，基于事实给三句话表扬；表扬要自然真诚，贴合当天记录。"
_RECORD_TAGS_INSTRUCTION = """“记录”的标签：
“临时：”代表临时插入的任务，不在当天任务计划内
“经验：”代表学习和工作中产生的心得，需要在review中总结归纳。"""
TASK_MANAGEMENT_CATEGORY_HEADINGS = {"目标", "日常", "xiushenlu维护", "修身炉维护"}


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
    rollover: bool = True,
) -> NightlyReviewResult:
    cfg = config or load_config()
    today = date.today()
    current_date = target_date or today
    date_text = current_date.isoformat()
    day_text = read_daily(cfg, date_text)
    event_logger = logger or EventLogger(config=cfg)
    events = _events_for_date(event_logger, date_text)
    rolled_over = rollover

    if rolled_over:
        tomorrow_plan = read_tomorrow_plan(cfg)
        daily_context = _build_daily_review_context(day_text)
        prompt = _build_rollover_prompt(
            date_text=date_text,
            daily_context=daily_context,
            tomorrow_plan=tomorrow_plan,
        )
        raw_reply = provider.chat(prompt).strip()
        if cancel_check is not None:
            cancel_check()
        append_llm_call_event(event_logger, provider, "nightly_review")
        parsed = parse_nightly_review_response(raw_reply)
        review = parsed.review
        next_today_tasks = _merge_recurring_daily_tasks(parsed.next_today_tasks, daily_context)

        path = write_daily_section("复盘", review, cfg, date_text, mode="replace")
        next_tasks_path = write_today_tasks(next_today_tasks, cfg)
        next_tomorrow_plan_path = clear_tomorrow_plan(cfg)
        if current_date == today:
            append_token_usage_report(cfg, event_logger, current_date)
    else:
        prompt = _build_prompt(date_text, _build_daily_review_context(day_text))
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
        value = value.strip()
        if key == "next_today_tasks":
            value = _normalize_next_today_tasks(value)
        values[key] = value

    return ParsedNightlyReview(review=values["review"], next_today_tasks=values["next_today_tasks"])


def _events_for_date(logger: EventLogger, date_text: str) -> list[dict[str, Any]]:
    return logger.read_events_for_date(date_text)


@dataclass(frozen=True)
class DailyReviewContext:
    today_tasks: str
    records: str
    plan_notes: str


def _build_prompt(date_text: str, daily_context: DailyReviewContext) -> str:
    return f"""你是一个帮助用户复盘学习和工作的个人执行助手。

请根据 {date_text} 的 daily 记录，生成review。

要求：
{_build_review_requirements()}

{_RECORD_TAGS_INSTRUCTION}

{_build_daily_context_block(daily_context)}
"""


def _build_rollover_prompt(
    date_text: str,
    daily_context: DailyReviewContext,
    tomorrow_plan: str,
) -> str:
    tomorrow_plan_text = tomorrow_plan.strip() or "（明日计划.md 为空）"
    return f"""你是一个帮助用户复盘学习和工作的个人执行助手。

请根据 {date_text} 的 daily 当天快照生成晚间复盘，并为明天滚动生成完整的 today_tasks.md 内容。

你必须只输出一个严格 JSON 对象，不要使用代码块，不要输出解释文字。
JSON 必须包含且只需要包含这些字符串字段：
- review：晚间复盘正文，必须遵守下面的“复盘要求”。
- next_today_tasks：新的完整 today_tasks.md 内容。只能由“任务管理”里的未完成计划任务、“今日待办”里“日常”分组的每日重复任务，和“明日计划.md”的显式内容组成；必须使用“【分组】”加编号列表；禁止输出“任务管理”标题或任何 Markdown 表格；不要输出任何 Markdown 标题行，禁止输出任何以 `#` 开头的标题行；不要生成口号、总结句或装饰性标题；不要判断优先级；未完成的任务，优先按“今日待办”的原小标题归类。

复盘要求：
{_build_review_requirements()}

{_RECORD_TAGS_INSTRUCTION}

判断未完成任务的规则：
- 今日记录只作为完成证据、取消证据或 review 分析依据；禁止从记录内容新增、派生或沉淀任务。记录里的临时任务、开始做某事，如果不在“今日待办”中，也不能写入 next_today_tasks。
- 今日待办里标题或分组为“日常”的任务代表每日重复；即使计划表“状态”列为“✓”表示今天已完成，仍必须继续写入 next_today_tasks 的“日常”分组。
- 计划表“状态”列为“×”的任务，表示已删除或取消，不能写入 next_today_tasks；非“日常”任务的“状态”列为“✓”表示已经完成，也不能写入 next_today_tasks。
- 如果今天没有未完成任务且明日计划也为空，next_today_tasks 仍输出完整 today_tasks.md，保留中文分组风格或写出空任务占位，但不要输出“任务管理”标题或任何 Markdown 表格，不要输出任何 Markdown 标题行，禁止输出任何以 `#` 开头的标题行，也不要生成新的口号。
- 生成 review 时不要引用“明日计划.md”；明日计划只允许用于生成 next_today_tasks。

{_build_daily_context_block(daily_context, today_tasks_label="今日待办（来自 daily 的当天快照，不读取当前 today_tasks.md）")}

明日计划.md（只用于 next_today_tasks，不用于 review）：
{tomorrow_plan_text}
"""


def _build_review_requirements() -> str:
    return "\n".join(
        [
            f"- {_REVIEW_STRUCTURE_INSTRUCTION}",
            f"- {_REVIEW_EVIDENCE_INSTRUCTION}",
            f"- {_REVIEW_PRAISE_INSTRUCTION}",
        ]
    )


def _build_daily_context_block(
    daily_context: DailyReviewContext,
    *,
    today_tasks_label: str = "今日待办（来自 daily 的当天快照）",
) -> str:
    today_tasks = daily_context.today_tasks.strip() or "（daily 的“今日待办”为空）"
    records = daily_context.records.strip() or "（今天还没有记录）"
    plan_notes = daily_context.plan_notes.strip() or "（没有额外计划建议）"
    return f"""{today_tasks_label}：
{today_tasks}

今日记录（完成证据只看这里）：
{records}

计划建议（只作为理解当天安排的辅助，不作为完成证据）：
{plan_notes}"""


def _build_daily_review_context(daily_text: str) -> DailyReviewContext:
    plan_section = _extract_level2_section(daily_text, "计划")
    today_tasks, plan_notes = _split_plan_original_tasks(plan_section)
    records = _extract_level2_section(daily_text, "记录")
    return DailyReviewContext(
        today_tasks=today_tasks,
        records=_remove_generated_at(records),
        plan_notes=plan_notes,
    )


def _extract_level2_section(text: str, title: str) -> str:
    heading = f"## {title}"
    lines = text.splitlines(keepends=True)
    start: int | None = None
    start_line_index: int | None = None
    offset = 0

    for index, line in enumerate(lines):
        if line.strip() == heading:
            start = offset + len(line)
            start_line_index = index + 1
            break
        offset += len(line)

    if start is None or start_line_index is None:
        return ""

    end = len(text)
    offset = start
    for line in lines[start_line_index:]:
        if line.startswith("## "):
            end = offset
            break
        offset += len(line)

    return text[start:end].strip()


def _split_plan_original_tasks(plan_section: str) -> tuple[str, str]:
    lines = _remove_generated_at(plan_section).splitlines()
    heading_index = _find_original_tasks_heading(lines)
    if heading_index is None:
        return "", "\n".join(lines).strip()

    start = heading_index + 1
    while start < len(lines) and not lines[start].strip():
        start += 1

    end = start
    while end < len(lines):
        if _looks_like_plan_notes_heading(lines[end]):
            break
        end += 1

    today_tasks = "\n".join(lines[start:end]).strip()
    plan_notes = "\n".join(lines[end:]).strip()
    return today_tasks, plan_notes


def _find_original_tasks_heading(lines: list[str]) -> int | None:
    for index, line in enumerate(lines):
        if _is_today_tasks_heading(line):
            return index
    return None


def _is_today_tasks_heading(line: str) -> bool:
    text = line.strip().strip("*").strip("#").strip()
    text = text.removeprefix("1.").strip()
    return text in {"今日待办", "今日待办原文"}


def _looks_like_plan_notes_heading(line: str) -> bool:
    text = line.strip().strip("*").strip("#").strip()
    if not text or len(text) > 80:
        return False
    keywords = ("任务管理", "计划建议", "任务建议", "根据长期目标", "风险提醒", "收尾检查", "新任务")
    return any(keyword in text for keyword in keywords)


def _normalize_next_today_tasks(text: str) -> str:
    return format_today_tasks_snapshot(_drop_markdown_tables(text))


def _merge_recurring_daily_tasks(next_today_tasks: str, daily_context: DailyReviewContext) -> str:
    recurring_items = _extract_recurring_daily_items(daily_context.today_tasks)
    if not recurring_items:
        return next_today_tasks

    base_next_today_tasks = (
        "" if next_today_tasks.strip() == EMPTY_TODAY_TASKS_PLACEHOLDER else next_today_tasks
    )
    canceled_tasks = _extract_task_statuses(daily_context.plan_notes, target_status="×")
    existing_items = _extract_task_items(base_next_today_tasks)
    additions = [
        item
        for item in recurring_items
        if _task_key(item) not in canceled_tasks and _task_key(item) not in existing_items
    ]
    if not additions:
        return next_today_tasks

    recurring_block = "\n".join(
        ["【日常】", *(f"{index}. {item}" for index, item in enumerate(additions, start=1))]
    )
    return format_today_tasks_snapshot(f"{base_next_today_tasks}\n\n{recurring_block}")


def _extract_recurring_daily_items(today_tasks: str) -> list[str]:
    sections = _parse_today_task_sections(today_tasks)
    return [
        item
        for heading, items in sections
        if _is_recurring_daily_heading(heading)
        for item in items
    ]


def _extract_task_items(today_tasks: str) -> set[str]:
    return {
        _task_key(item)
        for _, items in _parse_today_task_sections(today_tasks)
        for item in items
    }


def _parse_today_task_sections(text: str) -> list[tuple[str, list[str]]]:
    sections: list[tuple[str, list[str]]] = []
    current_heading: str | None = None
    current_items: list[str] | None = None
    previous_blank = True

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            previous_blank = True
            continue
        if _is_today_tasks_heading(line):
            previous_blank = True
            continue

        heading = _parse_today_task_heading(line)
        if heading is not None:
            current_heading, current_items = _get_or_create_today_task_section(sections, heading)
            previous_blank = False
            continue

        inline_heading = _parse_today_task_inline_heading(line)
        if inline_heading is not None and (current_heading is None or previous_blank):
            heading_text, item_text = inline_heading
            current_heading, current_items = _get_or_create_today_task_section(sections, heading_text)
            current_items.append(item_text)
            previous_blank = False
            continue

        if current_items is None:
            current_heading, current_items = _get_or_create_today_task_section(sections, "待办")
        item = _normalize_today_task_item(line)
        if item:
            current_items.append(item)
        previous_blank = False

    return sections


def _get_or_create_today_task_section(
    sections: list[tuple[str, list[str]]],
    heading: str,
) -> tuple[str, list[str]]:
    normalized = _normalize_today_task_heading(heading)
    for section_heading, section_items in sections:
        if section_heading == normalized:
            return section_heading, section_items
    section_items: list[str] = []
    sections.append((normalized, section_items))
    return normalized, section_items


def _parse_today_task_heading(line: str) -> str | None:
    stripped = line.strip()
    if len(stripped) > 2 and stripped.startswith("【") and stripped.endswith("】"):
        return stripped[1:-1].strip()
    if stripped.endswith(("：", ":")):
        return stripped[:-1].strip()
    return None


def _parse_today_task_inline_heading(line: str) -> tuple[str, str] | None:
    match = re.match(r"^([^：:\n]{1,30})[：:]\s*(.+)$", line, flags=re.DOTALL)
    if match is None:
        return None
    heading = _normalize_today_task_heading(match.group(1))
    item = _normalize_today_task_item(match.group(2))
    if not heading or not item:
        return None
    return heading, item


def _normalize_today_task_heading(text: str) -> str:
    return text.strip().strip("*").strip("#").strip()


def _normalize_today_task_item(text: str) -> str:
    stripped = text.strip()
    stripped = re.sub(r"^\s*(?:[-*+]\s+|\d+[.)、]\s*)", "", stripped).strip()
    return stripped


def _is_recurring_daily_heading(heading: str) -> bool:
    return _normalize_today_task_heading(heading) == "日常"


def _extract_task_statuses(plan_notes: str, target_status: str) -> set[str]:
    statuses: set[str] = set()
    for line in plan_notes.splitlines():
        if not _is_markdown_table_line(line):
            continue
        cells = _split_markdown_table_row(line)
        if cells[0] == "任务" or _is_markdown_separator_cell(cells[0]):
            continue
        status_index = 3 if len(cells) >= 5 else 2 if len(cells) >= 3 else None
        if status_index is not None and cells[status_index] == target_status:
            statuses.add(_task_key(cells[0]))
    return statuses


def _split_markdown_table_row(line: str) -> list[str]:
    normalized = line.replace("｜", "|")
    return [cell.strip() for cell in normalized.strip().strip("|").split("|")]


def _is_markdown_separator_cell(text: str) -> bool:
    stripped = text.strip()
    return bool(stripped) and all(char in "-:" for char in stripped)


def _task_key(text: str) -> str:
    return re.sub(r"\s+", "", text.strip()).casefold()


def _drop_markdown_tables(text: str) -> str:
    kept: list[str] = []
    dropping_task_table = False
    for line in text.splitlines():
        stripped = line.strip()
        if _is_task_management_heading(stripped) or _is_markdown_table_line(stripped):
            dropping_task_table = True
            continue
        if dropping_task_table:
            if _is_task_management_category_heading(stripped):
                continue
            if _is_today_task_section_start(stripped):
                dropping_task_table = False
            else:
                continue
        kept.append(line)
    return "\n".join(kept).strip()


def _is_task_management_heading(line: str) -> bool:
    text = line.strip().strip("*").strip("#").strip()
    return text == "任务管理"


def _is_task_management_category_heading(line: str) -> bool:
    text = line.strip().strip("*").strip("#").strip()
    if len(text) > 2 and text.startswith("【") and text.endswith("】"):
        text = text[1:-1].strip()
    return text in TASK_MANAGEMENT_CATEGORY_HEADINGS


def _is_markdown_table_line(line: str) -> bool:
    stripped = line.strip()
    return stripped.startswith("|") and stripped.endswith("|") and stripped.count("|") >= 2


def _is_today_task_section_start(line: str) -> bool:
    stripped = line.strip()
    if len(stripped) > 2 and stripped.startswith("【") and stripped.endswith("】"):
        return True
    if 1 < len(stripped) <= 40 and stripped.endswith(("：", ":")):
        return True
    for delimiter in ("：", ":"):
        if delimiter not in stripped:
            continue
        heading, item = stripped.split(delimiter, 1)
        if 0 < len(heading.strip()) <= 30 and item.strip() and "|" not in stripped:
            return True
    return False


def _remove_generated_at(text: str) -> str:
    kept = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(("生成时间：", "更新时间：")):
            continue
        kept.append(line)
    return "\n".join(kept).strip()
