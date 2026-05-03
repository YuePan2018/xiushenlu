from __future__ import annotations

import json
import re
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from app.config import load_config, resolve_project_path
from app.safety import safe_append_text, safe_read_text


DAILY_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
DAILY_LOG_FILE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}\.jsonl$")
MONTH_RE = re.compile(r"^\d{4}-\d{2}$")


class EventLogger:
    """Append-only JSON Lines event logger."""

    def __init__(self, log_path: str | Path | None = None, config: dict[str, Any] | None = None) -> None:
        config = config or load_config()
        self.config = config
        self.logs_dir = resolve_project_path(config["paths"]["logs_dir"])
        self._explicit_log_path = log_path is not None
        if log_path is None:
            log_path = self.logs_dir / f"{date.today().isoformat()}.jsonl"

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
        safe_append_text(self.log_path, json.dumps(event, ensure_ascii=False, default=str) + "\n", self.config)

        return event

    def read_events(self) -> list[dict[str, Any]]:
        if self._explicit_log_path:
            return self._read_event_file(self.log_path)

        events: list[dict[str, Any]] = []
        for path in sorted(self.logs_dir.glob("*.jsonl")):
            if DAILY_LOG_FILE_RE.fullmatch(path.name):
                events.extend(self._read_event_file(path))
        return events

    def read_events_for_date(self, date_text: str) -> list[dict[str, Any]]:
        if not DAILY_DATE_RE.fullmatch(date_text):
            return []
        if self._explicit_log_path:
            return self.read_events()
        return self._read_event_file(self.logs_dir / f"{date_text}.jsonl")

    def read_events_for_month(self, month_text: str) -> list[dict[str, Any]]:
        if not MONTH_RE.fullmatch(month_text):
            return []
        if self._explicit_log_path:
            return self.read_events()

        events: list[dict[str, Any]] = []
        for path in sorted(self.logs_dir.glob(f"{month_text}-*.jsonl")):
            if DAILY_LOG_FILE_RE.fullmatch(path.name):
                events.extend(self._read_event_file(path))
        return events

    def _read_event_file(self, path: Path) -> list[dict[str, Any]]:
        if not path.exists():
            return []
        return [
            json.loads(line)
            for line in safe_read_text(path, self.config).splitlines()
            if line.strip()
        ]
