from __future__ import annotations

import json
import shutil
import threading
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path
from typing import Any
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.console import create_app
from app.llm.provider import LLMCallUsage, LLMProvider
from app.posting.xhs_mcp import XhsToolResult


class FakeProvider(LLMProvider):
    def __init__(self, reply: str = "控制台测试计划") -> None:
        self.reply = reply
        self.last_usage: LLMCallUsage | None = None
        self.prompts: list[str] = []

    def chat(self, prompt: str) -> str:
        self.prompts.append(prompt)
        self.last_usage = LLMCallUsage(
            model="fake-model",
            tokens_in=7,
            tokens_out=11,
            total_tokens=18,
            estimated=False,
            raw=None,
            response_seconds=40.0,
        )
        if "next_today_tasks" in prompt:
            return json.dumps(
                {
                    "review": "控制台测试复盘",
                    "next_today_tasks": "# 今日待办\n\n控制台明日任务",
                },
                ensure_ascii=False,
            )
        if "updated_today_tasks" in prompt:
            return json.dumps(
                {
                    "updated_today_tasks": "# 今日待办\n\n原任务\n新增任务",
                    "updated_daily_original": "原任务\n新增任务",
                    "target_heading": "今日待办",
                    "schedule_task": "新增任务",
                    "schedule_priority": "P2",
                    "schedule_estimate": "30m",
                },
                ensure_ascii=False,
            )
        if "log_schedule_updates" in prompt:
            return json.dumps(
                {
                    "updates": [
                        {
                            "row_index": 1,
                            "completed": True,
                            "note": "保留两列白名单",
                            "evidence": "后续注意：保留两列白名单",
                        }
                    ]
                },
                ensure_ascii=False,
            )
        return self.reply


class BlockingProvider(FakeProvider):
    def __init__(self, started: threading.Event, release: threading.Event) -> None:
        super().__init__("慢速计划")
        self.started = started
        self.release = release

    def chat(self, prompt: str) -> str:
        self.started.set()
        if not self.release.wait(timeout=5):
            raise RuntimeError("测试 provider 等待超时。")
        return super().chat(prompt)


class ErrorProvider(FakeProvider):
    def chat(self, prompt: str) -> str:
        raise RuntimeError("DashScope 调用失败或返回为空：code=InvalidApiKey")


class FakeXhsClient:
    def __init__(
        self,
        *,
        connect_error: bool = False,
    ) -> None:
        self.connect_error = connect_error
        self.calls: list[tuple[str, dict[str, object]]] = []

    def can_connect(self) -> bool:
        self.calls.append(("can_connect", {}))
        return not self.connect_error

    def publish_content(self, arguments: dict[str, object]) -> XhsToolResult:
        self.calls.append(("publish_content", arguments))
        return XhsToolResult("publish_content", "发布成功 note_id=abc", {})


