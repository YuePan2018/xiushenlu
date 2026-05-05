from __future__ import annotations

import unittest

from app.pipelines.daily_plan import _normalize_markdown_section_spacing


class DailyPlanTests(unittest.TestCase):
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
