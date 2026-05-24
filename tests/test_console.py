from __future__ import annotations

import json
import shutil
import threading
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path
from types import SimpleNamespace
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
                            "evidence": "完成控制台任务",
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
            self.assertNotIn("本机入口", html)
            self.assertIn('id="sloganInput"', html)
            self.assertNotIn('id="sloganToggle"', html)
            self.assertIn('$("sloganInput").addEventListener("focus"', html)
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
            self.assertIn('class="warn reserved-action is-placeholder" id="stopBtn"', html)
            self.assertIn('button.classList.toggle("is-placeholder", !operationActive);', html)
            self.assertIn('button.setAttribute("aria-hidden", String(!operationActive));', html)
            self.assertIn('id="menuBtn"', html)
            self.assertIn('class="menu-button"', html)
            self.assertIn('aria-label="菜单"', html)
            self.assertIn('aria-haspopup="menu"', html)
            self.assertIn('class="menu-icon" aria-hidden="true"', html)
            self.assertNotIn("<span>发布</span>", html)
            self.assertIn('href="/task-tree">工作树</a>', html)
            self.assertNotIn('href="/task-tree/edit"', html)
            self.assertIn('href="/xhs">发布小红书</a>', html)
            self.assertIn('width: 132px;', html)
            self.assertLess(html.index('id="dateInput"'), html.index('id="menuBtn"'))
            self.assertLess(html.index('id="dateInput"'), html.index('id="refreshBtn"'))
            self.assertLess(html.index('id="refreshBtn"'), html.index('id="stopBtn"'))
            self.assertLess(html.index('id="stopBtn"'), html.index('id="menuBtn"'))
            self.assertIn("AbortController", html)
            self.assertIn('/api/tasks/open', html)
            self.assertIn('/api/operation/stop', html)
            self.assertIn('$("dateInput").addEventListener("change"', html)
            self.assertIn('const selectedDate = $("dateInput").value;', html)
            self.assertIn('$("reviewDateInput").value = selectedDate;', html)
            self.assertIn("loadState(selectedDate);", html)
            self.assertIn('/static/vendor/marked-16.2.1.umd.js', html)
            self.assertIn('/static/vendor/dompurify-3.2.6.min.js', html)
            self.assertIn("DOMPurify.sanitize", html)
            self.assertNotIn('<pre id="dailyText"', html)
            self.assertNotIn("<h2>日内更新</h2>", html)

    def test_task_tree_page_uses_work_tree_editor(self) -> None:
        with _temporary_directory() as temp_dir:
            config = _test_config(Path(temp_dir))
            client = TestClient(create_app(config=config, provider_factory=FakeProvider))

            response = client.get("/task-tree")

            self.assertEqual(response.status_code, 200)
            html = response.text
            self.assertIn("工作树", html)
            self.assertIn("工作树视图", html)
            self.assertIn("/static/vendor/simple-mind-map-0.14.0-fix.2/simpleMindMap.umd.min.js", html)
            self.assertIn("/static/vendor/simple-mind-map-0.14.0-fix.2/simpleMindMap.esm.min.css", html)
            self.assertIn('id="treeTitleInput"', html)
            self.assertIn('id="treeJsonInput"', html)
            self.assertIn('id="treeSelect"', html)
            self.assertIn('id="inputPanel"', html)
            self.assertIn('id="inputSummary"', html)
            self.assertIn('id="mindMap"', html)
            self.assertIn('id="workspace"', html)
            self.assertIn('id="leftCollapseBtn"', html)
            self.assertIn('id="rightCollapseBtn"', html)
            self.assertIn('id="nodeContentInput"', html)
            self.assertIn('class="canvas-tools"', html)
            self.assertIn('id="renderBtn"', html)
            self.assertIn('id="syncJsonBtn"', html)
            self.assertIn('id="saveBtn"', html)
            self.assertIn('id="editModeBtn"', html)
            self.assertIn('id="undoBtn"', html)
            self.assertIn('id="redoBtn"', html)
            self.assertIn('id="deleteNodeBtn"', html)
            self.assertIn('id="deleteFileBtn"', html)
            self.assertIn('aria-label="删除选中文件"', html)
            self.assertIn('class="source-divider"', html)
            self.assertIn('class="source-actions"', html)
            self.assertIn(">保存到本地</button>", html)
            self.assertIn('id="refreshSavedBtn"', html)
            self.assertIn(">选择文件</label>", html)
            self.assertIn(">保存标题</label>", html)
            self.assertIn(">节点标题</label>", html)
            self.assertIn("JSON 导入 / 导出", html)
            self.assertIn('aria-label="刷新文件列表"', html)
            self.assertNotIn('id="reloadBtn"', html)
            self.assertNotIn(">重读</button>", html)
            self.assertLess(html.index('id="treeSelect"'), html.index('class="source-divider"'))
            self.assertLess(html.index('class="source-divider"'), html.index('id="treeTitleInput"'))
            self.assertLess(html.index('id="inputPanel"'), html.index('id="saveBtn"'))
            self.assertIn('id="expandBtn"', html)
            self.assertIn('id="collapseBtn"', html)
            self.assertIn("/api/task-tree", html)
            self.assertIn("?filename=", html)
            self.assertIn('layout: "organizationStructure"', html)
            self.assertIn('mousewheelAction: "zoom"', html)
            self.assertIn('state.mindMap.execCommand("UNEXPAND_ALL")', html)
            self.assertIn("taskTreeToMindRoot", html)
            self.assertIn("mindRootToTaskTree", html)
            self.assertIn("jsonDraftDirty", html)
            self.assertIn("createNodeActionContent", html)
            self.assertIn('state.mindMap.execCommand("INSERT_CHILD_NODE"', html)
            self.assertIn('state.mindMap.execCommand("INSERT_NODE"', html)
            self.assertIn('state.mindMap.execCommand("REMOVE_NODE"', html)
            self.assertIn('state.mindMap.execCommand("SET_NODE_TEXT"', html)
            self.assertIn('state.mindMap.execCommand("SET_NODE_DATA"', html)
            self.assertIn('runHistoryCommand("BACK")', html)
            self.assertIn('runHistoryCommand("FORWARD")', html)
            self.assertIn("JSON 输入有未应用更改", html)
            self.assertIn("addCustomContentToNode", html)
            self.assertIn("updateSelectedContent", html)
            self.assertIn("updateSelectedTitle", html)
            self.assertIn("setLeftCollapsed", html)
            self.assertIn("setRightCollapsed", html)
            self.assertIn('id="nodePreview"', html)
            self.assertIn("node_mouseenter", html)
            self.assertIn("node_mouseleave", html)
            self.assertNotIn("node-preview-kind", html)
            self.assertNotIn("node-preview-title", html)
            self.assertIn("/api/task-tree/delete", html)
            self.assertIn("确定删除工作树文件", html)
            self.assertNotIn("contentEdits", html)
            self.assertNotIn("state.jsonDirty", html)
            self.assertNotIn('id="treeView"', html)
            self.assertNotIn("node-content", html)
            self.assertNotIn("note: node.content", html)
            self.assertNotIn('id="zoomInBtn"', html)
            self.assertNotIn('id="zoomOutBtn"', html)

    def test_task_tree_edit_route_is_removed(self) -> None:
        with _temporary_directory() as temp_dir:
            config = _test_config(Path(temp_dir))
            client = TestClient(create_app(config=config, provider_factory=FakeProvider))

            response = client.get("/task-tree/edit")

            self.assertEqual(response.status_code, 404)

    def test_task_tree_api_saves_title_named_json_file(self) -> None:
        with _temporary_directory() as temp_dir:
            config = _test_config(Path(temp_dir))
            client = TestClient(create_app(config=config, provider_factory=FakeProvider))
            text = json.dumps(
                {
                    "title": "JSON 内标题",
                    "nodes": [
                        {
                            "title": "每日记录",
                            "kind": "habit",
                            "cadence": "daily",
                            "status": "todo",
                            "children": [],
                        }
                    ],
                },
                ensure_ascii=False,
            )

            save_response = client.post(
                "/api/task-tree",
                json={"title": "我的长期任务", "text": text},
            )
            load_response = client.get("/api/task-tree?filename=我的长期任务.json")

            self.assertEqual(save_response.status_code, 200)
            self.assertEqual(load_response.status_code, 200)
            data = save_response.json()
            result = data["result"]
            expected_path = Path(config["paths"]["task_tree_dir"]) / "我的长期任务.json"
            self.assertEqual(result["filename"], "我的长期任务.json")
            self.assertEqual(Path(result["path"]), expected_path.resolve())
            self.assertEqual(result["tree"]["title"], "JSON 内标题")
            self.assertTrue(expected_path.exists())
            self.assertIn('"title": "JSON 内标题"', expected_path.read_text(encoding="utf-8"))
            self.assertEqual(data["state"]["selected"]["filename"], "我的长期任务.json")
            state = load_response.json()
            self.assertEqual(state["selected"]["filename"], "我的长期任务.json")
            self.assertEqual(state["items"][0]["filename"], "我的长期任务.json")

    def test_task_tree_api_lists_root_files_and_loads_by_filename(self) -> None:
        with _temporary_directory() as temp_dir:
            config = _test_config(Path(temp_dir))
            tree_dir = Path(config["paths"]["task_tree_dir"])
            tree_dir.mkdir(parents=True)
            (tree_dir / "第一棵.json").write_text('{"title":"JSON 第一棵","nodes":[]}\n', encoding="utf-8")
            (tree_dir / "第二棵.json").write_text('{"title":"JSON 第二棵","nodes":[]}\n', encoding="utf-8")
            child_dir = tree_dir / "子目录"
            child_dir.mkdir()
            (child_dir / "不加载.json").write_text('{"title":"子目录","nodes":[]}\n', encoding="utf-8")
            client = TestClient(create_app(config=config, provider_factory=FakeProvider))

            response = client.get("/api/task-tree?filename=第二棵.json")

            self.assertEqual(response.status_code, 200)
            data = response.json()
            self.assertEqual({item["filename"] for item in data["items"]}, {"第一棵.json", "第二棵.json"})
            self.assertEqual(data["selected"]["filename"], "第二棵.json")
            self.assertEqual(data["selected"]["title"], "第二棵")
            self.assertEqual(data["selected"]["text"], '{"title":"JSON 第二棵","nodes":[]}\n')
            self.assertEqual(data["selected"]["tree"]["title"], "JSON 第二棵")

    def test_task_tree_api_missing_filename_falls_back_to_first_file(self) -> None:
        with _temporary_directory() as temp_dir:
            config = _test_config(Path(temp_dir))
            tree_dir = Path(config["paths"]["task_tree_dir"])
            tree_dir.mkdir(parents=True)
            (tree_dir / "仍存在.json").write_text('{"title":"还在","nodes":[]}\n', encoding="utf-8")
            client = TestClient(create_app(config=config, provider_factory=FakeProvider))

            response = client.get("/api/task-tree?filename=已删除.json")

            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["selected"]["filename"], "仍存在.json")

    def test_task_tree_api_deletes_selected_file_and_returns_refreshed_state(self) -> None:
        with _temporary_directory() as temp_dir:
            config = _test_config(Path(temp_dir))
            tree_dir = Path(config["paths"]["task_tree_dir"])
            tree_dir.mkdir(parents=True)
            deleted_file = tree_dir / "要删除.json"
            kept_file = tree_dir / "保留.json"
            deleted_file.write_text('{"title":"要删除","nodes":[]}\n', encoding="utf-8")
            kept_file.write_text('{"title":"保留","nodes":[]}\n', encoding="utf-8")
            client = TestClient(create_app(config=config, provider_factory=FakeProvider))

            response = client.post("/api/task-tree/delete", json={"filename": "要删除.json"})

            self.assertEqual(response.status_code, 200)
            data = response.json()
            self.assertEqual(data["result"]["filename"], "要删除.json")
            self.assertFalse(deleted_file.exists())
            self.assertTrue(kept_file.exists())
            self.assertEqual(data["state"]["selected"]["filename"], "保留.json")
            self.assertEqual({item["filename"] for item in data["state"]["items"]}, {"保留.json"})

    def test_task_tree_api_rejects_nested_delete_filename(self) -> None:
        with _temporary_directory() as temp_dir:
            config = _test_config(Path(temp_dir))
            client = TestClient(create_app(config=config, provider_factory=FakeProvider))

            response = client.post("/api/task-tree/delete", json={"filename": "子目录/不要删.json"})

            self.assertEqual(response.status_code, 400)
            self.assertIn("根目录", response.json()["detail"])

    def test_task_tree_api_rejects_invalid_json_without_overwriting(self) -> None:
        with _temporary_directory() as temp_dir:
            config = _test_config(Path(temp_dir))
            tree_dir = Path(config["paths"]["task_tree_dir"])
            tree_dir.mkdir(parents=True)
            existing = tree_dir / "已有任务.json"
            existing.write_text('{"title":"旧内容","nodes":[]}\n', encoding="utf-8")
            client = TestClient(create_app(config=config, provider_factory=FakeProvider))

            response = client.post(
                "/api/task-tree",
                json={"title": "已有任务", "text": "{not json"},
            )

            self.assertEqual(response.status_code, 400)
            self.assertIn("JSON 解析失败", response.json()["detail"])
            self.assertEqual(existing.read_text(encoding="utf-8"), '{"title":"旧内容","nodes":[]}\n')

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
            self.assertNotIn('id="draftPreview"', html)
            self.assertNotIn('id="draftPreviewState"', html)
            self.assertIn('id="imageInput"', html)
            self.assertIn('id="imageState"', html)
            self.assertIn('id="generateCoverBtn"', html)
            self.assertIn("生成封面", html)
            self.assertIn('id="openImagesBtn"', html)
            self.assertIn("打开图片", html)
            self.assertIn('id="visibilityInput"', html)
            self.assertIn("可见范围", html)
            self.assertIn("图片路径（每行一张）", html)
            self.assertIn("标题</label>", html)
            self.assertIn("标签（可选）", html)
            self.assertIn("定时发布（可选）", html)
            self.assertLess(html.index("标题</label>"), html.index("可见范围"))
            self.assertLess(html.index("可见范围"), html.index("标签（可选）"))
            self.assertLess(html.index("标签（可选）"), html.index("定时发布（可选）"))
            self.assertIn("原创标记（可选）", html)
            self.assertIn('id="originalInput"', html)
            self.assertIn('is_original: $("originalInput").checked', html)
            self.assertIn('id="startMcpBtn" type="button" disabled', html)
            self.assertIn("statusLoading", html)
            self.assertIn("missingRequiredFields", html)
            self.assertIn("请补全", html)
            self.assertNotIn('id="coverPreview"', html)
            self.assertNotIn("默认封面", html)
            self.assertIn("/api/xhs/start", html)
            self.assertIn("/api/xhs/stop", html)
            self.assertIn("/api/xhs/draft/open", html)
            self.assertNotIn("/api/xhs/draft/read", html)
            self.assertIn("/api/xhs/cover/generate", html)
            self.assertIn("/api/xhs/images/open", html)
            self.assertNotIn("/api/xhs/account/refresh", html)
            self.assertIn("/api/xhs/publish", html)
            self.assertIn("/api/xhs/path-status", html)
            self.assertIn("schedulePathStatus", html)
            self.assertIn("openImages", html)
            self.assertNotIn("loadDraftPreview", html)
            self.assertNotIn("selectedDraftText", html)
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
            self.assertIn("data/post/data", response.json()["detail"])
            open_path.assert_not_called()

    def test_xhs_generate_cover_downloads_image_and_returns_local_path(self) -> None:
        with _temporary_directory() as temp_dir:
            config = _test_config(Path(temp_dir))
            draft_path = Path(config["paths"]["post_dir"]) / "cover-source.txt"
            draft_path.parent.mkdir(parents=True)
            draft_text = "封面第一行\n封面内容"
            draft_path.write_text(draft_text, encoding="utf-8")
            client = TestClient(create_app(config=config, provider_factory=FakeProvider))
            image_url = "https://example.com/generated.png"
            response_payload = SimpleNamespace(
                status_code=200,
                request_id="req-cover-1",
                output=SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            message=SimpleNamespace(content=[{"image": image_url}])
                        )
                    ]
                ),
                usage={"image_count": 1, "width": 2048, "height": 2048},
            )

            with (
                patch.dict("os.environ", {"DASHSCOPE_API_KEY": "test-key"}),
                patch(
                    "app.posting.xhs_cover.MultiModalConversation.call",
                    return_value=response_payload,
                ) as dashscope_call,
                patch("app.posting.xhs_cover._download_image_bytes", return_value=b"png-bytes") as download,
            ):
                response = client.post("/api/xhs/cover/generate", json={"draft": str(draft_path)})

            self.assertEqual(response.status_code, 200)
            result = response.json()["result"]
            expected_path = (
                Path(config["paths"]["post_image_dir"]) / f"{date.today().isoformat()}_封面第一行.png"
            ).resolve()
            self.assertEqual(Path(result["image_path"]), expected_path)
            self.assertEqual(result["model"], "fake-cover-model")
            self.assertEqual(result["image_count"], 1)
            self.assertEqual(expected_path.read_bytes(), b"png-bytes")
            download.assert_called_once_with(image_url, 30.0)
            log_path = Path(config["paths"]["logs_dir"]) / f"{date.today().isoformat()}.jsonl"
            events = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual([event["type"] for event in events], ["xhs_cover_generated", "image_generation_usage"])
            self.assertEqual(events[1]["detail"]["image_count"], 1)
            self.assertEqual(events[1]["detail"]["width"], 2048)
            self.assertEqual(events[1]["detail"]["height"], 2048)
            self.assertEqual(events[1]["detail"]["request_id"], "req-cover-1")
            kwargs = dashscope_call.call_args.kwargs
            self.assertEqual(kwargs["api_key"], "test-key")
            self.assertEqual(kwargs["model"], "fake-cover-model")
            self.assertEqual(kwargs["n"], 1)
            self.assertTrue(kwargs["watermark"])
            self.assertEqual(kwargs["negative_prompt"], "")
            self.assertIn(draft_text, kwargs["messages"][0]["content"][0]["text"])

    def test_xhs_generate_cover_uses_suffix_when_name_exists(self) -> None:
        with _temporary_directory() as temp_dir:
            config = _test_config(Path(temp_dir))
            image_dir = Path(config["paths"]["post_image_dir"])
            image_dir.mkdir(parents=True)
            first_path = image_dir / f"{date.today().isoformat()}_重复标题.png"
            first_path.write_bytes(b"old")
            draft_path = Path(config["paths"]["post_dir"]) / "duplicate-title.txt"
            draft_path.parent.mkdir(parents=True)
            draft_path.write_text("重复标题\n内容", encoding="utf-8")
            client = TestClient(create_app(config=config, provider_factory=FakeProvider))
            response_payload = SimpleNamespace(
                status_code=200,
                output=SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            message=SimpleNamespace(content=[{"image": "https://example.com/generated.png"}])
                        )
                    ]
                ),
            )

            with (
                patch.dict("os.environ", {"DASHSCOPE_API_KEY": "test-key"}),
                patch("app.posting.xhs_cover.MultiModalConversation.call", return_value=response_payload),
                patch("app.posting.xhs_cover._download_image_bytes", return_value=b"new"),
            ):
                response = client.post("/api/xhs/cover/generate", json={"draft": str(draft_path)})

            self.assertEqual(response.status_code, 200)
            expected_path = (image_dir / f"{date.today().isoformat()}_重复标题-2.png").resolve()
            self.assertEqual(Path(response.json()["result"]["image_path"]), expected_path)
            self.assertEqual(first_path.read_bytes(), b"old")
            self.assertEqual(expected_path.read_bytes(), b"new")

    def test_xhs_generate_cover_rejects_empty_draft(self) -> None:
        with _temporary_directory() as temp_dir:
            config = _test_config(Path(temp_dir))
            draft_path = Path(config["paths"]["post_dir"]) / "empty.txt"
            draft_path.parent.mkdir(parents=True)
            draft_path.write_text("  ", encoding="utf-8")
            client = TestClient(create_app(config=config, provider_factory=FakeProvider))

            response = client.post("/api/xhs/cover/generate", json={"draft": str(draft_path)})

            self.assertEqual(response.status_code, 400)
            self.assertIn("草稿正文为空", response.json()["detail"])

    def test_xhs_generate_cover_reports_missing_image_url(self) -> None:
        with _temporary_directory() as temp_dir:
            config = _test_config(Path(temp_dir))
            draft_path = Path(config["paths"]["post_dir"]) / "no-image-url.txt"
            draft_path.parent.mkdir(parents=True)
            draft_path.write_text("封面标题", encoding="utf-8")
            client = TestClient(create_app(config=config, provider_factory=FakeProvider))
            response_payload = SimpleNamespace(
                status_code=200,
                output=SimpleNamespace(
                    choices=[
                        SimpleNamespace(message=SimpleNamespace(content=[{"text": "no image"}]))
                    ]
                ),
            )

            with (
                patch.dict("os.environ", {"DASHSCOPE_API_KEY": "test-key"}),
                patch("app.posting.xhs_cover.MultiModalConversation.call", return_value=response_payload),
            ):
                response = client.post("/api/xhs/cover/generate", json={"draft": str(draft_path)})

            self.assertEqual(response.status_code, 400)
            self.assertIn("未返回图片", response.json()["detail"])

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

    def test_xhs_open_images_opens_all_local_files_and_urls(self) -> None:
        with _temporary_directory() as temp_dir:
            config = _test_config(Path(temp_dir))
            image_a = Path(config["paths"]["post_image_dir"]) / "cover-a.png"
            image_b = Path(config["paths"]["post_image_dir"]) / "cover-b.png"
            image_a.parent.mkdir(parents=True)
            image_a.write_bytes(b"a")
            image_b.write_bytes(b"b")
            client = TestClient(create_app(config=config, provider_factory=FakeProvider))

            opened: list[str] = []
            with patch("app.console._open_value_with_default_app", side_effect=lambda value: opened.append(value)):
                response = client.post(
                    "/api/xhs/images/open",
                    json={"images": [str(image_a), "https://example.com/cover.png", str(image_b)]},
                )

            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["result"]["images_count"], 3)
            self.assertEqual(opened, [str(image_a.resolve()), "https://example.com/cover.png", str(image_b.resolve())])

    def test_xhs_open_images_rejects_missing_local_file(self) -> None:
        with _temporary_directory() as temp_dir:
            config = _test_config(Path(temp_dir))
            missing = Path(config["paths"]["post_image_dir"]) / "missing.png"
            client = TestClient(create_app(config=config, provider_factory=FakeProvider))

            with patch("app.console._open_value_with_default_app") as opener:
                response = client.post("/api/xhs/images/open", json={"images": [str(missing)]})

            self.assertEqual(response.status_code, 400)
            self.assertIn("图片文件不存在", response.json()["detail"])
            opener.assert_not_called()

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
                    "is_original": True,
                },
            )

            self.assertEqual(response.status_code, 200)
            self.assertEqual(
                [name for name, _ in xhs_client.calls],
                ["publish_content"],
            )
            self.assertEqual(xhs_client.calls[-1][1]["visibility"], "公开可见")
            self.assertTrue(xhs_client.calls[-1][1]["is_original"])
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
                "| 任务 | 优先级 | 预计 | 状态 | 用时 |\n"
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
                "| 任务 | 优先级 | 预计 | 状态 | 用时 |\n"
                "|---|---|---|---|---|\n"
                "| 控制台任务 | P1 | 30m |  |  |\n\n"
                "## 记录\n\n",
                encoding="utf-8",
            )
            provider = FakeProvider()
            client = TestClient(create_app(config=config, provider_factory=lambda: provider))

            response = client.post("/api/log", json={"content": "完成控制台任务。"})

            self.assertEqual(response.status_code, 200)
            data = response.json()
            self.assertIn("任务表已更新", data["message"])
            self.assertTrue(data["result"]["schedule_updated"])
            daily_text = data["state"]["daily"]["text"]
            self.assertIn("| 控制台任务 | P1 | 30m | ✓ |  |", daily_text)
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
            self.assertIn("今日文生图图片数：0 张", daily_text)
            self.assertIn("本月文生图图片数：0 张", daily_text)
            self.assertNotIn("输入 token", daily_text)
            self.assertNotIn("输出 token", daily_text)

    def test_cost_endpoint_reports_image_generation_count_without_call_count(self) -> None:
        with _temporary_directory() as temp_dir:
            config = _test_config(Path(temp_dir))
            logs_dir = Path(config["paths"]["logs_dir"])
            logs_dir.mkdir(parents=True)
            today = date.today().isoformat()
            events = [
                {
                    "ts": f"{today}T08:00:00+00:00",
                    "type": "image_generation_usage",
                    "summary": "文生图生成图片",
                    "detail": {"model": "fake-cover-model", "image_count": 2},
                },
                {
                    "ts": f"{today}T08:01:00+00:00",
                    "type": "xhs_cover_generated",
                    "summary": "小红书封面已生成",
                    "detail": {"model": "fake-cover-model", "image_count": 2},
                },
            ]
            (logs_dir / f"{today}.jsonl").write_text(
                "\n".join(json.dumps(event, ensure_ascii=False) for event in events) + "\n",
                encoding="utf-8",
            )
            client = TestClient(create_app(config=config, provider_factory=FakeProvider))

            response = client.post("/api/cost", json={})

            self.assertEqual(response.status_code, 200)
            daily_text = response.json()["state"]["daily"]["text"]
            self.assertIn("今日 LLM 调用：0 次", daily_text)
            self.assertIn("今日 token 数：0", daily_text)
            self.assertIn("今日文生图图片数：2 张", daily_text)
            self.assertIn("本月文生图图片数：2 张", daily_text)
            self.assertNotIn("文生图调用", daily_text)

    def test_plan_update_rejects_empty_add(self) -> None:
        with _temporary_directory() as temp_dir:
            config = _test_config(Path(temp_dir))
            client = TestClient(create_app(config=config, provider_factory=FakeProvider))

            response = client.post("/api/plan", json={"add": "  "})

            self.assertEqual(response.status_code, 400)
            self.assertIn("新增任务不能为空", response.json()["detail"])

    def test_plan_endpoint_saves_submitted_tasks_before_generating(self) -> None:
        with _temporary_directory() as temp_dir:
            config = _test_config(Path(temp_dir))
            provider = FakeProvider()
            client = TestClient(create_app(config=config, provider_factory=lambda: provider))

            response = client.post("/api/plan", json={"tasks": "输入框里的新任务"})

            self.assertEqual(response.status_code, 200)
            inbox_dir = Path(config["paths"]["inbox_dir"])
            self.assertEqual((inbox_dir / "today_tasks.md").read_text(encoding="utf-8"), "输入框里的新任务\n")
            self.assertEqual(len(provider.prompts), 1)
            self.assertIn("输入框里的新任务", provider.prompts[0])
            self.assertIn("输入框里的新任务", response.json()["state"]["daily"]["text"])


def _test_config(root: Path) -> dict[str, Any]:
    paths = {
        "daily_dir": str(root / "user_records"),
        "inbox_dir": str(root / "user_inputs"),
        "memory_dir": str(root / "memory"),
        "logs_dir": str(root / "system_logs"),
        "state_dir": str(root / "state"),
        "quarantine_dir": str(root / "quarantine"),
        "task_tree_dir": str(root / "task_tree"),
        "post_dir": str(root / "data" / "post" / "data"),
        "post_image_dir": str(root / "data" / "post" / "images"),
    }
    xhs_dir = root / "xiaohongshu-mcp"
    return {
        "llm": {
            "api_key_env": "DASHSCOPE_API_KEY",
            "timeout": 30,
        },
        "paths": paths,
        "xiaohongshu": {
            "mcp_url": "http://localhost:18060/mcp",
            "timeout": 1,
            "publish_timeout": 60,
            "cover_model": "fake-cover-model",
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
