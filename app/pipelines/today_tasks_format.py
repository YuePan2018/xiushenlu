from __future__ import annotations

import re
from dataclasses import dataclass, field


EMPTY_TODAY_TASKS_PLACEHOLDER = "（尚未填写今日待办）"
DEFAULT_HEADING = "待办"


@dataclass
class TaskSection:
    heading: str
    items: list[str] = field(default_factory=list)


def format_today_tasks_snapshot(tasks: str) -> str:
    text = tasks.strip()
    if not text or text == EMPTY_TODAY_TASKS_PLACEHOLDER:
        return EMPTY_TODAY_TASKS_PLACEHOLDER

    sections = _parse_sections(text)
    if not sections:
        return EMPTY_TODAY_TASKS_PLACEHOLDER

    blocks: list[str] = []
    for section in sections:
        if not section.items:
            continue
        block_lines = [f"【{section.heading}】"]
        block_lines.extend(f"{index}. {item}" for index, item in enumerate(section.items, start=1))
        blocks.append("\n".join(block_lines))
    return "\n\n".join(blocks) if blocks else EMPTY_TODAY_TASKS_PLACEHOLDER


def _parse_sections(text: str) -> list[TaskSection]:
    sections: list[TaskSection] = []
    current: TaskSection | None = None
    previous_blank = True

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            previous_blank = True
            continue
        if _is_today_tasks_title(line):
            previous_blank = True
            continue

        heading = _parse_heading_line(line)
        if heading is not None:
            current = _get_or_create_section(sections, heading)
            previous_blank = False
            continue

        inline_heading = _parse_inline_heading(line)
        if inline_heading is not None and (current is None or previous_blank):
            heading_text, item_text = inline_heading
            current = _get_or_create_section(sections, heading_text)
            _append_item(current, item_text)
            previous_blank = False
            continue

        if current is None:
            current = _get_or_create_section(sections, DEFAULT_HEADING)
        _append_item(current, line)
        previous_blank = False

    return sections


def _get_or_create_section(sections: list[TaskSection], heading: str) -> TaskSection:
    normalized = _normalize_heading(heading)
    for section in sections:
        if section.heading == normalized:
            return section
    section = TaskSection(normalized)
    sections.append(section)
    return section


def _append_item(section: TaskSection, item: str) -> None:
    normalized = _normalize_item(item)
    if normalized:
        section.items.append(normalized)


def _is_today_tasks_title(line: str) -> bool:
    text = line.strip().strip("*").strip("#").strip()
    return text in {"今日待办", "今日待办原文"}


def _parse_heading_line(line: str) -> str | None:
    stripped = line.strip()
    if len(stripped) > 2 and stripped.startswith("【") and stripped.endswith("】"):
        return stripped[1:-1].strip()
    if stripped.endswith(("：", ":")):
        return stripped[:-1].strip()
    return None


def _parse_inline_heading(line: str) -> tuple[str, str] | None:
    match = re.match(r"^([^：:\n]{1,30})[：:]\s*(.+)$", line, flags=re.DOTALL)
    if match is None:
        return None
    heading = _normalize_heading(match.group(1))
    item = _normalize_item(match.group(2))
    if not heading or not item:
        return None
    return heading, item


def _normalize_heading(text: str) -> str:
    return text.strip().strip("*").strip("#").strip()


def _normalize_item(text: str) -> str:
    stripped = text.strip()
    stripped = re.sub(r"^\s*(?:[-*+]\s+|\d+[.)、]\s*)", "", stripped).strip()
    return stripped
