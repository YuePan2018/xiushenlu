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
    ScheduleRow,
    _build_prompt,
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
            "updated_daily_original": "学习：\n学习python",
            "target_heading": "学习",
            "schedule_task": "学习python",
            "schedule_priority": "P2",
            "schedule_estimate": "45m",
        }

        parsed = parse_plan_update_response(json.dumps(payload, ensure_ascii=False))

        self.assertEqual(parsed.target_heading, "学习")
        self.assertEqual(parsed.schedule_row, ScheduleRow("学习python", "P2", "45m"))
        self.assertIn("学习python", parsed.updated_today_tasks)

    def test_build_prompt_requests_original_and_schedule_row_only(self) -> None:
        prompt = _build_prompt(
            date_text="2026-05-04",
            goals="长期目标",
            today_tasks="修身炉：\n1. 原任务\n2. 继续任务\n\n杂事：\n游泳或篮球\n扫地拖地",
            daily_text=(
                "## 计划\n\n**今日待办**\n\n"
                "修身炉：\n1. 原任务\n2. 继续任务\n\n"
                "| 任务 | 优先级 | 预计 | 状态 | 用时 |\n"
                "|---|---|---|---|---|\n"
                "| 原任务 | P1 | 1h |  |  |"
            ),
            new_task="灵感：发现了codex可以优化的一些rule，比如去除常错的命令，提示plan mode和commit",
        )

        self.assertIn("逐字插入", prompt)
        self.assertIn("不能改写、概括或扩写", prompt)
        self.assertIn("schedule_task", prompt)
        self.assertIn("schedule_priority", prompt)
        self.assertIn("schedule_estimate", prompt)
        self.assertIn("状态由程序写空", prompt)
        self.assertIn("xiushenlu维护", prompt)
        self.assertIn("不会使用预计和用时", prompt)
        self.assertIn("冒号前是目标分组标题", prompt)
        self.assertIn("分组展示必须统一为“【分组】”", prompt)
        self.assertIn("新增“杂事： 游泳”，必须新建或使用“【杂事】”", prompt)
        self.assertIn("必须逐字等于新增任务正文", prompt)
        self.assertIn("不能缩短、概括或扩写", prompt)
        self.assertIn("不要输出状态、用时、单独建议正文", prompt)
        self.assertNotIn("new_task_advice", prompt)
        self.assertIn("不要输出状态、用时、单独建议正文、“新任务”标题或“### 新增”", prompt)

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
                    "schedule_task": "优化codex规则",
                    "schedule_priority": "P1",
                    "schedule_estimate": "30m",
                },
                ensure_ascii=False,
            )
        )

        with self.assertRaisesRegex(PlanUpdateParseError, "verbatim"):
            validate_plan_update_content(
                parsed,
                new_task="灵感：发现了codex可以优化的一些rule，比如去除常错的命令，提示plan mode和commit",
            )

    def test_validate_plan_update_content_rejects_unsafe_schedule_cells(self) -> None:
        new_task = "学习python"
        parsed = parse_plan_update_response(
            json.dumps(
                {
                    "updated_today_tasks": f"学习：\n{new_task}",
                    "updated_daily_original": f"学习：\n{new_task}",
                    "target_heading": "学习",
                    "schedule_task": "学习|python",
                    "schedule_priority": "P2",
                    "schedule_estimate": "45m",
                },
                ensure_ascii=False,
            )
        )

        with self.assertRaisesRegex(PlanUpdateParseError, "table separators"):
            validate_plan_update_content(parsed, new_task=new_task)

    def test_update_daily_replaces_original_and_appends_schedule_row(self) -> None:
        daily = """# 2026-04-30

## 计划

生成时间：2026-04-30 11:02:58

**今日待办**

学习：
视频：Pencil + Codex 实现 AI 日程助理 App

| 任务 | 优先级 | 预计 | 状态 | 用时 |
|---|---|---|---|---|
| 看视频 | P1 | 1h | ○ | 保持节奏 |

## 记录

- 10:00 开始
"""

        updated = update_daily_plan_text(
            daily_text=daily,
            date_text="2026-04-30",
            updated_daily_original="学习：\n视频：Pencil + Codex 实现 AI 日程助理 App\n学习python",
            schedule_row=ScheduleRow("学习python", "P2", "45m"),
        )

        self.assertIn("学习python", updated)
        self.assertIn("| 看视频 | P1 | 1h | ○ |  |", updated)
        self.assertIn("| 学习python | P2 | 45m |  |  |", updated)
        self.assertIn("## 记录", updated)
        self.assertNotIn("**新任务**", updated)
        self.assertNotIn("### 新增", updated)
        self.assertLess(updated.index("学习python"), updated.index("| 任务 | 优先级 | 预计 | 状态 | 用时 |"))

    def test_update_daily_adds_schedule_table_when_missing(self) -> None:
        daily = """# 2026-04-30

## 计划

**今日待办**

原文

**2. 计划建议**

建议
"""

        updated = update_daily_plan_text(
            daily_text=daily,
            date_text="2026-04-30",
            updated_daily_original="原文\n第二个任务",
            schedule_row=ScheduleRow("第二个任务", "P3", "30m"),
        )

        self.assertIn("原文\n第二个任务", updated)
        self.assertIn("**2. 计划建议**", updated)
        self.assertIn("| 第二个任务 | P3 | 30m |  |  |", updated)
        self.assertNotIn("**新任务**", updated)

    def test_update_daily_appends_daily_task_to_grouped_daily_table(self) -> None:
        daily = """# 2026-05-23

## 计划

**今日待办**

【日常】
1. 喝水

**任务管理**

【目标】
| 任务 | 优先级 | 预计 | 状态 | 用时 |
|---|---|---|---|---|
| 写复盘 | P1 | 30m |  |  |

【日常】
| 任务 | 优先级 | 预计 | 状态 | 用时 |
|---|---|---|---|---|
| 喝水 | P3 | 5m |  |  |

【xiushenlu维护】
| 任务 | 优先级 | 状态 |
|---|---|---|

## 记录

- 10:00 开始
"""

        updated = update_daily_plan_text(
            daily_text=daily,
            date_text="2026-05-23",
            updated_daily_original="【日常】\n1. 喝水\n2. 伸展",
            schedule_row=ScheduleRow("伸展", "P3", "5m", category="日常"),
        )

        self.assertIn("【日常】\n| 任务 | 优先级 | 预计 | 状态 | 用时 |", updated)
        self.assertIn("| 喝水 | P3 | 5m |  |  |\n| 伸展 | P3 | 5m |  |  |", updated)
        self.assertNotIn("| 伸展 | P3 |\n", updated)

    def test_update_daily_appends_maintenance_task_to_status_table(self) -> None:
        daily = """# 2026-05-23

## 计划

**今日待办**

【修身炉】
1. 修复计划表

**任务管理**

【目标】
| 任务 | 优先级 | 预计 | 状态 | 用时 |
|---|---|---|---|---|

【日常】
| 任务 | 优先级 | 预计 | 状态 | 用时 |
|---|---|---|---|---|

【xiushenlu维护】
| 任务 | 优先级 | 状态 |
|---|---|---|
| 修复计划表 | P1 |  |
"""

        updated = update_daily_plan_text(
            daily_text=daily,
            date_text="2026-05-23",
            updated_daily_original="【修身炉】\n1. 修复计划表\n2. 优化任务管理表",
            schedule_row=ScheduleRow("优化任务管理表", "P1", "30m", category="xiushenlu维护"),
        )

        self.assertIn("【xiushenlu维护】\n| 任务 | 优先级 | 状态 |\n|---|---|---|", updated)
        self.assertIn("| 修复计划表 | P1 |  |\n| 优化任务管理表 | P1 |  |", updated)
        self.assertNotIn("| 优化任务管理表 | P1 | 30m |", updated)

    def test_update_daily_creates_minimal_plan_when_missing(self) -> None:
        updated = update_daily_plan_text(
            daily_text="# 2026-04-30\n\n## 记录\n\n- 已有记录\n",
            date_text="2026-04-30",
            updated_daily_original="学习python",
            schedule_row=ScheduleRow("学习python", "P2", "45m"),
        )

        self.assertIn("## 计划", updated)
        self.assertIn("**今日待办**", updated)
        self.assertIn("| 学习python | P2 | 45m |  |  |", updated)
        self.assertIn("## 记录", updated)
        self.assertNotIn("**新任务**", updated)
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
                "# 2026-04-30\n\n"
                "## 计划\n\n"
                "**今日待办**\n\n"
                "学习：\n视频\n\n"
                "| 任务 | 优先级 | 预计 | 状态 | 用时 |\n"
                "|---|---|---|---|---|\n"
                "| 视频 | P1 | 1h |  |  |\n",
                encoding="utf-8",
            )
            reply = json.dumps(
                {
                    "updated_today_tasks": "# 今日待办\n\n学习：\n视频：Pencil + Codex 实现 AI 日程助理 App\n学习python",
                    "updated_daily_original": "学习：\n视频\n学习python",
                    "target_heading": "学习",
                    "schedule_task": "学习python",
                    "schedule_priority": "P2",
                    "schedule_estimate": "45m",
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
            self.assertIn("| 学习python | P2 | 45m |  |  |", daily_text)
            self.assertNotIn("**新任务**", daily_text)
            self.assertNotIn("任务建议", daily_text)
            self.assertEqual([event["type"] for event in logger.events], ["llm_call", "plan_updated"])

    def test_generate_plan_update_uses_original_new_task_in_schedule_row(self) -> None:
        with _temporary_directory() as temp_dir:
            config = _test_config(Path(temp_dir))
            inbox = Path(config["paths"]["inbox_dir"])
            daily_dir = Path(config["paths"]["daily_dir"])
            inbox.mkdir(parents=True)
            daily_dir.mkdir(parents=True)
            (inbox / "today_tasks.md").write_text(
                "# 今日待办\n\n学习：\n视频\n",
                encoding="utf-8",
            )
            (daily_dir / "2026-05-23.md").write_text(
                "# 2026-05-23\n\n"
                "## 计划\n\n"
                "**今日待办**\n\n"
                "学习：\n视频\n\n"
                "| 任务 | 优先级 | 预计 | 状态 | 用时 |\n"
                "|---|---|---|---|---|\n"
                "| 视频 | P1 | 1h |  |  |\n",
                encoding="utf-8",
            )
            new_task = "灵感：发现了codex可以优化的一些rule，比如去除常错的命令，提示plan mode和commit"
            reply = json.dumps(
                {
                    "updated_today_tasks": f"# 今日待办\n\n学习：\n视频\n{new_task}",
                    "updated_daily_original": f"学习：\n视频\n{new_task}",
                    "target_heading": "学习",
                    "schedule_task": "优化codex规则",
                    "schedule_priority": "P2",
                    "schedule_estimate": "30m",
                },
                ensure_ascii=False,
            )

            generate_plan_update(
                FakeProvider(reply),
                new_task,
                config=config,
                target_date=date(2026, 5, 23),
                logger=FakeLogger(),  # type: ignore[arg-type]
            )

            daily_text = (daily_dir / "2026-05-23.md").read_text(encoding="utf-8")
            original_task_body = "发现了codex可以优化的一些rule，比如去除常错的命令，提示plan mode和commit"
            self.assertIn(f"| {original_task_body} | P2 | 30m |  |  |", daily_text)
            self.assertNotIn("| 优化codex规则 | P2 | 30m |  |  |", daily_text)

    def test_generate_plan_update_uses_bracket_heading_style_for_new_group(self) -> None:
        with _temporary_directory() as temp_dir:
            config = _test_config(Path(temp_dir))
            inbox = Path(config["paths"]["inbox_dir"])
            daily_dir = Path(config["paths"]["daily_dir"])
            inbox.mkdir(parents=True)
            daily_dir.mkdir(parents=True)
            (inbox / "today_tasks.md").write_text(
                "重点：\n学习最新的知识库\n\n"
                "【工作与自动化】\n"
                "1. 配置Codex每日自动化任务\n",
                encoding="utf-8",
            )
            (daily_dir / "2026-05-13.md").write_text(
                "# 2026-05-13\n\n"
                "## 计划\n\n"
                "**今日待办**\n\n"
                "重点：\n学习最新的知识库\n\n"
                "【工作与自动化】\n"
                "1. 配置Codex每日自动化任务\n\n"
                "| 任务 | 优先级 | 预计 | 状态 | 用时 |\n"
                "|---|---|---|---|---|\n"
                "| 配置Codex每日自动化任务 | P1 | 30m |  |  |\n",
                encoding="utf-8",
            )
            reply = json.dumps(
                {
                    "updated_today_tasks": (
                        "重点：\n学习最新的知识库\n\n"
                        "【工作与自动化】\n"
                        "1. 配置Codex每日自动化任务\n\n"
                        "杂事：\n杂事： 游泳"
                    ),
                    "updated_daily_original": (
                        "重点：\n学习最新的知识库\n\n"
                        "【工作与自动化】\n"
                        "1. 配置Codex每日自动化任务\n\n"
                        "杂事：\n杂事： 游泳"
                    ),
                    "target_heading": "杂事",
                    "schedule_task": "游泳",
                    "schedule_priority": "P3",
                    "schedule_estimate": "60m",
                },
                ensure_ascii=False,
            )

            result = generate_plan_update(
                FakeProvider(reply),
                "杂事： 游泳",
                config=config,
                target_date=date(2026, 5, 13),
                logger=FakeLogger(),  # type: ignore[arg-type]
            )

            today_text = (inbox / "today_tasks.md").read_text(encoding="utf-8")
            daily_text = (daily_dir / "2026-05-13.md").read_text(encoding="utf-8")
            self.assertEqual(result.target_heading, "杂事")
            self.assertIn("【杂事】\n1. 游泳", today_text)
            self.assertIn("【杂事】\n1. 游泳", daily_text)
            self.assertNotIn("杂事：\n游泳", today_text)
            self.assertNotIn("杂事： 游泳", today_text)

    def test_generate_plan_update_normalizes_existing_colon_heading(self) -> None:
        with _temporary_directory() as temp_dir:
            config = _test_config(Path(temp_dir))
            inbox = Path(config["paths"]["inbox_dir"])
            daily_dir = Path(config["paths"]["daily_dir"])
            inbox.mkdir(parents=True)
            daily_dir.mkdir(parents=True)
            (inbox / "today_tasks.md").write_text(
                "# 今日待办\n\n杂事：\n扫地拖地\n",
                encoding="utf-8",
            )
            (daily_dir / "2026-05-13.md").write_text(
                "# 2026-05-13\n\n"
                "## 计划\n\n"
                "**今日待办**\n\n"
                "杂事：\n扫地拖地\n\n"
                "| 任务 | 优先级 | 预计 | 状态 | 用时 |\n"
                "|---|---|---|---|---|\n"
                "| 扫地拖地 | P3 | 30m |  |  |\n",
                encoding="utf-8",
            )
            reply = json.dumps(
                {
                    "updated_today_tasks": "# 今日待办\n\n【杂事】\n扫地拖地\n游泳",
                    "updated_daily_original": "【杂事】\n扫地拖地\n游泳",
                    "target_heading": "杂事",
                    "schedule_task": "游泳",
                    "schedule_priority": "P3",
                    "schedule_estimate": "60m",
                },
                ensure_ascii=False,
            )

            generate_plan_update(
                FakeProvider(reply),
                "游泳",
                config=config,
                target_date=date(2026, 5, 13),
                logger=FakeLogger(),  # type: ignore[arg-type]
            )

            today_text = (inbox / "today_tasks.md").read_text(encoding="utf-8")
            daily_text = (daily_dir / "2026-05-13.md").read_text(encoding="utf-8")
            self.assertIn("【杂事】\n1. 扫地拖地\n2. 游泳", today_text)
            self.assertIn("【杂事】\n1. 扫地拖地\n2. 游泳", daily_text)
            self.assertNotIn("杂事：", today_text)

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

    def test_generate_plan_update_does_not_write_tasks_when_daily_merge_fails(self) -> None:
        with _temporary_directory() as temp_dir:
            config = _test_config(Path(temp_dir))
            inbox = Path(config["paths"]["inbox_dir"])
            daily_dir = Path(config["paths"]["daily_dir"])
            inbox.mkdir(parents=True)
            daily_dir.mkdir(parents=True)
            today_tasks = inbox / "today_tasks.md"
            daily_file = daily_dir / "2026-04-30.md"
            today_tasks.write_text("# 今日待办\n\n学习：\n视频\n", encoding="utf-8")
            daily_file.write_text(
                "# 2026-04-30\n\n"
                "## 计划\n\n"
                "**今日待办**\n\n"
                "学习：\n视频\n\n"
                "| 任务 | 优先级 | 预计 | 状态 | 用时 |\n"
                "|---|---|---|---|---|\n"
                "| 破损行 | P1 |\n",
                encoding="utf-8",
            )
            reply = json.dumps(
                {
                    "updated_today_tasks": "# 今日待办\n\n学习：\n视频\n学习python",
                    "updated_daily_original": "学习：\n视频\n学习python",
                    "target_heading": "学习",
                    "schedule_task": "学习python",
                    "schedule_priority": "P2",
                    "schedule_estimate": "45m",
                },
                ensure_ascii=False,
            )

            with self.assertRaises(PlanUpdateParseError):
                generate_plan_update(
                    FakeProvider(reply),
                    "学习python",
                    config=config,
                    target_date=date(2026, 4, 30),
                    logger=FakeLogger(),  # type: ignore[arg-type]
                )

            self.assertEqual(today_tasks.read_text(encoding="utf-8"), "# 今日待办\n\n学习：\n视频\n")
            self.assertNotIn("学习python", daily_file.read_text(encoding="utf-8"))


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
