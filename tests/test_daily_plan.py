from __future__ import annotations

import unittest

from app.pipelines.daily_plan import _build_plan, _build_prompt, _normalize_markdown_section_spacing


class DailyPlanTests(unittest.TestCase):
    def test_build_plan_preserves_today_tasks_verbatim(self) -> None:
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
        advice = "2. 根据长期目标，对各任务给出简短建议\n建议正文"

        plan = _build_plan(tasks, advice)

        self.assertEqual(
            plan,
            "1. 今日待办原文\n\n"
            "# 今日待办\n\n"
            "口号：过最想要的一天！\n\n"
            "xiushenlu：\n"
            "1. 查看下一阶段计划\n"
            "2. 完成小红书post功能\n\n"
            "学习：\n"
            "思考：如何提速？\n"
            "视频：学习agent\n\n"
            "2. 根据长期目标，对各任务给出简短建议\n"
            "建议正文",
        )

    def test_build_plan_uses_empty_tasks_placeholder(self) -> None:
        plan = _build_plan("  ", "2. 建议")

        self.assertEqual(plan, "1. 今日待办原文\n\n（尚未填写今日待办）\n\n2. 建议")

    def test_build_prompt_tells_llm_not_to_copy_original_tasks(self) -> None:
        prompt = _build_prompt("2026-05-06", "长期目标", "# 今日待办\n\n任务")

        self.assertIn("不要输出“今日待办原文”", prompt)
        self.assertIn("不要复制、改写、重排今日待办原文", prompt)
        self.assertIn("输出结构（严格按此顺序）", prompt)
        self.assertIn("1. 根据长期目标", prompt)

    def test_normalize_adds_blank_line_after_numbered_bold_heading(self) -> None:
        plan = "**1. 今日待办原文**\n修身炉：\n1. 验证review"

        normalized = _normalize_markdown_section_spacing(plan)

        self.assertEqual(normalized, "**1. 今日待办原文**\n\n修身炉：\n1. 验证review")

    def test_normalize_does_not_duplicate_existing_blank_line(self) -> None:
        plan = "**1. 今日待办原文**\n\n修身炉：\n1. 验证review"

        normalized = _normalize_markdown_section_spacing(plan)

        self.assertEqual(normalized, plan)

    def test_normalize_keeps_non_heading_content_unchanged(self) -> None:
        plan = "普通正文\n修身炉：\n1. 验证review"

        normalized = _normalize_markdown_section_spacing(plan)

        self.assertEqual(normalized, plan)

    def test_normalize_applies_to_other_numbered_bold_headings(self) -> None:
        plan = "**2. 任务执行建议**\n建议正文"

        normalized = _normalize_markdown_section_spacing(plan)

        self.assertEqual(normalized, "**2. 任务执行建议**\n\n建议正文")


if __name__ == "__main__":
    unittest.main()
