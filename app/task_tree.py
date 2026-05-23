from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.config import load_config, resolve_project_path
from app.safety import safe_read_text, safe_write_text, validate_path


DEFAULT_TASK_TREE_DIR = "data/task_tree"


class TaskTreeError(ValueError):
    """Raised when a task tree JSON document is invalid."""


@dataclass(frozen=True)
class TaskTreeFile:
    title: str
    filename: str
    path: Path


@dataclass(frozen=True)
class TaskTreeDocument:
    title: str
    filename: str
    path: Path
    text: str
    tree: dict[str, Any]


def task_tree_dir(config: dict[str, Any] | None = None) -> Path:
    cfg = _task_tree_config(config)
    configured = cfg["paths"]["task_tree_dir"]
    return validate_path(resolve_project_path(configured), cfg)


def list_task_trees(config: dict[str, Any] | None = None) -> list[TaskTreeFile]:
    directory = task_tree_dir(config)
    if not directory.exists():
        return []

    files = sorted(
        (path for path in directory.glob("*.json") if path.is_file()),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    return [
        TaskTreeFile(title=path.stem, filename=path.name, path=path.resolve())
        for path in files
    ]


def read_task_tree(title: str, config: dict[str, Any] | None = None) -> TaskTreeDocument:
    cfg = _task_tree_config(config)
    path = task_tree_path_for_title(title, cfg)
    if not path.exists():
        raise TaskTreeError(f"工作树不存在：{path.name}")
    text = safe_read_text(path, cfg)
    tree, _ = normalize_task_tree_text(text)
    return TaskTreeDocument(
        title=path.stem,
        filename=path.name,
        path=path,
        text=text,
        tree=tree,
    )


def read_task_tree_file(filename: str, config: dict[str, Any] | None = None) -> TaskTreeDocument:
    cfg = _task_tree_config(config)
    path = task_tree_path_for_filename(filename, cfg)
    if not path.exists():
        raise TaskTreeError(f"工作树不存在：{path.name}")
    text = safe_read_text(path, cfg)
    tree, _ = normalize_task_tree_text(text)
    return TaskTreeDocument(
        title=path.stem,
        filename=path.name,
        path=path,
        text=text,
        tree=tree,
    )


def save_task_tree(
    title: str,
    text: str,
    config: dict[str, Any] | None = None,
) -> TaskTreeDocument:
    cfg = _task_tree_config(config)
    tree, normalized_text = normalize_task_tree_text(text)
    effective_title = title.strip() or str(tree.get("title", "")).strip()
    path = task_tree_path_for_title(effective_title, cfg)
    safe_write_text(path, normalized_text.rstrip() + "\n", cfg)
    return TaskTreeDocument(
        title=path.stem,
        filename=path.name,
        path=path,
        text=normalized_text,
        tree=tree,
    )


def delete_task_tree_file(filename: str, config: dict[str, Any] | None = None) -> TaskTreeFile:
    cfg = _task_tree_config(config)
    path = validate_path(task_tree_path_for_filename(filename, cfg), cfg, for_write=True)
    if not path.exists():
        raise TaskTreeError(f"工作树不存在：{path.name}")
    path.unlink()
    return TaskTreeFile(title=path.stem, filename=path.name, path=path)


def task_tree_path_for_title(title: str, config: dict[str, Any] | None = None) -> Path:
    cfg = _task_tree_config(config)
    filename = task_tree_filename(title)
    directory = task_tree_dir(cfg)
    return validate_path(directory / filename, cfg, for_write=True)


def task_tree_path_for_filename(filename: str, config: dict[str, Any] | None = None) -> Path:
    cfg = _task_tree_config(config)
    name = _validate_task_tree_filename(filename)
    directory = task_tree_dir(cfg)
    return validate_path(directory / name, cfg)


def task_tree_filename(title: str) -> str:
    stem = _sanitize_filename_stem(title)
    if not stem:
        raise TaskTreeError("工作树标题不能为空。")
    return f"{stem}.json"


def normalize_task_tree_text(text: str) -> tuple[dict[str, Any], str]:
    tree = parse_task_tree_text(text)
    normalized = json.dumps(tree, ensure_ascii=False, indent=2)
    return tree, normalized


def parse_task_tree_text(text: str) -> dict[str, Any]:
    source = _strip_json_fence(text)
    if not source.strip():
        raise TaskTreeError("工作树 JSON 不能为空。")
    try:
        data = json.loads(source)
    except json.JSONDecodeError as exc:
        raise TaskTreeError(f"工作树 JSON 解析失败：{exc.msg}") from exc
    return _normalize_tree(data)


def _task_tree_config(config: dict[str, Any] | None = None) -> dict[str, Any]:
    base = config or load_config()
    paths = dict(base.get("paths", {}))
    paths.setdefault("task_tree_dir", DEFAULT_TASK_TREE_DIR)
    safety = dict(base.get("safety", {}))
    allowed_dirs = list(safety.get("allowed_dirs") or [])
    if paths["task_tree_dir"] not in allowed_dirs:
        allowed_dirs.append(paths["task_tree_dir"])
    safety["allowed_dirs"] = allowed_dirs
    merged = dict(base)
    merged["paths"] = paths
    merged["safety"] = safety
    return merged


def _normalize_tree(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise TaskTreeError("工作树根节点必须是 JSON 对象。")

    title = _required_text(data, "title", "工作树标题")
    nodes_value = data.get("nodes")
    if not isinstance(nodes_value, list):
        raise TaskTreeError("工作树必须包含 nodes 数组。")

    normalized: dict[str, Any] = {
        "version": int(data.get("version", 1)),
        "title": title,
        "summary": _optional_text(data.get("summary")),
        "nodes": [_normalize_node(node, f"nodes[{index}]") for index, node in enumerate(nodes_value)],
    }
    return normalized


def _normalize_node(data: Any, path: str) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise TaskTreeError(f"{path} 必须是 JSON 对象。")

    title = _required_text(data, "title", f"{path}.title")
    children_value = data.get("children", [])
    if not isinstance(children_value, list):
        raise TaskTreeError(f"{path}.children 必须是数组。")

    normalized: dict[str, Any] = {
        "id": _optional_text(data.get("id")) or _fallback_node_id(path),
        "title": title,
    }
    content = _optional_node_content(data)
    if content:
        normalized["content"] = content
    children = [
        _normalize_node(child, f"{path}.children[{index}]")
        for index, child in enumerate(children_value)
    ]
    if children:
        normalized["children"] = children
    return normalized


def _required_text(data: dict[str, Any], key: str, label: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise TaskTreeError(f"{label} 不能为空。")
    cleaned = _clean_title_text(value)
    if not cleaned:
        raise TaskTreeError(f"{label} 不能为空。")
    return cleaned


def _optional_text(value: Any) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise TaskTreeError("可选文本字段必须是字符串。")
    return value.strip()


def _optional_node_content(data: dict[str, Any]) -> str:
    if "content" in data:
        return _optional_text(data.get("content"))
    return _optional_text(data.get("note"))


def _strip_json_fence(text: str) -> str:
    stripped = text.strip()
    match = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", stripped, flags=re.DOTALL | re.IGNORECASE)
    if match is None:
        return stripped
    return match.group(1).strip()


def _clean_title_text(value: str) -> str:
    text = value.strip()
    for _ in range(3):
        decoded = html.unescape(text)
        if decoded == text:
            break
        text = decoded
    return re.sub(r"</?p\b[^>]*>", "", text, flags=re.IGNORECASE).strip()


def _sanitize_filename_stem(title: str) -> str:
    value = re.sub(r"[\x00-\x1f<>:\"/\\|?*]+", "", title)
    value = re.sub(r"\s+", " ", value).strip(" .")
    if len(value) > 80:
        value = value[:80].rstrip(" .")
    if value.upper() in {"CON", "PRN", "AUX", "NUL", "COM1", "COM2", "COM3", "COM4", "LPT1", "LPT2", "LPT3"}:
        value = f"{value}_task_tree"
    return value


def _validate_task_tree_filename(filename: str) -> str:
    value = filename.strip()
    if not value:
        raise TaskTreeError("工作树文件名不能为空。")
    path = Path(value)
    if path.name != value or path.suffix.lower() != ".json":
        raise TaskTreeError("工作树文件名必须是 task_tree 根目录下的 JSON 文件。")
    return value


def _fallback_node_id(path: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]+", "-", path).strip("-") or "node"
