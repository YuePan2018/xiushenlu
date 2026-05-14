from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import load_config, resolve_project_path
from app.cost import append_token_usage_report
from app.daily import append_record, read_daily
from app.inbox import write_today_tasks
from app.llm.dashscope_impl import DashScopeProvider
from app.logger import EventLogger
from app.pipelines.daily_plan import generate_daily_plan
from app.pipelines.log_schedule_update import update_schedule_from_log
from app.pipelines.nightly_review import NightlyReviewParseError, generate_nightly_review
from app.pipelines.plan_update import PlanUpdateParseError, generate_plan_update
from app.posting import publish_xhs_from_draft
from app.posting.xhs_mcp import XhsMcpClient, XhsMcpError
from app.safety import safe_read_text


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
    review.add_argument(
        "--no-rollover",
        action="store_true",
        help="只生成复盘，不滚动 data/user_inputs/today_tasks.md",
    )

    log = subparsers.add_parser("log", help="添加一条今日记录")
    log.add_argument("content", nargs="+", help="记录内容")

    subparsers.add_parser("status", help="查看今日 daily 内容")
    subparsers.add_parser("cost", help="查看今日和本月 token 消耗")

    xhs = subparsers.add_parser("xhs", help="小红书图文发布")
    xhs_subparsers = xhs.add_subparsers(dest="xhs_command")

    xhs_subparsers.add_parser("status", help="检查 xiaohongshu-mcp 登录状态")

    xhs_publish = xhs_subparsers.add_parser("publish", help="从 post/data 草稿发布小红书图文")
    xhs_publish.add_argument("--draft", required=True, help="post/data 下的草稿文件路径")
    xhs_publish.add_argument("--title", required=True, help="小红书标题")
    xhs_publish.add_argument("--image", action="append", required=True, help="图片绝对路径或 HTTP/HTTPS URL，可重复传入")
    xhs_publish.add_argument("--tag", action="append", default=[], help="话题标签，可重复传入")
    xhs_publish.add_argument(
        "--visibility",
        default="仅自己可见",
        choices=["公开可见", "仅自己可见", "仅互关好友可见"],
        help="可见范围，默认仅自己可见",
    )
    xhs_publish.add_argument("--schedule-at", default="", help="定时发布时间，ISO8601 格式")
    xhs_publish.add_argument("--original", action="store_true", help="声明原创")
    xhs_publish.add_argument("--product", action="append", default=[], help="商品关键词，可重复传入")
    xhs_publish.add_argument("--approve", action="store_true", help="确认调用 xiaohongshu-mcp 真实发布")

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
            print(f"计划更新失败：LLM 没有返回符合约束的 JSON。{exc}", file=sys.stderr)
            return 1
        print(f"今日待办已更新：{result.today_tasks_path}")
        print(f"计划已局部更新：{result.daily_path}")
        print(f"归入标题：{result.target_heading}")
        return 0

    if tasks is not None:
        write_today_tasks(tasks, config)
    result = generate_daily_plan(provider, config=config, tasks_text=tasks)
    print(f"计划已写入：{result.path}")
    print()
    print(result.plan)
    return 0


def run_review(date_str: str | None = None, rollover: bool = True) -> int:
    from datetime import date as _date
    config = load_config()
    provider = DashScopeProvider(config)
    target_date = _date.fromisoformat(date_str) if date_str else None
    try:
        result = generate_nightly_review(
            provider,
            config=config,
            target_date=target_date,
            rollover=rollover,
        )
    except NightlyReviewParseError as exc:
        print(f"复盘失败：LLM 没有返回可解析的 JSON。{exc}", file=sys.stderr)
        return 1
    print(f"复盘已写入：{result.path}")
    print()
    print(result.review)
    return 0


def run_log(content_parts: list[str]) -> int:
    config = load_config()
    content = " ".join(content_parts)
    path = append_record(content, config)
    event_logger = EventLogger(config=config)
    event_logger.append_event(
        "user_log",
        "添加今日记录",
        {
            "date": path.stem,
            "daily_path": str(path),
            "content": content,
        },
    )
    provider = DashScopeProvider(config)
    try:
        schedule_result = update_schedule_from_log(
            provider,
            content,
            config=config,
            logger=event_logger,
        )
    except Exception as exc:
        print(f"记录已写入：{path}")
        print(f"任务表未更新：{exc}")
        return 0

    print(f"记录已写入：{path}")
    if schedule_result.updated:
        print("任务表已更新。")
    else:
        print(f"任务表未更新：{schedule_result.reason or '无需更新'}")
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
    result = append_token_usage_report(config)
    print(result.report)

    print()
    print(f"统计已写入：{result.path}")
    return 0


def run_xhs_status() -> int:
    config = load_config()
    settings = config.get("xiaohongshu", {})
    client = XhsMcpClient(
        url=settings.get("mcp_url", "http://localhost:18060/mcp"),
        timeout=float(settings.get("timeout", 30)),
    )
    if not client.can_connect():
        print("小红书 MCP 检查失败：未连接", file=sys.stderr)
        return 1

    username = _read_xhs_cached_username(config)
    if username:
        print("MCP 已连接")
        print(f"已缓存登录：{username}")
    else:
        print("MCP 已连接")
    return 0


def _read_xhs_cached_username(config: dict[str, Any]) -> str:
    state_dir = resolve_project_path(config.get("paths", {}).get("state_dir", "data/state"))
    cache_path = state_dir / "xhs_account.json"
    if not cache_path.exists():
        return ""
    try:
        data = json.loads(safe_read_text(cache_path, config))
    except (OSError, ValueError, json.JSONDecodeError):
        return ""
    username = data.get("username") if isinstance(data, dict) else None
    return username.strip() if isinstance(username, str) else ""


def run_xhs_publish(args: argparse.Namespace) -> int:
    config = load_config()
    try:
        result = publish_xhs_from_draft(
            draft=args.draft,
            title=args.title,
            images=args.image,
            tags=args.tag,
            visibility=args.visibility,
            approve=args.approve,
            schedule_at=args.schedule_at,
            is_original=args.original,
            products=args.product,
            config=config,
        )
    except (ValueError, XhsMcpError) as exc:
        print(f"小红书发布失败：{exc}", file=sys.stderr)
        return 1

    print(f"草稿：{result.draft_path}")
    print(f"标题：{result.payload.title}")
    print(f"图片：{len(result.payload.images)} 张")
    print(f"标签：{', '.join(result.payload.tags) if result.payload.tags else '无'}")
    print(f"可见范围：{result.payload.visibility}")
    if not result.approved:
        print("已记录发布请求，但未真实发布。追加 --approve 后才会调用 xiaohongshu-mcp。")
        return 0

    print("小红书图文已提交发布。")
    if result.publish_result and result.publish_result.text:
        print(result.publish_result.text)
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
        return run_review(date_str=args.date, rollover=not args.no_rollover)
    if args.command == "log":
        return run_log(args.content)
    if args.command == "status":
        return run_status()
    if args.command == "cost":
        return run_cost()
    if args.command == "xhs":
        if args.xhs_command == "status":
            return run_xhs_status()
        if args.xhs_command == "publish":
            return run_xhs_publish(args)
        parser.error("xhs 需要指定子命令：status 或 publish")
    if args.command == "console":
        return run_console(host=args.host, port=args.port, reload=args.reload)

    return smoke_test()


if __name__ == "__main__":
    raise SystemExit(main())
