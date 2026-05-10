from __future__ import annotations

import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.llm.dashscope_impl import DashScopeProvider


class DashScopeProviderTests(unittest.TestCase):
    def test_chat_raises_readable_error_when_output_is_missing(self) -> None:
        response = SimpleNamespace(
            output=None,
            code="InvalidApiKey",
            message="API key is invalid",
            request_id="req-123",
        )

        with patch.dict(os.environ, {"DASHSCOPE_API_KEY": "test-key"}):
            with patch("app.llm.dashscope_impl.dashscope.MultiModalConversation.call", return_value=response):
                provider = DashScopeProvider(_test_config())

                with self.assertRaises(RuntimeError) as caught:
                    provider.chat("生成计划")

        message = str(caught.exception)
        self.assertIn("DashScope 调用失败或返回为空", message)
        self.assertIn("InvalidApiKey", message)
        self.assertIn("req-123", message)

    def test_chat_reads_text_reply_and_usage(self) -> None:
        response = SimpleNamespace(
            output=SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(content=[{"text": "计划正文"}])
                    )
                ]
            ),
            usage=SimpleNamespace(input_tokens=3, output_tokens=5),
        )

        with patch.dict(os.environ, {"DASHSCOPE_API_KEY": "test-key"}):
            with patch("app.llm.dashscope_impl.dashscope.MultiModalConversation.call", return_value=response) as call:
                provider = DashScopeProvider(_test_config())

                reply = provider.chat("生成计划")

        self.assertEqual(reply, "计划正文")
        self.assertIsNotNone(provider.last_usage)
        self.assertEqual(provider.last_usage.total_tokens, 8)
        messages = call.call_args.kwargs["messages"]
        self.assertEqual(messages[0]["content"], "测试系统提示词")
        self.assertEqual(messages[1]["content"], "生成计划")

    def test_glm_model_uses_generation_api(self) -> None:
        response = SimpleNamespace(
            output=SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(
                            reasoning_content="思考内容",
                            content="GLM 回复",
                        )
                    )
                ]
            ),
            usage={"input_tokens": 4, "output_tokens": 6},
            status_code=200,
        )

        with patch.dict(os.environ, {"DASHSCOPE_API_KEY": "test-key"}):
            with patch("app.llm.dashscope_impl.Generation.call", return_value=response) as call:
                provider = DashScopeProvider(_glm_config())

                reply = provider.chat("你是谁？")

        self.assertEqual(reply, "GLM 回复")
        self.assertEqual(provider.last_usage.total_tokens, 10)
        kwargs = call.call_args.kwargs
        self.assertEqual(kwargs["model"], "glm-5.1")
        self.assertEqual(kwargs["result_format"], "message")
        self.assertTrue(kwargs["enable_thinking"])
        self.assertEqual(
            kwargs["messages"],
            [
                {"role": "system", "content": "测试系统提示词"},
                {"role": "user", "content": "你是谁？"},
            ],
        )


def _test_config() -> dict[str, object]:
    return {
        "llm": {
            "model": "qwen-test",
            "api_key_env": "DASHSCOPE_API_KEY",
        },
        "assistant": {
            "system_prompt": "测试系统提示词",
        },
    }


def _glm_config() -> dict[str, object]:
    config = _test_config()
    config["llm"]["model"] = "glm-5.1"
    return config


if __name__ == "__main__":
    unittest.main()
