from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from app.config import load_config
from app.daily import daily_path, read_daily
from app.inbox import read_today_tasks, write_today_tasks
from app.llm.provider import LLMProvider
from app.llm.usage import append_llm_call_event
from app.logger import EventLogger
from app.memory.goals import read_goals
from app.safety import safe_write_text


REQUIRED_RESPONSE_KEYS = (
    "updated_today_tasks",
    "updated_daily_original",
    "target_heading",
    "new_task_advice",
)


class PlanUpdateParseError(ValueError):
    """Raised when the LLM response is not the strict JSON shape we need."""


@dataclass(frozen=True)
class ParsedPlanUpdate:
    updated_today_tasks: str
    updated_daily_original: str
    target_heading: str
    new_task_advice: str


@dataclass(frozen=True)
class PlanUpdateResult:
    date: str
    daily_path: Path
    today_tasks_path: Path
    new_task: str
    target_heading: str
    new_task_advice: str


def generate_plan_update(
    provider: LLMProvider,
    new_task: str,
    config: dict[str, Any] | None = None,
    target_date: date | None = None,
    logger: EventLogger | None = None,
) -> PlanUpdateResult:
    task_text = new_task.strip()
    if not task_text:
        raise ValueError("Plan update content must not be empty.")

    cfg = config or load_config()
    current_date = target_date or date.today()
    date_text = current_date.isoformat()
    goals = read_goals(cfg)
    today_tasks = read_today_tasks(cfg)
    daily_text = read_daily(cfg, date_text)
    prompt = _build_prompt(
        date_text=date_text,
        goals=goals,
        today_tasks=today_tasks,
        daily_text=daily_text,
        new_task=task_text,
    )

    raw_reply = provider.chat(prompt).strip()
    event_logger = logger or EventLogger()
    append_llm_call_event(event_logger, provider, "plan_update")
    parsed = parse_plan_update_response(raw_reply)

    task_path = write_today_tasks(parsed.updated_today_tasks, cfg)
    daily_file = daily_path(cfg, date_text)
    updated_daily = update_daily_plan_text(
        daily_text=daily_text,
        date_text=date_text,
        updated_daily_original=parsed.updated_daily_original,
        new_task_entry=format_new_task_entry(
            new_task=task_text,
            target_heading=parsed.target_heading,
            new_task_advice=parsed.new_task_advice,
        ),
    )
    safe_write_text(daily_file, updated_daily, cfg)

    event_logger.append_event(
        "plan_updated",
        f"更新 {date_text} 的计划",
        {
            "date": date_text,
            "daily_path": str(daily_file),
            "today_tasks_path": str(task_path),
            "new_task": task_text,
            "target_heading": parsed.target_heading,
        },
    )

    return PlanUpdateResult(
        date=date_text,
        daily_path=daily_file,
        today_tasks_path=task_path,
        new_task=task_text,
        target_heading=parsed.target_heading,
        new_task_advice=parsed.new_task_advice,
    )


def parse_plan_update_response(text: str) -> ParsedPlanUpdate:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise PlanUpdateParseError(f"LLM response is not valid JSON: {exc}") from exc

    if not isinstance(data, dict):
        raise PlanUpdateParseError("LLM response must be a JSON object.")

    missing = [key for key in REQUIRED_RESPONSE_KEYS if key not in data]
    if missing:
        raise PlanUpdateParseError(f"LLM response is missing keys: {', '.join(missing)}")

    values: dict[str, str] = {}
    for key in REQUIRED_RESPONSE_KEYS:
        value = data[key]
        if not isinstance(value, str) or not value.strip():
            raise PlanUpdateParseError(f"LLM response key must be a non-empty string: {key}")
        values[key] = value.strip()

    return ParsedPlanUpdate(
        updated_today_tasks=values["updated_today_tasks"],
        updated_daily_original=values["updated_daily_original"],
        target_heading=values["target_heading"],
        new_task_advice=values["new_task_advice"],
    )


def update_daily_plan_text(
    daily_text: str,
    date_text: str,
    updated_daily_original: str,
    new_task_entry: str,
) -> str:
    original = daily_text.rstrip()
    if not original:
        original = f"# {date_text}"

    section_range = _find_level2_section(original, "计划")
    if section_range is None:
        plan_section = _minimal_plan_section(updated_daily_original, new_task_entry)
        return _insert_missing_plan_section(original, plan_section)

    start, end = section_range
    prefix = original[:start].strip()
    suffix = original[end:].strip()
    plan_section = original[start:end].strip()
    plan_section = _replace_daily_original(plan_section, updated_daily_original)
    plan_section = _append_new_task_entry(plan_section, new_task_entry)

    parts = [part for part in (prefix, plan_section.rstrip(), suffix) if part]
    return "\n\n".join(parts).rstrip() + "\n"


def format_new_task_entry(new_task: str, target_heading: str, new_task_advice: str) -> str:
    advice = new_task_advice.strip()
    return (
        f"### 新增：{new_task.strip()}\n\n"
        f"- 原文：{new_task.strip()}\n"
        f"- 归入标题：{target_heading.strip()}\n\n"
        f"{advice}"
    )


