from __future__ import annotations

import json
import shutil
import unittest
import uuid
from datetime import date
from pathlib import Path
from typing import Any

from app.llm.provider import LLMCallUsage, LLMProvider
from app.pipelines.log_schedule_update import (
    LogScheduleUpdateParseError,
    apply_schedule_patch,
    build_schedule_patch_prompt,
    parse_schedule_patch_response,
    update_schedule_from_log,
)


class FakeProvider(LLMProvider):
    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.prompts: list[str] = []
        self.last_usage: LLMCallUsage | None = None

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


class LogScheduleUpdateTests(unittest.TestCase):
    def test_update_schedule_changes_status_on_standard_duration_table(self) -> None:
        with _temporary_directory() as temp_dir:
            config = _test_config(Path(temp_dir))
            daily_file = _write_daily(
                config,
                _daily_text(
                    "| 计划表更新 | P1 | 30m |  |  |\n"
                    "| 复盘 | P2 | 20m |  |  |",
                    records="- 10:10:00 完成计划表更新。\n",
                ),
            )
            reply = json.dumps(
                {
                    "updates": [
                        {
                            "row_index": 1,
                            "completed": True,
                            "evidence": "完成计划表更新",
                            "task": "不要改任务名",
                            "priority": "P9",
                            "estimate": "999h",
                        }
                    ]
                },
                ensure_ascii=False,
            )
            logger = FakeLogger()

            result = update_schedule_from_log(
                FakeProvider(reply),
                "完成计划表更新。",
                config=config,
                target_date=date(2026, 5, 10),
                logger=logger,  # type: ignore[arg-type]
            )

            self.assertTrue(result.updated)
            daily_text = daily_file.read_text(encoding="utf-8")
            self.assertIn("| 任务 | 优先级 | 预计 | 状态 | 用时 |", daily_text)
            self.assertIn("| 计划表更新 | P1 | 30m | ✓ |  |", daily_text)
            self.assertIn("| 复盘 | P2 | 20m |  |  |", daily_text)
            self.assertNotIn("不要改任务名", daily_text)
            self.assertNotIn("P9", daily_text)
            self.assertNotIn("999h", daily_text)
            self.assertEqual([event["type"] for event in logger.events], ["llm_call", "schedule_updated_from_log"])

    def test_time_records_use_first_and_last_record_when_no_duration(self) -> None:
        with _temporary_directory() as temp_dir:
            config = _test_config(Path(temp_dir))
            daily_file = _write_daily(
                config,
                _daily_text(
                    "| 计划表更新 | P1 | 2h | ○ | 15m |",
                    records=(
                        "- 10:00:00 开始计划表更新。\n"
                        "- 10:30:00 暂停计划表更新。\n"
                        "- 11:00:00 继续计划表更新。\n"
                        "- 11:45:00 完成计划表更新 &。\n"
                    ),
                ),
            )
            reply = json.dumps(
                {
                    "updates": [
                        {
                            "row_index": 1,
                            "status": "completed",
                            "evidence": "完成计划表更新",
                            "time_records": [1, 2, 3, 4],
                        }
                    ]
                },
                ensure_ascii=False,
            )

            result = update_schedule_from_log(
                FakeProvider(reply),
                "完成计划表更新。",
                config=config,
                target_date=date(2026, 5, 10),
                logger=FakeLogger(),  # type: ignore[arg-type]
            )

            self.assertTrue(result.updated)
            self.assertIn("| 计划表更新 | P1 | 2h | ✓ | 1h45m |", daily_file.read_text(encoding="utf-8"))

    def test_interval_duration_requires_ampersand_on_last_record(self) -> None:
        with _temporary_directory() as temp_dir:
            config = _test_config(Path(temp_dir))
            daily_file = _write_daily(
                config,
                _daily_text(
                    "| 计划表更新 | P1 | 2h | ○ | 15m |",
                    records=(
                        "- 10:00:00 开始计划表更新。\n"
                        "- 11:45:00 完成计划表更新。\n"
                    ),
                ),
            )
            reply = json.dumps(
                {
                    "updates": [
                        {
                            "row_index": 1,
                            "status": "completed",
                            "evidence": "完成计划表更新",
                            "time_records": [1, 2],
                        }
                    ]
                },
                ensure_ascii=False,
            )

            result = update_schedule_from_log(
                FakeProvider(reply),
                "完成计划表更新。",
                config=config,
                target_date=date(2026, 5, 10),
                logger=FakeLogger(),  # type: ignore[arg-type]
            )

            self.assertTrue(result.updated)
            self.assertIn("| 计划表更新 | P1 | 2h | ✓ |  |", daily_file.read_text(encoding="utf-8"))

    def test_last_explicit_duration_wins_over_timestamp_diff(self) -> None:
        with _temporary_directory() as temp_dir:
            config = _test_config(Path(temp_dir))
            daily_file = _write_daily(
                config,
                _daily_text(
                    "| 计划表更新 | P1 | 2h |  |  |",
                    records="- 10:00:00 计划表更新用时40分钟，14:00开始15:00结束。\n",
                ),
            )
            reply = json.dumps(
                {
                    "updates": [
                        {
                            "row_index": 1,
                            "status": "keep",
                            "evidence": "计划表更新用时40分钟",
                            "time_records": [1],
                        }
                    ]
                },
                ensure_ascii=False,
            )

            result = update_schedule_from_log(
                FakeProvider(reply),
                "计划表更新用时40分钟，14:00开始15:00结束。",
                config=config,
                target_date=date(2026, 5, 10),
                logger=FakeLogger(),  # type: ignore[arg-type]
            )

            self.assertTrue(result.updated)
            self.assertIn("| 计划表更新 | P1 | 2h |  | 40m |", daily_file.read_text(encoding="utf-8"))

    def test_multiple_explicit_durations_use_last_one_only(self) -> None:
        with _temporary_directory() as temp_dir:
            config = _test_config(Path(temp_dir))
            daily_file = _write_daily(
                config,
                _daily_text(
                    "| 计划表更新 | P1 | 2h |  |  |",
                    records=(
                        "- 10:00:00 计划表更新用时10分钟。\n"
                        "- 11:00:00 计划表更新又花了20分钟。\n"
                    ),
                ),
            )
            reply = json.dumps(
                {
                    "updates": [
                        {
                            "row_index": 1,
                            "status": "keep",
                            "evidence": "计划表更新又花了20分钟",
                            "time_records": [1, 2],
                        }
                    ]
                },
                ensure_ascii=False,
            )

            result = update_schedule_from_log(
                FakeProvider(reply),
                "计划表更新又花了20分钟。",
                config=config,
                target_date=date(2026, 5, 10),
                logger=FakeLogger(),  # type: ignore[arg-type]
            )

            self.assertTrue(result.updated)
            self.assertIn("| 计划表更新 | P1 | 2h |  | 20m |", daily_file.read_text(encoding="utf-8"))

    def test_later_records_do_not_override_last_explicit_duration(self) -> None:
        with _temporary_directory() as temp_dir:
            config = _test_config(Path(temp_dir))
            daily_file = _write_daily(
                config,
                _daily_text(
                    "| 计划表更新 | P1 | 2h |  |  |",
                    records=(
                        "- 15:09:45 计划表更新。\n"
                        "- 15:13:31 计划表更新20分钟。\n"
                        "- 15:13:54 继续计划表更新。\n"
                        "- 15:14:54 计划表更新结束。\n"
                        "- 15:15:15 计划表更新39分钟。\n"
                        "- 15:15:38 开始计划表更新。\n"
                        "- 15:21:41 结束计划表更新。\n"
                        "- 15:22:34 计划表更新再次开始。\n"
                    ),
                ),
            )
            reply = json.dumps(
                {
                    "updates": [
                        {
                            "row_index": 1,
                            "status": "in_progress",
                            "evidence": "计划表更新再次开始",
                            "time_records": [1, 2, 3, 4, 5, 6, 7, 8],
                        }
                    ]
                },
                ensure_ascii=False,
            )

            result = update_schedule_from_log(
                FakeProvider(reply),
                "计划表更新再次开始。",
                config=config,
                target_date=date(2026, 5, 10),
                logger=FakeLogger(),  # type: ignore[arg-type]
            )

            self.assertTrue(result.updated)
            self.assertIn("| 计划表更新 | P1 | 2h | ○ | 39m |", daily_file.read_text(encoding="utf-8"))

    def test_single_record_without_explicit_duration_clears_duration(self) -> None:
        with _temporary_directory() as temp_dir:
            config = _test_config(Path(temp_dir))
            daily_file = _write_daily(
                config,
                _daily_text(
                    "| 计划表更新 | P1 | 2h |  | 15m |",
                    records="- 10:00:00 开始计划表更新。\n",
                ),
            )
            reply = json.dumps(
                {
                    "updates": [
                        {
                            "row_index": 1,
                            "status": "in_progress",
                            "evidence": "开始计划表更新",
                            "time_records": [1],
                        }
                    ]
                },
                ensure_ascii=False,
            )

            result = update_schedule_from_log(
                FakeProvider(reply),
                "开始计划表更新。",
                config=config,
                target_date=date(2026, 5, 10),
                logger=FakeLogger(),  # type: ignore[arg-type]
            )

            self.assertTrue(result.updated)
            self.assertIn("| 计划表更新 | P1 | 2h | ○ |  |", daily_file.read_text(encoding="utf-8"))

    def test_status_field_supports_in_progress_completed_not_started_dropped_and_keep(self) -> None:
        with _temporary_directory() as temp_dir:
            config = _test_config(Path(temp_dir))
            daily_file = _write_daily(
                config,
                _daily_text(
                    "| 计划表更新 | P1 | 30m |  |  |\n"
                    "| 复盘 | P2 | 20m | ○ |  |\n"
                    "| 收尾 | P3 | 10m | ✓ |  |\n"
                    "| 临时任务 | P3 | 10m |  |  |\n"
                    "| 保持状态 | P3 | 10m | ○ |  |",
                    records=(
                        "- 10:00:00 开始做计划表更新。复盘已经完成。收尾还没开始。删除临时任务，不再追踪。保持状态用时20m。\n"
                    ),
                ),
            )
            reply = json.dumps(
                {
                    "updates": [
                        {"row_index": 1, "status": "in_progress", "evidence": "开始做计划表更新"},
                        {"row_index": 2, "status": "completed", "evidence": "复盘已经完成"},
                        {"row_index": 3, "status": "not_started", "evidence": "收尾还没开始"},
                        {"row_index": 4, "status": "dropped", "evidence": "删除临时任务"},
                        {
                            "row_index": 5,
                            "status": "keep",
                            "evidence": "保持状态用时20m",
                            "time_records": [1],
                        },
                    ]
                },
                ensure_ascii=False,
            )

            result = update_schedule_from_log(
                FakeProvider(reply),
                "开始做计划表更新。复盘已经完成。收尾还没开始。删除临时任务，不再追踪。保持状态用时20m。",
                config=config,
                target_date=date(2026, 5, 10),
                logger=FakeLogger(),  # type: ignore[arg-type]
            )

            self.assertTrue(result.updated)
            daily_text = daily_file.read_text(encoding="utf-8")
            self.assertIn("| 计划表更新 | P1 | 30m | ○ |  |", daily_text)
            self.assertIn("| 复盘 | P2 | 20m | ✓ |  |", daily_text)
            self.assertIn("| 收尾 | P3 | 10m |  |  |", daily_text)
            self.assertIn("| 临时任务 | P3 | 10m | × |  |", daily_text)
            self.assertIn("| 保持状态 | P3 | 10m | ○ | 20m |", daily_text)

    def test_invalid_status_is_rejected(self) -> None:
        with self.assertRaisesRegex(LogScheduleUpdateParseError, "status must be one of"):
            parse_schedule_patch_response(
                json.dumps(
                    {
                        "updates": [
                            {"row_index": 1, "status": "paused", "evidence": ""}
                        ]
                    }
                ),
                record_content="计划表更新暂停。",
            )

    def test_prompt_explains_timing_rules(self) -> None:
        prompt = build_schedule_patch_prompt(
            "2026-05-10",
            "计划表更新完成了一部分，剩余短期内不做。",
            _schedule_table("| 计划表更新 | P1 | 30m |  |  |"),
        )

        self.assertIn("keep", prompt)
        self.assertIn("not_started", prompt)
        self.assertIn("in_progress", prompt)
        self.assertIn("completed", prompt)
        self.assertIn("dropped", prompt)
        self.assertIn("用时列是派生值", prompt)
        self.assertIn("不要把旧表格里的用时当作累计变量", prompt)
        self.assertIn("任务简称", prompt)
        self.assertIn("核心关键词", prompt)
        self.assertIn("最后一条记录内容包含“&”", prompt)
        self.assertIn("time_records", prompt)
        self.assertIn("最后一次明确时长", prompt)
        self.assertNotIn("direct_duration", prompt)
        self.assertNotIn("record_interval", prompt)
        self.assertNotIn("time_entries", prompt)
        self.assertNotIn("clock_range", prompt)
        self.assertNotIn("start_time", prompt)
        self.assertNotIn("end_time", prompt)

    def test_legacy_time_entries_are_rejected(self) -> None:
        with self.assertRaisesRegex(LogScheduleUpdateParseError, "time_entries is no longer supported"):
            parse_schedule_patch_response(
                json.dumps(
                    {
                        "updates": [
                            {
                                "row_index": 1,
                                "status": "keep",
                                "evidence": "",
                                "time_entries": [
                                    {"type": "clock_range", "record": 1, "start_time": "14:00", "end_time": "15:00"}
                                ],
                            }
                        ]
                    },
                    ensure_ascii=False,
                ),
                record_content="14:00开始15:00结束。",
                records=(),
            )

    def test_invalid_json_does_not_write_schedule(self) -> None:
        with _temporary_directory() as temp_dir:
            config = _test_config(Path(temp_dir))
            daily_file = _write_daily(
                config,
                _daily_text("| 计划表更新 | P1 | 30m |  |  |", records="- 10:00:00 完成计划表更新。\n"),
            )
            original = daily_file.read_text(encoding="utf-8")
            logger = FakeLogger()

            result = update_schedule_from_log(
                FakeProvider("not json"),
                "完成计划表更新。",
                config=config,
                target_date=date(2026, 5, 10),
                logger=logger,  # type: ignore[arg-type]
            )

            self.assertFalse(result.updated)
            self.assertEqual(daily_file.read_text(encoding="utf-8"), original)
            self.assertEqual([event["type"] for event in logger.events], ["llm_call"])

    def test_empty_updates_do_not_rewrite_schedule(self) -> None:
        with _temporary_directory() as temp_dir:
            config = _test_config(Path(temp_dir))
            daily_file = _write_daily(
                config,
                _daily_text("| 计划表更新 | P1 | 30m |  |  |", records="- 10:00:00 只是记录一下。\n"),
            )
            original = daily_file.read_text(encoding="utf-8")

            result = update_schedule_from_log(
                FakeProvider(json.dumps({"updates": []}, ensure_ascii=False)),
                "只是记录一下，没有任务状态变化。",
                config=config,
                target_date=date(2026, 5, 10),
                logger=FakeLogger(),  # type: ignore[arg-type]
            )

            self.assertFalse(result.updated)
            self.assertEqual(result.reason, "no_updates")
            self.assertEqual(daily_file.read_text(encoding="utf-8"), original)

    def test_row_index_out_of_range_does_not_write_schedule(self) -> None:
        daily = _daily_text("| 计划表更新 | P1 | 30m |  |  |", records="- 10:00:00 完成计划表更新。\n")
        parsed = parse_schedule_patch_response(
            json.dumps({"updates": [{"row_index": 2, "completed": True, "evidence": "完成计划表更新"}]}),
            record_content="完成计划表更新。",
        )

        with self.assertRaisesRegex(LogScheduleUpdateParseError, "out of range"):
            apply_schedule_patch(daily, parsed)

    def test_non_empty_evidence_must_match_daily_record(self) -> None:
        with self.assertRaisesRegex(LogScheduleUpdateParseError, "exact substring"):
            parse_schedule_patch_response(
                json.dumps(
                    {
                        "updates": [
                            {
                                "row_index": 1,
                                "completed": True,
                                "evidence": "记录里不存在",
                            }
                        ]
                    },
                    ensure_ascii=False,
                ),
                record_content="完成计划表更新。",
            )

    def test_unexpected_table_header_skips_llm_and_keeps_daily(self) -> None:
        with _temporary_directory() as temp_dir:
            config = _test_config(Path(temp_dir))
            daily_file = _write_daily(
                config,
                "# 2026-05-10\n\n"
                "## 计划\n\n"
                "**时间安排**\n\n"
                "| 任务 | 优先级 | 预估时间 |\n"
                "|---|---|---|\n"
                "| 旧表 | P1 | 30m |\n\n"
                "## 记录\n\n"
                "- 10:00:00 已有记录\n",
            )
            original = daily_file.read_text(encoding="utf-8")
            provider = FakeProvider(json.dumps({"updates": []}))

            result = update_schedule_from_log(
                provider,
                "完成旧表。",
                config=config,
                target_date=date(2026, 5, 10),
                logger=FakeLogger(),  # type: ignore[arg-type]
            )

            self.assertFalse(result.updated)
            self.assertEqual(provider.prompts, [])
            self.assertEqual(daily_file.read_text(encoding="utf-8"), original)

    def test_legacy_note_header_skips_llm_and_keeps_daily(self) -> None:
        with _temporary_directory() as temp_dir:
            config = _test_config(Path(temp_dir))
            daily_file = _write_daily(
                config,
                "# 2026-05-10\n\n"
                "## 计划\n\n"
                "**任务管理**\n\n"
                "| 任务 | 优先级 | 预计 | 状态 | 备注 |\n"
                "|---|---|---|---|---|\n"
                "| 旧表 | P1 | 30m |  | 旧备注 |\n\n"
                "## 记录\n\n"
                "- 10:00:00 已有记录\n",
            )
            original = daily_file.read_text(encoding="utf-8")
            provider = FakeProvider(json.dumps({"updates": []}))

            result = update_schedule_from_log(
                provider,
                "完成旧表。",
                config=config,
                target_date=date(2026, 5, 10),
                logger=FakeLogger(),  # type: ignore[arg-type]
            )

            self.assertFalse(result.updated)
            self.assertIn("header is not the expected", result.reason)
            self.assertEqual(provider.prompts, [])
            self.assertEqual(daily_file.read_text(encoding="utf-8"), original)


