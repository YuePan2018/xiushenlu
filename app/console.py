from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from threading import Lock
from typing import Any, Callable
from uuid import uuid4

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app.config import ConfigError, load_config, resolve_project_path
from app.cost import append_token_usage_report
from app.daily import append_record, daily_path, read_daily
from app.inbox import ensure_today_tasks_file, read_today_tasks, today_tasks_path, write_today_tasks
from app.llm.dashscope_impl import DashScopeProvider
from app.llm.provider import LLMProvider
from app.logger import EventLogger
from app.pipelines.daily_plan import generate_daily_plan
from app.pipelines.log_schedule_update import update_schedule_from_log
from app.pipelines.nightly_review import NightlyReviewParseError, generate_nightly_review
from app.pipelines.plan_update import PlanUpdateParseError, generate_plan_update


ProviderFactory = Callable[[], LLMProvider]
STATIC_DIR = Path(__file__).resolve().parent / "static"


class OperationCancelled(RuntimeError):
    """Raised when a stopped console operation returns after the user cancelled it."""


@dataclass(frozen=True)
class OperationToken:
    id: str
    label: str


@dataclass
class OperationState:
    id: str
    label: str
    started_at: str
    cancel_requested: bool = False


class OperationManager:
    def __init__(self) -> None:
        self._lock = Lock()
        self._active: OperationState | None = None

    def begin(self, label: str) -> OperationToken:
        with self._lock:
            if self._active is not None:
                raise RuntimeError(f"已有操作正在运行：{self._active.label}")
            state = OperationState(
                id=uuid4().hex,
                label=label,
                started_at=datetime.now().isoformat(timespec="seconds"),
            )
            self._active = state
            return OperationToken(id=state.id, label=label)

    def finish(self, token: OperationToken) -> None:
        with self._lock:
            if self._active is not None and self._active.id == token.id:
                self._active = None

    def stop(self) -> dict[str, Any]:
        with self._lock:
            if self._active is None:
                return {
                    "message": "当前没有正在运行的 LLM 操作。",
                    "operation": self._snapshot_locked(),
                }
            self._active.cancel_requested = True
            return {
                "message": f"已请求停止：{self._active.label}。",
                "operation": self._snapshot_locked(),
            }

    def check_cancelled(self, token: OperationToken) -> None:
        with self._lock:
            if (
                self._active is not None
                and self._active.id == token.id
                and self._active.cancel_requested
            ):
                raise OperationCancelled("操作已停止，LLM 返回结果已丢弃。")

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return self._snapshot_locked()

    def _snapshot_locked(self) -> dict[str, Any]:
        if self._active is None:
            return {
                "active": False,
                "id": None,
                "label": None,
                "started_at": None,
                "cancel_requested": False,
            }
        return {
            "active": True,
            "id": self._active.id,
            "label": self._active.label,
            "started_at": self._active.started_at,
            "cancel_requested": self._active.cancel_requested,
        }


class PlanRequest(BaseModel):
    add: str | None = None

    class Config:
        extra = "forbid"


class TasksRequest(BaseModel):
    tasks: str

    class Config:
        extra = "forbid"


class LogRequest(BaseModel):
    content: str

    class Config:
        extra = "forbid"


class ReviewRequest(BaseModel):
    date: str | None = None
    rollover: bool = True

    class Config:
        extra = "forbid"


class CostRequest(BaseModel):
    date: str | None = None

    class Config:
        extra = "forbid"


