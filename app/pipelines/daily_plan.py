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

FULL_TABLE_HEADERS = ("任务", "优先级", "预计", "状态", "用时")
MAINTENANCE_TABLE_HEADERS = ("任务", "优先级", "状态")
LEGACY_MAINTENANCE_TABLE_HEADERS = ("任务", "优先级")
CATEGORY_TARGET = "目标"
CATEGORY_DAILY = "日常"
CATEGORY_MAINTENANCE = "xiushenlu维护"
SCHEDULE_CATEGORIES = (CATEGORY_TARGET, CATEGORY_DAILY, CATEGORY_MAINTENANCE)
MAINTENANCE_HEADINGS = {"xiushenlu", "修身炉", "xiushenlu维护", "修身炉维护"}
MAINTENANCE_KEYWORDS = (
    "bug",
    "修bug",
    "修复",
    "修正",
    "修补",
    "排查",
    "排错",
    "报错",
    "错误",
    "问题",
    "优化",
    "维护",
    "改进",
    "调整",
    "重构",
    "清理",
    "完善",
)


@dataclass(frozen=True)
class SourceTaskItem:
    heading: str
    text: str


@dataclass(frozen=True)
class NormalizedScheduleRow:
    category: str
    task: str
    priority: str
    estimate: str


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
1. 只输出"**任务管理**"、三个任务分类标题和 markdown 表格。
2. 任务管理表格前固定输出一行：**任务管理**
3. 必须拆成三个小表，标题固定为：`【目标】`、`【日常】`、`【xiushenlu维护】`。
4. 表头必须使用英文竖线。`【目标】` 和 `【日常】` 表头固定为：| 任务 | 优先级 | 预计 | 状态 | 用时 |。“状态”和“用时”两列都不填。
5. `【xiushenlu维护】` 只输出三列表头：| 任务 | 优先级 | 状态 |。“状态”列不填，不要输出“预计”或“用时”列。
6. “任务”列必须逐字使用今日待办里的任务正文，不能改写、概括、扩写、删掉括号补充或改成短标题。
7. 今日待办中 `【日常】` 分组的任务放入 `【日常】` 表；修身炉 / xiushenlu 项目的修 bug、修复、优化、维护类任务放入 `【xiushenlu维护】` 表；除此之外都放入 `【目标】` 表。
8. 预估时要考虑用户会用 Codex 辅助工作。如果工作总时间超出6小时（工作外的杂事不算入时间），在表格后用一句话提示超时，并说明6小时内优先做哪几个任务。

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
    if not _has_task_management_title(schedule_text):
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
    parsed = _collect_schedule_rows(schedule_text, task_items=_extract_task_items(tasks))
    if parsed is None:
        return schedule_text
    rows, notes = parsed
    return _render_grouped_schedule(rows, notes)


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


def _collect_schedule_rows(
    schedule_text: str,
    task_items: list[SourceTaskItem],
) -> tuple[list[NormalizedScheduleRow], list[str]] | None:
    lines = schedule_text.splitlines()
    rows: list[NormalizedScheduleRow] = []
    notes: list[str] = []
    used_source_indexes: set[int] = set()
    category_hint: str | None = None
    saw_table = False
    index = 0
    while index < len(lines):
        line = lines[index]
        stripped = line.strip()
        category = _parse_schedule_category(stripped)
        if _is_task_management_heading(stripped) or _is_time_schedule_heading(stripped):
            index += 1
            continue
        if category is not None:
            category_hint = category
            index += 1
            continue
        if _looks_like_table_line(line):
            table_end = index
            while table_end < len(lines) and _looks_like_table_line(lines[table_end]):
                table_end += 1
            normalized_rows = _normalize_table_lines(
                lines[index:table_end],
                task_items=task_items,
                used_source_indexes=used_source_indexes,
                category_hint=category_hint,
            )
            if normalized_rows is None:
                return None
            rows.extend(normalized_rows)
            saw_table = True
            index = table_end
            continue
        if stripped:
            notes.append(stripped)
        index += 1

    if not saw_table:
        return None
    return rows, notes


def _normalize_table_lines(
    table_lines: list[str],
    task_items: list[SourceTaskItem],
    used_source_indexes: set[int],
    category_hint: str | None,
) -> list[NormalizedScheduleRow] | None:
    if len(table_lines) < 2:
        return None

    rows = [_split_table_row(line) for line in table_lines]
    headers = tuple(rows[0])
    if headers == FULL_TABLE_HEADERS:
        expected_cols = len(FULL_TABLE_HEADERS)
    elif headers == MAINTENANCE_TABLE_HEADERS:
        expected_cols = len(MAINTENANCE_TABLE_HEADERS)
    elif headers == LEGACY_MAINTENANCE_TABLE_HEADERS:
        expected_cols = len(LEGACY_MAINTENANCE_TABLE_HEADERS)
    else:
        return None

    separator = rows[1]
    if len(separator) != expected_cols:
        return None

    normalized: list[NormalizedScheduleRow] = []
    data_rows = rows[2:]
    for cells in data_rows:
        if len(cells) != expected_cols:
            return None
        task = cells[0]
        priority = cells[1]
        estimate = cells[2] if headers == FULL_TABLE_HEADERS else ""
        source_task = _match_source_task(task, task_items, used_source_indexes)
        source_heading = source_task.heading if source_task is not None else ""
        if source_task is not None:
            task = source_task.text
        category = _resolve_schedule_category(category_hint, source_heading, task)
        if category == CATEGORY_MAINTENANCE:
            estimate = ""
        normalized.append(
            NormalizedScheduleRow(
                category=category,
                task=task,
                priority=priority,
                estimate=estimate,
            )
        )
    return normalized


