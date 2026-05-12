from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Callable

from app.config import load_config
from app.daily import daily_path, read_daily
from app.llm.provider import LLMProvider
from app.llm.usage import append_llm_call_event
from app.logger import EventLogger
from app.safety import safe_write_text


EXPECTED_HEADERS = ("任务", "优先级", "预计", "状态", "备注")
LEGACY_HEADERS = ("任务", "优先级", "预估时间", "完成", "备注")
IN_PROGRESS_MARK = "○"
CHECK_MARK = "✓"
DROPPED_MARK = "×"
NOTE_MAX_CHARS = 80
STATUS_TO_MARK = {
    "not_started": "",
    "in_progress": IN_PROGRESS_MARK,
    "completed": CHECK_MARK,
    "dropped": DROPPED_MARK,
}
VALID_COMPLETION_MARKS = frozenset(STATUS_TO_MARK.values())
VALID_STATUS_NAMES = ", ".join(STATUS_TO_MARK)


class LogScheduleUpdateParseError(ValueError):
    """Raised when the LLM schedule patch is not safe to apply."""


@dataclass(frozen=True)
class SchedulePatch:
    row_index: int
    completion_mark: str
    note: str
    evidence: str


@dataclass(frozen=True)
class ParsedSchedulePatch:
    updates: tuple[SchedulePatch, ...]


@dataclass(frozen=True)
class ScheduleTable:
    start_line: int
    end_line: int
    rows: tuple[tuple[str, str, str, str, str], ...]

    def to_markdown(self) -> str:
        return _render_table(self.rows)


@dataclass(frozen=True)
class LogScheduleUpdateResult:
    date: str
    path: Path
    updated: bool
    updates_count: int = 0
    reason: str = ""


def update_schedule_from_log(
    provider: LLMProvider,
    record_content: str,
    config: dict[str, Any] | None = None,
    target_date: date | None = None,
    logger: EventLogger | None = None,
    cancel_check: Callable[[], None] | None = None,
) -> LogScheduleUpdateResult:
    cfg = config or load_config()
    current_date = target_date or date.today()
    date_text = current_date.isoformat()
    path = daily_path(cfg, date_text)
    daily_text = read_daily(cfg, date_text)

    try:
        table = find_schedule_table(daily_text)
    except LogScheduleUpdateParseError as exc:
        return LogScheduleUpdateResult(date=date_text, path=path, updated=False, reason=str(exc))
    if table is None:
        return LogScheduleUpdateResult(date=date_text, path=path, updated=False, reason="no_schedule_table")

    prompt = build_schedule_patch_prompt(date_text, record_content, table.to_markdown())
    raw_reply = provider.chat(prompt).strip()
    if cancel_check is not None:
        cancel_check()

    event_logger = logger or EventLogger(config=cfg)
    append_llm_call_event(event_logger, provider, "log_schedule_update")

    try:
        parsed = parse_schedule_patch_response(raw_reply, record_content=record_content)
        if not parsed.updates:
            return LogScheduleUpdateResult(date=date_text, path=path, updated=False, reason="no_updates")
        updated_daily, updates_count = apply_schedule_patch(daily_text, parsed)
    except LogScheduleUpdateParseError as exc:
        return LogScheduleUpdateResult(date=date_text, path=path, updated=False, reason=str(exc))

    if updated_daily == daily_text:
        return LogScheduleUpdateResult(date=date_text, path=path, updated=False, reason="no_changes")

    safe_write_text(path, updated_daily, cfg)
    event_logger.append_event(
        "schedule_updated_from_log",
        f"根据记录更新 {date_text} 的时间安排表",
        {
            "date": date_text,
            "daily_path": str(path),
            "updates_count": updates_count,
        },
    )
    return LogScheduleUpdateResult(
        date=date_text,
        path=path,
        updated=True,
        updates_count=updates_count,
    )


def find_schedule_table(daily_text: str) -> ScheduleTable | None:
    lines = daily_text.splitlines()
    plan_start, plan_end = _find_level2_section_lines(lines, "计划")
    if plan_start is None:
        return None

    for index in range(plan_start, plan_end):
        if not _is_table_line(lines[index]):
            continue

        table_start = index
        table_end = table_start
        while table_end < plan_end and _is_table_line(lines[table_end]):
            table_end += 1

        table_lines = lines[table_start:table_end]
        rows = _parse_table_lines(table_lines)
        return ScheduleTable(start_line=table_start, end_line=table_end, rows=tuple(rows))

    return None


