from __future__ import annotations

import shutil
import unittest
import uuid
from pathlib import Path
from typing import Any
from unittest.mock import patch

from app.logger import EventLogger
from app.posting.publisher import publish_xhs_from_draft
from app.posting.validation import build_xhs_payload
from app.posting.xhs_mcp import XhsMcpClient, XhsMcpError, XhsToolResult


class FakeXhsClient:
    def __init__(
        self,
        *,
        publish_error: bool = False,
    ) -> None:
        self.publish_error = publish_error
        self.calls: list[tuple[str, dict[str, object]]] = []

    def publish_content(self, arguments: dict[str, object]) -> XhsToolResult:
        self.calls.append(("publish_content", arguments))
        if self.publish_error:
            return XhsToolResult(
                name="publish_content",
                text="发布失败: cookies 已失效",
                raw={},
                is_error=True,
            )
        return XhsToolResult(
            name="publish_content",
            text="内容发布成功: note_id=abc",
            raw={},
        )


class PostingTests(unittest.TestCase):
    def test_build_xhs_payload_normalizes_tags_and_validates_images(self) -> None:
        payload = build_xhs_payload(
            title="修身炉进展",
            content="今天把发布链路收敛成轻量模块。",
            images=["https://example.com/a.png"],
            tags=["#修身炉", " AI工具 ", "修身炉"],
        )

        self.assertEqual(payload.tags, ["修身炉", "AI工具"])
        self.assertEqual(payload.visibility, "仅自己可见")
        self.assertEqual(payload.to_mcp_arguments()["visibility"], "仅自己可见")

    def test_build_xhs_payload_rejects_overlong_content(self) -> None:
        with self.assertRaisesRegex(ValueError, "1000"):
            build_xhs_payload(
                title="修身炉进展",
                content="太" * 1001,
                images=["https://example.com/a.png"],
            )

    def test_dry_run_records_request_without_calling_mcp(self) -> None:
        with _temporary_config() as config:
            draft = _write_draft(config, "今天完成小红书发布模块。")
            client = FakeXhsClient()
            logger = EventLogger(config=config)

            result = publish_xhs_from_draft(
                draft=draft,
                title="修身炉发布模块",
                images=["https://example.com/a.png"],
                tags=["修身炉"],
                config=config,
                logger=logger,
                client=client,  # type: ignore[arg-type]
            )

            self.assertFalse(result.approved)
            self.assertEqual(client.calls, [])
            events = logger.read_events()
            self.assertEqual([event["type"] for event in events], ["post_publish_requested"])
            self.assertFalse(events[0]["detail"]["approved"])
            self.assertEqual(events[0]["detail"]["content_summary"], "今天完成小红书发布模块。")

    def test_approve_publishes_without_login_precheck_and_records_success(self) -> None:
        with _temporary_config() as config:
            draft = _write_draft(config, "发布成功路径测试正文。")
            client = FakeXhsClient()
            logger = EventLogger(config=config)

            result = publish_xhs_from_draft(
                draft=draft,
                title="发布成功测试",
                images=["https://example.com/a.png"],
                approve=True,
                config=config,
                logger=logger,
                client=client,  # type: ignore[arg-type]
            )

            self.assertTrue(result.approved)
            self.assertEqual([name for name, _ in client.calls], ["publish_content"])
            publish_args = client.calls[0][1]
            self.assertEqual(publish_args["title"], "发布成功测试")
            self.assertEqual(publish_args["visibility"], "仅自己可见")
            events = logger.read_events()
            self.assertEqual(
                [event["type"] for event in events],
                ["post_publish_requested", "post_published"],
            )

    def test_approve_records_failure_when_publish_reports_login_error(self) -> None:
        with _temporary_config() as config:
            draft = _write_draft(config, "登录失败路径测试正文。")
            client = FakeXhsClient(publish_error=True)
            logger = EventLogger(config=config)

            with self.assertRaises(XhsMcpError):
                publish_xhs_from_draft(
                    draft=draft,
                    title="登录失败测试",
                    images=["https://example.com/a.png"],
                    approve=True,
                    config=config,
                    logger=logger,
                    client=client,  # type: ignore[arg-type]
                )

            self.assertEqual([name for name, _ in client.calls], ["publish_content"])
            events = logger.read_events()
            self.assertEqual(
                [event["type"] for event in events],
                ["post_publish_requested", "post_failed"],
            )
            self.assertIn("cookies", events[1]["detail"]["error"])

    def test_mcp_client_wraps_request_timeout(self) -> None:
        client = XhsMcpClient("http://localhost:18060/mcp", timeout=0.5)

        with patch("urllib.request.urlopen", side_effect=TimeoutError("timed out")):
            with self.assertRaisesRegex(XhsMcpError, "请求超时"):
                client.publish_content({"title": "超时测试"})


def _write_draft(config: dict[str, Any], text: str) -> Path:
    post_dir = Path(config["paths"]["post_dir"])
    post_dir.mkdir(parents=True, exist_ok=True)
    path = post_dir / "2026-05-12.txt"
    path.write_text(text, encoding="utf-8")
    return path


class _temporary_config:
    def __enter__(self) -> dict[str, Any]:
        parent = Path("workspace") / "test_posting"
        parent.mkdir(parents=True, exist_ok=True)
        self.root = parent / uuid.uuid4().hex
        post_dir = self.root / "data" / "post" / "data"
        logs_dir = self.root / "system_logs"
        self.config = {
            "paths": {
                "post_dir": str(post_dir),
                "logs_dir": str(logs_dir),
            },
            "safety": {
                "allowed_dirs": [str(post_dir), str(logs_dir)],
                "protected_files": [],
            },
            "xiaohongshu": {
                "mcp_url": "http://localhost:18060/mcp",
                "timeout": 1,
            },
        }
        return self.config

    def __exit__(self, exc_type, exc, tb) -> None:
        shutil.rmtree(self.root, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
