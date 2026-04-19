from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from app.config import load_config, resolve_project_path
from app.daily import write_daily_section
from app.llm.provider import LLMProvider
from app.llm.usage import append_llm_call_event
from app.logger import EventLogger
from app.memory.goals import read_goals
from app.safety import safe_read_text


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
) -> DailyPlanResult:
    cfg = config or load_config()
    current_date = target_date or date.today()
    date_text = current_date.isoformat()
    goals = read_goals(cfg)
    tasks = tasks_text if tasks_text is not None else _read_today_tasks(cfg)
    prompt = _build_prompt(date_text=date_text, goals=goals, tasks=tasks)

    plan = provider.chat(prompt).strip()
    daily_path = _daily_path(cfg, date_text)
    _write_plan_section(date_text=date_text, plan=plan)

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


def _read_today_tasks(config: dict[str, Any]) -> str:
    path = resolve_project_path(config["paths"]["inbox_dir"]) / "today_tasks.md"
    if not path.exists():
        return ""
    return safe_read_text(path, config).strip()


def _daily_path(config: dict[str, Any], date_text: str) -> Path:
    daily_dir = resolve_project_path(config["paths"]["daily_dir"])
    daily_dir.mkdir(parents=True, exist_ok=True)
    return daily_dir / f"{date_text}.md"


def _build_prompt(date_text: str, goals: str, tasks: str) -> str:
    goals_text = goals.strip() or "（尚未填写长期目标）"
    tasks_text = tasks.strip() or "（尚未填写今日待办）"
    return f"""你是修身炉，一个帮助用户稳定推进学习和工作的个人执行助手。

请根据长期目标和今日待办，为 {date_text} 生成一份当天计划。

要求：
- 用中文输出。
- 计划要具体、可执行，不要空泛鼓励。
- 如果长期目标或今日待办看起来还只是模板或为空，请先提醒用户补充，但仍给出一个轻量可执行的临时计划。
- 优先输出 3-5 个主任务，并说明建议顺序。
- 最后给出一个风险提醒和一个收尾检查项。
- 不要使用 emoji。

长期目标：
{goals_text}

今日待办：
{tasks_text}
"""


def _write_plan_section(date_text: str, plan: str) -> None:
    write_daily_section("计划", plan, target_date=date_text, mode="replace")
