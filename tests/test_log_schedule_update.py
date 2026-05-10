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
    def test_update_schedule_changes_only_completion_and_note_columns(self) -> None:
        with _temporary_directory() as temp_dir:
            config = _test_config(Path(temp_dir))
            daily_file = _write_daily(
                config,
                _daily_text(
                    "| 计划表更新 | P1 | 30m |  |  |\n"
                    "| 复盘 | P2 | 20m |  |  |"
                ),
            )
            reply = json.dumps(
                {
                    "updates": [
                        {
                            "row_index": 1,
                            "completed": True,
                            "note": "下次先验表头",
                            "evidence": "后续注意：下次先验表头",
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
                "完成计划表更新。后续注意：下次先验表头。",
                config=config,
                target_date=date(2026, 5, 10),
                logger=logger,  # type: ignore[arg-type]
            )

            self.assertTrue(result.updated)
            daily_text = daily_file.read_text(encoding="utf-8")
            self.assertIn("| 计划表更新 | P1 | 30m | ✓ | 下次先验表头 |", daily_text)
            self.assertIn("| 复盘 | P2 | 20m |  |  |", daily_text)
            self.assertNotIn("不要改任务名", daily_text)
            self.assertNotIn("P9", daily_text)
            self.assertNotIn("999h", daily_text)
            self.assertEqual([event["type"] for event in logger.events], ["llm_call", "schedule_updated_from_log"])

    def test_completion_and_note_can_be_rewritten_or_cleared(self) -> None:
        with _temporary_directory() as temp_dir:
            config = _test_config(Path(temp_dir))
            daily_file = _write_daily(
                config,
                _daily_text("| 复盘 | P0 | 20m | ✓ | 先补证据 |"),
            )
            reply = json.dumps(
                {
                    "updates": [
                        {
                            "row_index": 1,
                            "completed": False,
                            "note": "",
                            "evidence": "",
                        }
                    ]
                },
                ensure_ascii=False,
            )

            result = update_schedule_from_log(
                FakeProvider(reply),
                "复盘还没做完，需要重新打开。",
                config=config,
                target_date=date(2026, 5, 10),
                logger=FakeLogger(),  # type: ignore[arg-type]
            )

            self.assertTrue(result.updated)
            self.assertIn("| 复盘 | P0 | 20m |  |  |", daily_file.read_text(encoding="utf-8"))

    def test_invalid_json_does_not_write_schedule(self) -> None:
        with _temporary_directory() as temp_dir:
            config = _test_config(Path(temp_dir))
            daily_file = _write_daily(
                config,
                _daily_text("| 计划表更新 | P1 | 30m |  |  |"),
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
                _daily_text("| 计划表更新 | P1 | 30m |  |  |"),
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
        daily = _daily_text("| 计划表更新 | P1 | 30m |  |  |")
        parsed = parse_schedule_patch_response(
            json.dumps({"updates": [{"row_index": 2, "completed": True, "note": "", "evidence": ""}]}),
            record_content="完成计划表更新。",
        )

        with self.assertRaisesRegex(LogScheduleUpdateParseError, "out of range"):
            apply_schedule_patch(daily, parsed)

    def test_non_empty_note_must_have_record_evidence(self) -> None:
        with self.assertRaisesRegex(LogScheduleUpdateParseError, "exact substring"):
            parse_schedule_patch_response(
                json.dumps(
                    {
                        "updates": [
                            {
                                "row_index": 1,
                                "completed": True,
                                "note": "下次先验表头",
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
                "- 已有记录\n",
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


def _daily_text(rows: str) -> str:
    return (
        "# 2026-05-10\n\n"
        "## 计划\n\n"
        "**今日待办**\n\n"
        "修身炉：\n"
        "1. 计划表更新\n\n"
        "| 任务 | 优先级 | 预估时间 | 完成 | 备注 |\n"
        "|---|---|---|---|---|\n"
        f"{rows}\n\n"
        "## 记录\n\n"
        "- 已有记录\n"
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
