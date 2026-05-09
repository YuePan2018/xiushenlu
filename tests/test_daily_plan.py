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
            "**时间安排**\n\n"
            "| 任务 | 优先级 | 预估时间 |\n"
            "|---|---|---|\n"
            "| 小红书post功能 | P0 | 1.5h |"
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
            "**时间安排**\n\n"
            "| 任务 | 优先级 | 预估时间 |\n"
            "|---|---|---|\n"
            "| 小红书post功能 | P0 | 1.5h |",
        )

    def test_build_plan_uses_empty_tasks_placeholder(self) -> None:
        plan = _build_plan("  ", "")

        self.assertEqual(plan, "**今日待办**\n\n（尚未填写今日待办）")

    def test_build_prompt_only_requests_schedule_table(self) -> None:
        prompt = _build_prompt("2026-05-09", "长期目标", "修身炉：\n1. 只写待办")

        self.assertIn("只输出“时间安排”一段", prompt)
        self.assertIn("任务｜优先级｜预估时间", prompt)
        self.assertNotIn("建议时段", prompt)
        self.assertIn("不要输出风险提醒、收尾检查、注意事项、保底完成标准、对应执行内容", prompt)
        self.assertNotIn("调度风险与调整规则", prompt)
        self.assertNotIn("晚间收口动作", prompt)

if __name__ == "__main__":
    unittest.main()
