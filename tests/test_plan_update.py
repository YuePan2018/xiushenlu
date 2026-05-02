from __future__ import annotations

import json
import shutil
import unittest
import uuid
from datetime import date
from pathlib import Path
from typing import Any

from app.llm.provider import LLMCallUsage, LLMProvider
from app.pipelines.plan_update import (
    PlanUpdateParseError,
    format_new_task_entry,
    generate_plan_update,
    parse_plan_update_response,
    update_daily_plan_text,
)


class FakeProvider(LLMProvider):
    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.last_usage: LLMCallUsage | None = None

    def chat(self, prompt: str) -> str:
        self.last_usage = LLMCallUsage(
            model="fake-model",
            tokens_in=10,
            tokens_out=20,
            total_tokens=30,
            estimated=False,
            raw=None,
        )
        return self.reply


class FakeLogger:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def append_event(self, type: str, summary: str, detail: Any | None = None) -> dict[str, Any]:
        event = {"type": type, "summary": summary, "detail": detail}
        self.events.append(event)
        return event


class PlanUpdateTests(unittest.TestCase):
    def test_parse_plan_update_response(self) -> None:
        payload = {
            "updated_today_tasks": "# 今日待办\n\n学习：\n学习python",
            "updated_daily_original": "**学习：**\n学习python",
            "target_heading": "学习",
            "new_task_advice": "- 优先级：P2",
        }

        parsed = parse_plan_update_response(json.dumps(payload, ensure_ascii=False))

        self.assertEqual(parsed.target_heading, "学习")
        self.assertIn("学习python", parsed.updated_today_tasks)

    def test_parse_plan_update_response_rejects_non_json(self) -> None:
        with self.assertRaises(PlanUpdateParseError):
            parse_plan_update_response("```json\n{}\n```")

    def test_update_daily_replaces_original_without_rewriting_existing_advice(self) -> None:
        daily = """# 2026-04-30

## 计划

生成时间：2026-04-30 11:02:58

**1. 今日待办原文**

**学习：**
视频：Pencil + Codex 实现 AI 日程助理 App

**2. 计划建议与时间预估**

旧建议内容

## 记录

- 10:00 开始
"""
        entry = format_new_task_entry(
            new_task="学习python",
            target_heading="学习",
            new_task_advice="- 优先级：P2\n- 任务建议：控制在 45 分钟内。",
        )

        updated = update_daily_plan_text(
            daily_text=daily,
            date_text="2026-04-30",
            updated_daily_original="**学习：**\n视频：Pencil + Codex 实现 AI 日程助理 App\n学习python",
            new_task_entry=entry,
        )

        self.assertIn("学习python", updated)
        self.assertIn("旧建议内容", updated)
        self.assertIn("## 记录", updated)
        self.assertEqual(updated.count("**新任务**"), 1)
        self.assertLess(updated.index("学习python"), updated.index("旧建议内容"))

    def test_update_daily_appends_to_existing_new_task_section(self) -> None:
        daily = """# 2026-04-30

## 计划

**1. 今日待办原文**

原文

**2. 计划建议**

建议

**新任务**

### 新增：第一个任务

- 原文：第一个任务
"""

        updated = update_daily_plan_text(
            daily_text=daily,
            date_text="2026-04-30",
            updated_daily_original="原文\n第二个任务",
            new_task_entry=format_new_task_entry(
                new_task="第二个任务",
                target_heading="学习",
                new_task_advice="- 优先级：P3",
            ),
        )

        self.assertEqual(updated.count("**新任务**"), 1)
        self.assertLess(updated.index("第一个任务"), updated.rindex("第二个任务"))

    def test_update_daily_creates_minimal_plan_when_missing(self) -> None:
        updated = update_daily_plan_text(
            daily_text="# 2026-04-30\n\n## 记录\n\n- 已有记录\n",
            date_text="2026-04-30",
            updated_daily_original="学习python",
            new_task_entry=format_new_task_entry(
                new_task="学习python",
                target_heading="学习",
                new_task_advice="- 优先级：P2",
            ),
        )

        self.assertIn("## 计划", updated)
        self.assertIn("**1. 今日待办原文**", updated)
        self.assertIn("**新任务**", updated)
        self.assertIn("## 记录", updated)
        self.assertLess(updated.index("## 计划"), updated.index("## 记录"))

    def test_generate_plan_update_writes_files_and_events(self) -> None:
        with _temporary_directory() as temp_dir:
            config = _test_config(Path(temp_dir))
            inbox = Path(config["paths"]["inbox_dir"])
            daily_dir = Path(config["paths"]["daily_dir"])
            inbox.mkdir(parents=True)
            daily_dir.mkdir(parents=True)
            (inbox / "today_tasks.md").write_text(
                "# 今日待办\n\n学习：\n视频：Pencil + Codex 实现 AI 日程助理 App\n",
                encoding="utf-8",
            )
            (daily_dir / "2026-04-30.md").write_text(
                "# 2026-04-30\n\n## 计划\n\n**1. 今日待办原文**\n\n**学习：**\n视频\n\n**2. 计划建议**\n\n已有建议\n",
                encoding="utf-8",
            )
            reply = json.dumps(
                {
                    "updated_today_tasks": "# 今日待办\n\n学习：\n视频：Pencil + Codex 实现 AI 日程助理 App\n学习python",
                    "updated_daily_original": "**学习：**\n视频\n学习python",
                    "target_heading": "学习",
                    "new_task_advice": "- 优先级：P2\n- 任务建议：先完成当前视频，再补 python。",
                },
                ensure_ascii=False,
            )
            logger = FakeLogger()

            result = generate_plan_update(
                FakeProvider(reply),
                "学习python",
                config=config,
                target_date=date(2026, 4, 30),
                logger=logger,  # type: ignore[arg-type]
            )

            self.assertEqual(result.target_heading, "学习")
            self.assertIn("学习python", (inbox / "today_tasks.md").read_text(encoding="utf-8"))
            daily_text = (daily_dir / "2026-04-30.md").read_text(encoding="utf-8")
            self.assertIn("**新任务**", daily_text)
            self.assertEqual([event["type"] for event in logger.events], ["llm_call", "plan_updated"])

    def test_generate_plan_update_does_not_write_files_on_parse_error(self) -> None:
        with _temporary_directory() as temp_dir:
            config = _test_config(Path(temp_dir))
            inbox = Path(config["paths"]["inbox_dir"])
            daily_dir = Path(config["paths"]["daily_dir"])
            inbox.mkdir(parents=True)
            daily_dir.mkdir(parents=True)
            today_tasks = inbox / "today_tasks.md"
            daily_file = daily_dir / "2026-04-30.md"
            today_tasks.write_text("# 今日待办\n\n学习：\n视频\n", encoding="utf-8")
            daily_file.write_text("# 2026-04-30\n\n## 计划\n\n原计划\n", encoding="utf-8")
            logger = FakeLogger()

            with self.assertRaises(PlanUpdateParseError):
                generate_plan_update(
                    FakeProvider("not json"),
                    "学习python",
                    config=config,
                    target_date=date(2026, 4, 30),
                    logger=logger,  # type: ignore[arg-type]
                )

            self.assertEqual(today_tasks.read_text(encoding="utf-8"), "# 今日待办\n\n学习：\n视频\n")
            self.assertEqual(daily_file.read_text(encoding="utf-8"), "# 2026-04-30\n\n## 计划\n\n原计划\n")
            self.assertEqual([event["type"] for event in logger.events], ["llm_call"])


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
        parent = Path("workspace") / "test_plan_update"
        parent.mkdir(parents=True, exist_ok=True)
        self.path = parent / uuid.uuid4().hex
        self.path.mkdir(parents=True)
        return str(self.path)

    def __exit__(self, exc_type, exc, tb) -> None:
        shutil.rmtree(self.path, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
