from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Callable

from app.config import load_config, resolve_project_path
from app.daily import write_daily_section
from app.inbox import read_today_tasks
from app.llm.provider import LLMProvider
from app.llm.usage import append_llm_call_event
from app.logger import EventLogger
from app.memory.goals import read_goals
from app.pipelines.today_tasks_format import (
    EMPTY_TODAY_TASKS_PLACEHOLDER,
    format_today_tasks_snapshot,
)


@dataclass(frozen=True)
class DailyPlanResult:
    date: str
    path: Path
    plan: str


def generate_daily_plan(
    provider: LLMProvider,
    config: dict[str, Any] | None = None,
    target_date: date | None = None,
    tasks_text: str | None = None,
    logger: EventLogger | None = None,
    cancel_check: Callable[[], None] | None = None,
) -> DailyPlanResult:
    cfg = config or load_config()
    current_date = target_date or date.today()
    date_text = current_date.isoformat()
    goals = read_goals(cfg)
    tasks = tasks_text if tasks_text is not None else read_today_tasks(cfg)
    prompt = _build_prompt(date_text=date_text, goals=goals, tasks=tasks)

    plan_schedule = provider.chat(prompt).strip()
    if cancel_check is not None:
        cancel_check()
    plan = _build_plan(tasks=tasks, plan_schedule=plan_schedule)
    daily_path = _daily_path(cfg, date_text)
    _write_plan_section(config=cfg, date_text=date_text, plan=plan)

    event_logger = logger or EventLogger()
    append_llm_call_event(event_logger, provider, "daily_plan")
    event_logger.append_event(
        "plan_generated",
        f"生成 {date_text} 的计划",
        {
            "date": date_text,
            "daily_path": str(daily_path),
            "goals_chars": len(goals),
            "tasks_chars": len(tasks.strip()),
        },
    )

    return DailyPlanResult(date=date_text, path=daily_path, plan=plan)


def _daily_path(config: dict[str, Any], date_text: str) -> Path:
    daily_dir = resolve_project_path(config["paths"]["daily_dir"])
    daily_dir.mkdir(parents=True, exist_ok=True)
    return daily_dir / f"{date_text}.md"


def _build_prompt(date_text: str, goals: str, tasks: str) -> str:
    goals_text = goals.strip() or "（尚未填写长期目标）"
    tasks_text = tasks.strip() or "（尚未填写今日待办）"
    return f"""你是一个帮助用户安排当天时间的个人执行助手。

请根据长期目标和今日待办，为 {date_text} 生成一份当天任务管理表。

输出结构：
1. 只输出"**任务管理**"和 markdown 表格。
2. 任务管理表格前固定输出一行：**任务管理**
3. 任务管理表格的表头必须使用英文竖线，固定为：| 任务 | 优先级 | 预计 | 状态 | 用时 |。“状态”和“用时”两列都不填。
4. “任务”列必须逐字使用今日待办里的任务正文，不能改写、概括、扩写、删掉括号补充或改成短标题。
5. 预估时要考虑用户会用 Codex 辅助工作。如果工作总时间超出6小时（工作外的杂事不算入时间），在表格后用一句话提示超时，并说明6小时内优先做哪几个任务。

其他要求：
- 不要输出“今日待办”原文；这部分由程序直接写入。
- 今日待办只用于分析；除了任务管理表“任务”列需要逐字使用任务正文外，不要复制、改写、重排今日待办原文。
- 如果长期目标或今日待办看起来还只是模板或为空，在表格后用一句话提醒用户补充。
- 输出采用 markdown 格式，但不要用```markdown，标题不可使用#和##。
- 不以询问句结尾。

长期目标：
{goals_text}

今日待办：
{tasks_text}
"""


def _build_plan(tasks: str, plan_schedule: str) -> str:
    today_tasks = _format_today_tasks_section(tasks)
    schedule_text = _normalize_schedule_table(plan_schedule.strip(), tasks=tasks)
    if not schedule_text:
        return today_tasks
    schedule_text = _ensure_task_management_title(schedule_text)
    return f"{today_tasks}\n\n{schedule_text}".strip()


def _format_today_tasks_section(tasks: str) -> str:
    tasks_text = format_today_tasks_snapshot(tasks)
    return f"**今日待办**\n\n{tasks_text}"


def _write_plan_section(config: dict[str, Any], date_text: str, plan: str) -> None:
    write_daily_section(
        "计划",
        plan,
        config=config,
        target_date=date_text,
        mode="replace",
        include_generated_at=False,
    )


def _normalize_schedule_table(schedule_text: str, tasks: str = "") -> str:
    lines = schedule_text.splitlines()
    table_start = _find_schedule_table_start(lines)
    if table_start is None:
        return schedule_text

    table_end = table_start
    while table_end < len(lines) and _looks_like_table_line(lines[table_end]):
        table_end += 1

    normalized = _normalize_table_lines(
        lines[table_start:table_end],
        task_items=_extract_task_items(tasks),
    )
    if normalized is None:
        return schedule_text
    prefix = _drop_trailing_schedule_heading(lines[:table_start])
    return "\n".join(prefix + normalized + lines[table_end:]).strip()


def _find_schedule_table_start(lines: list[str]) -> int | None:
    schedule_heading = None
    for index, line in enumerate(lines):
        if line.strip().strip("*").strip("#").strip() == "时间安排":
            schedule_heading = index
            break

    search_start = schedule_heading + 1 if schedule_heading is not None else 0
    for index in range(search_start, len(lines)):
        if _looks_like_table_line(lines[index]):
            return index
    return None


