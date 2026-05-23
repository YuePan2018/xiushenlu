from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Callable

from app.config import load_config
from app.daily import daily_path, read_daily
from app.inbox import read_today_tasks, write_today_tasks
from app.llm.provider import LLMProvider
from app.llm.usage import append_llm_call_event
from app.logger import EventLogger
from app.memory.goals import read_goals
from app.pipelines.today_tasks_format import format_today_tasks_snapshot
from app.safety import safe_write_text


REQUIRED_RESPONSE_KEYS = (
    "updated_today_tasks",
    "updated_daily_original",
    "target_heading",
    "schedule_task",
    "schedule_priority",
    "schedule_estimate",
)
SCHEDULE_HEADERS = ("任务", "优先级", "预计", "状态", "用时")
MAINTENANCE_HEADERS = ("任务", "优先级", "状态")
LEGACY_MAINTENANCE_HEADERS = ("任务", "优先级")
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


class PlanUpdateParseError(ValueError):
    """Raised when the LLM response is not the strict JSON shape we need."""


@dataclass(frozen=True)
class ScheduleRow:
    task: str
    priority: str
    estimate: str
    category: str = CATEGORY_TARGET


@dataclass(frozen=True)
class ScheduleTableRow:
    task: str
    priority: str
    estimate: str
    status: str
    duration: str
    category: str


@dataclass(frozen=True)
class ScheduleTableBlock:
    start_line: int
    end_line: int
    category_hint: str | None
    headers: tuple[str, ...]


@dataclass(frozen=True)
class NewTaskIntent:
    raw_text: str
    task_text: str
    target_heading: str | None = None


@dataclass(frozen=True)
class ParsedPlanUpdate:
    updated_today_tasks: str
    updated_daily_original: str
    target_heading: str
    schedule_row: ScheduleRow


@dataclass(frozen=True)
class PlanUpdateResult:
    date: str
    daily_path: Path
    today_tasks_path: Path
    new_task: str
    target_heading: str


def generate_plan_update(
    provider: LLMProvider,
    new_task: str,
    config: dict[str, Any] | None = None,
    target_date: date | None = None,
    logger: EventLogger | None = None,
    cancel_check: Callable[[], None] | None = None,
) -> PlanUpdateResult:
    task_text = new_task.strip()
    if not task_text:
        raise ValueError("Plan update content must not be empty.")

    task_intent = parse_new_task_intent(task_text)
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
    if cancel_check is not None:
        cancel_check()
    event_logger = logger or EventLogger()
    append_llm_call_event(event_logger, provider, "plan_update")
    parsed = parse_plan_update_response(raw_reply)
    parsed = normalize_plan_update_content(
        parsed,
        new_task_intent=task_intent,
    )
    validate_plan_update_content(parsed, new_task=task_intent.task_text)

    daily_file = daily_path(cfg, date_text)
    updated_daily = update_daily_plan_text(
        daily_text=daily_text,
        date_text=date_text,
        updated_daily_original=parsed.updated_daily_original,
        schedule_row=parsed.schedule_row,
    )

    task_path = write_today_tasks(parsed.updated_today_tasks, cfg)
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
        schedule_row=ScheduleRow(
            task=values["schedule_task"],
            priority=values["schedule_priority"],
            estimate=values["schedule_estimate"],
        ),
    )


def parse_new_task_intent(new_task: str) -> NewTaskIntent:
    task_text = new_task.strip()
    match = re.match(r"^([^：:\n]{1,30})[：:]\s*(.+)$", task_text, flags=re.DOTALL)
    if match is None:
        return NewTaskIntent(raw_text=task_text, task_text=task_text)

    heading = _normalize_heading_name(match.group(1))
    item = match.group(2).strip()
    if not heading or not item:
        return NewTaskIntent(raw_text=task_text, task_text=task_text)
    return NewTaskIntent(raw_text=task_text, task_text=item, target_heading=heading)


def normalize_plan_update_content(
    parsed: ParsedPlanUpdate,
    new_task_intent: NewTaskIntent,
) -> ParsedPlanUpdate:
    target_heading = _normalize_heading_name(new_task_intent.target_heading or parsed.target_heading)
    updated_today_tasks = parsed.updated_today_tasks
    updated_daily_original = parsed.updated_daily_original

    if new_task_intent.target_heading is not None:
        updated_today_tasks = updated_today_tasks.replace(
            new_task_intent.raw_text, new_task_intent.task_text
        )
        updated_daily_original = updated_daily_original.replace(
            new_task_intent.raw_text, new_task_intent.task_text
        )

    updated_today_tasks = format_today_tasks_snapshot(updated_today_tasks)
    updated_daily_original = format_today_tasks_snapshot(updated_daily_original)

    return ParsedPlanUpdate(
        updated_today_tasks=updated_today_tasks,
        updated_daily_original=updated_daily_original,
        target_heading=target_heading,
        schedule_row=ScheduleRow(
            task=new_task_intent.task_text,
            priority=parsed.schedule_row.priority,
            estimate=parsed.schedule_row.estimate,
            category=_classify_schedule_category(target_heading, new_task_intent.task_text),
        ),
    )


