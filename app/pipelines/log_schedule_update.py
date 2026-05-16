from __future__ import annotations

import json
import re
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


CURRENT_HEADERS = ("任务", "优先级", "预计", "状态", "用时")
IN_PROGRESS_MARK = "○"
CHECK_MARK = "✓"
DROPPED_MARK = "×"
STATUS_KEEP = "keep"
STATUS_TO_MARK: dict[str, str | None] = {
    STATUS_KEEP: None,
    "not_started": "",
    "in_progress": IN_PROGRESS_MARK,
    "completed": CHECK_MARK,
    "dropped": DROPPED_MARK,
}
VALID_COMPLETION_MARKS = frozenset(mark for mark in STATUS_TO_MARK.values() if mark is not None)
VALID_STATUS_NAMES = ", ".join(STATUS_TO_MARK)

RECORD_START_RE = re.compile(r"^-\s*(?P<time>\d{2}:\d{2}:\d{2})\s*(?P<content>.*)$")
DURATION_TOKEN_RE = re.compile(
    r"(?P<value>\d+(?:\.\d+)?)\s*"
    r"(?P<unit>小时|小時|时|時|hours?|hrs?|h|分钟|分鐘|分|min(?:ute)?s?|m)",
    flags=re.IGNORECASE,
)
CLOCK_TIME_RE = re.compile(r"^(?P<hour>\d{1,2}):(?P<minute>\d{2})(?::(?P<second>\d{2}))?$")


class LogScheduleUpdateParseError(ValueError):
    """Raised when the LLM schedule patch is not safe to apply."""


@dataclass(frozen=True)
class DailyRecord:
    index: int
    timestamp: str
    content: str


@dataclass(frozen=True)
class TimingEntry:
    entry_type: str
    start_record: int | None = None
    end_record: int | None = None
    record: int | None = None
    duration_seconds: int | None = None


@dataclass(frozen=True)
class SchedulePatch:
    row_index: int
    completion_mark: str | None
    evidence: str
    timing_entries: tuple[TimingEntry, ...] | None = None


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

    records = extract_daily_records(daily_text)
    prompt = build_schedule_patch_prompt(date_text, record_content, table.to_markdown(), records)
    raw_reply = provider.chat(prompt).strip()
    if cancel_check is not None:
        cancel_check()

    event_logger = logger or EventLogger(config=cfg)
    append_llm_call_event(event_logger, provider, "log_schedule_update")

    try:
        parsed = parse_schedule_patch_response(raw_reply, record_content=record_content, records=records)
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
        f"根据记录更新 {date_text} 的任务管理表",
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


def extract_daily_records(daily_text: str) -> tuple[DailyRecord, ...]:
    records_section = _extract_level2_section(daily_text, "记录")
    records: list[DailyRecord] = []
    current_time: str | None = None
    current_lines: list[str] = []

    def flush_current() -> None:
        nonlocal current_time, current_lines
        if current_time is None:
            return
        content = "\n".join(line.rstrip() for line in current_lines).strip()
        records.append(DailyRecord(index=len(records) + 1, timestamp=current_time, content=content))
        current_time = None
        current_lines = []

    for line in records_section.splitlines():
        match = RECORD_START_RE.match(line.strip())
        if match:
            flush_current()
            current_time = match.group("time")
            current_lines = [match.group("content").strip()]
            continue
        if current_time is not None:
            current_lines.append(line[2:] if line.startswith("  ") else line)

    flush_current()
    return tuple(records)