def _normalize_table_lines(
    table_lines: list[str],
    task_items: list[str] | None = None,
) -> list[str] | None:
    if len(table_lines) < 2:
        return None

    rows = [_split_table_row(line) for line in table_lines]
    headers = rows[0]
    if headers == ["任务", "优先级", "预计", "状态", "用时"]:
        expected_cols = 5
    else:
        return None

    separator = rows[1]
    if len(separator) != expected_cols:
        return None

    normalized = [
        "| 任务 | 优先级 | 预计 | 状态 | 用时 |",
        "|---|---|---|---|---|",
    ]
    data_rows = rows[2:]
    source_items = task_items or []
    used_source_indexes: set[int] = set()
    for cells in data_rows:
        if len(cells) != expected_cols:
            return None
        task, priority, estimate = cells[:3]
        source_task = _match_source_task(task, source_items, used_source_indexes)
        if source_task is not None:
            task = source_task
        normalized.append(f"| {task} | {priority} | {estimate} |  |  |")
    return normalized


def _extract_task_items(tasks: str) -> list[str]:
    formatted = format_today_tasks_snapshot(tasks)
    if formatted == EMPTY_TODAY_TASKS_PLACEHOLDER:
        return []

    items: list[str] = []
    for line in formatted.splitlines():
        stripped = line.strip()
        prefix, separator, item = stripped.partition(". ")
        if separator and prefix.isdecimal() and item.strip():
            candidate = item.strip()
            if _is_safe_table_cell(candidate):
                items.append(candidate)
    return items


def _match_source_task(
    table_task: str,
    source_items: list[str],
    used_source_indexes: set[int],
) -> str | None:
    table_key = _task_match_key(table_task)
    if not table_key:
        return None

    exact_matches: list[int] = []
    partial_matches: list[tuple[int, int]] = []
    fuzzy_matches: list[tuple[float, int]] = []
    for index, source_item in enumerate(source_items):
        if index in used_source_indexes:
            continue
        source_key = _task_match_key(source_item)
        if not source_key:
            continue
        if source_key == table_key:
            exact_matches.append(index)
            continue
        if table_key in source_key or source_key in table_key:
            partial_matches.append((min(len(table_key), len(source_key)), index))
            continue
        similarity = _task_similarity(table_key, source_key)
        if similarity >= 0.5:
            fuzzy_matches.append((similarity, index))

    if exact_matches:
        matched_index = exact_matches[0]
        used_source_indexes.add(matched_index)
        return source_items[matched_index]

    if not partial_matches:
        return _pick_best_fuzzy_source(fuzzy_matches, source_items, used_source_indexes)

    partial_matches.sort(key=lambda item: (-item[0], item[1]))
    best_score = partial_matches[0][0]
    best_matches = [index for score, index in partial_matches if score == best_score]
    if len(best_matches) != 1:
        return _pick_best_fuzzy_source(fuzzy_matches, source_items, used_source_indexes)

    matched_index = best_matches[0]
    used_source_indexes.add(matched_index)
    return source_items[matched_index]


def _pick_best_fuzzy_source(
    fuzzy_matches: list[tuple[float, int]],
    source_items: list[str],
    used_source_indexes: set[int],
) -> str | None:
    if not fuzzy_matches:
        return None

    fuzzy_matches.sort(key=lambda item: (-item[0], item[1]))
    best_score = fuzzy_matches[0][0]
    best_matches = [index for score, index in fuzzy_matches if score == best_score]
    if len(best_matches) != 1:
        return None

    matched_index = best_matches[0]
    used_source_indexes.add(matched_index)
    return source_items[matched_index]


def _task_similarity(left: str, right: str) -> float:
    left_chars = set(left)
    right_chars = set(right)
    min_chars = min(len(left_chars), len(right_chars))
    if min_chars < 4:
        return 0.0

    overlap = len(left_chars & right_chars)
    coverage = overlap / min_chars
    jaccard = overlap / len(left_chars | right_chars)
    return coverage * 0.7 + jaccard * 0.3


def _task_match_key(text: str) -> str:
    return re.sub(r"[\s，,。.!！？、：:；;（）()【】\[\]《》<>\"'`*_#-]+", "", text).lower()


def _is_safe_table_cell(text: str) -> bool:
    return "\n" not in text and "\r" not in text and "|" not in text


def _drop_trailing_schedule_heading(lines: list[str]) -> list[str]:
    prefix = list(lines)
    while prefix and not prefix[-1].strip():
        prefix.pop()
    if prefix and prefix[-1].strip().strip("*").strip("#").strip() == "时间安排":
        prefix.pop()
    while prefix and not prefix[-1].strip():
        prefix.pop()
    return prefix


def _ensure_task_management_title(schedule_text: str) -> str:
    lines = schedule_text.splitlines()
    table_start = _find_schedule_table_start(lines)
    if table_start is None:
        return schedule_text

    prefix = list(lines[:table_start])
    table_and_after = lines[table_start:]
    while prefix and not prefix[-1].strip():
        prefix.pop()

    if prefix and prefix[-1].strip().strip("*").strip("#").strip() == "任务管理":
        prefix[-1] = "**任务管理**"
    else:
        prefix.append("**任务管理**")

    return "\n".join(prefix + table_and_after).strip()


def _looks_like_table_line(line: str) -> bool:
    stripped = line.strip()
    if not stripped.startswith("|") or not stripped.endswith("|"):
        return False
    return "|" in stripped.strip("|") or "｜" in stripped.strip("|")


def _split_table_row(line: str) -> list[str]:
    normalized = line.replace("｜", "|")
    return [cell.strip() for cell in normalized.strip().strip("|").split("|")]