def parse_schedule_patch_response(text: str, *, record_content: str) -> ParsedSchedulePatch:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise LogScheduleUpdateParseError(f"LLM response is not valid JSON: {exc}") from exc

    if not isinstance(data, dict):
        raise LogScheduleUpdateParseError("LLM response must be a JSON object.")

    updates = data.get("updates")
    if not isinstance(updates, list):
        raise LogScheduleUpdateParseError("LLM response must contain an updates list.")

    parsed: list[SchedulePatch] = []
    for item in updates:
        if not isinstance(item, dict):
            raise LogScheduleUpdateParseError("Each update must be a JSON object.")

        row_index = item.get("row_index")
        completion_mark = _parse_completion_mark(item)
        note = item.get("note", "")
        evidence = item.get("evidence", "")

        if not isinstance(row_index, int):
            raise LogScheduleUpdateParseError("row_index must be an integer.")
        if not isinstance(note, str):
            raise LogScheduleUpdateParseError("note must be a string.")
        if not isinstance(evidence, str):
            raise LogScheduleUpdateParseError("evidence must be a string.")

        note_text = note.strip()
        evidence_text = evidence.strip()
        _validate_note(note_text, evidence_text, record_content)
        parsed.append(
            SchedulePatch(
                row_index=row_index,
                completion_mark=completion_mark,
                note=note_text,
                evidence=evidence_text,
            )
        )

    return ParsedSchedulePatch(updates=tuple(parsed))


def apply_schedule_patch(daily_text: str, parsed: ParsedSchedulePatch) -> tuple[str, int]:
    if not parsed.updates:
        return daily_text, 0

    lines = daily_text.splitlines()
    table = find_schedule_table(daily_text)
    if table is None:
        raise LogScheduleUpdateParseError("schedule table not found.")

    rows = [list(row) for row in table.rows]
    for update in parsed.updates:
        if update.row_index < 1 or update.row_index > len(rows):
            raise LogScheduleUpdateParseError("row_index is out of range.")
        row = rows[update.row_index - 1]
        row[3] = update.completion_mark
        row[4] = update.note

    new_table_lines = _render_table(tuple(tuple(row) for row in rows)).splitlines()
    new_lines = lines[: table.start_line] + new_table_lines + lines[table.end_line :]
    updated = "\n".join(new_lines).rstrip() + "\n"
    return updated, len(parsed.updates)


def build_schedule_patch_prompt(date_text: str, record_content: str, schedule_table: str) -> str:
    record_text = record_content.strip()
    return f"""你是个人执行管理助手，只根据“本次写入记录”更新当天时间安排表的状态列。

任务：根据本次记录返回 log_schedule_updates JSON。

日期：{date_text}

当前时间安排表：
{schedule_table}

本次写入记录：
{record_text}

硬性规则：
- 你只能判断已有任务行的“状态”和“备注”是否需要变化，不要输出整张表。
- row_index 使用 1-based 行号，第一条任务行是 1。
- status 使用四种值：not_started 表示“状态”列清空，in_progress 表示写为“{IN_PROGRESS_MARK}”，completed 表示写为“{CHECK_MARK}”，dropped 表示写为“{DROPPED_MARK}”。
- 记录没有提到任务开始或推进，使用 not_started 或保持原状态不输出更新；记录提到开始做、正在做、推进中、完成了一部分但任务还会继续，使用 in_progress；记录明确完成，使用 completed。
- 记录说删除这个任务、取消、不再追踪、不再纳入今天任务、短期内不做或这个任务今天不管了，使用 dropped；dropped 表示任务退出追踪，不等同于 completed。
- note 是要写入“备注”列的短句；只有本次记录里明确出现“后续计划和执行要注意的点”时才写，普通完成事实不要写备注。
- note 可以用于新增、更新或清空备注；没有要注意的点时必须是空字符串。
- 非空 note 必须有 evidence，且 evidence 必须是本次写入记录中的原文片段。
- 不要新增任务行，不要删除任务行，不要改任务名、优先级、预计或行顺序。
- note 最长 {NOTE_MAX_CHARS} 字，不能包含换行或 |。

你必须只输出一个严格 JSON 对象，不要使用代码块，不要输出解释文字。
JSON 格式固定为：
{{"updates":[{{"row_index":1,"status":"in_progress","note":"","evidence":""}}]}}
如果没有任何任务行需要变化，输出 {{"updates":[]}}。
"""


