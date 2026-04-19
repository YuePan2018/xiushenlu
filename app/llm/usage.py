from __future__ import annotations

from app.llm.provider import LLMProvider
from app.logger import EventLogger


def append_llm_call_event(logger: EventLogger, provider: LLMProvider, task: str) -> None:
    usage = getattr(provider, "last_usage", None)
    if usage is None:
        return
    detail = usage.to_event_detail()
    detail["task"] = task
    logger.append_event("llm_call", f"{task} 调用 LLM", detail)