def parse_schedule_patch_response(
    text: str,
    *,
    record_content: str,
    records: tuple[DailyRecord, ...] = (),
) -> ParsedSchedulePatch:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise LogScheduleUpdateParseError(f"LLM response is not valid JSON: {exc}") from exc

    if not isinstance(data, dict):
        raise LogScheduleUpdateParseError("LLM response must be a JSON object.")

    updates = data.get("updates")
    if not isinstance(updates, list):
        raise LogScheduleUpdateParseError("LLM response must contain an updates list.")

    record_text = "\n".join(record.content for record in records) or record_content
    parsed: list[SchedulePatch] = []
    for item in updates:
        if not isinstance(item, dict):
            raise LogScheduleUpdateParseError("Each update must be a JSON object.")

        row_index = item.get("row_index")
        completion_mark = _parse_completion_mark(item)
        evidence = item.get("evidence", "")
        timing_entries = _parse_timing_entries(item.get("time_entries"), records)

        if not isinstance(row_index, int):
            raise LogScheduleUpdateParseError("row_index must be an integer.")
        if not isinstance(evidence, str):
            raise LogScheduleUpdateParseError("evidence must be a string.")

        evidence_text = evidence.strip()
        if evidence_text and evidence_text not in record_text:
            raise LogScheduleUpdateParseError("evidence must be an exact substring of a daily record.")

        parsed.append(
            SchedulePatch(
                row_index=row_index,
                completion_mark=completion_mark,
                evidence=evidence_text,
                timing_entries=timing_entries,
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

    records = extract_daily_records(daily_text)
    rows = [list(row) for row in table.rows]
    for update in parsed.updates:
        if update.row_index < 1 or update.row_index > len(rows):
            raise LogScheduleUpdateParseError("row_index is out of range.")
        row = rows[update.row_index - 1]
        if update.completion_mark is not None:
            row[3] = update.completion_mark
        if update.timing_entries is not None:
            total_seconds = _calculate_total_duration_seconds(update.timing_entries, records)
            row[4] = _format_duration(total_seconds)

    new_table_lines = _render_table(tuple(tuple(row) for row in rows)).splitlines()
    new_lines = lines[: table.start_line] + new_table_lines + lines[table.end_line :]
    updated = "\n".join(new_lines).rstrip() + "\n"
    return updated, len(parsed.updates)


def build_schedule_patch_prompt(
    date_text: str,
    record_content: str,
    schedule_table: str,
    records: tuple[DailyRecord, ...] = (),
) -> str:
    record_text = record_content.strip()
    records_text = _format_records_for_prompt(records)
    return f"""你是个人执行管理助手，只根据 daily 记录更新当天任务管理表的状态列和用时列。

任务：根据本次记录返回 log_schedule_updates JSON。

日期：{date_text}

当前任务管理表：
{schedule_table}

本次写入记录：
{record_text}

当天全部记录（用于重新计算相关任务的总用时）：
{records_text}

硬性规则：
- 你只能判断已有任务行的“状态”和“用时”是否需要变化，不要输出整张表。
- row_index 使用 1-based 行号，第一条任务行是 1。
- status 使用五种值：keep 表示状态列不变，not_started 表示状态列清空，in_progress 表示写为“{IN_PROGRESS_MARK}”，completed 表示写为“{CHECK_MARK}”，dropped 表示写为“{DROPPED_MARK}”。
- 记录没有提到任务开始或推进时，用 keep 或不输出更新；记录提到开始做、正在做、推进中、完成了一部分但任务还会继续，使用 in_progress；记录明确完成，使用 completed。
- 记录说删除这个任务、取消、不再追踪、不再纳入今天任务、短期内不做或这个任务今天不管了，使用 dropped；dropped 表示任务退出追踪，不等同于 completed。
- 用时列是派生值：每次都从当天全部记录重新找出该任务相关的完整用时条目，不要把旧表格里的用时当作累计变量。
- 如果记录明确写了“用时/耗时/花了”等直接用时，使用 direct_duration，record 填这条记录 ID，duration 原样提取。
- 如果两条记录形成明确的开始/结束区间，使用 record_interval；start_record 填开始记录 ID，end_record 填结束记录 ID。
- time_entries 可以包含多个 direct_duration 和多个 record_interval；程序会累加明确用时和闭合区间，并按 direct_duration 的 record 端点去重。
- 无法判断是开始或结束的记录不要作为 record_interval 边界；开放中的开始记录不要计入用时。
- evidence 如果填写，必须是当天记录里的原文片段；没有可靠证据时用空字符串。
- 不要新增任务行，不要删除任务行，不要改任务名、优先级、预计或行顺序。

你必须只输出一个严格 JSON 对象，不要使用代码块，不要输出解释文字。
JSON 格式固定为：
{{"updates":[{{"row_index":1,"status":"completed","evidence":"完成原文","time_entries":[{{"type":"record_interval","start_record":2,"end_record":5}},{{"type":"direct_duration","record":6,"duration":"40m"}}]}}]}}
如果只需要改状态且不改用时，可以省略 time_entries。
如果只需要重算用时且状态不变，status 使用 keep。
如果没有任何任务行需要变化，输出 {{"updates":[]}}。
"""


def _parse_table_lines(table_lines: list[str]) -> list[tuple[str, str, str, str, str]]:
    if len(table_lines) < 2:
        raise LogScheduleUpdateParseError("schedule table must contain header and separator.")

    headers = _split_table_row(table_lines[0])
    if tuple(headers) != CURRENT_HEADERS:
        raise LogScheduleUpdateParseError("schedule table header is not the expected five columns.")

    separator = _split_table_row(table_lines[1])
    if len(separator) != len(CURRENT_HEADERS) or not all(_is_separator_cell(cell) for cell in separator):
        raise LogScheduleUpdateParseError("schedule table separator is invalid.")

    rows: list[tuple[str, str, str, str, str]] = []
    for line in table_lines[2:]:
        cells = _split_table_row(line)
        if len(cells) != len(CURRENT_HEADERS):
            raise LogScheduleUpdateParseError("schedule table row does not match the expected columns.")
        if cells[3] not in VALID_COMPLETION_MARKS:
            raise LogScheduleUpdateParseError("status column must be empty, in progress, completed, or dropped.")
        if "\n" in cells[4] or "|" in cells[4]:
            raise LogScheduleUpdateParseError("duration column contains unsafe content.")
        cells[4] = _normalize_duration_cell(cells[4])
        rows.append(tuple(cells))  # type: ignore[arg-type]
    return rows


def _parse_completion_mark(item: dict[str, Any]) -> str | None:
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
    return None


def _parse_timing_entries(
    value: Any,
    records: tuple[DailyRecord, ...],
) -> tuple[TimingEntry, ...] | None:
    if value is None:
        return None
    if not isinstance(value, list):
        raise LogScheduleUpdateParseError("time_entries must be a list.")

    entries: list[TimingEntry] = []
    for item in value:
        if not isinstance(item, dict):
            raise LogScheduleUpdateParseError("Each time entry must be a JSON object.")
        entry_type = item.get("type") or item.get("kind")
        if not isinstance(entry_type, str):
            raise LogScheduleUpdateParseError("time entry type must be a string.")
        entry_type = entry_type.strip()

        if entry_type == "record_interval":
            start_record = _parse_record_index(item.get("start_record"), records, "start_record")
            end_record = _parse_record_index(item.get("end_record"), records, "end_record")
            if end_record <= start_record:
                raise LogScheduleUpdateParseError("end_record must be greater than start_record.")
            entries.append(TimingEntry(entry_type=entry_type, start_record=start_record, end_record=end_record))
        elif entry_type == "direct_duration":
            record = _parse_record_index(item.get("record"), records, "record")
            duration = item.get("duration")
            if not isinstance(duration, str):
                raise LogScheduleUpdateParseError("direct duration must be a string.")
            duration_seconds = _parse_duration_seconds(duration)
            if duration_seconds is None or duration_seconds <= 0:
                raise LogScheduleUpdateParseError("direct duration is not parseable.")
            entries.append(
                TimingEntry(entry_type=entry_type, record=record, duration_seconds=duration_seconds)
            )
        else:
            raise LogScheduleUpdateParseError("time entry type must be record_interval or direct_duration.")

    return tuple(entries)


def _parse_record_index(value: Any, records: tuple[DailyRecord, ...], field_name: str) -> int:
    if not isinstance(value, int):
        raise LogScheduleUpdateParseError(f"{field_name} must be an integer.")
    if value < 1 or value > len(records):
        raise LogScheduleUpdateParseError(f"{field_name} is out of range.")
    return value


def _calculate_total_duration_seconds(
    timing_entries: tuple[TimingEntry, ...],
    records: tuple[DailyRecord, ...],
) -> int:
    records_by_index = {record.index: record for record in records}
    direct_entries = [entry for entry in timing_entries if entry.entry_type == "direct_duration"]
    direct_records = {entry.record for entry in direct_entries if entry.record is not None}
    total = sum(entry.duration_seconds or 0 for entry in direct_entries)

    seen_intervals: set[tuple[int, int]] = set()
    intervals: list[tuple[int, int]] = []
    for entry in timing_entries:
        if entry.entry_type != "record_interval":
            continue
        if entry.start_record is None or entry.end_record is None:
            continue
        if entry.start_record in direct_records or entry.end_record in direct_records:
            continue
        key = (entry.start_record, entry.end_record)
        if key in seen_intervals:
            continue
        seen_intervals.add(key)
        intervals.append(key)

    previous_end: int | None = None
    for start_record, end_record in sorted(intervals):
        if previous_end is not None and start_record < previous_end:
            raise LogScheduleUpdateParseError("record intervals overlap.")
        previous_end = end_record if previous_end is None else max(previous_end, end_record)

        start = records_by_index[start_record]
        end = records_by_index[end_record]
        total += _seconds_between_clock_times(start.timestamp, end.timestamp)

    return total


def _parse_duration_seconds(text: str) -> int | None:
    duration = text.strip()
    if not duration:
        return None
    if duration == "<1m":
        return 30

    total = 0.0
    matched = False
    for match in DURATION_TOKEN_RE.finditer(duration):
        matched = True
        value = float(match.group("value"))
        unit = match.group("unit").lower()
        if unit in {"小时", "小時", "时", "時", "h", "hr", "hrs", "hour", "hours"}:
            total += value * 3600
        else:
            total += value * 60

    if matched:
        return int(round(total))
    return None


def _normalize_duration_cell(text: str) -> str:
    duration_seconds = _parse_duration_seconds(text)
    if duration_seconds is None:
        return ""
    return _format_duration(duration_seconds)


def _format_duration(seconds: int) -> str:
    if seconds <= 0:
        return ""
    if seconds < 60:
        return "<1m"

    minutes = max(1, int(round(seconds / 60)))
    hours, rest_minutes = divmod(minutes, 60)
    if hours and rest_minutes:
        return f"{hours}h{rest_minutes}m"
    if hours:
        return f"{hours}h"
    return f"{rest_minutes}m"


def _seconds_between_clock_times(start_text: str, end_text: str) -> int:
    start = _parse_clock_seconds(start_text)
    end = _parse_clock_seconds(end_text)
    if end < start:
        end += 24 * 60 * 60
    return end - start


def _parse_clock_seconds(text: str) -> int:
    match = CLOCK_TIME_RE.match(text.strip())
    if match is None:
        raise LogScheduleUpdateParseError("clock time must be HH:MM or HH:MM:SS.")
    hour = int(match.group("hour"))
    minute = int(match.group("minute"))
    second = int(match.group("second") or 0)
    if hour > 23 or minute > 59 or second > 59:
        raise LogScheduleUpdateParseError("clock time is out of range.")
    return hour * 3600 + minute * 60 + second


def _render_table(rows: tuple[tuple[str, str, str, str, str], ...]) -> str:
    lines = [
        "| 任务 | 优先级 | 预计 | 状态 | 用时 |",
        "|---|---|---|---|---|",
    ]
    for row in rows:
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines)


def _format_records_for_prompt(records: tuple[DailyRecord, ...]) -> str:
    if not records:
        return "（今天还没有记录）"
    return "\n".join(
        f"[{record.index}] {record.timestamp} {record.content}"
        for record in records
    )


def _extract_level2_section(text: str, title: str) -> str:
    lines = text.splitlines(keepends=True)
    start, end = _find_level2_section_lines([line.rstrip("\n") for line in lines], title)
    if start is None:
        return ""
    return "".join(lines[start:end]).strip()


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
    normalized = line.replace("｜", "|")
    return [cell.strip() for cell in normalized.strip().strip("|").split("|")]


def _is_separator_cell(cell: str) -> bool:
    text = cell.replace(" ", "")
    if len(text) < 3:
        return False
    return set(text) <= {"-", ":"} and "-" in text
