from __future__ import annotations

import logging
import math
import os
from collections.abc import Mapping
from typing import Any

import dashscope
from dotenv import load_dotenv
from qwen_agent.agents import Assistant

from app.llm.provider import LLMCallUsage, LLMProvider


logging.disable(logging.INFO)


class QwenAgentProvider(LLMProvider):
    """Qwen Agent implementation backed by DashScope credentials."""

    def __init__(self, config: Mapping[str, Any]) -> None:
        load_dotenv()

        llm_config = config.get("llm", {})
        assistant_config = config.get("assistant", {})
        if not isinstance(llm_config, Mapping):
            raise ValueError("Config key 'llm' must be a mapping.")
        if not isinstance(assistant_config, Mapping):
            raise ValueError("Config key 'assistant' must be a mapping.")

        api_key_env = str(llm_config.get("api_key_env", "DASHSCOPE_API_KEY"))
        api_key = os.getenv(api_key_env)
        if not api_key:
            raise RuntimeError(
                f"Missing {api_key_env}. Set it before running the assistant."
            )

        model = llm_config.get("model")
        if not model:
            raise ValueError("Config key 'llm.model' is required.")
        self.model = str(model)
        self.last_usage: LLMCallUsage | None = None

        dashscope.api_key = api_key
        dashscope.timeout = int(llm_config.get("timeout", 30))

        qwen_llm_config = {
            "model": self.model,
            "timeout": int(llm_config.get("timeout", 30)),
            "retry_count": int(llm_config.get("retry_count", 2)),
        }

        self._bot = Assistant(
            llm=qwen_llm_config,
            name=str(assistant_config.get("name", "修身炉")),
            description=str(assistant_config.get("description", "个人认知与执行助手")),
            system_message=str(
                assistant_config.get(
                    "system_prompt",
                    "你是一个帮助用户记录、计划和复盘的个人执行助手。",
                )
            ),
            function_list=list(assistant_config.get("function_list", [])),
        )

    def chat(self, prompt: str) -> str:
        if not prompt.strip():
            raise ValueError("Prompt must not be empty.")

        final_text = ""
        raw_usage: Any | None = None
        messages = [{"role": "user", "content": prompt}]
        for response in self._bot.run(messages):
            text = self._extract_response_text(response)
            if text:
                final_text = text
            raw_usage = raw_usage or self._extract_usage(response)

        final_text = final_text.strip()
        self.last_usage = self._build_usage(prompt, final_text, raw_usage)
        return final_text

    def _extract_response_text(self, response: Any) -> str:
        if isinstance(response, list):
            return "".join(self._extract_response_text(item) for item in response)
        if isinstance(response, Mapping):
            return self._content_to_text(response.get("content"))
        if hasattr(response, "content"):
            return self._content_to_text(response.content)
        return ""

    def _content_to_text(self, content: Any) -> str:
        if content is None:
            return ""
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "".join(self._content_to_text(item) for item in content)
        if isinstance(content, Mapping):
            if "text" in content:
                return self._content_to_text(content["text"])
            if "content" in content:
                return self._content_to_text(content["content"])
        return str(content)

    def _extract_usage(self, response: Any) -> Any | None:
        if isinstance(response, Mapping):
            for key in ("usage", "token_usage", "usage_metadata"):
                if key in response and response[key]:
                    return response[key]
            for key in ("input_tokens", "output_tokens", "prompt_tokens", "completion_tokens"):
                if key in response:
                    return response
            for value in response.values():
                found = self._extract_usage(value)
                if found:
                    return found
        if isinstance(response, list):
            for item in response:
                found = self._extract_usage(item)
                if found:
                    return found
        for attr in ("usage", "token_usage", "usage_metadata"):
            if hasattr(response, attr):
                value = getattr(response, attr)
                if value:
                    return value
        return None

    def _build_usage(self, prompt: str, reply: str, raw_usage: Any | None) -> LLMCallUsage:
        parsed = self._parse_usage(raw_usage)
        if parsed:
            tokens_in, tokens_out, total_tokens = parsed
            return LLMCallUsage(
                model=self.model,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                total_tokens=total_tokens,
                estimated=False,
                raw=raw_usage,
            )

        tokens_in = _estimate_tokens(prompt)
        tokens_out = _estimate_tokens(reply)
        return LLMCallUsage(
            model=self.model,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            total_tokens=tokens_in + tokens_out,
            estimated=True,
            raw=None,
        )

    def _parse_usage(self, raw_usage: Any | None) -> tuple[int, int, int] | None:
        if raw_usage is None:
            return None
        if not isinstance(raw_usage, Mapping):
            raw_usage = getattr(raw_usage, "__dict__", None)
        if not isinstance(raw_usage, Mapping):
            return None

        tokens_in = _first_int(raw_usage, "tokens_in", "input_tokens", "prompt_tokens")
        tokens_out = _first_int(raw_usage, "tokens_out", "output_tokens", "completion_tokens")
        total_tokens = _first_int(raw_usage, "total_tokens", "total")
        if tokens_in is None and tokens_out is None and total_tokens is None:
            return None
        tokens_in = tokens_in or 0
        tokens_out = tokens_out or 0
        total_tokens = total_tokens or (tokens_in + tokens_out)
        return tokens_in, tokens_out, total_tokens


def _first_int(mapping: Mapping[str, Any], *keys: str) -> int | None:
    for key in keys:
        value = mapping.get(key)
        if value is None:
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return None


def _estimate_tokens(text: str) -> int:
    if not text:
        return 0
    return max(1, math.ceil(len(text) / 4))