def _parse_table_lines(table_lines: list[str]) -> list[tuple[str, str, str, str, str]]:
    if len(table_lines) < 2:
        raise LogScheduleUpdateParseError("schedule table must contain header and separator.")

    headers = _split_table_row(table_lines[0])
    if tuple(headers) not in (EXPECTED_HEADERS, LEGACY_HEADERS):
        raise LogScheduleUpdateParseError("schedule table header is not the expected five columns.")

    separator = _split_table_row(table_lines[1])
    if len(separator) != len(EXPECTED_HEADERS) or not all(_is_separator_cell(cell) for cell in separator):
        raise LogScheduleUpdateParseError("schedule table separator is invalid.")

    rows: list[tuple[str, str, str, str, str]] = []
    for line in table_lines[2:]:
        cells = _split_table_row(line)
        if len(cells) != len(EXPECTED_HEADERS):
            raise LogScheduleUpdateParseError("schedule table row does not match the expected columns.")
        if cells[3] not in VALID_COMPLETION_MARKS:
            raise LogScheduleUpdateParseError("status column must be empty, in progress, completed, or dropped.")
        if "\n" in cells[4] or "|" in cells[4]:
            raise LogScheduleUpdateParseError("note column contains unsafe content.")
        rows.append(tuple(cells))  # type: ignore[arg-type]
    return rows


def _parse_completion_mark(item: dict[str, Any]) -> str:
    status = item.get("status")
    if isinstance(status, str):
        status_text = status.strip()
        if status_text not in STATUS_TO_MARK:
            raise LogScheduleUpdateParseError(
                f"status must be one of: {VALID_STATUS_NAMES}."
            )
        return STATUS_TO_MARK[status_text]
    if status is not None:
        raise LogScheduleUpdateParseError("status must be a string.")

    completed = item.get("completed")
    if isinstance(completed, bool):
        return CHECK_MARK if completed else ""
    raise LogScheduleUpdateParseError("status must be provided, or completed must be a boolean.")


def _validate_note(note: str, evidence: str, record_content: str) -> None:
    if len(note) > NOTE_MAX_CHARS:
        raise LogScheduleUpdateParseError(f"note must be no longer than {NOTE_MAX_CHARS} characters.")
    if "\n" in note or "\r" in note or "|" in note:
        raise LogScheduleUpdateParseError("note must not contain line breaks or table separators.")
    if not note:
        return
    if not evidence:
        raise LogScheduleUpdateParseError("non-empty note must include evidence.")
    if evidence not in record_content:
        raise LogScheduleUpdateParseError("note evidence must be an exact substring of the record.")


def _render_table(rows: tuple[tuple[str, str, str, str, str], ...]) -> str:
    lines = [
        "| 任务 | 优先级 | 预计 | 状态 | 备注 |",
        "|---|---|---|---|---|",
    ]
    for row in rows:
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines)


def _find_level2_section_lines(lines: list[str], title: str) -> tuple[int | None, int]:
    heading = f"## {title}"
    start: int | None = None
    for index, line in enumerate(lines):
        if line.strip() == heading:
            start = index + 1
            break
    if start is None:
        return None, len(lines)

    end = len(lines)
    for index in range(start, len(lines)):
        if lines[index].startswith("## "):
            end = index
            break
    return start, end


def _is_table_line(line: str) -> bool:
    stripped = line.strip()
    return stripped.startswith("|") and stripped.endswith("|")


def _split_table_row(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def _is_separator_cell(cell: str) -> bool:
    text = cell.replace(" ", "")
    if len(text) < 3:
        return False
    return set(text) <= {"-", ":"} and "-" in text
