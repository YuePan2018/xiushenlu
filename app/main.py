from __future__ import annotations

import argparse
import sys
from pathlib import Path


if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import load_config
from app.cost import format_token_report, summarize_token_usage
from app.daily import append_record, read_daily
from app.inbox import write_today_tasks
from app.llm.dashscope_impl import DashScopeProvider
from app.logger import EventLogger
from app.pipelines.daily_plan import generate_daily_plan
from app.pipelines.nightly_review import generate_nightly_review
from app.pipelines.plan_update import PlanUpdateParseError, generate_plan_update


def configure_output_encoding() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="xiushenlu")
    subparsers = parser.add_subparsers(dest="command")

    plan = subparsers.add_parser("plan", help="生成或更新今日计划")
    plan_inputs = plan.add_mutually_exclusive_group()
    plan_inputs.add_argument("--tasks", help="直接传入今日待办，并写入 data/user_inputs/today_tasks.md")
    plan_inputs.add_argument("--add", help="追加一条今日待办，并局部更新当天计划")

    review = subparsers.add_parser("review", help="生成晚间复盘")
    review.add_argument("--date", help="指定日期，格式 YYYY-MM-DD，默认今天")

    log = subparsers.add_parser("log", help="添加一条今日记录")
    log.add_argument("content", nargs="+", help="记录内容")

    subparsers.add_parser("status", help="查看今日 daily 内容")
    subparsers.add_parser("cost", help="查看今日和本月 token 消耗")

    console = subparsers.add_parser("console", help="启动本地控制台")
    console.add_argument("--host", default="127.0.0.1", help="监听地址，默认只监听本机")
    console.add_argument("--port", type=int, default=8765, help="监听端口")
    console.add_argument("--reload", action="store_true", help="开发时自动重载")

    return parser


def smoke_test() -> int:
    config = load_config()
    provider = DashScopeProvider(config)
    reply = provider.chat("你好，请用一句话确认你已经连通。")
    print(reply)
    return 0


def run_plan(tasks: str | None = None, add: str | None = None) -> int:
    config = load_config()
    provider = DashScopeProvider(config)
    if add is not None:
        try:
            result = generate_plan_update(provider, add, config=config)
        except PlanUpdateParseError as exc:
            print(f"计划更新失败：LLM 没有返回可解析的 JSON。{exc}", file=sys.stderr)
            return 1
        print(f"今日待办已更新：{result.today_tasks_path}")
        print(f"计划已局部更新：{result.daily_path}")
        print(f"归入标题：{result.target_heading}")
        print()
        print(result.new_task_advice)
        return 0

    if tasks is not None:
        write_today_tasks(tasks, config)
    result = generate_daily_plan(provider, config=config, tasks_text=tasks)
    print(f"计划已写入：{result.path}")
    print()
    print(result.plan)
    return 0


def run_review(date_str: str | None = None) -> int:
    from datetime import date as _date
    config = load_config()
    provider = DashScopeProvider(config)
    target_date = _date.fromisoformat(date_str) if date_str else None
    result = generate_nightly_review(provider, config=config, target_date=target_date)
    print(f"复盘已写入：{result.path}")
    print()
    print(result.review)
    return 0


def run_log(content_parts: list[str]) -> int:
    config = load_config()
    content = " ".join(content_parts)
    path = append_record(content, config)
    EventLogger().append_event(
        "user_log",
        "添加今日记录",
        {
            "date": path.stem,
            "daily_path": str(path),
            "content": content,
        },
    )
    print(f"记录已写入：{path}")
    return 0


def run_status() -> int:
    text = read_daily()
    if not text.strip():
        print("今天还没有 daily 记录。")
        return 0
    print(text)
    return 0


def run_cost() -> int:
    config = load_config()
    stats = summarize_token_usage()
    report = format_token_report(stats)
    print(report)

    path = append_record(f"token 消耗统计\n```text\n{report}\n```", config)
    print()
    print(f"统计已写入：{path}")
    return 0


def run_console(host: str = "127.0.0.1", port: int = 8765, reload: bool = False) -> int:
    import uvicorn

    uvicorn.run("app.console:app", host=host, port=port, reload=reload)
    return 0


def main(argv: list[str] | None = None) -> int:
    configure_output_encoding()
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "plan":
        return run_plan(tasks=args.tasks, add=args.add)
    if args.command == "review":
        return run_review(date_str=args.date)
    if args.command == "log":
        return run_log(args.content)
    if args.command == "status":
        return run_status()
    if args.command == "cost":
        return run_cost()
    if args.command == "console":
        return run_console(host=args.host, port=args.port, reload=args.reload)

    return smoke_test()


if __name__ == "__main__":
    raise SystemExit(main())
