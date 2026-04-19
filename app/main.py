from __future__ import annotations

import argparse
import sys
from pathlib import Path


if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import load_config
from app.llm.qwen_agent_impl import QwenAgentProvider
from app.pipelines.daily_plan import generate_daily_plan


def configure_output_encoding() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="xiushenlu")
    subparsers = parser.add_subparsers(dest="command")

    plan = subparsers.add_parser("plan", help="生成今日计划")
    plan.add_argument("--tasks", help="直接从命令行传入今日待办，覆盖 data/inbox/today_tasks.md")

    return parser


def smoke_test() -> int:
    config = load_config()
    provider = QwenAgentProvider(config)
    reply = provider.chat("你好，请用一句话确认你已经连通。")
    print(reply)
    return 0


def run_plan(tasks: str | None = None) -> int:
    config = load_config()
    provider = QwenAgentProvider(config)
    result = generate_daily_plan(provider, config=config, tasks_text=tasks)
    print(f"计划已写入：{result.path}")
    print()
    print(result.plan)
    return 0


def main(argv: list[str] | None = None) -> int:
    configure_output_encoding()
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "plan":
        return run_plan(tasks=args.tasks)

    return smoke_test()


if __name__ == "__main__":
    raise SystemExit(main())