def validate_plan_update_content(parsed: ParsedPlanUpdate, new_task: str) -> None:
    task_text = parse_new_task_intent(new_task).task_text
    if task_text not in parsed.updated_today_tasks:
        raise PlanUpdateParseError(
            "LLM response must include the new task verbatim in updated_today_tasks."
        )
    if task_text not in parsed.updated_daily_original:
        raise PlanUpdateParseError(
            "LLM response must include the new task verbatim in updated_daily_original."
        )

    for key, value in (
        ("schedule_task", parsed.schedule_row.task),
        ("schedule_priority", parsed.schedule_row.priority),
        ("schedule_estimate", parsed.schedule_row.estimate),
    ):
        if any(char in value for char in ("\n", "\r", "|")):
            raise PlanUpdateParseError(f"LLM response {key} must not contain line breaks or table separators.")


def _normalize_heading_name(text: str) -> str:
    stripped = text.strip()
    parsed = _parse_heading_line(stripped)
    if parsed is not None:
        return parsed
    return stripped


def _parse_heading_line(line: str) -> str | None:
    stripped = line.strip()
    if not stripped:
        return None
    if _is_bracket_heading(stripped):
        return stripped[1:-1].strip()
    if stripped.endswith(("：", ":")):
        return stripped[:-1].strip()
    return None


def _is_bracket_heading(line: str) -> bool:
    stripped = line.strip()
    return len(stripped) > 2 and stripped.startswith("【") and stripped.endswith("】")


def update_daily_plan_text(
    daily_text: str,
    date_text: str,
    updated_daily_original: str,
    schedule_row: ScheduleRow,
) -> str:
    original = daily_text.rstrip()
    if not original:
        original = f"# {date_text}"

    section_range = _find_level2_section(original, "计划")
    if section_range is None:
        plan_section = _minimal_plan_section(updated_daily_original, schedule_row)
        return _insert_missing_plan_section(original, plan_section)

    start, end = section_range
    prefix = original[:start].strip()
    suffix = original[end:].strip()
    plan_section = original[start:end].strip()
    plan_section = _replace_daily_original(plan_section, updated_daily_original)
    plan_section = _append_schedule_row(plan_section, schedule_row)

    parts = [part for part in (prefix, plan_section.rstrip(), suffix) if part]
    return "\n\n".join(parts).rstrip() + "\n"


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


