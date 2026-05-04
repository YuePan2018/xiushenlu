from __future__ import annotations

from pathlib import Path
from typing import Any

from app.config import load_config, resolve_project_path
from app.safety import safe_read_text, safe_write_text


TODAY_TASKS_HEADING = "# 今日待办"


def today_tasks_path(config: dict[str, Any] | None = None) -> Path:
    cfg = config or load_config()
    inbox_dir = resolve_project_path(cfg["paths"]["inbox_dir"])
    return inbox_dir / "today_tasks.md"


def tomorrow_plan_path(config: dict[str, Any] | None = None) -> Path:
    cfg = config or load_config()
    inbox_dir = resolve_project_path(cfg["paths"]["inbox_dir"])
    return inbox_dir / "明日计划.md"


def read_today_tasks(config: dict[str, Any] | None = None) -> str:
    cfg = config or load_config()
    path = today_tasks_path(cfg)
    if not path.exists():
        return ""
    return safe_read_text(path, cfg).strip()


def write_today_tasks(tasks: str, config: dict[str, Any] | None = None) -> Path:
    if not tasks.strip():
        raise ValueError("Today tasks must not be empty.")

    cfg = config or load_config()
    path = today_tasks_path(cfg)
    text = tasks.strip()
    safe_write_text(path, text.rstrip() + "\n", cfg)
    return path


def has_leading_markdown_heading(tasks: str) -> bool:
    for line in tasks.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        return stripped.startswith("#")
    return False


def remove_added_today_tasks_heading(tasks: str, preserve_heading: bool) -> str:
    text = tasks.strip()
    if preserve_heading:
        return text

    lines = text.splitlines()
    if not lines or lines[0].strip() != TODAY_TASKS_HEADING:
        return text

    remaining = lines[1:]
    if remaining and not remaining[0].strip():
        remaining = remaining[1:]
    return "\n".join(remaining).strip()


def read_tomorrow_plan(config: dict[str, Any] | None = None) -> str:
    cfg = config or load_config()
    path = tomorrow_plan_path(cfg)
    if not path.exists():
        return ""
    return safe_read_text(path, cfg).strip()


def clear_tomorrow_plan(config: dict[str, Any] | None = None) -> Path:
    cfg = config or load_config()
    path = tomorrow_plan_path(cfg)
    safe_write_text(path, "", cfg)
    return path
