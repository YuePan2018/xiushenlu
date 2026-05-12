from __future__ import annotations

import json
import shutil
import unittest
import uuid
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from app.llm.provider import LLMCallUsage, LLMProvider
from app.pipelines.nightly_review import (
    NightlyReviewParseError,
    _build_daily_review_context,
    generate_nightly_review,
    parse_nightly_review_response,
)


class FakeProvider(LLMProvider):
    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.last_usage: LLMCallUsage | None = None
        self.prompts: list[str] = []

    def chat(self, prompt: str) -> str:
        self.prompts.append(prompt)
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

    def read_events_for_date(self, date_text: str) -> list[dict[str, Any]]:
        return self.events

    def read_events_for_month(self, month_text: str) -> list[dict[str, Any]]:
        return self.events


class NightlyReviewTests(unittest.TestCase):
    def test_parse_nightly_review_response(self) -> None:
        parsed = parse_nightly_review_response(
            json.dumps(
                {
                    "review": "完成了什么\n- 完成复盘",
                    "next_today_tasks": "修身炉：\n1. 规划下一进度",
                },
                ensure_ascii=False,
            )
        )

        self.assertIn("完成复盘", parsed.review)
        self.assertIn("规划下一进度", parsed.next_today_tasks)

    def test_daily_review_context_uses_original_tasks_snapshot(self) -> None:
        daily_text = (
            "# 2026-05-07\n\n"
            "## 计划\n\n"
            "生成时间：2026-05-07 10:00:00\n\n"
            "**今日待办**\n\n"
            "# 今日待办\n\n"
            "xiushenlu：\n"
            "1. 修复重复 review\n\n"
            "1. 根据长期目标，对各任务给出简短建议\n"
            "- 先保证状态语义正确。\n\n"
            "## 记录\n\n"
            "- 10:22:55 修复bug：review会改today_tasks\n\n"
            "## 复盘\n\n"
            "旧复盘不应进入上下文\n"
        )

        context = _build_daily_review_context(daily_text)

        self.assertIn("修复重复 review", context.today_tasks)
        self.assertNotIn("根据长期目标", context.today_tasks)
        self.assertIn("根据长期目标", context.plan_notes)
        self.assertIn("review会改today_tasks", context.records)
        self.assertNotIn("旧复盘不应进入上下文", context.records)

    def test_today_review_rolls_over_tasks_and_clears_tomorrow_plan(self) -> None:
        with _temporary_directory() as temp_dir:
            root = Path(temp_dir).resolve()
            config = _test_config(root)
            today = date.today()
            today_text = today.isoformat()
            inbox = Path(config["paths"]["inbox_dir"])
            daily_dir = Path(config["paths"]["daily_dir"])
            inbox.mkdir(parents=True)
            daily_dir.mkdir(parents=True)

            today_tasks = inbox / "today_tasks.md"
            tomorrow_plan = inbox / "明日计划.md"
            daily_file = daily_dir / f"{today_text}.md"
            today_tasks.write_text("# 今日待办\n\n已经滚动的明日槽\n", encoding="utf-8")
            tomorrow_plan.write_text("去浙大\n", encoding="utf-8")
            daily_file.write_text(
                f"# {today_text}\n\n"
                "## 计划\n\n"
                "生成时间：2026-05-07 08:00:00\n\n"
                "1. 今日待办原文\n\n"
                "# 今日待办\n\n"
                "修身炉：\n"
                "1. 未完成任务\n\n"
                "| 任务 | 优先级 | 预计 | 状态 | 备注 |\n"
                "|---|---|---|---|---|\n"
                "| 删除任务 | P2 | 10m | × | 不再追踪 |\n\n"
                "2. 风险提醒\n\n"
                "- 注意收口。\n\n"
                "## 记录\n\n"
                "- 10:00 做了别的事\n\n"
                "## 复盘\n\n"
                "旧复盘不应再次喂给 LLM\n",
                encoding="utf-8",
            )
            reply = json.dumps(
                {
                    "review": "完成了什么\n- 做了别的事\n\n改进建议\n- 明天收口。\n\n值得肯定的行为\n- 有记录。",
                    "next_today_tasks": "修身炉：\n1. 未完成任务\n\n杂事：\n去浙大",
                },
                ensure_ascii=False,
            )
            logger = FakeLogger()
            provider = FakeProvider(reply)

            result = generate_nightly_review(
                provider,
                config=config,
                target_date=today,
                logger=logger,  # type: ignore[arg-type]
            )

            self.assertTrue(result.rolled_over)
            self.assertEqual(result.today_tasks_path, today_tasks)
            self.assertEqual(result.tomorrow_plan_path, tomorrow_plan)
            daily_text = daily_file.read_text(encoding="utf-8")
            self.assertIn("做了别的事", daily_text)
            self.assertIn("token 消耗统计", daily_text)
            self.assertIn("今日 LLM 调用：1 次", daily_text)
            self.assertIn("今日 token 数：30", daily_text)
            self.assertIn("本月 LLM 调用：1 次", daily_text)
            self.assertIn("本月 token 数：30", daily_text)
            self.assertNotIn("输入 token", daily_text)
            self.assertNotIn("输出 token", daily_text)
            saved_tasks = today_tasks.read_text(encoding="utf-8")
            self.assertFalse(saved_tasks.startswith("# 今日待办"))
            self.assertIn("去浙大", saved_tasks)
            self.assertEqual(tomorrow_plan.read_text(encoding="utf-8"), "")
            self.assertEqual([event["type"] for event in logger.events], ["llm_call", "review_generated"])
            self.assertTrue(logger.events[-1]["detail"]["rolled_over"])
            self.assertEqual(len(provider.prompts), 1)
            self.assertIn("未完成任务", provider.prompts[0])
            self.assertIn("| 删除任务 | P2 | 10m | × | 不再追踪 |", provider.prompts[0])
            self.assertIn("状态”列为“×”", provider.prompts[0])
            self.assertIn("不能写入 next_today_tasks", provider.prompts[0])
            self.assertIn("去浙大", provider.prompts[0])
            self.assertIn("不要输出任何 Markdown 标题行", provider.prompts[0])
            self.assertIn("禁止输出任何以 `#` 开头的标题行", provider.prompts[0])
            self.assertIn("禁止从记录内容新增、派生或沉淀任务", provider.prompts[0])
            self.assertIn("不要生成口号、总结句或装饰性标题", provider.prompts[0])
            self.assertIn("不要判断优先级", provider.prompts[0])
            self.assertNotIn("已经滚动的明日槽", provider.prompts[0])
            self.assertNotIn("旧复盘不应再次喂给 LLM", provider.prompts[0])

    def test_today_review_keeps_record_only_tasks_out_of_next_tasks_prompt(self) -> None:
        with _temporary_directory() as temp_dir:
            root = Path(temp_dir).resolve()
            config = _test_config(root)
            today = date.today()
            today_text = today.isoformat()
            inbox = Path(config["paths"]["inbox_dir"])
            daily_dir = Path(config["paths"]["daily_dir"])
            inbox.mkdir(parents=True)
            daily_dir.mkdir(parents=True)

            today_tasks = inbox / "today_tasks.md"
            tomorrow_plan = inbox / "明日计划.md"
            daily_file = daily_dir / f"{today_text}.md"
            today_tasks.write_text("# 今日待办\n\n已经滚动的明日槽\n", encoding="utf-8")
            tomorrow_plan.write_text("去浙大\n", encoding="utf-8")
            daily_file.write_text(
                f"# {today_text}\n\n"
                "## 计划\n\n"
                "1. 今日待办原文\n\n"
                "口号：过最想要的一天！\n\n"
                "修身炉：\n"
                "1. 未完成任务\n\n"
                "2. 风险提醒\n\n"
                "- 注意收口。\n\n"
                "## 记录\n\n"
                "- 10:00 做了别的事\n"
                "- 11:00 学习 NotebookLM 并准备整理成工作流文档\n",
                encoding="utf-8",
            )
            reply = json.dumps(
                {
                    "review": "完成了什么\n- 做了别的事",
                    "next_today_tasks": "修身炉：\n1. 未完成任务\n\n杂事：\n去浙大",
                },
                ensure_ascii=False,
            )
            provider = FakeProvider(reply)

            result = generate_nightly_review(
                provider,
                config=config,
                target_date=today,
                logger=FakeLogger(),  # type: ignore[arg-type]
            )

            saved_tasks = today_tasks.read_text(encoding="utf-8")
            self.assertTrue(result.rolled_over)
            self.assertEqual(saved_tasks, "修身炉：\n1. 未完成任务\n\n杂事：\n去浙大\n")
            self.assertNotIn("NotebookLM", saved_tasks)
            self.assertIn("去浙大", saved_tasks)
            self.assertEqual(tomorrow_plan.read_text(encoding="utf-8"), "")
            self.assertIn("学习 NotebookLM", provider.prompts[0])
            self.assertIn("如果不在“今日待办”中，也不能写入 next_today_tasks", provider.prompts[0])

    def test_historical_review_rolls_over_current_tasks_by_default(self) -> None:
        with _temporary_directory() as temp_dir:
            root = Path(temp_dir).resolve()
            config = _test_config(root)
            target_date = date.today() - timedelta(days=1)
            date_text = target_date.isoformat()
            inbox = Path(config["paths"]["inbox_dir"])
            daily_dir = Path(config["paths"]["daily_dir"])
            inbox.mkdir(parents=True)
            daily_dir.mkdir(parents=True)

            today_tasks = inbox / "today_tasks.md"
            tomorrow_plan = inbox / "明日计划.md"
            daily_file = daily_dir / f"{date_text}.md"
            today_tasks.write_text("# 今日待办\n\n当前任务\n", encoding="utf-8")
            tomorrow_plan.write_text("明日输入\n", encoding="utf-8")
            daily_file.write_text(
                f"# {date_text}\n\n"
                "## 计划\n\n"
                "1. 今日待办原文\n\n"
                "旧任务\n\n"
                "## 记录\n\n"
                "- 历史记录\n",
                encoding="utf-8",
            )
            provider = FakeProvider(
                json.dumps(
                    {
                        "review": "历史复盘",
                        "next_today_tasks": "旧任务\n明日输入",
                    },
                    ensure_ascii=False,
                )
            )

            result = generate_nightly_review(provider, config=config, target_date=target_date)

            self.assertTrue(result.rolled_over)
            self.assertEqual(result.today_tasks_path, today_tasks)
            self.assertIn("历史复盘", daily_file.read_text(encoding="utf-8"))
            self.assertNotIn("token 消耗统计", daily_file.read_text(encoding="utf-8"))
            self.assertEqual(today_tasks.read_text(encoding="utf-8"), "旧任务\n明日输入\n")
            self.assertEqual(tomorrow_plan.read_text(encoding="utf-8"), "")
            self.assertIn("严格 JSON", provider.prompts[0])

    def test_historical_review_can_disable_rollover(self) -> None:
        with _temporary_directory() as temp_dir:
            root = Path(temp_dir).resolve()
            config = _test_config(root)
            target_date = date.today() - timedelta(days=1)
            date_text = target_date.isoformat()
            inbox = Path(config["paths"]["inbox_dir"])
            daily_dir = Path(config["paths"]["daily_dir"])
            inbox.mkdir(parents=True)
            daily_dir.mkdir(parents=True)

            today_tasks = inbox / "today_tasks.md"
            tomorrow_plan = inbox / "明日计划.md"
            daily_file = daily_dir / f"{date_text}.md"
            today_tasks.write_text("# 今日待办\n\n当前任务\n", encoding="utf-8")
            tomorrow_plan.write_text("明日输入\n", encoding="utf-8")
            daily_file.write_text(f"# {date_text}\n\n## 记录\n\n- 历史记录\n", encoding="utf-8")
            provider = FakeProvider("历史复盘")

            result = generate_nightly_review(
                provider,
                config=config,
                target_date=target_date,
                rollover=False,
            )

            self.assertFalse(result.rolled_over)
            self.assertIsNone(result.today_tasks_path)
            self.assertIn("历史复盘", daily_file.read_text(encoding="utf-8"))
            self.assertNotIn("token 消耗统计", daily_file.read_text(encoding="utf-8"))
            self.assertEqual(today_tasks.read_text(encoding="utf-8"), "# 今日待办\n\n当前任务\n")
            self.assertEqual(tomorrow_plan.read_text(encoding="utf-8"), "明日输入\n")
            self.assertNotIn("严格 JSON", provider.prompts[0])

    def test_rollover_parse_error_does_not_write_any_user_files(self) -> None:
        with _temporary_directory() as temp_dir:
            root = Path(temp_dir).resolve()
            config = _test_config(root)
            today = date.today()
            today_text = today.isoformat()
            inbox = Path(config["paths"]["inbox_dir"])
            daily_dir = Path(config["paths"]["daily_dir"])
            inbox.mkdir(parents=True)
            daily_dir.mkdir(parents=True)

            today_tasks = inbox / "today_tasks.md"
            tomorrow_plan = inbox / "明日计划.md"
            daily_file = daily_dir / f"{today_text}.md"
            today_tasks.write_text("# 今日待办\n\n原任务\n", encoding="utf-8")
            tomorrow_plan.write_text("明日任务\n", encoding="utf-8")
            original_daily_text = (
                f"# {today_text}\n\n"
                "## 计划\n\n"
                "1. 今日待办原文\n\n"
                "# 今日待办\n\n"
                "原任务\n\n"
                "2. 风险提醒\n\n"
                "- 注意收口。\n\n"
                "## 记录\n\n"
                "- 原记录\n"
            )
            daily_file.write_text(original_daily_text, encoding="utf-8")
            logger = FakeLogger()

            with self.assertRaises(NightlyReviewParseError):
                generate_nightly_review(
                    FakeProvider("not json"),
                    config=config,
                    target_date=today,
                    logger=logger,  # type: ignore[arg-type]
                )

            self.assertEqual(today_tasks.read_text(encoding="utf-8"), "# 今日待办\n\n原任务\n")
            self.assertEqual(tomorrow_plan.read_text(encoding="utf-8"), "明日任务\n")
            self.assertEqual(daily_file.read_text(encoding="utf-8"), original_daily_text)
            self.assertNotIn("token 消耗统计", daily_file.read_text(encoding="utf-8"))
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
        parent = Path("workspace") / "test_nightly_review"
        parent.mkdir(parents=True, exist_ok=True)
        self.path = parent / uuid.uuid4().hex
        self.path.mkdir(parents=True)
        return str(self.path)

    def __exit__(self, exc_type, exc, tb) -> None:
        shutil.rmtree(self.path, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
