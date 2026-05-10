from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Callable

from app.config import load_config, resolve_project_path
from app.daily import write_daily_section
from app.inbox import read_today_tasks
from app.llm.provider import LLMProvider
from app.llm.usage import append_llm_call_event
from app.logger import EventLogger
from app.memory.goals import read_goals


@dataclass(frozen=True)
class DailyPlanResult:
    date: str
    path: Path
    plan: str


def generate_daily_plan(
    provider: LLMProvider,
    config: dict[str, Any] | None = None,
    target_date: date | None = None,
    tasks_text: str | None = None,
    logger: EventLogger | None = None,
    cancel_check: Callable[[], None] | None = None,
) -> DailyPlanResult:
    cfg = config or load_config()
    current_date = target_date or date.today()
    date_text = current_date.isoformat()
    goals = read_goals(cfg)
    tasks = tasks_text if tasks_text is not None else read_today_tasks(cfg)
    prompt = _build_prompt(date_text=date_text, goals=goals, tasks=tasks)

    plan_schedule = provider.chat(prompt).strip()
    if cancel_check is not None:
        cancel_check()
    plan = _build_plan(tasks=tasks, plan_schedule=plan_schedule)
    daily_path = _daily_path(cfg, date_text)
    _write_plan_section(config=cfg, date_text=date_text, plan=plan)

    event_logger = logger or EventLogger()
    append_llm_call_event(event_logger, provider, "daily_plan")
    event_logger.append_event(
        "plan_generated",
        f"生成 {date_text} 的计划",
        {
            "date": date_text,
            "daily_path": str(daily_path),
            "goals_chars": len(goals),
            "tasks_chars": len(tasks.strip()),
        },
    )

    return DailyPlanResult(date=date_text, path=daily_path, plan=plan)


def _daily_path(config: dict[str, Any], date_text: str) -> Path:
    daily_dir = resolve_project_path(config["paths"]["daily_dir"])
    daily_dir.mkdir(parents=True, exist_ok=True)
    return daily_dir / f"{date_text}.md"


def _build_prompt(date_text: str, goals: str, tasks: str) -> str:
    goals_text = goals.strip() or "（尚未填写长期目标）"
    tasks_text = tasks.strip() or "（尚未填写今日待办）"
    return f"""你是一个帮助用户安排当天时间的个人执行助手。

请根据长期目标和今日待办，为 {date_text} 生成一份当天时间安排表。

输出结构：
1. 只输出 markdown 表格，不要输出“时间安排”标题，不要输出风险提醒、收尾检查、注意事项、保底完成标准、对应执行内容或具体技术步骤。
2. 时间安排用 markdown 表格，表头必须使用英文竖线，固定为：| 任务 | 优先级 | 预估时间 | 完成 | 备注 |。“完成”和“备注”两列都不填。
3. 预估时要考虑用户会用 Codex 辅助工作。如果工作总时间超出6小时（工作外的杂事不算入时间），在表格后用一句话提示超时，并说明6小时内优先做哪几个任务。

其他要求：
- 不要输出“今日待办”原文；这部分由程序直接写入。
- 今日待办只用于分析，不要复制、改写、重排今日待办原文。
- 如果长期目标或今日待办看起来还只是模板或为空，在表格后用一句话提醒用户补充。
- 输出采用 markdown 格式，但不要用```markdown，标题不可使用#和##。
- 不以询问句结尾。

长期目标：
{goals_text}

今日待办：
{tasks_text}
"""


def _build_plan(tasks: str, plan_schedule: str) -> str:
    today_tasks = _format_today_tasks_section(tasks)
    schedule_text = _normalize_schedule_table(plan_schedule.strip())
    if not schedule_text:
        return today_tasks
    return f"{today_tasks}\n\n{schedule_text}".strip()


def _format_today_tasks_section(tasks: str) -> str:
    tasks_text = tasks.strip() or "（尚未填写今日待办）"
    return f"**今日待办**\n\n{tasks_text}"


def _write_plan_section(config: dict[str, Any], date_text: str, plan: str) -> None:
    write_daily_section("计划", plan, config=config, target_date=date_text, mode="replace")


def _normalize_schedule_table(schedule_text: str) -> str:
    lines = schedule_text.splitlines()
    table_start = _find_schedule_table_start(lines)
    if table_start is None:
        return schedule_text

    table_end = table_start
    while table_end < len(lines) and _looks_like_table_line(lines[table_end]):
        table_end += 1

    normalized = _normalize_table_lines(lines[table_start:table_end])
    if normalized is None:
        return schedule_text
    prefix = _drop_trailing_schedule_heading(lines[:table_start])
    return "\n".join(prefix + normalized + lines[table_end:]).strip()


def _find_schedule_table_start(lines: list[str]) -> int | None:
    schedule_heading = None
    for index, line in enumerate(lines):
        if line.strip().strip("*").strip("#").strip() == "时间安排":
            schedule_heading = index
            break

    search_start = schedule_heading + 1 if schedule_heading is not None else 0
    for index in range(search_start, len(lines)):
        if _looks_like_table_line(lines[index]):
            return index
    return None


def _normalize_table_lines(table_lines: list[str]) -> list[str] | None:
    if len(table_lines) < 2:
        return None

    rows = [_split_table_row(line) for line in table_lines]
    headers = rows[0]
    if headers == ["任务", "优先级", "预估时间"]:
        expected_cols = 3
    elif headers == ["任务", "优先级", "预估时间", "完成", "备注"]:
        expected_cols = 5
    else:
        return None

    separator = rows[1]
    if len(separator) != expected_cols:
        return None

    normalized = [
        "| 任务 | 优先级 | 预估时间 | 完成 | 备注 |",
        "|---|---|---|---|---|",
    ]
    for cells in rows[2:]:
        if len(cells) != expected_cols:
            return None
        task, priority, estimate = cells[:3]
        normalized.append(f"| {task} | {priority} | {estimate} |  |  |")
    return normalized


def _drop_trailing_schedule_heading(lines: list[str]) -> list[str]:
    prefix = list(lines)
    while prefix and not prefix[-1].strip():
        prefix.pop()
    if prefix and prefix[-1].strip().strip("*").strip("#").strip() == "时间安排":
        prefix.pop()
    while prefix and not prefix[-1].strip():
        prefix.pop()
    return prefix


def _looks_like_table_line(line: str) -> bool:
    stripped = line.strip()
    if not stripped.startswith("|") or not stripped.endswith("|"):
        return False
    return "|" in stripped.strip("|") or "｜" in stripped.strip("|")


def _split_table_row(line: str) -> list[str]:
    normalized = line.replace("｜", "|")
    return [cell.strip() for cell in normalized.strip().strip("|").split("|")]