def _find_level2_section(text: str, title: str) -> tuple[int, int] | None:
    heading = f"## {title}"
    lines = text.splitlines(keepends=True)
    positions: list[int] = []
    offset = 0
    for line in lines:
        positions.append(offset)
        if line.strip() == heading:
            start = offset
            break
        offset += len(line)
    else:
        return None

    offset = start + len(lines[len(positions) - 1])
    for line in lines[len(positions) :]:
        if line.startswith("## "):
            return start, offset
        offset += len(line)
    return start, len(text)


def _minimal_plan_section(updated_daily_original: str, new_task_entry: str) -> str:
    return (
        "## 计划\n\n"
        "**1. 今日待办原文**\n\n"
        f"{updated_daily_original.strip()}\n\n"
        "**新任务**\n\n"
        f"{new_task_entry.strip()}"
    )


def _insert_missing_plan_section(original: str, plan_section: str) -> str:
    lines = original.splitlines()
    if lines and lines[0].startswith("# "):
        prefix = "\n".join(lines[:1]).strip()
        suffix = "\n".join(lines[1:]).strip()
        parts = [part for part in (prefix, plan_section.rstrip(), suffix) if part]
        return "\n\n".join(parts).rstrip() + "\n"
    return f"{original.rstrip()}\n\n{plan_section.rstrip()}\n"


def _replace_daily_original(plan_section: str, updated_daily_original: str) -> str:
    lines = plan_section.splitlines()
    heading_index = _find_daily_original_heading(lines)
    if heading_index is None:
        insert_at = _daily_original_insert_index(lines)
        block = ["**1. 今日待办原文**", "", updated_daily_original.strip(), ""]
        return "\n".join(lines[:insert_at] + block + lines[insert_at:]).rstrip()

    end_index = _find_next_plan_subsection(lines, heading_index + 1)
    block = [lines[heading_index].rstrip(), "", updated_daily_original.strip(), ""]
    if end_index is None:
        return "\n".join(lines[:heading_index] + block).rstrip()
    return "\n".join(lines[:heading_index] + block + lines[end_index:]).rstrip()


def _append_new_task_entry(plan_section: str, new_task_entry: str) -> str:
    if _has_new_task_heading(plan_section.splitlines()):
        return f"{plan_section.rstrip()}\n\n{new_task_entry.strip()}"
    return f"{plan_section.rstrip()}\n\n**新任务**\n\n{new_task_entry.strip()}"


def _find_daily_original_heading(lines: list[str]) -> int | None:
    for index, line in enumerate(lines):
        if "今日待办原文" in line:
            return index
    return None


def _daily_original_insert_index(lines: list[str]) -> int:
    index = 1 if lines and lines[0].strip() == "## 计划" else 0
    while index < len(lines):
        stripped = lines[index].strip()
        if not stripped or stripped.startswith("生成时间：") or stripped.startswith("更新时间："):
            index += 1
            continue
        break
    return index


def _find_next_plan_subsection(lines: list[str], start_index: int) -> int | None:
    for index in range(start_index, len(lines)):
        if _looks_like_plan_subsection(lines[index]):
            return index
    return None


def _looks_like_plan_subsection(line: str) -> bool:
    text = line.strip().strip("*").strip("#").strip()
    if len(text) > 80:
        return False
    keywords = ("计划建议", "任务建议", "根据长期目标", "风险提醒", "收尾检查")
    return any(keyword in text for keyword in keywords)


def _has_new_task_heading(lines: list[str]) -> bool:
    for line in lines:
        text = line.strip().strip("*").strip("#").strip()
        if text == "新任务":
            return True
    return False


def _build_prompt(
    date_text: str,
    goals: str,
    today_tasks: str,
    daily_text: str,
    new_task: str,
) -> str:
    goals_text = goals.strip() or "（尚未填写长期目标）"
    tasks_text = today_tasks.strip() or "（尚未填写今日待办）"
    daily_plan_text = daily_text.strip() or "（今天还没有 daily）"
    return f"""你是一个帮助用户稳定推进学习和工作的个人执行助手。

请把用户新增的今日任务合并进当天任务输入，并为这个新增任务单独生成计划建议。

日期：{date_text}

新增任务：
{new_task}

长期目标：
{goals_text}

当前 today_tasks.md：
{tasks_text}

当前 daily：
{daily_plan_text}

你必须只输出一个严格 JSON 对象，不要使用代码块，不要输出解释文字。
JSON 必须包含且只需要包含这些字符串字段：
- updated_today_tasks：完整的 today_tasks.md 新内容。保持原有风格和标题格式；判断新增任务应归入哪个已有小标题；如果没有合适小标题，新增一个同风格小标题。
- updated_daily_original：daily 计划中“今日待办原文”小节的新内容。保持原有 daily 待办原文风格，并包含新增任务。
- target_heading：新增任务最终归入的小标题名称，不要带序号。
- new_task_advice：只针对新增任务的 markdown 文本，必须包含“优先级”“任务建议”“预估时间”“与原计划关系”“风险提醒”。要考虑它和原计划中其他任务的关系，但不要重写已有计划建议。
"""