def _minimal_plan_section(updated_daily_original: str, schedule_row: ScheduleRow) -> str:
    return (
        "## 计划\n\n"
        "**今日待办**\n\n"
        f"{updated_daily_original.strip()}\n\n"
        f"{_render_grouped_schedule_tables([_new_table_row(schedule_row)])}"
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
        block = ["**今日待办**", "", updated_daily_original.strip(), ""]
        return "\n".join(lines[:insert_at] + block + lines[insert_at:]).rstrip()

    end_index = _find_daily_original_end(lines, heading_index + 1)
    block = ["**今日待办**", "", updated_daily_original.strip(), ""]
    if end_index is None:
        return "\n".join(lines[:heading_index] + block).rstrip()
    return "\n".join(lines[:heading_index] + block + lines[end_index:]).rstrip()


def _append_schedule_row(plan_section: str, schedule_row: ScheduleRow) -> str:
    lines = plan_section.splitlines()
    blocks = _find_schedule_table_blocks(lines)
    new_row = _new_table_row(schedule_row)
    if not blocks:
        return f"{plan_section.rstrip()}\n\n{_render_grouped_schedule_tables([new_row])}"

    existing_rows = _parse_schedule_blocks(lines, blocks)
    area_start, area_end = _schedule_area_range(lines, blocks)
    new_table = _render_grouped_schedule_tables(existing_rows + [new_row]).splitlines()
    return "\n".join(lines[:area_start] + new_table + lines[area_end:]).rstrip()


def _find_daily_original_heading(lines: list[str]) -> int | None:
    for index, line in enumerate(lines):
        if _is_today_tasks_heading(line):
            return index
    return None


def _is_today_tasks_heading(line: str) -> bool:
    text = line.strip().strip("*").strip("#").strip()
    text = text.removeprefix("1.").strip()
    return text in {"今日待办", "今日待办原文"}


def _daily_original_insert_index(lines: list[str]) -> int:
    index = 1 if lines and lines[0].strip() == "## 计划" else 0
    while index < len(lines):
        stripped = lines[index].strip()
        if not stripped or stripped.startswith("生成时间：") or stripped.startswith("更新时间："):
            index += 1
            continue
        break
    return index


def _find_daily_original_end(lines: list[str], start_index: int) -> int | None:
    for index in range(start_index, len(lines)):
        if _is_schedule_table_start(lines, index) or _looks_like_plan_subsection(lines[index]):
            return index
    return None


def _looks_like_plan_subsection(line: str) -> bool:
    text = line.strip().strip("*").strip("#").strip()
    if len(text) > 80:
        return False
    keywords = (
        "计划建议",
        "任务建议",
        "根据长期目标",
        "风险提醒",
        "收尾检查",
        "时间块安排",
        "调度风险",
        "晚间收口",
    )
    return any(keyword in text for keyword in keywords)


def _find_schedule_table_blocks(lines: list[str]) -> list[ScheduleTableBlock]:
    blocks: list[ScheduleTableBlock] = []
    index = 0
    while index < len(lines):
        if not _is_schedule_table_start(lines, index):
            index += 1
            continue
        table_end = index
        while table_end < len(lines) and _is_table_line(lines[table_end]):
            table_end += 1
        headers = tuple(_split_table_row(lines[index]))
        blocks.append(
            ScheduleTableBlock(
                start_line=index,
                end_line=table_end,
                category_hint=_category_hint_before_table(lines, index),
                headers=_normalize_table_headers(headers),
            )
        )
        index = table_end
    return blocks


def _find_schedule_table_range(lines: list[str]) -> tuple[int, int] | None:
    for index in range(len(lines)):
        if not _is_schedule_table_start(lines, index):
            continue
        table_end = index
        while table_end < len(lines) and _is_table_line(lines[table_end]):
            table_end += 1
        return index, table_end
    return None


def _is_schedule_table_start(lines: list[str], index: int) -> bool:
    if index + 1 >= len(lines) or not _is_table_line(lines[index]):
        return False
    headers = _split_table_row(lines[index])
    normalized_headers = _normalize_table_headers(tuple(headers))
    if normalized_headers is None:
        return False
    separator = _split_table_row(lines[index + 1])
    return len(separator) == len(headers) and all(_is_separator_cell(cell) for cell in separator)


def _parse_schedule_blocks(lines: list[str], blocks: list[ScheduleTableBlock]) -> list[ScheduleTableRow]:
    rows: list[ScheduleTableRow] = []
    for block in blocks:
        table_lines = lines[block.start_line : block.end_line]
        rows.extend(_parse_schedule_table(table_lines, block))
    return rows


def _parse_schedule_table(table_lines: list[str], block: ScheduleTableBlock) -> list[ScheduleTableRow]:
    if len(table_lines) < 2:
        raise PlanUpdateParseError("schedule table must contain header and separator.")
    headers = tuple(_split_table_row(table_lines[0]))
    normalized_headers = _normalize_table_headers(headers)
    if normalized_headers is None:
        raise PlanUpdateParseError("schedule table header is not the expected columns.")

    rows: list[ScheduleTableRow] = []
    for line in table_lines[2:]:
        cells = _split_table_row(line)
        if len(cells) != len(headers):
            raise PlanUpdateParseError("schedule table row does not match the expected columns.")
        if normalized_headers == SCHEDULE_HEADERS:
            task, priority, estimate, status, duration = cells
            duration = _normalize_duration_cell(duration)
        elif headers == MAINTENANCE_HEADERS:
            task, priority, status = cells
            estimate = ""
            duration = ""
        else:
            task, priority = cells
            estimate = ""
            status = ""
            duration = ""
        rows.append(
            ScheduleTableRow(
                task=task,
                priority=priority,
                estimate=estimate,
                status=status,
                duration=duration,
                category=_resolve_schedule_category(block.category_hint, task),
            )
        )
    return rows


def _render_grouped_schedule_tables(rows: list[ScheduleTableRow]) -> str:
    grouped = {category: [] for category in SCHEDULE_CATEGORIES}
    for row in rows:
        grouped.setdefault(row.category, []).append(row)

    lines: list[str] = ["**任务管理**", ""]
    lines.extend(_render_full_schedule_category(CATEGORY_TARGET, grouped[CATEGORY_TARGET]))
    lines.append("")
    lines.extend(_render_full_schedule_category(CATEGORY_DAILY, grouped[CATEGORY_DAILY]))
    lines.append("")
    lines.extend(_render_maintenance_schedule_category(grouped[CATEGORY_MAINTENANCE]))
    return "\n".join(lines)


def _render_full_schedule_category(category: str, rows: list[ScheduleTableRow]) -> list[str]:
    lines = [
        f"【{category}】",
        "| 任务 | 优先级 | 预计 | 状态 | 用时 |",
        "|---|---|---|---|---|",
    ]
    for row in rows:
        lines.append(f"| {row.task} | {row.priority} | {row.estimate} | {row.status} | {row.duration} |")
    return lines


def _render_maintenance_schedule_category(rows: list[ScheduleTableRow]) -> list[str]:
    lines = [
        f"【{CATEGORY_MAINTENANCE}】",
        "| 任务 | 优先级 | 状态 |",
        "|---|---|---|",
    ]
    for row in rows:
        lines.append(f"| {row.task} | {row.priority} | {row.status} |")
    return lines


def _new_table_row(schedule_row: ScheduleRow) -> ScheduleTableRow:
    category = _normalize_schedule_category(schedule_row.category)
    estimate = "" if category == CATEGORY_MAINTENANCE else schedule_row.estimate
    return ScheduleTableRow(
        task=schedule_row.task,
        priority=schedule_row.priority,
        estimate=estimate,
        status="",
        duration="",
        category=category,
    )


def _schedule_area_range(lines: list[str], blocks: list[ScheduleTableBlock]) -> tuple[int, int]:
    start = blocks[0].start_line
    while start > 0:
        previous = lines[start - 1].strip()
        if not previous or _is_task_management_heading(previous) or _parse_schedule_category(previous) is not None:
            start -= 1
            continue
        break
    end = blocks[-1].end_line
    while end < len(lines) and not lines[end].strip():
        end += 1
    return start, end


def _category_hint_before_table(lines: list[str], table_start: int) -> str | None:
    index = table_start - 1
    while index >= 0 and not lines[index].strip():
        index -= 1
    if index < 0:
        return None
    return _parse_schedule_category(lines[index].strip())


def _normalize_table_headers(headers: tuple[str, ...]) -> tuple[str, ...] | None:
    if headers == SCHEDULE_HEADERS:
        return SCHEDULE_HEADERS
    if headers == MAINTENANCE_HEADERS:
        return MAINTENANCE_HEADERS
    if headers == LEGACY_MAINTENANCE_HEADERS:
        return MAINTENANCE_HEADERS
    return None


def _resolve_schedule_category(category_hint: str | None, task: str) -> str:
    if category_hint in SCHEDULE_CATEGORIES:
        return category_hint
    return _classify_schedule_category(category_hint or "", task)


def _classify_schedule_category(heading: str, task: str) -> str:
    normalized_heading = _normalize_schedule_category(heading)
    if normalized_heading == CATEGORY_DAILY:
        return CATEGORY_DAILY
    if _is_maintenance_task(task, normalized_heading):
        return CATEGORY_MAINTENANCE
    return CATEGORY_TARGET


def _normalize_schedule_category(text: str) -> str:
    normalized = text.strip().strip("*").strip("#").strip().casefold()
    if normalized == CATEGORY_DAILY:
        return CATEGORY_DAILY
    if normalized in {CATEGORY_MAINTENANCE, "修身炉维护"}:
        return CATEGORY_MAINTENANCE
    if normalized == CATEGORY_TARGET:
        return CATEGORY_TARGET
    return normalized


def _is_maintenance_task(task: str, heading: str) -> bool:
    heading_key = heading.casefold()
    task_key = task.casefold()
    if heading_key not in MAINTENANCE_HEADINGS and CATEGORY_MAINTENANCE not in heading_key:
        return False
    return any(keyword.casefold() in task_key for keyword in MAINTENANCE_KEYWORDS)


def _parse_schedule_category(line: str) -> str | None:
    text = line.strip().strip("*").strip("#").strip()
    if len(text) > 2 and text.startswith("【") and text.endswith("】"):
        normalized = _normalize_schedule_category(text[1:-1])
        if normalized in SCHEDULE_CATEGORIES:
            return normalized
    return None


def _is_task_management_heading(line: str) -> bool:
    return line.strip().strip("*").strip("#").strip() == "任务管理"


def _normalize_duration_cell(text: str) -> str:
    from app.pipelines.log_schedule_update import _format_duration, _parse_duration_seconds

    duration_seconds = _parse_duration_seconds(text)
    if duration_seconds is None:
        return ""
    return _format_duration(duration_seconds)


def _is_table_line(line: str) -> bool:
    stripped = line.strip()
    return stripped.startswith("|") and stripped.endswith("|")


def _split_table_row(line: str) -> list[str]:
    normalized = line.replace("｜", "|")
    return [cell.strip() for cell in normalized.strip().strip("|").split("|")]


def _is_separator_cell(cell: str) -> bool:
    text = cell.replace(" ", "")
    if len(text) < 3:
        return False
    return set(text) <= {"-", ":"} and "-" in text


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
    return f"""你是个人执行管理助手，只做“日内新增任务”的局部更新。

你的任务：
1. 把“新增任务”逐字插入 today_tasks.md，不能改写、概括或扩写。
2. 同步更新 daily 里的“今日待办”小节，只更新待办快照，不重写原有计划建议。
3. 为任务管理表提供一行新增任务的任务名、优先级和预计耗时；状态由程序写空；`xiushenlu维护` 表不会使用预计和用时。

日期：{date_text}

新增任务：
{new_task}

长期目标：
{goals_text}

当前 today_tasks.md：
{tasks_text}

当前 daily：
{daily_plan_text}

硬性规则：
- 如果新增任务是“标题：任务正文”格式，冒号前是目标分组标题，冒号后是要插入的任务正文；用这个标题去匹配已有分组或新建分组，不要把“标题：”当成任务正文。
- 新增任务正文必须逐字使用，不要改成短标题、括号解释或“灵感：优化...”这类摘要。
- 保留已有内容的原文、顺序和中文分组，但分组展示必须统一为“【分组】”，分组下统一使用“1. 任务”编号列表。
- 如果目标分组已存在，任务插入到该分组；如果目标分组不存在，新建“【目标分组】”。
- `updated_today_tasks` 是完整的新 today_tasks.md；可以保留原有 `# 今日待办` 标题，但只允许增加这一个新增任务，除必要编号外不要改动旧内容。
- `updated_daily_original` 只填写 daily “今日待办”小节的新内容，并包含逐字新增任务正文；不要包含计划建议、时间安排表或任何 Markdown 标题行。
- daily “今日待办”小节和 today_tasks.md 的最终样式都必须类似：“【杂事】”下一行“1. 游泳”；不要输出“杂事：\n游泳”或“杂事：游泳”。
- `target_heading` 只用于内部归类，不会展示在 daily；今日待办中的“日常”归入任务管理 `【日常】`，修身炉 / xiushenlu 项目的修 bug、修复、优化、维护类任务归入 `【xiushenlu维护】`，其他任务归入 `【目标】`。
- `schedule_task` 是写入任务管理表“任务”列的任务正文，必须逐字等于新增任务正文；如果新增任务是“标题：任务正文”格式，只填冒号后的任务正文；不能缩短、概括或扩写，不能包含换行或 `|`。
- `schedule_priority` 是“优先级”列，例如 P0、P1、P2、P3，不能包含换行或 `|`。
- `schedule_estimate` 是“预计”列，例如 30m、1h、1.5h，不能包含换行或 `|`；如果新增任务属于 `xiushenlu维护`，程序会忽略这个值。
- 不要输出状态、用时、单独建议正文、“新任务”标题或“### 新增”。

插入格式示例：
- 如果“修身炉：”下面已有“1.”到“5.”，新增“灵感：优化codex规则”应写成“【修身炉】”分组下的“6. 灵感：优化codex规则”。
- 如果“杂事：”下面已有“游泳或篮球”“扫地拖地”，新增“看鸡汤和英雄传记（调节今日心情）”应写成“【杂事】”分组下的下一条编号。
- 如果新增“杂事： 游泳”，必须新建或使用“【杂事】”并在下面写“1. 游泳”。

你必须只输出一个严格 JSON 对象，不要使用代码块，不要输出解释文字。
JSON 必须包含且只需要包含这些字符串字段：
- updated_today_tasks：字符串，完整的新 today_tasks.md。
- updated_daily_original：字符串，daily “今日待办”小节的新内容，不含标题和时间安排表。
- target_heading：字符串，新增任务最终归入的小标题名称，不要带序号。
- schedule_task：字符串，时间安排表新增行的任务列。
- schedule_priority：字符串，时间安排表新增行的优先级列。
- schedule_estimate：字符串，时间安排表新增行的预计列。
"""
