from __future__ import annotations

import unittest

from app.pipelines.daily_plan import _build_plan, _build_prompt


class DailyPlanTests(unittest.TestCase):
    def test_build_plan_preserves_today_tasks_verbatim_and_appends_schedule(self) -> None:
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
            "| 任务 | 优先级 | 预计 | 状态 | 备注 |\n"
            "|---|---|---|---|---|\n"
            "| 小红书post功能 | P0 | 1.5h |  |  |"
        )

        plan = _build_plan(tasks, schedule)

        self.assertEqual(
            plan,
            "**今日待办**\n\n"
            "# 今日待办\n\n"
            "口号：过最想要的一天！\n\n"
            "xiushenlu：\n"
            "1. 查看下一阶段计划\n"
            "2. 完成小红书post功能\n\n"
            "学习：\n"
            "思考：如何提速？\n"
            "视频：学习agent\n\n"
            "**任务管理**\n"
            "| 任务 | 优先级 | 预计 | 状态 | 备注 |\n"
            "|---|---|---|---|---|\n"
            "| 小红书post功能 | P0 | 1.5h |  |  |",
        )

    def test_build_plan_uses_empty_tasks_placeholder(self) -> None:
        plan = _build_plan("  ", "")

        self.assertEqual(plan, "**今日待办**\n\n（尚未填写今日待办）")

    def test_build_plan_normalizes_fullwidth_pipe_schedule_table(self) -> None:
        plan = _build_plan(
            "修身炉：\n1. 修复表格渲染",
            (
                "时间安排\n\n"
                "| 任务｜优先级｜预估时间｜完成｜备注 |\n"
                "| :--- | :--- | :--- | :--- | :--- |\n"
                "| 修复表格渲染｜P0｜30m｜✓｜先看 marked 渲染 |"
            ),
        )

        self.assertIn("| 任务 | 优先级 | 预计 | 状态 | 备注 |", plan)
        self.assertIn("**任务管理**\n| 任务 | 优先级 | 预计 | 状态 | 备注 |", plan)
        self.assertIn("| 修复表格渲染 | P0 | 30m |  |  |", plan)
        self.assertNotIn("｜", plan)
        self.assertNotIn("时间安排", plan)
        self.assertNotIn("先看 marked 渲染", plan)

    def test_build_plan_expands_three_column_schedule_table(self) -> None:
        plan = _build_plan(
            "修身炉：\n1. 修复表格渲染",
            (
                "| 任务 | 优先级 | 预估时间 |\n"
                "|---|---|---|\n"
                "| 修复表格渲染 | P0 | 30m |"
            ),
        )

        self.assertIn("| 任务 | 优先级 | 预计 | 状态 | 备注 |", plan)
        self.assertIn("**任务管理**\n| 任务 | 优先级 | 预计 | 状态 | 备注 |", plan)
        self.assertIn("| 修复表格渲染 | P0 | 30m |  |  |", plan)
        self.assertNotIn("时间安排", plan)

    def test_build_prompt_only_requests_schedule_table(self) -> None:
        prompt = _build_prompt("2026-05-09", "长期目标", "修身炉：\n1. 只写待办")

        self.assertIn("只输出普通文本标题“任务管理”和 markdown 表格", prompt)
        self.assertIn("任务管理表格前固定输出一行普通文本：**任务管理**", prompt)
        self.assertIn("| 任务 | 优先级 | 预计 | 状态 | 备注 |", prompt)
        self.assertIn("必须使用英文竖线", prompt)
        self.assertIn("“状态”和“备注”两列都不填", prompt)
        self.assertNotIn("建议时段", prompt)
        self.assertNotIn("调度风险与调整规则", prompt)
        self.assertNotIn("晚间收口动作", prompt)

if __name__ == "__main__":
    unittest.main()
