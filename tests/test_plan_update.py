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
    NEW_TASK_ADVICE_MAX_CHARS,
    PlanUpdateParseError,
    _build_prompt,
    format_new_task_entry,
    generate_plan_update,
    parse_plan_update_response,
    update_daily_plan_text,
    validate_plan_update_content,
)


class FakeProvider(LLMProvider):
    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.last_prompt: str | None = None
        self.last_usage: LLMCallUsage | None = None

    def chat(self, prompt: str) -> str:
        self.last_prompt = prompt
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

    def test_build_prompt_keeps_new_task_verbatim_and_limits_advice(self) -> None:
        prompt = _build_prompt(
            date_text="2026-05-04",
            goals="长期目标",
            today_tasks="修身炉：\n1. 原任务\n2. 继续任务\n\n杂事：\n游泳或篮球\n扫地拖地",
            daily_text=(
                "## 计划\n\n**1. 今日待办原文**\n\n"
                "修身炉：\n1. 原任务\n2. 继续任务\n\n杂事：\n游泳或篮球\n扫地拖地"
            ),
            new_task="灵感：发现了codex可以优化的一些rule，比如去除常错的命令，提示plan mode和commit",
        )

        self.assertIn("逐字插入", prompt)
        self.assertIn("不能改写、概括或扩写", prompt)
        self.assertIn("插入前先判断目标分组下已有任务行的主格式", prompt)
        self.assertIn("都不要包含任何 Markdown 标题行", prompt)
        self.assertIn("禁止输出 `# 今日待办`、`#今日待办`、`## ...`", prompt)
        self.assertIn("新增项必须使用下一个编号", prompt)
        self.assertIn("普通文本行，不加", prompt)
        self.assertIn("只有目标分组原本就是", prompt)
        self.assertIn("6. 灵感：优化codex规则", prompt)
        self.assertIn("看鸡汤和英雄传记（调节今日心情）", prompt)
        self.assertIn("不要加", prompt)
        self.assertIn(f"不超过 {NEW_TASK_ADVICE_MAX_CHARS} 字", prompt)
        self.assertIn("不要写“### 新增”“原文”“归入标题”", prompt)
        self.assertIn("只用于内部归类，不会展示在 daily", prompt)

    def test_parse_plan_update_response_rejects_non_json(self) -> None:
        with self.assertRaises(PlanUpdateParseError):
            parse_plan_update_response("```json\n{}\n```")

    def test_validate_plan_update_content_rejects_rewritten_new_task(self) -> None:
        parsed = parse_plan_update_response(
            json.dumps(
                {
                    "updated_today_tasks": "修身炉：\n- 灵感：优化codex规则（去除常错命令，增加plan mode和commit提示）",
                    "updated_daily_original": "修身炉：\n- 灵感：优化codex规则（去除常错命令，增加plan mode和commit提示）",
                    "target_heading": "修身炉",
                    "new_task_advice": "- 优先级：P1",
                },
                ensure_ascii=False,
            )
        )

        with self.assertRaisesRegex(PlanUpdateParseError, "verbatim"):
            validate_plan_update_content(
                parsed,
                new_task="灵感：发现了codex可以优化的一些rule，比如去除常错的命令，提示plan mode和commit",
            )

    def test_validate_plan_update_content_rejects_duplicate_new_task_metadata(self) -> None:
        new_task = "学习python"
        parsed = parse_plan_update_response(
            json.dumps(
                {
                    "updated_today_tasks": f"学习：\n{new_task}",
                    "updated_daily_original": f"学习：\n{new_task}",
                    "target_heading": "学习",
                    "new_task_advice": f"### 新增：{new_task}\n\n- 优先级：P2",
                },
                ensure_ascii=False,
            )
        )

        with self.assertRaisesRegex(PlanUpdateParseError, "duplicate task"):
            validate_plan_update_content(parsed, new_task=new_task)

    def test_validate_plan_update_content_rejects_long_advice(self) -> None:
        new_task = "学习python"
        parsed = parse_plan_update_response(
            json.dumps(
                {
                    "updated_today_tasks": f"学习：\n{new_task}",
                    "updated_daily_original": f"学习：\n{new_task}",
                    "target_heading": "学习",
                    "new_task_advice": "建议" * (NEW_TASK_ADVICE_MAX_CHARS + 1),
                },
                ensure_ascii=False,
            )
        )

        with self.assertRaisesRegex(PlanUpdateParseError, "no longer than"):
            validate_plan_update_content(parsed, new_task=new_task)

    def test_format_new_task_entry_omits_metadata_lines(self) -> None:
        entry = format_new_task_entry(
            new_task="xiushenlu：刚才的update没有调好，还需要继续调试输出。",
            new_task_advice="- 优先级：P1\n- 任务建议：先收紧展示格式。",
        )

        self.assertIn("### 新增：xiushenlu：刚才的update没有调好，还需要继续调试输出。", entry)
        self.assertNotIn("- 原文：", entry)
        self.assertNotIn("- 归入标题：", entry)

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
                new_task_advice="- 优先级：P2",
            ),
        )

        self.assertIn("## 计划", updated)
        self.assertIn("**今日待办原文**", updated)
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
            self.assertNotIn("- 原文：学习python", daily_text)
            self.assertNotIn("- 归入标题：学习", daily_text)
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
