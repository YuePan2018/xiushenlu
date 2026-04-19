from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from typing import Any

import dashscope
from dotenv import load_dotenv
from qwen_agent.agents import Assistant

from app.llm.provider import LLMProvider


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

        dashscope.api_key = api_key
        dashscope.timeout = int(llm_config.get("timeout", 30))

        qwen_llm_config = {
            "model": str(model),
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
        messages = [{"role": "user", "content": prompt}]
        for response in self._bot.run(messages):
            text = self._extract_response_text(response)
            if text:
                final_text = text

        return final_text.strip()

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

