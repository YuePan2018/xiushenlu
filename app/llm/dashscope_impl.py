from __future__ import annotations

import math
import os
from collections.abc import Mapping
from typing import Any

import dashscope
from dotenv import load_dotenv
from dashscope import Generation

from app.llm.provider import LLMCallUsage, LLMProvider


class DashScopeProvider(LLMProvider):
    """DashScope MultiModalConversation implementation.

    注意：Qwen3 系列模型默认开启 thinking mode，响应文本会包含
    <think>...</think> 前缀。直接调用不会报错，但内容会混入输出。
    如需干净输出，可在 call() 中传 extra_body={"enable_thinking": False}，
    或在拿到 reply 后用 re.sub 过滤掉 <think> 块。
    """

    def __init__(self, config: Mapping[str, Any]) -> None:
        load_dotenv()

        llm_config = config.get("llm", {})
        assistant_config = config.get("assistant", {})

        api_key_env = str(llm_config.get("api_key_env", "DASHSCOPE_API_KEY"))
        api_key = os.getenv(api_key_env)
        if not api_key:
            raise RuntimeError(f"Missing {api_key_env}. Set it before running the assistant.")

        model = llm_config.get("model")
        if not model:
            raise ValueError("Config key 'llm.model' is required.")
        self.model = str(model)
        self._api_key = api_key
        self._system_prompt = str(
            assistant_config.get("system_prompt", "你是一个帮助用户记录、计划和复盘的个人执行助手。")
        )
        self.last_usage: LLMCallUsage | None = None

        dashscope.base_http_api_url = "https://dashscope.aliyuncs.com/api/v1"

    def chat(self, prompt: str) -> str:
        if not prompt.strip():
            raise ValueError("Prompt must not be empty.")

        messages = [
            {"role": "system", "content": self._system_prompt},
            {"role": "user", "content": prompt},
        ]

        if self._uses_generation_api():
            response = Generation.call(
                api_key=self._api_key,
                model=self.model,
                messages=messages,
                result_format="message",
                enable_thinking=True,
            )
        else:
            response = dashscope.MultiModalConversation.call(
                api_key=self._api_key,
                model=self.model,
                messages=messages,
            )

        reply = _extract_reply(response)
        self.last_usage = self._build_usage(prompt, reply, response)
        return reply

    def _uses_generation_api(self) -> bool:
        return self.model.lower().startswith("glm-")

    def _build_usage(self, prompt: str, reply: str, response: Any) -> LLMCallUsage:
        try:
            usage = response.usage
            tokens_in = _first_int(usage, "input_tokens", "prompt_tokens") or 0
            tokens_out = _first_int(usage, "output_tokens", "completion_tokens") or 0
            return LLMCallUsage(
                model=self.model,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                total_tokens=tokens_in + tokens_out,
                estimated=False,
                raw=str(usage),
            )
        except Exception:
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


def _estimate_tokens(text: str) -> int:
    if not text:
        return 0
    return max(1, math.ceil(len(text) / 4))


def _extract_reply(response: Any) -> str:
    try:
        output = response.output
        choices = output.choices
        message = choices[0].message
        reply = _content_to_text(message.content)
    except (AttributeError, IndexError, KeyError, TypeError) as exc:
        raise RuntimeError(_format_response_error(response)) from exc

    if not isinstance(reply, str) or not reply.strip():
        raise RuntimeError(_format_response_error(response))
    return reply


def _content_to_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(_content_to_text(item) for item in content)
    if isinstance(content, Mapping):
        if "text" in content:
            return _content_to_text(content["text"])
        if "content" in content:
            return _content_to_text(content["content"])
    return str(content)


def _first_int(source: Any, *keys: str) -> int | None:
    if not isinstance(source, Mapping):
        source = getattr(source, "__dict__", None)
    if not isinstance(source, Mapping):
        return None
    for key in keys:
        value = source.get(key)
        if value is None:
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return None


def _format_response_error(response: Any) -> str:
    code = getattr(response, "code", None) or getattr(response, "status_code", None)
    message = getattr(response, "message", None) or getattr(response, "msg", None)
    request_id = getattr(response, "request_id", None)

    parts = ["DashScope 调用失败或返回为空"]
    if code:
        parts.append(f"code={code}")
    if message:
        parts.append(f"message={message}")
    if request_id:
        parts.append(f"request_id={request_id}")
    return "：".join([parts[0], "，".join(parts[1:])]) if len(parts) > 1 else parts[0]