class ConsoleService:
    def __init__(self, config: dict[str, Any], provider_factory: ProviderFactory) -> None:
        self.config = config
        self.provider_factory = provider_factory
        self.operations = OperationManager()

    def snapshot(self, date_text: str | None = None) -> dict[str, Any]:
        target_date = _parse_date(date_text) if date_text else date.today()
        target_text = target_date.isoformat()
        daily_file = daily_path(self.config, target_text)
        tasks_file = today_tasks_path(self.config)

        return {
            "date": target_text,
            "today": date.today().isoformat(),
            "daily": {
                "path": str(daily_file),
                "exists": daily_file.exists(),
                "text": read_daily(self.config, target_text),
            },
            "tasks": {
                "path": str(tasks_file),
                "exists": tasks_file.exists(),
                "text": read_today_tasks(self.config),
            },
            "future": [
                {"name": "自动化", "status": "预留"},
                {"name": "通知", "status": "预留"},
                {"name": "审批", "status": "预留"},
                {"name": "工具", "status": "预留"},
                {"name": "知识", "status": "预留"},
            ],
        }

    def save_tasks(self, request: TasksRequest) -> dict[str, Any]:
        tasks = request.tasks.strip()
        if not tasks:
            raise ValueError("今日待办不能为空。")
        path = write_today_tasks(tasks, self.config)
        return {
            "message": "今日待办已保存。",
            "result": {"today_tasks_path": str(path)},
            "state": self.snapshot(),
        }

    def open_today_tasks_file(self) -> dict[str, Any]:
        path = ensure_today_tasks_file(self.config)
        _open_path_with_default_app(path)
        return {
            "message": "已请求系统打开 today_tasks.md。",
            "result": {"today_tasks_path": str(path)},
            "state": self.snapshot(),
        }

    def generate_plan(self, request: PlanRequest) -> dict[str, Any]:
        add = request.add.strip() if request.add is not None else None
        if request.add is not None and not add:
            raise ValueError("新增任务不能为空。")

        label = "局部更新计划" if add is not None else "生成计划"
        token = self.operations.begin(label)
        try:
            return self._generate_plan_locked(add, token)
        finally:
            self.operations.finish(token)

    def _generate_plan_locked(
        self,
        add: str | None,
        token: OperationToken,
    ) -> dict[str, Any]:
        provider = self.provider_factory()
        event_logger = EventLogger(config=self.config)
        if add is not None:
            result = generate_plan_update(
                provider,
                add,
                config=self.config,
                logger=event_logger,
                cancel_check=lambda: self.operations.check_cancelled(token),
            )
            return {
                "message": _message_with_llm_elapsed("计划已局部更新。", provider),
                "result": {
                    "date": result.date,
                    "daily_path": str(result.daily_path),
                    "today_tasks_path": str(result.today_tasks_path),
                    "target_heading": result.target_heading,
                },
                "state": self.snapshot(result.date),
            }

        result = generate_daily_plan(
            provider,
            config=self.config,
            logger=event_logger,
            cancel_check=lambda: self.operations.check_cancelled(token),
        )
        return {
            "message": _message_with_llm_elapsed("计划已生成。", provider),
            "result": {
                "date": result.date,
                "daily_path": str(result.path),
                "plan": result.plan,
            },
            "state": self.snapshot(result.date),
        }

    def add_log(self, request: LogRequest) -> dict[str, Any]:
        content = request.content.strip()
        if not content:
            raise ValueError("记录内容不能为空。")
        token = self.operations.begin("写入记录")
        try:
            return self._add_log_locked(content, token)
        finally:
            self.operations.finish(token)

    def _add_log_locked(self, content: str, token: OperationToken) -> dict[str, Any]:
        path = append_record(content, self.config)
        event_logger = EventLogger(config=self.config)
        event_logger.append_event(
            "user_log",
            "添加今日记录",
            {
                "date": path.stem,
                "daily_path": str(path),
                "content": content,
            },
        )
        provider = self.provider_factory()
        try:
            schedule_result = update_schedule_from_log(
                provider,
                content,
                config=self.config,
                logger=event_logger,
                cancel_check=lambda: self.operations.check_cancelled(token),
            )
        except OperationCancelled:
            raise
        except Exception as exc:
            return {
                "message": f"记录已写入，任务表未更新：{exc}",
                "result": {
                    "daily_path": str(path),
                    "schedule_updated": False,
                    "schedule_reason": str(exc),
                },
                "state": self.snapshot(path.stem),
            }

        if schedule_result.updated:
            message = _message_with_llm_elapsed("记录已写入，任务表已更新。", provider)
        else:
            reason = schedule_result.reason or "无需更新"
            message = _message_with_llm_elapsed(f"记录已写入，任务表未更新：{reason}", provider)
        return {
            "message": message,
            "result": {
                "daily_path": str(path),
                "schedule_updated": schedule_result.updated,
                "schedule_reason": schedule_result.reason,
            },
            "state": self.snapshot(path.stem),
        }

    def generate_review(self, request: ReviewRequest) -> dict[str, Any]:
        token = self.operations.begin("生成复盘")
        try:
            return self._generate_review_locked(request, token)
        finally:
            self.operations.finish(token)

    def _generate_review_locked(self, request: ReviewRequest, token: OperationToken) -> dict[str, Any]:
        target_date = _parse_date(request.date) if request.date else None
        provider = self.provider_factory()
        result = generate_nightly_review(
            provider,
            config=self.config,
            target_date=target_date,
            logger=EventLogger(config=self.config),
            cancel_check=lambda: self.operations.check_cancelled(token),
            rollover=request.rollover,
        )
        return {
            "message": _message_with_llm_elapsed("复盘已生成。", provider),
            "result": {
                "date": result.date,
                "daily_path": str(result.path),
                "review": result.review,
            },
            "state": self.snapshot(result.date),
        }

    def report_tokens(self, request: CostRequest) -> dict[str, Any]:
        target_date = _parse_date(request.date) if request.date else date.today()
        result = append_token_usage_report(
            self.config,
            EventLogger(config=self.config),
            target_date,
        )
        return {
            "message": "token 统计已更新。",
            "result": {
                "date": target_date.isoformat(),
                "daily_path": str(result.path),
                "report": result.report,
            },
            "state": self.snapshot(target_date.isoformat()),
        }

    def operation_snapshot(self) -> dict[str, Any]:
        return self.operations.snapshot()

    def stop_operation(self) -> dict[str, Any]:
        return self.operations.stop()


