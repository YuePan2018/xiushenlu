from __future__ import annotations

from pathlib import Path
from typing import Any

from app.config import load_config, resolve_project_path
from app.safety import safe_read_text, safe_write_text


def today_tasks_path(config: dict[str, Any] | None = None) -> Path:
    cfg = config or load_config()
    inbox_dir = resolve_project_path(cfg["paths"]["inbox_dir"])
    return inbox_dir / "today_tasks.md"


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
    if not text.lstrip().startswith("#"):
        text = f"# 今日待办\n\n{text}"
    safe_write_text(path, text.rstrip() + "\n", cfg)
    return path
