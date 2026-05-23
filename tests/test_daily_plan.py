from __future__ import annotations

import shutil
import unittest
import uuid
from datetime import date
from pathlib import Path
from typing import Any

from app.llm.provider import LLMCallUsage, LLMProvider
from app.pipelines.daily_plan import _build_plan, _build_prompt, generate_daily_plan


class FakeProvider(LLMProvider):
    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.last_usage = LLMCallUsage(
            model="fake-model",
            tokens_in=10,
            tokens_out=20,
            total_tokens=30,
            estimated=False,
            raw=None,
        )

    def chat(self, prompt: str) -> str:
        return self.reply


class FakeLogger:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def append_event(self, type: str, summary: str, detail: Any | None = None) -> dict[str, Any]:
        event = {"type": type, "summary": summary, "detail": detail}
        self.events.append(event)
        return event


class DailyPlanTests(unittest.TestCase):
    def test_build_plan_formats_today_tasks_snapshot_and_appends_schedule(self) -> None:
        tasks = (
            "# 今日待办\n\n"
            "口号：过最想要的一天！\n\n"
            "xiushenlu：\n"
            "1. 查看下一阶段计划\n"
            "2. 完成小红书post功能\n\n"
            "学习：\n"
            "思考：如何提速？\n"
            "视频：学习agent\n"
        )
        schedule = (
            "| 任务 | 优先级 | 预计 | 状态 | 用时 |\n"
            "|---|---|---|---|---|\n"
            "| 小红书post功能 | P0 | 1.5h |  |  |"
        )

        plan = _build_plan(tasks, schedule)

        self.assertEqual(
            plan,
            "**今日待办**\n\n"
            "【口号】\n"
            "1. 过最想要的一天！\n\n"
            "【xiushenlu】\n"
            "1. 查看下一阶段计划\n"
            "2. 完成小红书post功能\n\n"
            "【学习】\n"
            "1. 思考：如何提速？\n"
            "2. 视频：学习agent\n\n"
            "**任务管理**\n"
            "| 任务 | 优先级 | 预计 | 状态 | 用时 |\n"
            "|---|---|---|---|---|\n"
            "| 完成小红书post功能 | P0 | 1.5h |  |  |",
        )

    def test_build_plan_uses_original_task_text_in_schedule_column(self) -> None:
        plan = _build_plan(
            "学习：\n"
            "1. 看 NotebookLM 的分享视频\n"
            "2. 整理知识库入口",
            (
                "| 任务 | 优先级 | 预计 | 状态 | 用时 |\n"
                "|---|---|---|---|---|\n"
                "| 深度理解 NotebookLM 视频内容 | P1 | 45m |  |  |\n"
                "| 整理知识库入口结构 | P2 | 30m |  |  |"
            ),
        )

        self.assertIn("| 看 NotebookLM 的分享视频 | P1 | 45m |  |  |", plan)
        self.assertIn("| 整理知识库入口 | P2 | 30m |  |  |", plan)
        self.assertNotIn("深度理解 NotebookLM 视频内容", plan)
        self.assertNotIn("整理知识库入口结构", plan)

    def test_build_plan_matches_original_task_text_after_priority_reorder(self) -> None:
        plan = _build_plan(
            "看近几日的b站视频学习\n\n"
            "【日常】\n"
            "1. 小红书工作信息\n"
            "2. 微信资讯\n"
            "3. b站关注的当日动态看完。\n"
            "4. 昨天的照片整理好，发给深深\n\n"
            "【学习】\n"
            "1. codex子代理",
            (
                "| 任务 | 优先级 | 预计 | 状态 | 用时 |\n"
                "|---|---|---|---|---|\n"
                "| Codex子代理开发与调试 | P0 | 3.5h |  |  |\n"
                "| 整理昨日照片并发送给深深 | P1 | 0.5h |  |  |\n"
                "| 浏览小红书工作信息 | P2 | 0.5h |  |  |\n"
                "| 快速扫读微信资讯 | P2 | 0.5h |  |  |\n"
                "| 观看B站学习视频（结合Codex辅助总结） | P1 | 2.0h |  |  |\n"
                "| 浏览B站关注动态 | P3 | 0.5h |  |  |"
            ),
        )

        self.assertIn("| codex子代理 | P0 | 3.5h |  |  |", plan)
        self.assertIn("| 昨天的照片整理好，发给深深 | P1 | 0.5h |  |  |", plan)
        self.assertIn("| 小红书工作信息 | P2 | 0.5h |  |  |", plan)
        self.assertIn("| 微信资讯 | P2 | 0.5h |  |  |", plan)
        self.assertIn("| 看近几日的b站视频学习 | P1 | 2.0h |  |  |", plan)
        self.assertIn("| b站关注的当日动态看完。 | P3 | 0.5h |  |  |", plan)
        self.assertNotIn("Codex子代理开发与调试", plan)
        self.assertNotIn("整理昨日照片并发送给深深", plan)
        self.assertNotIn("观看B站学习视频（结合Codex辅助总结）", plan)

    def test_build_plan_formats_inline_heading_as_bracket_numbered_list(self) -> None:
        plan = _build_plan("杂事：游泳", "")

        self.assertEqual(plan, "**今日待办**\n\n【杂事】\n1. 游泳")

    def test_build_plan_uses_empty_tasks_placeholder(self) -> None:
        plan = _build_plan("  ", "")

        self.assertEqual(plan, "**今日待办**\n\n（尚未填写今日待办）")

    def test_build_plan_normalizes_fullwidth_pipe_schedule_table(self) -> None:
        plan = _build_plan(
            "修身炉：\n1. 修复表格渲染",
            (
                "时间安排\n\n"
                "| 任务｜优先级｜预计｜状态｜用时 |\n"
                "| :--- | :--- | :--- | :--- | :--- |\n"
                "| 修复表格渲染｜P0｜30m｜✓｜40m |"
            ),
        )

        self.assertIn("| 任务 | 优先级 | 预计 | 状态 | 用时 |", plan)
        self.assertIn("**任务管理**\n| 任务 | 优先级 | 预计 | 状态 | 用时 |", plan)
        self.assertIn("| 修复表格渲染 | P0 | 30m |  |  |", plan)
        self.assertNotIn("｜", plan)
        self.assertNotIn("时间安排", plan)
        self.assertNotIn("40m", plan)

    def test_build_plan_does_not_normalize_three_column_schedule_table(self) -> None:
        plan = _build_plan(
            "修身炉：\n1. 修复表格渲染",
            (
                "| 任务 | 优先级 | 预估时间 |\n"
                "|---|---|---|\n"
                "| 修复表格渲染 | P0 | 30m |"
            ),
        )

        self.assertNotIn("| 任务 | 优先级 | 预计 | 状态 | 用时 |", plan)
        self.assertIn("| 任务 | 优先级 | 预估时间 |", plan)
        self.assertIn("| 修复表格渲染 | P0 | 30m |", plan)

    def test_build_prompt_only_requests_schedule_table(self) -> None:
        prompt = _build_prompt("2026-05-09", "长期目标", "修身炉：\n1. 只写待办")

        self.assertIn('只输出"**任务管理**"和 markdown 表格', prompt)
        self.assertIn("任务管理表格前固定输出一行：**任务管理**", prompt)
        self.assertIn("| 任务 | 优先级 | 预计 | 状态 | 用时 |", prompt)
        self.assertIn("必须使用英文竖线", prompt)
        self.assertIn("“状态”和“用时”两列都不填", prompt)
        self.assertIn("“任务”列必须逐字使用今日待办里的任务正文", prompt)
        self.assertNotIn("建议时段", prompt)
        self.assertNotIn("调度风险与调整规则", prompt)
        self.assertNotIn("晚间收口动作", prompt)

    def test_generate_daily_plan_writes_plan_without_generated_time(self) -> None:
        with _temporary_directory() as temp_dir:
            config = _test_config(Path(temp_dir))
            Path(config["paths"]["daily_dir"]).mkdir(parents=True)
            provider = FakeProvider(
                "| 任务 | 优先级 | 预计 | 状态 | 用时 |\n"
                "|---|---|---|---|---|\n"
                "| 学习python | P2 | 45m |  |  |"
            )

            result = generate_daily_plan(
                provider,
                config=config,
                target_date=date(2026, 5, 13),
                tasks_text="学习：\n学习python",
                logger=FakeLogger(),  # type: ignore[arg-type]
            )

            daily_text = result.path.read_text(encoding="utf-8")
            self.assertIn("## 计划", daily_text)
            self.assertIn("**今日待办**", daily_text)
            self.assertIn("| 学习python | P2 | 45m |  |  |", daily_text)
            self.assertNotIn("生成时间：", daily_text)


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
        parent = Path("workspace") / "test_daily_plan"
        parent.mkdir(parents=True, exist_ok=True)
        self.path = parent / uuid.uuid4().hex
        self.path.mkdir(parents=True)
        return str(self.path)

    def __exit__(self, exc_type, exc, tb) -> None:
        shutil.rmtree(self.path, ignore_errors=True)

if __name__ == "__main__":
    unittest.main()
