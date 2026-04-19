from __future__ import annotations

from pathlib import Path
from typing import Any

from app.config import load_config, resolve_project_path


def goals_path(config: dict[str, Any] | None = None) -> Path:
    cfg = config or load_config()
    memory_dir = resolve_project_path(cfg["paths"]["memory_dir"])
    return memory_dir / "goals.md"


def read_goals(config: dict[str, Any] | None = None) -> str:
    path = goals_path(config)
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8").strip()