def _extract_task_items(tasks: str) -> list[SourceTaskItem]:
    formatted = format_today_tasks_snapshot(tasks)
    if formatted == EMPTY_TODAY_TASKS_PLACEHOLDER:
        return []

    items: list[SourceTaskItem] = []
    current_heading = ""
    for line in formatted.splitlines():
        stripped = line.strip()
        heading = _parse_bracket_heading(stripped)
        if heading is not None:
            current_heading = heading
            continue
        prefix, separator, item = stripped.partition(". ")
        if separator and prefix.isdecimal() and item.strip():
            candidate = item.strip()
            if _is_safe_table_cell(candidate):
                items.append(SourceTaskItem(heading=current_heading, text=candidate))
    return items


def _match_source_task(
    table_task: str,
    source_items: list[SourceTaskItem],
    used_source_indexes: set[int],
) -> SourceTaskItem | None:
    table_key = _task_match_key(table_task)
    if not table_key:
        return None

    exact_matches: list[int] = []
    partial_matches: list[tuple[int, int]] = []
    fuzzy_matches: list[tuple[float, int]] = []
    for index, source_item in enumerate(source_items):
        if index in used_source_indexes:
            continue
        source_key = _task_match_key(source_item.text)
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
    source_items: list[SourceTaskItem],
    used_source_indexes: set[int],
) -> SourceTaskItem | None:
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


def _resolve_schedule_category(category_hint: str | None, source_heading: str, task: str) -> str:
    if category_hint == CATEGORY_DAILY or _normalize_category_text(source_heading) == CATEGORY_DAILY:
        return CATEGORY_DAILY
    if category_hint == CATEGORY_MAINTENANCE or _is_maintenance_task(task, source_heading):
        return CATEGORY_MAINTENANCE
    return CATEGORY_TARGET


def _is_maintenance_task(task: str, heading: str = "") -> bool:
    heading_key = _normalize_category_text(heading)
    task_key = task.casefold()
    if heading_key not in MAINTENANCE_HEADINGS and CATEGORY_MAINTENANCE not in heading_key:
        return False
    return any(keyword.casefold() in task_key for keyword in MAINTENANCE_KEYWORDS)


def _render_grouped_schedule(rows: list[NormalizedScheduleRow], notes: list[str]) -> str:
    grouped = {category: [] for category in SCHEDULE_CATEGORIES}
    for row in rows:
        grouped.setdefault(row.category, []).append(row)

    lines: list[str] = ["**任务管理**", ""]
    lines.extend(_render_full_category(CATEGORY_TARGET, grouped[CATEGORY_TARGET]))
    lines.append("")
    lines.extend(_render_full_category(CATEGORY_DAILY, grouped[CATEGORY_DAILY]))
    lines.append("")
    lines.extend(_render_maintenance_category(grouped[CATEGORY_MAINTENANCE]))
    if notes:
        lines.append("")
        lines.extend(notes)
    return "\n".join(lines).strip()


def _render_full_category(category: str, rows: list[NormalizedScheduleRow]) -> list[str]:
    lines = [
        f"【{category}】",
        "| 任务 | 优先级 | 预计 | 状态 | 用时 |",
        "|---|---|---|---|---|",
    ]
    for row in rows:
        lines.append(f"| {row.task} | {row.priority} | {row.estimate} |  |  |")
    return lines


def _render_maintenance_category(rows: list[NormalizedScheduleRow]) -> list[str]:
    lines = [
        f"【{CATEGORY_MAINTENANCE}】",
        "| 任务 | 优先级 | 状态 |",
        "|---|---|---|",
    ]
    for row in rows:
        lines.append(f"| {row.task} | {row.priority} |  |")
    return lines


def _parse_schedule_category(line: str) -> str | None:
    heading = _parse_bracket_heading(line)
    if heading is None:
        return None
    normalized = _normalize_category_text(heading)
    if normalized == CATEGORY_DAILY:
        return CATEGORY_DAILY
    if normalized in {CATEGORY_MAINTENANCE, "修身炉维护"}:
        return CATEGORY_MAINTENANCE
    if normalized == CATEGORY_TARGET:
        return CATEGORY_TARGET
    return None


def _parse_bracket_heading(line: str) -> str | None:
    stripped = line.strip().strip("*").strip("#").strip()
    if len(stripped) > 2 and stripped.startswith("【") and stripped.endswith("】"):
        return stripped[1:-1].strip()
    return None


def _normalize_category_text(text: str) -> str:
    return text.strip().strip("*").strip("#").strip().casefold()


def _is_task_management_heading(line: str) -> bool:
    return line.strip().strip("*").strip("#").strip() == "任务管理"


def _is_time_schedule_heading(line: str) -> bool:
    return line.strip().strip("*").strip("#").strip() == "时间安排"


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


def _has_task_management_title(schedule_text: str) -> bool:
    return any(_is_task_management_heading(line.strip()) for line in schedule_text.splitlines())


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
