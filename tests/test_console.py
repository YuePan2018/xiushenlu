from __future__ import annotations

import json
import shutil
import unittest
import uuid
from datetime import date
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from app.console import create_app
from app.llm.provider import LLMCallUsage, LLMProvider


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
                    "new_task_advice": "- 优先级：P2\n- 任务建议：控制范围。",
                },
                ensure_ascii=False,
            )
        return self.reply


class ConsoleTests(unittest.TestCase):
    def test_index_uses_local_markdown_renderer_assets(self) -> None:
        with _temporary_directory() as temp_dir:
            config = _test_config(Path(temp_dir))
            client = TestClient(create_app(config=config, provider_factory=FakeProvider))

            response = client.get("/")

            self.assertEqual(response.status_code, 200)
            html = response.text
            self.assertIn('<article id="dailyText" class="daily-markdown empty"', html)
            self.assertIn('/static/vendor/marked-16.2.1.umd.js', html)
            self.assertIn('/static/vendor/dompurify-3.2.6.min.js', html)
            self.assertIn("DOMPurify.sanitize", html)
            self.assertNotIn('<pre id="dailyText"', html)

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
            self.assertEqual(len(provider.prompts), 1)
            self.assertIn("本地保存的任务", provider.prompts[0])
            self.assertIn("控制台测试计划", response.json()["state"]["daily"]["text"])

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
            self.assertIn("输入 token：7", daily_text)
            self.assertIn("输出 token：11", daily_text)
            self.assertIn("控制台明日任务", (inbox_dir / "today_tasks.md").read_text(encoding="utf-8"))
            self.assertEqual((inbox_dir / "明日计划.md").read_text(encoding="utf-8"), "")

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
    }
    return {
        "paths": paths,
        "safety": {
            "allowed_dirs": list(paths.values()),
            "protected_files": [str(root / "memory" / "goals.md")],
        },
    }


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
