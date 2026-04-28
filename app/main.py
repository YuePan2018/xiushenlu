from __future__ import annotations

import argparse
import sys
from pathlib import Path


if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import load_config
from app.cost import summarize_token_usage
from app.daily import append_record, read_daily
from app.inbox import write_today_tasks
from app.llm.qwen_agent_impl import QwenAgentProvider
from app.logger import EventLogger
from app.pipelines.daily_plan import generate_daily_plan
from app.pipelines.nightly_review import generate_nightly_review


def configure_output_encoding() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="xiushenlu")
    subparsers = parser.add_subparsers(dest="command")

    plan = subparsers.add_parser("plan", help="生成今日计划")
    plan.add_argument("--tasks", help="直接传入今日待办，并写入 data/user_inputs/today_tasks.md")

    review = subparsers.add_parser("review", help="生成晚间复盘")
    review.add_argument("--date", help="指定日期，格式 YYYY-MM-DD，默认今天")

    log = subparsers.add_parser("log", help="添加一条今日记录")
    log.add_argument("content", nargs="+", help="记录内容")

    subparsers.add_parser("status", help="查看今日 daily 内容")
    subparsers.add_parser("cost", help="查看今日和本月 token 消耗")

    return parser


def smoke_test() -> int:
    config = load_config()
    provider = QwenAgentProvider(config)
    reply = provider.chat("你好，请用一句话确认你已经连通。")
    print(reply)
    return 0


def run_plan(tasks: str | None = None) -> int:
    config = load_config()
    if tasks is not None:
        write_today_tasks(tasks, config)
    provider = QwenAgentProvider(config)
    result = generate_daily_plan(provider, config=config, tasks_text=tasks)
    print(f"计划已写入：{result.path}")
    print()
    print(result.plan)
    return 0


def run_review(date_str: str | None = None) -> int:
    from datetime import date as _date
    config = load_config()
    provider = QwenAgentProvider(config)
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
    today_text = _format_stats("今日", stats["today"])
    month_text = _format_stats("本月", stats["month"])
    report = f"{today_text}\n\n{month_text}"
    print(report)

    path = append_record(f"token 消耗统计\n```text\n{report}\n```", config)
    print()
    print(f"统计已写入：{path}")
    return 0


def _format_stats(label: str, stats) -> str:
    lines = [
        f"{label} LLM 调用：{stats.calls} 次",
        f"输入 token：{stats.tokens_in}",
        f"输出 token：{stats.tokens_out}",
        f"总 token：{stats.total_tokens}",
        f"估算调用：{stats.estimated_calls} 次",
    ]
    if stats.by_model:
        lines.append("按模型：")
        for model, total in sorted(stats.by_model.items()):
            lines.append(f"- {model}: {total} tokens")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    configure_output_encoding()
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "plan":
        return run_plan(tasks=args.tasks)
    if args.command == "review":
        return run_review(date_str=args.date)
    if args.command == "log":
        return run_log(args.content)
    if args.command == "status":
        return run_status()
    if args.command == "cost":
        return run_cost()

    return smoke_test()


if __name__ == "__main__":
    raise SystemExit(main())
