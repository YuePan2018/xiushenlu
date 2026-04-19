from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.config import load_config, resolve_project_path
from app.safety import safe_append_text, safe_read_text


class EventLogger:
    """Append-only JSON Lines event logger."""

    def __init__(self, log_path: str | Path | None = None) -> None:
        if log_path is None:
            config = load_config()
            logs_dir = resolve_project_path(config["paths"]["logs_dir"])
            log_path = logs_dir / "events.jsonl"

        self.log_path = resolve_project_path(log_path)

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
        safe_append_text(self.log_path, json.dumps(event, ensure_ascii=False, default=str) + "\n")

        return event

    def read_events(self) -> list[dict[str, Any]]:
        if not self.log_path.exists():
            return []

        events: list[dict[str, Any]] = []
        for line in safe_read_text(self.log_path).splitlines():
            if not line.strip():
                continue
            events.append(json.loads(line))
        return events

    def read_events_for_date(self, date_text: str) -> list[dict[str, Any]]:
        return [
            event
            for event in self.read_events()
            if str(event.get("ts", "")).startswith(date_text)
        ]
