from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class LLMCallUsage:
    model: str
    tokens_in: int
    tokens_out: int
    total_tokens: int
    estimated: bool
    raw: Any | None = None

    def to_event_detail(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "total_tokens": self.total_tokens,
            "estimated": self.estimated,
            "raw": _json_safe(self.raw),
        }


class LLMProvider(ABC):
    """Minimal interface used by Phase 1 pipelines."""

    @abstractmethod
    def chat(self, prompt: str) -> str:
        """Return a text response for a single user prompt."""


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return str(value)
