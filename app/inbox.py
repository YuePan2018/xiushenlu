from __future__ import annotations

from pathlib import Path
from typing import Any

from app.config import load_config, resolve_project_path
from app.safety import safe_read_text, safe_write_text, validate_path


def today_tasks_path(config: dict[str, Any] | None = None) -> Path:
    cfg = config or load_config()
    inbox_dir = resolve_project_path(cfg["paths"]["inbox_dir"])
    return inbox_dir / "today_tasks.md"


def ensure_today_tasks_file(config: dict[str, Any] | None = None) -> Path:
    cfg = config or load_config()
    path = validate_path(today_tasks_path(cfg), cfg, for_write=True)
    if not path.exists():
        safe_write_text(path, "", cfg)
    return path


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
