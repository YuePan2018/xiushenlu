from __future__ import annotations

from pathlib import Path
from typing import Any

from app.config import load_config, resolve_project_path


class PathSafetyError(ValueError):
    """Raised when a path is outside the configured safety boundary."""


def validate_path(
    path: str | Path,
    config: dict[str, Any] | None = None,
    *,
    for_write: bool = False,
) -> Path:
    cfg = config or load_config()
    resolved = resolve_project_path(path).resolve()

    allowed_dirs = [_resolve_config_path(item) for item in _allowed_dir_values(cfg)]
    if not any(_is_relative_to(resolved, allowed) for allowed in allowed_dirs):
        allowed_text = ", ".join(str(item) for item in allowed_dirs)
        raise PathSafetyError(f"Path is outside allowed directories: {resolved} (allowed: {allowed_text})")

    if for_write:
        protected_files = {_resolve_config_path(item) for item in cfg.get("safety", {}).get("protected_files", [])}
        if resolved in protected_files:
            raise PathSafetyError(f"Refusing to write protected file: {resolved}")

    return resolved


def safe_read_text(path: str | Path, config: dict[str, Any] | None = None) -> str:
    resolved = validate_path(path, config)
    return resolved.read_text(encoding="utf-8")


def safe_write_text(path: str | Path, text: str, config: dict[str, Any] | None = None) -> None:
    resolved = validate_path(path, config, for_write=True)
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(text, encoding="utf-8")


def safe_write_bytes(path: str | Path, data: bytes, config: dict[str, Any] | None = None) -> None:
    resolved = validate_path(path, config, for_write=True)
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_bytes(data)


def safe_append_text(path: str | Path, text: str, config: dict[str, Any] | None = None) -> None:
    resolved = validate_path(path, config, for_write=True)
    resolved.parent.mkdir(parents=True, exist_ok=True)
    with resolved.open("a", encoding="utf-8") as f:
        f.write(text)


def _allowed_dir_values(config: dict[str, Any]) -> list[str]:
    safety = config.get("safety", {})
    values = safety.get("allowed_dirs")
    if values:
        return list(values)
    paths = config.get("paths", {})
    return [
        value
        for key, value in paths.items()
        if key.endswith("_dir") and key != "data_dir"
    ]


def _resolve_config_path(path: str | Path) -> Path:
    return resolve_project_path(path).resolve()


def _is_relative_to(path: Path, parent: Path) -> bool:
    return path == parent or parent in path.parents
