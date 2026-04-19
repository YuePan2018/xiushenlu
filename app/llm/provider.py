from __future__ import annotations

from abc import ABC, abstractmethod


class LLMProvider(ABC):
    """Minimal interface used by Phase 1 pipelines."""

    @abstractmethod
    def chat(self, prompt: str) -> str:
        """Return a text response for a single user prompt."""

