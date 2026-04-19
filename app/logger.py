from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.config import load_config, resolve_project_path


class EventLogger:
    """Append-only JSON Lines event logger."""

    def __init__(self, log_path: str | Path | None = None) -> None:
        if log_path is None:
            config = load_config()
            logs_dir = resolve_project_path(config["paths"]["logs_dir"])
            log_path = logs_dir / "events.jsonl"

        self.log_path = resolve_project_path(log_path)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)

    def append_event(
        self,
        type: str,
        summary: str,
        detail: Any | None = None,
    ) -> dict[str, Any]:
        if not type.strip():
            raise ValueError("Event type must not be empty.")
        if not summary.strip():
            raise ValueError("Event summary must not be empty.")

        event = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "type": type,
            "summary": summary,
            "detail": detail,
        }
        with self.log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")

        return event