class ConsoleTests(unittest.TestCase):
    def test_index_uses_local_markdown_renderer_assets(self) -> None:
        with _temporary_directory() as temp_dir:
            config = _test_config(Path(temp_dir))
            client = TestClient(create_app(config=config, provider_factory=FakeProvider))

            response = client.get("/")

            self.assertEqual(response.status_code, 200)
            html = response.text
            self.assertIn('<article id="dailyText" class="daily-markdown empty"', html)
            self.assertIn('<span class="panel-title">添加任务</span>', html)
            self.assertIn("<h2>口号</h2>", html)
            self.assertIn('id="sloganInput"', html)
            self.assertIn("xiushenlu.console.slogan", html)
            self.assertIn('id="tokenBtn">token</button>', html)
            self.assertIn('<span class="panel-title">杂事</span>', html)
            self.assertIn('<span class="panel-title">长远计划</span>', html)
            self.assertIn('id="miscInput"', html)
            self.assertIn('id="longPlanInput"', html)
            self.assertIn('/api/user-notes', html)
            self.assertIn('submitOnCtrlEnter("miscInput", "saveMiscBtn")', html)
            self.assertIn('submitOnCtrlEnter("longPlanInput", "saveLongPlanBtn")', html)
            self.assertIn('id="openTasksBtn">打开文件</button>', html)
            self.assertIn('id="stopBtn"', html)
            self.assertIn('id="menuBtn"', html)
            self.assertIn('class="menu-button"', html)
            self.assertIn('aria-haspopup="menu"', html)
            self.assertIn('class="chevron" aria-hidden="true"', html)
            self.assertIn('href="/xhs">发布小红书</a>', html)
            self.assertLess(html.index('id="dateInput"'), html.index('id="menuBtn"'))
            self.assertLess(html.index('id="menuBtn"'), html.index('id="refreshBtn"'))
            self.assertIn("AbortController", html)
            self.assertIn('/api/tasks/open', html)
            self.assertIn('/api/operation/stop', html)
            self.assertIn('/static/vendor/marked-16.2.1.umd.js', html)
            self.assertIn('/static/vendor/dompurify-3.2.6.min.js', html)
            self.assertIn("DOMPurify.sanitize", html)
            self.assertNotIn('<pre id="dailyText"', html)
            self.assertNotIn("<h2>日内更新</h2>", html)

    def test_xhs_page_contains_publish_controls(self) -> None:
        with _temporary_directory() as temp_dir:
            config = _test_config(Path(temp_dir))
            client = TestClient(create_app(config=config, provider_factory=FakeProvider))

            response = client.get("/xhs")

            self.assertEqual(response.status_code, 200)
            html = response.text
            self.assertIn("发布小红书", html)
            self.assertIn('id="startMcpBtn"', html)
            self.assertIn('id="openDraftBtn"', html)
            self.assertIn("打开草稿", html)
            self.assertLess(html.index("文本路径"), html.index('id="openDraftBtn"'))
            self.assertLess(html.index('id="openDraftBtn"'), html.index("图片路径（每行一张）"))
            self.assertIn('id="publishBtn"', html)
            self.assertIn('id="draftInput"', html)
            self.assertIn('id="imageInput"', html)
            self.assertIn('id="imageState"', html)
            self.assertIn('id="visibilityInput"', html)
            self.assertIn("可见范围", html)
            self.assertIn("图片路径（每行一张）", html)
            self.assertIn("标题</label>", html)
            self.assertIn("标签（可选）", html)
            self.assertIn("定时发布（可选）", html)
            self.assertIn("原创标记（可选）", html)
            self.assertIn('id="startMcpBtn" type="button" disabled', html)
            self.assertIn("statusLoading", html)
            self.assertIn("missingRequiredFields", html)
            self.assertIn("请补全", html)
            self.assertNotIn('id="coverPreview"', html)
            self.assertNotIn("默认封面", html)
            self.assertIn("/api/xhs/start", html)
            self.assertIn("/api/xhs/stop", html)
            self.assertIn("/api/xhs/draft/open", html)
            self.assertNotIn("/api/xhs/account/refresh", html)
            self.assertIn("/api/xhs/publish", html)
            self.assertIn("/api/xhs/path-status", html)
            self.assertIn("schedulePathStatus", html)
            self.assertIn("toggleMcp", html)
            self.assertIn("关闭 MCP", html)

    def test_xhs_defaults_use_today_draft_and_default_cover(self) -> None:
        with _temporary_directory() as temp_dir:
            config = _test_config(Path(temp_dir))
            post_image_dir = Path(config["paths"]["post_image_dir"])
            post_image_dir.mkdir(parents=True)
            (post_image_dir / "xiushenlu-xhs-cover.png").write_bytes(b"fake")
            client = TestClient(create_app(config=config, provider_factory=FakeProvider))

            response = client.get("/api/xhs/defaults")

            self.assertEqual(response.status_code, 200)
            data = response.json()
            today = date.today().isoformat()
            self.assertEqual(data["draft_path"], str((Path(config["paths"]["post_dir"]) / f"{today}.txt").resolve()))
            self.assertEqual(data["image_path"], str((post_image_dir / "xiushenlu-xhs-cover.png").resolve()))
            self.assertEqual(data["visibility"], "公开可见")
            self.assertEqual(data["visibility_options"], ["仅自己可见", "公开可见", "仅互关好友可见"])
            self.assertEqual(data["title"], "")
            self.assertEqual(data["tags"], [])

    def test_xhs_open_draft_creates_input_file_and_uses_vscode(self) -> None:
        with _temporary_directory() as temp_dir:
            config = _test_config(Path(temp_dir))
            client = TestClient(create_app(config=config, provider_factory=FakeProvider))
            draft_path = (Path(config["paths"]["post_dir"]) / "manual-name.txt").resolve()

            with patch("app.console._open_path_with_vscode") as open_path:
                response = client.post("/api/xhs/draft/open", json={"draft": str(draft_path)})

            self.assertEqual(response.status_code, 200)
            data = response.json()
            self.assertEqual(Path(data["result"]["draft_path"]), draft_path)
            self.assertTrue(data["result"]["created"])
            self.assertTrue(draft_path.exists())
            self.assertEqual(draft_path.read_text(encoding="utf-8"), "")
            open_path.assert_called_once_with(draft_path)

    def test_xhs_open_draft_preserves_existing_file(self) -> None:
        with _temporary_directory() as temp_dir:
            config = _test_config(Path(temp_dir))
            draft_path = Path(config["paths"]["post_dir"]) / "existing-name.txt"
            draft_path.parent.mkdir(parents=True)
            draft_path.write_text("已有草稿", encoding="utf-8")
            client = TestClient(create_app(config=config, provider_factory=FakeProvider))

            with patch("app.console._open_path_with_vscode") as open_path:
                response = client.post("/api/xhs/draft/open", json={"draft": str(draft_path)})

            self.assertEqual(response.status_code, 200)
            self.assertFalse(response.json()["result"]["created"])
            self.assertEqual(draft_path.read_text(encoding="utf-8"), "已有草稿")
            open_path.assert_called_once_with(draft_path.resolve())

    def test_xhs_open_draft_reports_vscode_error(self) -> None:
        with _temporary_directory() as temp_dir:
            config = _test_config(Path(temp_dir))
            client = TestClient(create_app(config=config, provider_factory=FakeProvider))
            draft_path = Path(config["paths"]["post_dir"]) / "vscode-error.txt"

            with patch("app.console._open_path_with_vscode", side_effect=RuntimeError("未找到 VS Code 命令行 `code`。")):
                response = client.post("/api/xhs/draft/open", json={"draft": str(draft_path)})

            self.assertEqual(response.status_code, 400)
            self.assertIn("VS Code", response.json()["detail"])

    def test_xhs_open_draft_rejects_path_outside_post_dir(self) -> None:
        with _temporary_directory() as temp_dir:
            config = _test_config(Path(temp_dir))
            client = TestClient(create_app(config=config, provider_factory=FakeProvider))
            outside_path = Path(config["paths"]["inbox_dir"]) / "not-post-draft.txt"

            with patch("app.console._open_path_with_vscode") as open_path:
                response = client.post("/api/xhs/draft/open", json={"draft": str(outside_path)})

            self.assertEqual(response.status_code, 400)
            self.assertIn("post/data", response.json()["detail"])
            open_path.assert_not_called()

    def test_xhs_path_status_reports_local_files_and_urls(self) -> None:
        with _temporary_directory() as temp_dir:
            config = _test_config(Path(temp_dir))
            draft_path = Path(config["paths"]["post_dir"]) / "2026-05-14.txt"
            image_path = Path(config["paths"]["post_image_dir"]) / "cover.png"
            draft_path.parent.mkdir(parents=True)
            image_path.parent.mkdir(parents=True)
            draft_path.write_text("正文", encoding="utf-8")
            image_path.write_bytes(b"fake")
            client = TestClient(create_app(config=config, provider_factory=FakeProvider))

            response = client.post(
                "/api/xhs/path-status",
                json={
                    "draft": str(draft_path),
                    "images": [str(image_path), "https://example.com/cover.png", str(image_path.parent / "missing.png")],
                },
            )

            self.assertEqual(response.status_code, 200)
            data = response.json()
            self.assertTrue(data["draft"]["exists"])
            self.assertTrue(data["draft"]["is_file"])
            self.assertEqual(data["draft"]["message"], "文件存在")
            self.assertTrue(data["images"][0]["exists"])
            self.assertEqual(data["images"][0]["message"], "文件存在")
            self.assertEqual(data["images"][1]["kind"], "url")
            self.assertEqual(data["images"][1]["message"], "远程 URL")
            self.assertIsNone(data["images"][1]["exists"])
            self.assertFalse(data["images"][2]["exists"])
            self.assertEqual(data["images"][2]["message"], "文件不存在")

    def test_xhs_start_does_not_open_login_when_mcp_is_available(self) -> None:
        with _temporary_directory() as temp_dir:
            config = _test_config(Path(temp_dir))
            _create_fake_xhs_executables(config)
            xhs_client = FakeXhsClient()
            starts: list[tuple[Path, Path, bool]] = []
            client = TestClient(
                create_app(
                    config=config,
                    provider_factory=FakeProvider,
                    xhs_client_factory=lambda: xhs_client,  # type: ignore[arg-type]
                    process_starter=lambda exe, cwd, hidden: starts.append((exe, cwd, hidden)),
                )
            )

            response = client.post("/api/xhs/start")

            self.assertEqual(response.status_code, 200)
            self.assertIn("已连接", response.json()["message"])
            self.assertEqual(starts, [])
            self.assertNotIn("check_login_status", [name for name, _ in xhs_client.calls])

    def test_xhs_status_uses_mcp_connection_without_process_scan(self) -> None:
        with _temporary_directory() as temp_dir:
            config = _test_config(Path(temp_dir))
            _create_fake_xhs_executables(config)
            xhs_client = FakeXhsClient()
            client = TestClient(
                create_app(
                    config=config,
                    provider_factory=FakeProvider,
                    xhs_client_factory=lambda: xhs_client,  # type: ignore[arg-type]
                    process_finder=lambda exe: self.fail("状态加载不应扫描 Windows 进程"),
                )
            )

            response = client.get("/api/xhs/status")

            self.assertEqual(response.status_code, 200)
            data = response.json()
            self.assertTrue(data["mcp_running"])
            self.assertTrue(data["can_stop"])
            self.assertEqual(data["pids"], [])
            self.assertNotIn("check_login_status", [name for name, _ in xhs_client.calls])

    def test_xhs_status_reports_connected(self) -> None:
        with _temporary_directory() as temp_dir:
            config = _test_config(Path(temp_dir))
            _create_fake_xhs_executables(config)
            xhs_client = FakeXhsClient()
            client = TestClient(
                create_app(
                    config=config,
                    provider_factory=FakeProvider,
                    xhs_client_factory=lambda: xhs_client,  # type: ignore[arg-type]
                    process_finder=lambda exe: [],
                )
            )

            response = client.get("/api/xhs/status")

            self.assertEqual(response.status_code, 200)
            data = response.json()
            self.assertFalse(data["logged_in"])
            self.assertEqual(data["text"], "MCP 已连接")
            self.assertNotIn("check_login_status", [name for name, _ in xhs_client.calls])

    def test_xhs_status_simplifies_disconnected_error(self) -> None:
        with _temporary_directory() as temp_dir:
            config = _test_config(Path(temp_dir))
            _create_fake_xhs_executables(config)
            client = TestClient(
                create_app(
                    config=config,
                    provider_factory=FakeProvider,
                    xhs_client_factory=lambda: FakeXhsClient(connect_error=True),  # type: ignore[arg-type]
                    process_finder=lambda exe: [],
                )
            )

            response = client.get("/api/xhs/status")

            self.assertEqual(response.status_code, 200)
            data = response.json()
            self.assertFalse(data["connected"])
            self.assertEqual(data["error"], "未连接")

    def test_xhs_stop_closes_all_xhs_mcp_processes(self) -> None:
        with _temporary_directory() as temp_dir:
            config = _test_config(Path(temp_dir))
            _create_fake_xhs_executables(config)
            active_pids = [321, 654]
            stopped: list[int] = []
            xhs_client = FakeXhsClient()

            def finder(exe: Path) -> list[int]:
                return list(active_pids)

            def stopper(pids: list[int]) -> None:
                stopped.extend(pids)
                active_pids.clear()
                xhs_client.connect_error = True

            client = TestClient(
                create_app(
                    config=config,
                    provider_factory=FakeProvider,
                    xhs_client_factory=lambda: xhs_client,  # type: ignore[arg-type]
                    process_finder=finder,
                    process_stopper=stopper,
                )
            )

            response = client.post("/api/xhs/stop")

            self.assertEqual(response.status_code, 200)
            self.assertEqual(stopped, [321, 654])
            data = response.json()
            self.assertIn("已关闭", data["message"])
            self.assertFalse(data["result"]["mcp_running"])
            self.assertFalse(data["result"]["can_stop"])
            self.assertEqual(data["result"]["pids"], [])

    def test_xhs_stop_is_friendly_when_no_configured_mcp_process_exists(self) -> None:
        with _temporary_directory() as temp_dir:
            config = _test_config(Path(temp_dir))
            _create_fake_xhs_executables(config)
            stopped: list[int] = []
            client = TestClient(
                create_app(
                    config=config,
                    provider_factory=FakeProvider,
                    xhs_client_factory=lambda: FakeXhsClient(),  # type: ignore[arg-type]
                    process_finder=lambda exe: [],
                    process_stopper=lambda pids: stopped.extend(pids),
                )
            )

            response = client.post("/api/xhs/stop")

            self.assertEqual(response.status_code, 200)
            self.assertIn("没有找到", response.json()["message"])
            self.assertEqual(stopped, [])

    def test_xhs_process_lookup_still_uses_configured_name_as_fallback(self) -> None:
        with _temporary_directory() as temp_dir:
            config = _test_config(Path(temp_dir))
            _create_fake_xhs_executables(config)
            configured = Path(config["xiaohongshu"]["mcp_exe"]).resolve()
            seen_paths: list[Path] = []

            def finder(exe: Path) -> list[int]:
                seen_paths.append(exe)
                return []

            client = TestClient(
                create_app(
                    config=config,
                    provider_factory=FakeProvider,
                    xhs_client_factory=lambda: FakeXhsClient(),  # type: ignore[arg-type]
                    process_finder=finder,
                    process_stopper=lambda pids: self.fail("不应关闭路径不匹配的进程"),
                )
            )

            response = client.post("/api/xhs/stop")

            self.assertEqual(response.status_code, 200)
            self.assertEqual(seen_paths, [configured])

    def test_xhs_start_runs_mcp_when_disconnected(self) -> None:
        with _temporary_directory() as temp_dir:
            config = _test_config(Path(temp_dir))
            _create_fake_xhs_executables(config)
            clients = [
                FakeXhsClient(connect_error=True),
                FakeXhsClient(),
            ]
            starts: list[tuple[Path, Path, bool]] = []
            client = TestClient(
                create_app(
                    config=config,
                    provider_factory=FakeProvider,
                    xhs_client_factory=lambda: clients.pop(0),  # type: ignore[arg-type]
                    process_starter=lambda exe, cwd, hidden: starts.append((exe, cwd, hidden)),
                )
            )

            response = client.post("/api/xhs/start")

            self.assertEqual(response.status_code, 200)
            self.assertIn("已连接", response.json()["message"])
            self.assertEqual(len(starts), 1)
            self.assertEqual(starts[0][0], Path(config["xiaohongshu"]["mcp_exe"]).resolve())
            self.assertTrue(starts[0][2])

    def test_xhs_publish_uses_approve_true_and_visibility(self) -> None:
        with _temporary_directory() as temp_dir:
            config = _test_config(Path(temp_dir))
            draft_path = Path(config["paths"]["post_dir"]) / "2026-05-14.txt"
            draft_path.parent.mkdir(parents=True)
            draft_path.write_text("发布页面测试正文。", encoding="utf-8")
            xhs_client = FakeXhsClient()
            client = TestClient(
                create_app(
                    config=config,
                    provider_factory=FakeProvider,
                    xhs_client_factory=lambda: xhs_client,  # type: ignore[arg-type]
                )
            )

            response = client.post(
                "/api/xhs/publish",
                json={
                    "draft": str(draft_path),
                    "title": "发布页面测试",
                    "images": ["https://example.com/a.png"],
                    "tags": ["修身炉"],
                    "visibility": "公开可见",
                },
            )

            self.assertEqual(response.status_code, 200)
            self.assertEqual(
                [name for name, _ in xhs_client.calls],
                ["publish_content"],
            )
            self.assertEqual(xhs_client.calls[-1][1]["visibility"], "公开可见")
            self.assertEqual(response.json()["result"]["visibility"], "公开可见")

    def test_local_markdown_vendor_assets_are_served(self) -> None:
        with _temporary_directory() as temp_dir:
            config = _test_config(Path(temp_dir))
            client = TestClient(create_app(config=config, provider_factory=FakeProvider))

            marked_response = client.get("/static/vendor/marked-16.2.1.umd.js")
            purify_response = client.get("/static/vendor/dompurify-3.2.6.min.js")

            self.assertEqual(marked_response.status_code, 200)
            self.assertIn("marked v16.2.1", marked_response.text)
            self.assertEqual(purify_response.status_code, 200)
            self.assertIn("DOMPurify 3.2.6", purify_response.text)

    def test_state_returns_existing_daily_without_events_or_tokens(self) -> None:
        with _temporary_directory() as temp_dir:
            config = _test_config(Path(temp_dir))
            daily_dir = Path(config["paths"]["daily_dir"])
            daily_dir.mkdir(parents=True)
            (daily_dir / "2026-05-03.md").write_text("# 2026-05-03\n\n## 记录\n\n- 已启动控制台\n", encoding="utf-8")
            client = TestClient(create_app(config=config, provider_factory=FakeProvider))

            response = client.get("/api/state?date=2026-05-03")

            self.assertEqual(response.status_code, 200)
            data = response.json()
            self.assertEqual(data["date"], "2026-05-03")
            self.assertIn("已启动控制台", data["daily"]["text"])
            self.assertNotIn("events", data)
            self.assertNotIn("tokens", data)

    def test_tasks_endpoint_writes_today_tasks_without_llm(self) -> None:
        with _temporary_directory() as temp_dir:
            config = _test_config(Path(temp_dir))
            provider = FakeProvider()
            client = TestClient(create_app(config=config, provider_factory=lambda: provider))

            response = client.post("/api/tasks", json={"tasks": "学习控制台保存待办"})

            self.assertEqual(response.status_code, 200)
            tasks_path = Path(response.json()["result"]["today_tasks_path"])
            saved_tasks = tasks_path.read_text(encoding="utf-8")
            self.assertEqual(saved_tasks, "学习控制台保存待办\n")
            self.assertEqual(provider.prompts, [])
            self.assertEqual(list(Path(config["paths"]["logs_dir"]).glob("*.jsonl")), [])

    def test_user_notes_endpoint_persists_misc_and_long_plan(self) -> None:
        with _temporary_directory() as temp_dir:
            config = _test_config(Path(temp_dir))
            provider = FakeProvider()
            client = TestClient(create_app(config=config, provider_factory=lambda: provider))

            response = client.post(
                "/api/user-notes",
                json={
                    "misc": "买菜\n整理发票",
                    "long_plan": "半年内完成知识库升级",
                },
            )

            self.assertEqual(response.status_code, 200)
            inbox_dir = Path(config["paths"]["inbox_dir"])
            self.assertEqual((inbox_dir / "杂事.md").read_text(encoding="utf-8"), "买菜\n整理发票\n")
            self.assertEqual((inbox_dir / "长远计划.md").read_text(encoding="utf-8"), "半年内完成知识库升级\n")
            state = response.json()["state"]["user_notes"]
            self.assertEqual(state["misc"]["text"], "买菜\n整理发票")
            self.assertEqual(state["long_plan"]["text"], "半年内完成知识库升级")
            self.assertEqual(provider.prompts, [])

    def test_open_tasks_endpoint_ensures_file_and_uses_default_app(self) -> None:
        with _temporary_directory() as temp_dir:
            config = _test_config(Path(temp_dir))
            client = TestClient(create_app(config=config, provider_factory=FakeProvider))

            with patch("app.console._open_path_with_default_app") as open_path:
                response = client.post("/api/tasks/open")

            self.assertEqual(response.status_code, 200)
            tasks_path = Path(response.json()["result"]["today_tasks_path"])
            self.assertTrue(tasks_path.exists())
            self.assertEqual(tasks_path.read_text(encoding="utf-8"), "")
            open_path.assert_called_once_with(tasks_path)

    def test_operation_stop_without_active_operation_is_friendly(self) -> None:
        with _temporary_directory() as temp_dir:
            config = _test_config(Path(temp_dir))
            client = TestClient(create_app(config=config, provider_factory=FakeProvider))

            state_response = client.get("/api/operation")
            stop_response = client.post("/api/operation/stop")

            self.assertEqual(state_response.status_code, 200)
            self.assertFalse(state_response.json()["active"])
            self.assertEqual(stop_response.status_code, 200)
            self.assertIn("当前没有正在运行", stop_response.json()["message"])
            self.assertFalse(stop_response.json()["operation"]["active"])

    def test_stopped_plan_discards_late_llm_result_without_writes(self) -> None:
        with _temporary_directory() as temp_dir:
            config = _test_config(Path(temp_dir))
            inbox_dir = Path(config["paths"]["inbox_dir"])
            inbox_dir.mkdir(parents=True)
            (inbox_dir / "today_tasks.md").write_text("# 今日待办\n\n不要落盘", encoding="utf-8")
            started = threading.Event()
            release = threading.Event()
            provider = BlockingProvider(started, release)
            client = TestClient(create_app(config=config, provider_factory=lambda: provider))

            with ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(lambda: client.post("/api/plan", json={}))
                self.assertTrue(started.wait(timeout=5))

                operation_response = client.get("/api/operation")
                stop_response = client.post("/api/operation/stop")
                release.set()
                plan_response = future.result(timeout=5)

            self.assertEqual(operation_response.status_code, 200)
            self.assertTrue(operation_response.json()["active"])
            self.assertEqual(operation_response.json()["label"], "生成计划")
            self.assertEqual(stop_response.status_code, 200)
            self.assertTrue(stop_response.json()["operation"]["cancel_requested"])
            self.assertEqual(plan_response.status_code, 409)
            self.assertIn("LLM 返回结果已丢弃", plan_response.json()["detail"])
            self.assertEqual(list(Path(config["paths"]["daily_dir"]).glob("*.md")), [])
            self.assertEqual(list(Path(config["paths"]["logs_dir"]).glob("*.jsonl")), [])
            self.assertEqual((inbox_dir / "today_tasks.md").read_text(encoding="utf-8"), "# 今日待办\n\n不要落盘")

            final_operation = client.get("/api/operation")
            self.assertEqual(final_operation.status_code, 200)
            self.assertFalse(final_operation.json()["active"])

    def test_plan_endpoint_reads_saved_today_tasks(self) -> None:
        with _temporary_directory() as temp_dir:
            config = _test_config(Path(temp_dir))
            inbox_dir = Path(config["paths"]["inbox_dir"])
            memory_dir = Path(config["paths"]["memory_dir"])
            inbox_dir.mkdir(parents=True)
            memory_dir.mkdir(parents=True)
            (inbox_dir / "today_tasks.md").write_text("# 今日待办\n\n本地保存的任务", encoding="utf-8")
            provider = FakeProvider()
            client = TestClient(create_app(config=config, provider_factory=lambda: provider))

            response = client.post("/api/plan", json={})

            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["message"], "计划已生成。耗时40s")
            self.assertEqual(len(provider.prompts), 1)
            self.assertIn("本地保存的任务", provider.prompts[0])
            self.assertIn("控制台测试计划", response.json()["state"]["daily"]["text"])

    def test_plan_update_endpoint_updates_original_and_schedule_table(self) -> None:
        with _temporary_directory() as temp_dir:
            config = _test_config(Path(temp_dir))
            inbox_dir = Path(config["paths"]["inbox_dir"])
            daily_dir = Path(config["paths"]["daily_dir"])
            inbox_dir.mkdir(parents=True)
            daily_dir.mkdir(parents=True)
            today = date.today().isoformat()
            (inbox_dir / "today_tasks.md").write_text("# 今日待办\n\n原任务\n", encoding="utf-8")
            (daily_dir / f"{today}.md").write_text(
                f"# {today}\n\n"
                "## 计划\n\n"
                "**今日待办**\n\n"
                "原任务\n\n"
                "| 任务 | 优先级 | 预计 | 状态 | 备注 |\n"
                "|---|---|---|---|---|\n"
                "| 原任务 | P1 | 1h |  |  |\n",
                encoding="utf-8",
            )
            provider = FakeProvider()
            client = TestClient(create_app(config=config, provider_factory=lambda: provider))

            response = client.post("/api/plan", json={"add": "新增任务"})

            self.assertEqual(response.status_code, 200)
            data = response.json()
            self.assertEqual(data["message"], "计划已局部更新。耗时40s")
            self.assertIn("target_heading", data["result"])
            self.assertNotIn("new_task_advice", data["result"])
            daily_text = data["state"]["daily"]["text"]
            self.assertIn("【待办】\n1. 原任务\n2. 新增任务", daily_text)
            self.assertIn("| 新增任务 | P2 | 30m |  |  |", daily_text)
            self.assertNotIn("**新任务**", daily_text)

    def test_plan_endpoint_returns_provider_error_without_500(self) -> None:
        with _temporary_directory() as temp_dir:
            config = _test_config(Path(temp_dir))
            inbox_dir = Path(config["paths"]["inbox_dir"])
            inbox_dir.mkdir(parents=True)
            (inbox_dir / "today_tasks.md").write_text("# 今日待办\n\n触发模型错误", encoding="utf-8")
            client = TestClient(create_app(config=config, provider_factory=ErrorProvider))

            response = client.post("/api/plan", json={})

            self.assertEqual(response.status_code, 400)
            self.assertIn("DashScope 调用失败", response.json()["detail"])

    def test_log_endpoint_writes_daily_and_event(self) -> None:
        with _temporary_directory() as temp_dir:
            config = _test_config(Path(temp_dir))
            client = TestClient(create_app(config=config, provider_factory=FakeProvider))

            response = client.post("/api/log", json={"content": "控制台写入记录"})

            self.assertEqual(response.status_code, 200)
            daily_path = Path(response.json()["result"]["daily_path"])
            self.assertIn("控制台写入记录", daily_path.read_text(encoding="utf-8"))
            logs = list(Path(config["paths"]["logs_dir"]).glob("*.jsonl"))
            self.assertEqual(len(logs), 1)
            self.assertIn("user_log", logs[0].read_text(encoding="utf-8"))

    def test_log_endpoint_updates_schedule_table_when_present(self) -> None:
        with _temporary_directory() as temp_dir:
            config = _test_config(Path(temp_dir))
            daily_dir = Path(config["paths"]["daily_dir"])
            daily_dir.mkdir(parents=True)
            today = date.today().isoformat()
            (daily_dir / f"{today}.md").write_text(
                f"# {today}\n\n"
                "## 计划\n\n"
                "**今日待办**\n\n"
                "控制台任务\n\n"
                "**时间安排**\n\n"
                "| 任务 | 优先级 | 预估时间 | 完成 | 备注 |\n"
                "|---|---|---|---|---|\n"
                "| 控制台任务 | P1 | 30m |  |  |\n\n"
                "## 记录\n\n",
                encoding="utf-8",
            )
            provider = FakeProvider()
            client = TestClient(create_app(config=config, provider_factory=lambda: provider))

            response = client.post("/api/log", json={"content": "完成控制台任务。后续注意：保留两列白名单。"})

            self.assertEqual(response.status_code, 200)
            data = response.json()
            self.assertIn("任务表已更新", data["message"])
            self.assertTrue(data["result"]["schedule_updated"])
            daily_text = data["state"]["daily"]["text"]
            self.assertIn("| 控制台任务 | P1 | 30m | ✓ | 保留两列白名单 |", daily_text)
            self.assertEqual(len(provider.prompts), 1)

    def test_review_endpoint_rolls_over_today_tasks(self) -> None:
        with _temporary_directory() as temp_dir:
            config = _test_config(Path(temp_dir))
            today = date.today().isoformat()
            inbox_dir = Path(config["paths"]["inbox_dir"])
            daily_dir = Path(config["paths"]["daily_dir"])
            inbox_dir.mkdir(parents=True)
            daily_dir.mkdir(parents=True)
            (inbox_dir / "today_tasks.md").write_text("# 今日待办\n\n原任务\n", encoding="utf-8")
            (inbox_dir / "明日计划.md").write_text("控制台明日任务\n", encoding="utf-8")
            (daily_dir / f"{today}.md").write_text(f"# {today}\n\n## 记录\n\n- 控制台记录\n", encoding="utf-8")
            client = TestClient(create_app(config=config, provider_factory=FakeProvider))

            response = client.post("/api/review", json={"date": today})

            self.assertEqual(response.status_code, 200)
            daily_text = response.json()["state"]["daily"]["text"]
            self.assertIn("控制台测试复盘", daily_text)
            self.assertIn("token 消耗统计", daily_text)
            self.assertIn("今日 LLM 调用：1 次", daily_text)
            self.assertIn("今日 token 数：18", daily_text)
            self.assertIn("本月 LLM 调用：1 次", daily_text)
            self.assertIn("本月 token 数：18", daily_text)
            self.assertNotIn("输入 token", daily_text)
            self.assertNotIn("输出 token", daily_text)
            self.assertIn("控制台明日任务", (inbox_dir / "today_tasks.md").read_text(encoding="utf-8"))
            self.assertEqual((inbox_dir / "明日计划.md").read_text(encoding="utf-8"), "")

    def test_review_endpoint_can_skip_rollover(self) -> None:
        with _temporary_directory() as temp_dir:
            config = _test_config(Path(temp_dir))
            today = date.today().isoformat()
            inbox_dir = Path(config["paths"]["inbox_dir"])
            daily_dir = Path(config["paths"]["daily_dir"])
            inbox_dir.mkdir(parents=True)
            daily_dir.mkdir(parents=True)
            (inbox_dir / "today_tasks.md").write_text("# 今日待办\n\n原任务\n", encoding="utf-8")
            (inbox_dir / "明日计划.md").write_text("控制台明日任务\n", encoding="utf-8")
            (daily_dir / f"{today}.md").write_text(f"# {today}\n\n## 记录\n\n- 控制台记录\n", encoding="utf-8")
            client = TestClient(create_app(config=config, provider_factory=FakeProvider))

            response = client.post("/api/review", json={"date": today, "rollover": False})

            self.assertEqual(response.status_code, 200)
            daily_text = response.json()["state"]["daily"]["text"]
            self.assertIn("控制台测试计划", daily_text)
            self.assertNotIn("token 消耗统计", daily_text)
            self.assertEqual((inbox_dir / "today_tasks.md").read_text(encoding="utf-8"), "# 今日待办\n\n原任务\n")
            self.assertEqual((inbox_dir / "明日计划.md").read_text(encoding="utf-8"), "控制台明日任务\n")

    def test_cost_endpoint_updates_token_section_once(self) -> None:
        with _temporary_directory() as temp_dir:
            config = _test_config(Path(temp_dir))
            inbox_dir = Path(config["paths"]["inbox_dir"])
            memory_dir = Path(config["paths"]["memory_dir"])
            inbox_dir.mkdir(parents=True)
            memory_dir.mkdir(parents=True)
            (inbox_dir / "today_tasks.md").write_text("# 今日待办\n\n统计 token", encoding="utf-8")
            provider = FakeProvider()
            client = TestClient(create_app(config=config, provider_factory=lambda: provider))

            plan_response = client.post("/api/plan", json={})
            self.assertEqual(plan_response.status_code, 200)

            first_response = client.post("/api/cost", json={})
            second_response = client.post("/api/cost", json={})

            self.assertEqual(first_response.status_code, 200)
            self.assertEqual(second_response.status_code, 200)
            data = second_response.json()
            self.assertEqual(data["message"], "token 统计已更新。")
            daily_text = data["state"]["daily"]["text"]
            self.assertEqual(daily_text.count("## token 消耗统计"), 1)
            self.assertIn("今日 LLM 调用：1 次", daily_text)
            self.assertIn("今日 token 数：18", daily_text)
            self.assertIn("本月 LLM 调用：1 次", daily_text)
            self.assertIn("本月 token 数：18", daily_text)
            self.assertNotIn("输入 token", daily_text)
            self.assertNotIn("输出 token", daily_text)

    def test_plan_update_rejects_empty_add(self) -> None:
        with _temporary_directory() as temp_dir:
            config = _test_config(Path(temp_dir))
            client = TestClient(create_app(config=config, provider_factory=FakeProvider))

            response = client.post("/api/plan", json={"add": "  "})

            self.assertEqual(response.status_code, 400)
            self.assertIn("新增任务不能为空", response.json()["detail"])

    def test_plan_endpoint_rejects_tasks_field(self) -> None:
        with _temporary_directory() as temp_dir:
            config = _test_config(Path(temp_dir))
            client = TestClient(create_app(config=config, provider_factory=FakeProvider))

            response = client.post("/api/plan", json={"tasks": "旧的 plan tasks 模式"})

            self.assertEqual(response.status_code, 422)