def _schedule_table(rows: str) -> str:
    return (
        "| 任务 | 优先级 | 预计 | 状态 | 用时 |\n"
        "|---|---|---|---|---|\n"
        f"{rows}"
    )


def _daily_text(rows: str, *, records: str = "- 09:00:00 已有记录\n") -> str:
    return (
        "# 2026-05-10\n\n"
        "## 计划\n\n"
        "**今日待办**\n\n"
        "修身炉：\n"
        "1. 计划表更新\n\n"
        "| 任务 | 优先级 | 预计 | 状态 | 用时 |\n"
        "|---|---|---|---|---|\n"
        f"{rows}\n\n"
        "## 记录\n\n"
        f"{records}"
    )


def _write_daily(config: dict[str, Any], text: str) -> Path:
    daily_dir = Path(config["paths"]["daily_dir"])
    daily_dir.mkdir(parents=True)
    daily_file = daily_dir / "2026-05-10.md"
    daily_file.write_text(text, encoding="utf-8")
    return daily_file


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
        parent = Path("workspace") / "test_log_schedule_update"
        parent.mkdir(parents=True, exist_ok=True)
        self.path = parent / uuid.uuid4().hex
        self.path.mkdir(parents=True)
        return str(self.path)

    def __exit__(self, exc_type, exc, tb) -> None:
        shutil.rmtree(self.path, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
