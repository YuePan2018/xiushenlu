from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import Any

from app.config import load_config, resolve_project_path
from app.safety import safe_read_text, safe_write_text


def date_text(target_date: date | None = None) -> str:
    return (target_date or date.today()).isoformat()


def daily_path(config: dict[str, Any] | None = None, target_date: date | str | None = None) -> Path:
    cfg = config or load_config()
    if isinstance(target_date, date):
        name = target_date.isoformat()
    else:
        name = target_date or date_text()
    daily_dir = resolve_project_path(cfg["paths"]["daily_dir"])
    return daily_dir / f"{name}.md"


def read_daily(config: dict[str, Any] | None = None, target_date: date | str | None = None) -> str:
    cfg = config or load_config()
    path = daily_path(cfg, target_date)
    if not path.exists():
        return ""
    return safe_read_text(path, cfg)


def write_daily_section(
    title: str,
    body: str,
    config: dict[str, Any] | None = None,
    target_date: date | str | None = None,
    *,
    mode: str = "replace",
    include_generated_at: bool = True,
    place_at_end: bool = False,
) -> Path:
    cfg = config or load_config()
    path = daily_path(cfg, target_date)
    day = path.stem
    section_body = body.strip()
    if include_generated_at:
        generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        section_body = f"生成时间：{generated_at}\n\n{section_body}"
    section = f"## {title}\n\n{section_body}\n"

    if path.exists():
        original = safe_read_text(path, cfg).strip()
        if mode == "replace":
            content = _replace_or_append_section(original, title, section, place_at_end=place_at_end)
        elif mode == "append":
            content = original.rstrip() + "\n\n" + section
        else:
            raise ValueError(f"Unsupported write mode: {mode}")
    else:
        content = f"# {day}\n\n{section}"

    safe_write_text(path, content.rstrip() + "\n", cfg)
    return path


def remove_daily_section(
    title: str,
    config: dict[str, Any] | None = None,
    target_date: date | str | None = None,
) -> Path:
    cfg = config or load_config()
    path = daily_path(cfg, target_date)
    if not path.exists():
        return path

    original = safe_read_text(path, cfg).strip()
    content = _remove_section(original, title)
    safe_write_text(path, content.rstrip() + "\n", cfg)
    return path


def append_record(
    content: str,
    config: dict[str, Any] | None = None,
    target_date: date | str | None = None,
) -> Path:
    if not content.strip():
        raise ValueError("Record content must not be empty.")

    cfg = config or load_config()
    path = daily_path(cfg, target_date)
    day = path.stem
    timestamp = datetime.now().strftime("%H:%M:%S")
    record = _format_record(timestamp, content)

    if path.exists():
        original = safe_read_text(path, cfg).strip()
        content_text = _append_to_section(original, "记录", record)
    else:
        content_text = f"# {day}\n\n## 记录\n\n{record}\n"

    safe_write_text(path, content_text.rstrip() + "\n", cfg)
    return path


def _format_record(timestamp: str, content: str) -> str:
    lines = content.strip().splitlines()
    if len(lines) == 1:
        return f"- {timestamp} {lines[0]}"

    formatted = [f"- {timestamp} {lines[0]}"]
    for line in lines[1:]:
        formatted.append(f"  {line}" if line else "  ")
    return "\n".join(formatted)


def _replace_or_append_section(
    original: str,
    title: str,
    section: str,
    *,
    place_at_end: bool = False,
) -> str:
    heading = f"## {title}"
    if heading not in original:
        return original.rstrip() + "\n\n" + section

    start = original.index(heading)
    next_heading = original.find("\n## ", start + len(heading))
    if place_at_end:
        if next_heading == -1:
            without_section = original[:start].rstrip()
        else:
            before = original[:start].rstrip()
            after = original[next_heading:].lstrip("\n")
            without_section = f"{before}\n\n{after}".strip()
        if not without_section:
            return section
        return without_section.rstrip() + "\n\n" + section

    if next_heading == -1:
        return original[:start].rstrip() + "\n\n" + section
    return original[:start].rstrip() + "\n\n" + section.rstrip() + "\n" + original[next_heading:]


def _remove_section(original: str, title: str) -> str:
    heading = f"## {title}"
    if heading not in original:
        return original

    start = original.index(heading)
    next_heading = original.find("\n## ", start + len(heading))
    if next_heading == -1:
        return original[:start].rstrip()
    return (original[:start].rstrip() + "\n\n" + original[next_heading:].lstrip("\n")).strip()


def _append_to_section(original: str, title: str, line: str) -> str:
    heading = f"## {title}"
    if heading not in original:
        return original.rstrip() + f"\n\n{heading}\n\n{line}\n"

    start = original.index(heading)
    next_heading = original.find("\n## ", start + len(heading))
    if next_heading == -1:
        return original.rstrip() + "\n" + line + "\n"

    before = original[:next_heading].rstrip()
    after = original[next_heading:]
    return before + "\n" + line + "\n" + after
