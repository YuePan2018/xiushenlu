from __future__ import annotations

from pathlib import Path
from typing import Any

from app.config import load_config, resolve_project_path
from app.safety import safe_read_text, validate_path


def post_dir(config: dict[str, Any] | None = None) -> Path:
    cfg = config or load_config()
    return resolve_project_path(cfg.get("paths", {}).get("post_dir", "data/post/data"))


def read_post_draft(path: str | Path, config: dict[str, Any] | None = None) -> tuple[Path, str]:
    cfg = config or load_config()
    resolved = validate_path(path, cfg)
    base_dir = post_dir(cfg).resolve()
    if not _is_relative_to(resolved, base_dir):
        raise ValueError(f"草稿必须位于 data/post/data 目录内（当前配置：{base_dir}）：{resolved}")
    text = safe_read_text(resolved, cfg).strip()
    if not text:
        raise ValueError(f"草稿为空：{resolved}")
    return resolved, text


def summarize_content(text: str, limit: int = 120) -> str:
    compact = " ".join(text.split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 1].rstrip() + "…"


def _is_relative_to(path: Path, parent: Path) -> bool:
    return path == parent or parent in path.parents