def create_app(
    config: dict[str, Any] | None = None,
    provider_factory: ProviderFactory | None = None,
) -> FastAPI:
    cfg = config or load_config()
    factory = provider_factory or (lambda: DashScopeProvider(cfg))
    service = ConsoleService(cfg, factory)
    app = FastAPI(title="修身炉本地控制台")
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.state.console_service = service

    @app.get("/", response_class=HTMLResponse)
    def index() -> HTMLResponse:
        return HTMLResponse(CONSOLE_HTML)

    @app.get("/api/state")
    def api_state(date: str | None = None) -> dict[str, Any]:
        return _handle(lambda: service.snapshot(date))

    @app.get("/api/operation")
    def api_operation() -> dict[str, Any]:
        return _handle(service.operation_snapshot)

    @app.post("/api/operation/stop")
    def api_stop_operation() -> dict[str, Any]:
        return _handle(service.stop_operation)

    @app.post("/api/tasks")
    def api_tasks(request: TasksRequest) -> dict[str, Any]:
        return _handle(lambda: service.save_tasks(request))

    @app.post("/api/tasks/open")
    def api_open_tasks() -> dict[str, Any]:
        return _handle(service.open_today_tasks_file)

    @app.post("/api/plan")
    def api_plan(request: PlanRequest) -> dict[str, Any]:
        return _handle(lambda: service.generate_plan(request))

    @app.post("/api/log")
    def api_log(request: LogRequest) -> dict[str, Any]:
        return _handle(lambda: service.add_log(request))

    @app.post("/api/review")
    def api_review(request: ReviewRequest) -> dict[str, Any]:
        return _handle(lambda: service.generate_review(request))

    @app.post("/api/cost")
    def api_cost(request: CostRequest) -> dict[str, Any]:
        return _handle(lambda: service.report_tokens(request))

    return app