def _test_config(root: Path) -> dict[str, Any]:
    paths = {
        "daily_dir": str(root / "user_records"),
        "inbox_dir": str(root / "user_inputs"),
        "memory_dir": str(root / "memory"),
        "logs_dir": str(root / "system_logs"),
        "state_dir": str(root / "state"),
        "quarantine_dir": str(root / "quarantine"),
        "post_dir": str(root / "post" / "data"),
        "post_image_dir": str(root / "post" / "images"),
    }
    xhs_dir = root / "xiaohongshu-mcp"
    return {
        "paths": paths,
        "xiaohongshu": {
            "mcp_url": "http://localhost:18060/mcp",
            "timeout": 1,
            "working_dir": str(xhs_dir),
            "mcp_exe": str(xhs_dir / "xiaohongshu-mcp.exe"),
            "login_exe": str(xhs_dir / "xiaohongshu-login.exe"),
        },
        "safety": {
            "allowed_dirs": list(paths.values()),
            "protected_files": [str(root / "memory" / "goals.md")],
        },
    }


def _create_fake_xhs_executables(config: dict[str, Any]) -> None:
    xhs = config["xiaohongshu"]
    working_dir = Path(xhs["working_dir"])
    working_dir.mkdir(parents=True, exist_ok=True)
    Path(xhs["mcp_exe"]).write_text("fake mcp", encoding="utf-8")
    Path(xhs["login_exe"]).write_text("fake login", encoding="utf-8")


class _temporary_directory:
    def __enter__(self) -> str:
        parent = Path("workspace") / "test_console"
        parent.mkdir(parents=True, exist_ok=True)
        self.path = parent / uuid.uuid4().hex
        self.path.mkdir(parents=True)
        return str(self.path)

    def __exit__(self, exc_type, exc, tb) -> None:
        shutil.rmtree(self.path, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
