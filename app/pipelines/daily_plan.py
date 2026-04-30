from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

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
) -> DailyPlanResult:
    cfg = config or load_config()
    current_date = target_date or date.today()
    date_text = current_date.isoformat()
    goals = read_goals(cfg)
    tasks = tasks_text if tasks_text is not None else read_today_tasks(cfg)
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


def _daily_path(config: dict[str, Any], date_text: str) -> Path:
    daily_dir = resolve_project_path(config["paths"]["daily_dir"])
    daily_dir.mkdir(parents=True, exist_ok=True)
    return daily_dir / f"{date_text}.md"


def _build_prompt(date_text: str, goals: str, tasks: str) -> str:
    goals_text = goals.strip() or "（尚未填写长期目标）"
    tasks_text = tasks.strip() or "（尚未填写今日待办）"
    return f"""你是一个帮助用户稳定推进学习和工作的个人执行助手。

请根据长期目标和今日待办，为 {date_text} 生成一份当天计划

输出结构（严格按此顺序）：
1. 今日待办原文（逐字复制，但可以重新标序号和调整格式）
2. 根据长期目标，对各任务给出简短建议（优先级、注意事项，预估时间）注意事项每条一句话，不给具体步骤。
预估时要考虑我会用codex辅助工作。而且如果总时间超出6小时，要给出提示，并且建议6h能做完哪几个任务。
3. 风险提醒
4. 收尾检查项

其他要求：
- 如果长期目标或今日待办看起来还只是模板或为空，提醒用户补充
- 输出采用markdown格式，可以包含表格。但不要用```markdown，标题不可使用#和##。
- 不以询问句结尾

长期目标：
{goals_text}

今日待办：
{tasks_text}
"""


def _write_plan_section(date_text: str, plan: str) -> None:
    write_daily_section("计划", plan, target_date=date_text, mode="replace")