def _handle(operation: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    try:
        return operation()
    except OperationCancelled as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (ValueError, PlanUpdateParseError, NightlyReviewParseError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _message_with_llm_elapsed(message: str, provider: LLMProvider) -> str:
    response_seconds = _provider_llm_response_seconds(provider)
    if response_seconds is None:
        return message
    return f"{message}耗时{_format_elapsed_seconds(response_seconds)}"


def _provider_llm_response_seconds(provider: LLMProvider) -> float | None:
    usage = getattr(provider, "last_usage", None)
    response_seconds = getattr(usage, "response_seconds", None)
    if response_seconds is None:
        response_seconds = getattr(provider, "last_response_seconds", None)
    if isinstance(response_seconds, (int, float)) and response_seconds >= 0:
        return float(response_seconds)
    return None


def _format_elapsed_seconds(seconds: float) -> str:
    if seconds < 1:
        return f"{max(seconds, 0.1):.1f}s"
    if seconds < 10:
        return f"{seconds:.1f}s"
    return f"{seconds:.0f}s"


def _parse_date(value: str | None) -> date:
    text = (value or "").strip()
    if not text:
        return date.today()
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise ValueError("日期格式必须是 YYYY-MM-DD。") from exc


def _project_file(path: str) -> str:
    return str(resolve_project_path(Path(path)))


def _open_path_with_default_app(path: Path) -> None:
    try:
        if sys.platform.startswith("win"):
            os.startfile(str(path))  # type: ignore[attr-defined]
            return
        if sys.platform == "darwin":
            subprocess.Popen(["open", str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return
        subprocess.Popen(["xdg-open", str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as exc:
        raise RuntimeError(f"无法打开文件：{exc}") from exc


CONSOLE_HTML = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>修身炉控制台</title>
  <style>
    :root {{
      color-scheme: light;
      --bg: #f7f4ed;
      --surface: #fffdf8;
      --surface-2: #f1eee7;
      --text: #24211d;
      --muted: #6f6a60;
      --line: #d8d1c5;
      --accent: #2f6f5e;
      --accent-dark: #245548;
      --warn: #a15d18;
      --danger: #a33b32;
      --shadow: 0 16px 36px rgba(37, 32, 25, 0.08);
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font-family: "Segoe UI", "Microsoft YaHei", Arial, sans-serif;
      font-size: 15px;
      line-height: 1.5;
    }}
    header {{
      position: sticky;
      top: 0;
      z-index: 5;
      border-bottom: 1px solid var(--line);
      background: rgba(247, 244, 237, 0.94);
      backdrop-filter: blur(10px);
    }}
    .bar {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      max-width: 1480px;
      margin: 0 auto;
      padding: 14px 20px;
    }}
    .brand {{
      flex: 1 1 auto;
      min-width: 220px;
    }}
    .slogan {{
      flex: 1 1 260px;
      max-width: 420px;
      border-left: 1px solid var(--line);
      padding-left: 16px;
    }}
    .slogan h2 {{
      margin-bottom: 6px;
    }}
    .slogan input {{
      min-height: 36px;
      padding: 7px 10px;
    }}
    .top-status {{
      flex: 1 1 340px;
      max-width: 520px;
      border-left: 1px solid var(--line);
      padding-left: 16px;
    }}
    .top-status h2 {{
      margin-bottom: 4px;
    }}
    h1 {{
      margin: 0;
      font-size: 22px;
      font-weight: 720;
      letter-spacing: 0;
    }}
    h2 {{
      margin: 0 0 12px;
      font-size: 16px;
      font-weight: 700;
      letter-spacing: 0;
    }}
    h3 {{
      margin: 0 0 8px;
      font-size: 14px;
      font-weight: 700;
      letter-spacing: 0;
    }}
    main {{
      display: grid;
      grid-template-columns: minmax(320px, 420px) minmax(420px, 1fr);
      gap: 16px;
      max-width: 1480px;
      margin: 0 auto;
      padding: 16px 20px 28px;
    }}
    section,
    details.panel {{
      background: var(--surface);
      border: 1px solid var(--line);
      border-radius: 8px;
      box-shadow: var(--shadow);
      padding: 14px;
    }}
    details.panel summary {{
      list-style: none;
    }}
    details.panel summary::-webkit-details-marker {{
      display: none;
    }}
    .collapsible-summary {{
      display: flex;
      align-items: center;
      gap: 9px;
      cursor: pointer;
      user-select: none;
    }}
    .collapsible-summary:focus-visible {{
      border-radius: 7px;
      outline: 3px solid rgba(47, 111, 94, 0.18);
      outline-offset: 5px;
    }}
    .collapsible-summary::before {{
      content: "";
      width: 7px;
      height: 7px;
      border-right: 2px solid var(--muted);
      border-bottom: 2px solid var(--muted);
      transform: rotate(-45deg);
      transition: transform 0.14s ease;
      flex: 0 0 auto;
    }}
    details.panel[open] .collapsible-summary::before {{
      transform: rotate(45deg);
    }}
    .panel-title {{
      margin: 0;
      font-size: 16px;
      font-weight: 700;
      letter-spacing: 0;
    }}
    .collapsible-body {{
      margin-top: 12px;
    }}
    .stack {{ display: grid; gap: 14px; align-content: start; }}
    .row {{
      display: flex;
      align-items: center;
      gap: 8px;
      flex-wrap: wrap;
    }}
    label {{
      display: block;
      margin: 0 0 6px;
      color: var(--muted);
      font-size: 13px;
    }}
    .inline-control {{
      display: inline-flex;
      align-items: center;
      gap: 6px;
      margin: 8px 0 0;
      color: var(--text);
    }}
    .inline-control input {{
      width: auto;
      margin: 0;
    }}
    input, textarea {{
      width: 100%;
      border: 1px solid var(--line);
      border-radius: 7px;
      background: #fffefa;
      color: var(--text);
      font: inherit;
      padding: 9px 10px;
      outline: none;
    }}
    input:focus, textarea:focus {{
      border-color: var(--accent);
      box-shadow: 0 0 0 3px rgba(47, 111, 94, 0.14);
    }}
    textarea {{
      min-height: 120px;
      resize: vertical;
    }}
    button {{
      border: 0;
      border-radius: 7px;
      background: var(--accent);
      color: white;
      cursor: pointer;
      font: inherit;
      font-weight: 650;
      min-height: 38px;
      padding: 8px 12px;
      white-space: nowrap;
    }}
    button:hover {{ background: var(--accent-dark); }}
    button.secondary {{
      background: var(--surface-2);
      color: var(--text);
      border: 1px solid var(--line);
    }}
    button.secondary:hover {{ background: #e7e1d7; }}
    button.warn {{ background: var(--warn); }}
    button:disabled {{
      cursor: wait;
      opacity: 0.65;
    }}
    .meta {{
      color: var(--muted);
      font-size: 13px;
      overflow-wrap: anywhere;
    }}
    .status {{
      min-height: 24px;
      color: var(--muted);
      font-size: 13px;
    }}
    .status.error {{ color: var(--danger); }}
    .daily-markdown {{
      margin: 0;
      min-height: 520px;
      max-height: calc(100vh - 168px);
      overflow: auto;
      word-break: break-word;
      font-size: 13px;
      line-height: 1.62;
      background: #fffcf4;
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 14px;
    }}
    .daily-markdown.empty {{
      color: var(--muted);
    }}
    .daily-markdown > :first-child {{
      margin-top: 0;
    }}
    .daily-markdown > :last-child {{
      margin-bottom: 0;
    }}
    .daily-markdown h1,
    .daily-markdown h2,
    .daily-markdown h3 {{
      color: var(--text);
      letter-spacing: 0;
      line-height: 1.3;
    }}
    .daily-markdown h1 {{
      margin: 0 0 14px;
      padding-bottom: 8px;
      border-bottom: 1px solid var(--line);
      font-size: 22px;
      font-weight: 720;
    }}
    .daily-markdown h2 {{
      margin: 18px 0 10px;
      padding-bottom: 6px;
      border-bottom: 1px solid #ebe4d8;
      font-size: 17px;
      font-weight: 700;
    }}
    .daily-markdown h3 {{
      margin: 14px 0 8px;
      font-size: 15px;
      font-weight: 700;
    }}
    .daily-markdown p,
    .daily-markdown ul,
    .daily-markdown ol,
    .daily-markdown blockquote,
    .daily-markdown table,
    .daily-markdown pre {{
      margin: 0 0 12px;
    }}
    .daily-markdown ul,
    .daily-markdown ol {{
      padding-left: 22px;
    }}
    .daily-markdown li + li {{
      margin-top: 4px;
    }}
    .daily-markdown table {{
      width: 100%;
      border-collapse: collapse;
      overflow-wrap: normal;
    }}
    .daily-markdown th,
    .daily-markdown td {{
      border: 1px solid var(--line);
      padding: 7px 9px;
      vertical-align: top;
    }}
    .daily-markdown th {{
      background: var(--surface-2);
      font-weight: 700;
      text-align: left;
    }}
    .daily-markdown blockquote {{
      border-left: 3px solid var(--accent);
      color: var(--muted);
      padding: 2px 0 2px 12px;
    }}
    .daily-markdown code {{
      font-family: Consolas, "Cascadia Mono", "Microsoft YaHei UI", monospace;
      font-size: 0.94em;
      background: #f1eee7;
      border-radius: 5px;
      padding: 1px 5px;
    }}
    .daily-markdown pre {{
      overflow: auto;
      white-space: pre-wrap;
      background: #f3efe6;
      border: 1px solid var(--line);
      border-radius: 7px;
      padding: 10px;
    }}
    .daily-markdown pre code {{
      display: block;
      background: transparent;
      border-radius: 0;
      padding: 0;
    }}
    .daily-markdown hr {{
      border: 0;
      border-top: 1px solid var(--line);
      margin: 16px 0;
    }}
    .slots {{
      display: grid;
      grid-template-columns: 1fr;
      gap: 8px;
    }}
    .slot {{
      border: 1px dashed #b9afa0;
      border-radius: 7px;
      padding: 10px;
      color: var(--muted);
      background: #fbf7ee;
    }}
    .path {{
      margin-top: 8px;
      color: var(--muted);
      font-size: 12px;
      overflow-wrap: anywhere;
    }}
    .split {{
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 8px;
    }}
    @media (max-width: 1100px) {{
      main {{ grid-template-columns: 1fr; }}
      .daily-markdown {{ max-height: none; }}
    }}
    @media (max-width: 640px) {{
      .bar {{ align-items: flex-start; flex-direction: column; }}
      .top-status {{
        width: 100%;
        max-width: none;
        border-left: 0;
        border-top: 1px solid var(--line);
        padding-left: 0;
        padding-top: 10px;
      }}
      .slogan {{
        width: 100%;
        max-width: none;
        border-left: 0;
        border-top: 1px solid var(--line);
        padding-left: 0;
        padding-top: 10px;
      }}
      main {{ padding: 12px; }}
      .split, .slots {{ grid-template-columns: 1fr; }}
      button {{ width: 100%; }}
    }}
  </style>
</head>
<body>
  <header>
    <div class="bar">
      <div class="brand">
        <h1>修身炉控制台</h1>
        <div class="meta">本机入口：{_project_file("app/console.py")}</div>
      </div>
      <div class="slogan">
        <h2>口号</h2>
        <input id="sloganInput" type="text" autocomplete="off">
      </div>
      <div class="top-status">
        <h2>运行状态</h2>
        <div class="status" id="statusText">准备中</div>
      </div>
      <div class="row">
        <input id="dateInput" type="date" aria-label="日期">
        <button class="secondary" id="refreshBtn">刷新</button>
        <button class="warn" id="stopBtn" hidden>停止</button>
      </div>
    </div>
  </header>
  <main>
    <div class="stack">
      <details class="panel">
        <summary class="collapsible-summary">
          <span class="panel-title">过程记录</span>
        </summary>
        <div class="collapsible-body">
          <label for="logInput">记录内容</label>
          <textarea id="logInput" spellcheck="false"></textarea>
          <button id="logBtn">写入记录</button>
        </div>
      </details>
      <details class="panel">
        <summary class="collapsible-summary">
          <span class="panel-title">更新计划</span>
        </summary>
        <div class="collapsible-body">
          <label for="addInput">新增任务</label>
          <textarea id="addInput" spellcheck="false"></textarea>
          <button id="addBtn">局部更新计划</button>
        </div>
      </details>
      <details class="panel">
        <summary class="collapsible-summary">
          <span class="panel-title">生成计划</span>
        </summary>
        <div class="collapsible-body">
          <label for="tasksInput">today_tasks.md</label>
          <textarea id="tasksInput" spellcheck="false"></textarea>
          <div class="row">
            <button class="secondary" id="saveTasksBtn">保存待办</button>
            <button id="planBtn">生成计划</button>
            <button class="secondary" id="reloadTasksBtn">读取待办</button>
            <button class="secondary" id="openTasksBtn">打开文件</button>
          </div>
          <div class="path" id="tasksPath"></div>
        </div>
      </details>
      <details class="panel">
        <summary class="collapsible-summary">
          <span class="panel-title">复盘</span>
        </summary>
        <div class="collapsible-body">
          <div class="split">
            <div>
              <label for="reviewDateInput">复盘日期</label>
              <input id="reviewDateInput" type="date">
              <label class="inline-control">
                <input id="reviewRolloverInput" type="checkbox" checked>
                滚动待办
              </label>
            </div>
            <div>
              <label>&nbsp;</label>
              <div class="row">
                <button id="reviewBtn">生成复盘</button>
                <button class="secondary" id="tokenBtn">token</button>
              </div>
            </div>
          </div>
        </div>
      </details>
    </div>
    <section>
      <h2>Daily</h2>
      <article id="dailyText" class="daily-markdown empty" aria-live="polite">加载中...</article>
      <div class="path" id="dailyPath"></div>
    </section>
    <section hidden>
      <h2>后续区域</h2>
      <div class="slots" id="futureSlots"></div>
    </section>
  </main>
  <script src="/static/vendor/marked-16.2.1.umd.js"></script>
  <script src="/static/vendor/dompurify-3.2.6.min.js"></script>
  <script>
    const state = {{
      current: null,
      busy: false,
      operation: {{ active: false, cancel_requested: false }},
      actionController: null,
      operationPoller: null,
    }};
    const $ = (id) => document.getElementById(id);
    const llmButtonIds = ["planBtn", "addBtn", "logBtn", "reviewBtn"];
    const sloganStorageKey = "xiushenlu.console.slogan";

    if (window.marked) {{
      marked.setOptions({{
        gfm: true,
        breaks: false,
      }});
    }}

    function setBusy(value) {{
      state.busy = value;
      updateControls();
    }}

    function setOperation(operation) {{
      state.operation = operation || {{ active: false, cancel_requested: false }};
      updateControls();
      if (state.operation.active) {{
        startOperationPolling();
      }} else {{
        stopOperationPolling();
      }}
    }}

    function updateControls() {{
      const operationActive = Boolean(state.operation && state.operation.active);
      const cancelRequested = Boolean(state.operation && state.operation.cancel_requested);
      for (const button of document.querySelectorAll("button")) {{
        if (button.id === "stopBtn") {{
          button.hidden = !operationActive;
          button.disabled = !operationActive || cancelRequested;
          continue;
        }}
        if (llmButtonIds.includes(button.id)) {{
          button.disabled = state.busy || operationActive;
          continue;
        }}
        button.disabled = state.busy;
      }}
    }}

    function setStatus(text, error = false) {{
      const el = $("statusText");
      el.textContent = text;
      el.className = error ? "status error" : "status";
    }}

    async function requestJson(url, options = {{}}) {{
      const response = await fetch(url, {{
        headers: {{ "Content-Type": "application/json" }},
        ...options,
      }});
      const body = await response.json().catch(() => ({{ detail: response.statusText }}));
      if (!response.ok) {{
        const error = new Error(body.detail || response.statusText);
        error.status = response.status;
        throw error;
      }}
      return body;
    }}

    async function loadOperation() {{
      const operation = await requestJson("/api/operation");
      setOperation(operation);
      return operation;
    }}

    async function loadState(dateValue = $("dateInput").value) {{
      setBusy(true);
      try {{
        const query = dateValue ? `?date=${{encodeURIComponent(dateValue)}}` : "";
        const data = await requestJson(`/api/state${{query}}`);
        renderState(data);
        setStatus(`已加载 ${{data.date}}`);
      }} catch (error) {{
        setStatus(error.message, true);
      }} finally {{
        setBusy(false);
      }}
    }}

    function renderState(data) {{
      state.current = data;
      $("dateInput").value = data.date;
      $("reviewDateInput").value = data.date;
      renderDaily(data.daily.text);
      $("dailyPath").textContent = data.daily.path;
      $("tasksInput").value = data.tasks.text || "";
      $("tasksPath").textContent = data.tasks.path;
      renderFuture(data.future);
    }}

    function renderDaily(text) {{
      const el = $("dailyText");
      const markdown = String(text || "").trim();
      if (!markdown) {{
        el.classList.add("empty");
        el.textContent = "今天还没有 daily 记录。";
        return;
      }}
      el.classList.remove("empty");
      if (!window.marked || !window.DOMPurify) {{
        el.textContent = markdown;
        return;
      }}
      const html = marked.parse(markdown);
      el.innerHTML = DOMPurify.sanitize(html, {{ USE_PROFILES: {{ html: true }} }});
    }}

    function renderFuture(items) {{
      const el = $("futureSlots");
      if (!el) {{
        return;
      }}
      el.innerHTML = items.map((item) =>
        `<div class="slot"><strong>${{escapeHtml(item.name)}}</strong><div>${{escapeHtml(item.status)}}</div></div>`
      ).join("");
    }}

    function escapeHtml(value) {{
      return String(value)
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;")
        .replaceAll("'", "&#039;");
    }}

    async function runAction(label, operation) {{
      setBusy(true);
      setStatus(`${{label}}中...`);
      try {{
        const data = await operation();
        if (data.state) {{
          renderState(data.state);
        }} else {{
          await loadState();
        }}
        setStatus(data.message || `${{label}}完成`);
      }} catch (error) {{
        setStatus(error.message, true);
      }} finally {{
        setBusy(false);
      }}
    }}

    async function runLongAction(label, operation) {{
      if (state.operation.active) {{
        setStatus(`已有操作正在运行：${{state.operation.label || "LLM 操作"}}`, true);
        return;
      }}
      const controller = new AbortController();
      state.actionController = controller;
      setOperation({{
        active: true,
        id: null,
        label,
        started_at: null,
        cancel_requested: false,
      }});
      setBusy(true);
      setStatus(`${{label}}中...`);
      try {{
        const data = await operation(controller.signal);
        if (data.state) {{
          renderState(data.state);
        }} else {{
          await loadState();
        }}
        setStatus(data.message || `${{label}}完成`);
      }} catch (error) {{
        if (error.name === "AbortError") {{
          setStatus("已请求停止，等待后端丢弃 LLM 返回结果。");
        }} else if (error.status === 409) {{
          setStatus(error.message);
        }} else {{
          setStatus(error.message, true);
        }}
      }} finally {{
        state.actionController = null;
        setBusy(false);
        await loadOperation().catch(() => null);
      }}
    }}

    async function stopCurrentOperation() {{
      if (state.actionController) {{
        state.actionController.abort();
      }}
      try {{
        const data = await requestJson("/api/operation/stop", {{
          method: "POST",
        }});
        setOperation(data.operation);
        setStatus(data.message || "已请求停止。");
      }} catch (error) {{
        setStatus(error.message, true);
      }}
    }}

    function startOperationPolling() {{
      if (state.operationPoller) {{
        return;
      }}
      state.operationPoller = window.setInterval(async () => {{
        try {{
          const operation = await loadOperation();
          if (!operation.active) {{
            await loadState();
          }}
        }} catch (error) {{
          stopOperationPolling();
        }}
      }}, 2000);
    }}

    function stopOperationPolling() {{
      if (!state.operationPoller) {{
        return;
      }}
      window.clearInterval(state.operationPoller);
      state.operationPoller = null;
    }}

    function submitOnCtrlEnter(inputId, buttonId) {{
      $(inputId).addEventListener("keydown", (event) => {{
        if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) {{
          event.preventDefault();
          if (!$(buttonId).disabled) {{
            $(buttonId).click();
          }}
        }}
      }});
    }}

    function loadSlogan() {{
      $("sloganInput").value = window.localStorage.getItem(sloganStorageKey) || "";
    }}

    function saveSlogan() {{
      window.localStorage.setItem(sloganStorageKey, $("sloganInput").value);
    }}

    $("refreshBtn").addEventListener("click", () => loadState());
    $("stopBtn").addEventListener("click", () => stopCurrentOperation());
    $("sloganInput").addEventListener("input", saveSlogan);
    $("reloadTasksBtn").addEventListener("click", () => loadState());
    $("openTasksBtn").addEventListener("click", () => runAction("打开文件", () =>
      requestJson("/api/tasks/open", {{
        method: "POST",
      }})
    ));
    $("saveTasksBtn").addEventListener("click", () => runAction("保存待办", () =>
      requestJson("/api/tasks", {{
        method: "POST",
        body: JSON.stringify({{ tasks: $("tasksInput").value }}),
      }})
    ));
    $("planBtn").addEventListener("click", () => runLongAction("生成计划", (signal) =>
      requestJson("/api/plan", {{
        method: "POST",
        signal,
        body: JSON.stringify({{}}),
      }})
    ));
    $("addBtn").addEventListener("click", () => runLongAction("局部更新", async (signal) => {{
      const data = await requestJson("/api/plan", {{
        method: "POST",
        signal,
        body: JSON.stringify({{ add: $("addInput").value }}),
      }});
      $("addInput").value = "";
      return data;
    }}));
    $("logBtn").addEventListener("click", () => runLongAction("写入记录", async (signal) => {{
      const data = await requestJson("/api/log", {{
        method: "POST",
        signal,
        body: JSON.stringify({{ content: $("logInput").value }}),
      }});
      $("logInput").value = "";
      return data;
    }}));
    $("reviewBtn").addEventListener("click", () => runLongAction("生成复盘", (signal) =>
      requestJson("/api/review", {{
        method: "POST",
        signal,
        body: JSON.stringify({{
          date: $("reviewDateInput").value,
          rollover: $("reviewRolloverInput").checked,
        }}),
      }})
    ));
    $("tokenBtn").addEventListener("click", () => runAction("token 统计", () =>
      requestJson("/api/cost", {{
        method: "POST",
        body: JSON.stringify({{ date: $("reviewDateInput").value }}),
      }})
    ));
    submitOnCtrlEnter("logInput", "logBtn");
    submitOnCtrlEnter("addInput", "addBtn");
    loadSlogan();
    loadState();
    loadOperation().catch(() => null);
  </script>
</body>
</html>
"""


try:
    app = create_app()
except ConfigError as exc:
    _CONFIG_ERROR = str(exc)
    app = FastAPI(title="修身炉本地控制台")

    @app.get("/")
    def config_error() -> dict[str, str]:
        raise HTTPException(status_code=500, detail=_CONFIG_ERROR)
