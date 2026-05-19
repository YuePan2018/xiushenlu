from __future__ import annotations

import os
import json
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from threading import Lock
from typing import Any, Callable
from uuid import uuid4

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app.config import ConfigError, load_config, resolve_project_path
from app.cost import append_token_usage_report
from app.daily import append_record, daily_path, read_daily
from app.inbox import (
    ensure_today_tasks_file,
    long_plan_path,
    misc_tasks_path,
    read_long_plan,
    read_misc_tasks,
    read_today_tasks,
    today_tasks_path,
    write_long_plan,
    write_misc_tasks,
    write_today_tasks,
)
from app.llm.dashscope_impl import DashScopeProvider
from app.llm.provider import LLMProvider
from app.logger import EventLogger
from app.pipelines.daily_plan import generate_daily_plan
from app.pipelines.log_schedule_update import update_schedule_from_log
from app.pipelines.nightly_review import NightlyReviewParseError, generate_nightly_review
from app.pipelines.plan_update import PlanUpdateParseError, generate_plan_update
from app.posting import publish_xhs_from_draft
from app.posting.xhs_cover import generate_xhs_cover_from_text
from app.posting.xhs_mcp import XhsMcpClient
from app.safety import safe_read_text, safe_write_text
from app.task_tree import (
    list_task_trees,
    read_task_tree,
    save_task_tree as persist_task_tree,
    task_tree_dir,
)


ProviderFactory = Callable[[], LLMProvider]
XhsClientFactory = Callable[[], XhsMcpClient]
ProcessStarter = Callable[[Path, Path, bool], None]
XhsProcessFinder = Callable[[Path], list[int]]
XhsProcessStopper = Callable[[list[int]], None]
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
    tasks: str | None = None

    class Config:
        extra = "forbid"


class TasksRequest(BaseModel):
    tasks: str

    class Config:
        extra = "forbid"


class UserNotesRequest(BaseModel):
    misc: str = ""
    long_plan: str = ""

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


class TaskTreeRequest(BaseModel):
    title: str = ""
    text: str

    class Config:
        extra = "forbid"


class XhsPublishRequest(BaseModel):
    draft: str
    title: str
    images: list[str]
    tags: list[str] = []
    visibility: str = "公开可见"
    schedule_at: str = ""
    is_original: bool = False
    products: list[str] = []

    class Config:
        extra = "forbid"


class XhsPathStatusRequest(BaseModel):
    draft: str = ""
    images: list[str] = []

    class Config:
        extra = "forbid"


class XhsOpenDraftRequest(BaseModel):
    draft: str

    class Config:
        extra = "forbid"


class XhsCoverGenerateRequest(BaseModel):
    draft: str = ""

    class Config:
        extra = "forbid"


class XhsOpenImagesRequest(BaseModel):
    images: list[str] = []

    class Config:
        extra = "forbid"


class ConsoleService:
    def __init__(
        self,
        config: dict[str, Any],
        provider_factory: ProviderFactory,
        xhs_client_factory: XhsClientFactory | None = None,
        process_starter: ProcessStarter | None = None,
        process_finder: XhsProcessFinder | None = None,
        process_stopper: XhsProcessStopper | None = None,
    ) -> None:
        self.config = config
        self.provider_factory = provider_factory
        self.xhs_client_factory = xhs_client_factory or (lambda: _build_xhs_client(config))
        self.process_starter = process_starter or _start_configured_process
        self.process_finder = process_finder or _find_configured_process_ids
        self.process_stopper = process_stopper or _stop_process_ids
        self.operations = OperationManager()

    def snapshot(self, date_text: str | None = None) -> dict[str, Any]:
        target_date = _parse_date(date_text) if date_text else date.today()
        target_text = target_date.isoformat()
        daily_file = daily_path(self.config, target_text)
        tasks_file = today_tasks_path(self.config)
        misc_file = misc_tasks_path(self.config)
        long_plan_file = long_plan_path(self.config)

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
            "user_notes": {
                "misc": {
                    "path": str(misc_file),
                    "exists": misc_file.exists(),
                    "text": read_misc_tasks(self.config),
                },
                "long_plan": {
                    "path": str(long_plan_file),
                    "exists": long_plan_file.exists(),
                    "text": read_long_plan(self.config),
                },
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

    def save_user_notes(self, request: UserNotesRequest) -> dict[str, Any]:
        misc_path = write_misc_tasks(request.misc, self.config)
        long_path = write_long_plan(request.long_plan, self.config)
        return {
            "message": "杂事和长远计划已保存。",
            "result": {
                "misc_path": str(misc_path),
                "long_plan_path": str(long_path),
            },
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
        tasks = request.tasks.strip() if request.tasks is not None else None
        if request.tasks is not None and not tasks:
            raise ValueError("今日待办不能为空。")
        if add is not None and tasks is not None:
            raise ValueError("新增任务和今日待办不能同时提交。")

        label = "添加任务" if add is not None else "生成计划"
        token = self.operations.begin(label)
        try:
            return self._generate_plan_locked(add, tasks, token)
        finally:
            self.operations.finish(token)

    def _generate_plan_locked(
        self,
        add: str | None,
        tasks: str | None,
        token: OperationToken,
    ) -> dict[str, Any]:
        event_logger = EventLogger(config=self.config)
        saved_tasks_path = None
        if add is None and tasks is not None:
            saved_tasks_path = write_today_tasks(tasks, self.config)

        provider = self.provider_factory()
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
                "today_tasks_path": str(saved_tasks_path) if saved_tasks_path else None,
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

    def task_tree_state(self, title: str | None = None) -> dict[str, Any]:
        items = list_task_trees(self.config)
        selected_title = (title or "").strip()
        selected = None
        if selected_title:
            selected = _task_tree_document_payload(read_task_tree(selected_title, self.config))
        elif items:
            selected = _task_tree_document_payload(read_task_tree(items[0].title, self.config))

        return {
            "directory": str(task_tree_dir(self.config)),
            "items": [_task_tree_file_payload(item) for item in items],
            "selected": selected,
            "sample": TASK_TREE_SAMPLE_JSON,
        }

    def save_task_tree(self, request: TaskTreeRequest) -> dict[str, Any]:
        document = persist_task_tree(request.title, request.text, self.config)
        return {
            "message": f"任务树已保存：{document.filename}",
            "result": _task_tree_document_payload(document),
            "state": self.task_tree_state(document.title),
        }

    def xhs_defaults(self) -> dict[str, Any]:
        today_text = date.today().isoformat()
        draft_path = _today_xhs_draft_path(self.config, today_text)
        image_path = (
            resolve_project_path(self.config.get("paths", {}).get("post_image_dir", "data/post/images"))
            / "xiushenlu-xhs-cover.png"
        )
        return {
            "date": today_text,
            "draft_path": str(draft_path),
            "draft_exists": draft_path.exists(),
            "image_path": str(image_path),
            "image_exists": image_path.exists(),
            "visibility": "公开可见",
            "visibility_options": ["仅自己可见", "公开可见", "仅互关好友可见"],
            "title": "",
            "tags": [],
        }

    def open_xhs_draft(self, request: XhsOpenDraftRequest) -> dict[str, Any]:
        draft_path = _resolve_xhs_draft_path(self.config, request.draft)
        created = not draft_path.exists()
        if created:
            safe_write_text(draft_path, "", self.config)
        _open_path_with_vscode(draft_path)
        return {
            "message": "已用 VS Code 打开小红书草稿。",
            "result": {
                "draft_path": str(draft_path),
                "created": created,
            },
        }

    def xhs_status(self) -> dict[str, Any]:
        return self._with_xhs_connection_info(_xhs_status_payload(self.xhs_client_factory(), self.config))

    def xhs_path_status(self, request: XhsPathStatusRequest) -> dict[str, Any]:
        return {
            "draft": _describe_publish_path(request.draft),
            "images": [_describe_publish_path(path) for path in request.images],
        }

    def generate_xhs_cover(self, request: XhsCoverGenerateRequest) -> dict[str, Any]:
        draft_path, text = _read_xhs_draft_text(request.draft, self.config)
        result = generate_xhs_cover_from_text(text, self.config)
        logger = EventLogger(config=self.config)
        logger.append_event(
            "xhs_cover_generated",
            "小红书封面已生成",
            {
                "draft_path": str(draft_path),
                "image_path": str(result.image_path),
                "image_count": result.image_count,
                "width": result.width,
                "height": result.height,
                "model": result.model,
                "request_id": result.request_id,
            },
        )
        logger.append_event(
            "image_generation_usage",
            "文生图生成图片",
            {
                "task": "xhs_cover",
                "model": result.model,
                "image_count": result.image_count,
                "width": result.width,
                "height": result.height,
                "request_id": result.request_id,
                "image_path": str(result.image_path),
            },
        )
        return {
            "message": "小红书封面已生成。",
            "result": {
                "image_path": str(result.image_path),
                "image_count": result.image_count,
                "model": result.model,
            },
        }

    def open_xhs_images(self, request: XhsOpenImagesRequest) -> dict[str, Any]:
        image_targets = _resolve_image_open_targets(request.images)
        for image_target in image_targets:
            _open_value_with_default_app(image_target)
        return {
            "message": f"已打开 {len(image_targets)} 张图片。",
            "result": {
                "image_paths": image_targets,
                "images_count": len(image_targets),
            },
        }

    def start_xhs_mcp(self) -> dict[str, Any]:
        status = self.xhs_status()
        if not status["connected"]:
            mcp_exe = _configured_xhs_file(self.config, "mcp_exe")
            working_dir = _configured_xhs_working_dir(self.config, mcp_exe)
            self.process_starter(mcp_exe, working_dir, True)
            status = self._with_xhs_connection_info(_wait_for_xhs_connection(self.xhs_client_factory))

        if status.get("error"):
            return {
                "message": "MCP 已启动，但连接检查失败。",
                "result": status,
            }

        return {
            "message": "小红书 MCP 已连接。",
            "result": status,
        }

    def stop_xhs_mcp(self) -> dict[str, Any]:
        process_info = self._xhs_process_info()
        pids = process_info["pids"]
        if not pids:
            return {
                "message": "没有找到由当前配置文件指定的 xiaohongshu-mcp 进程。",
                "result": self.xhs_status(),
            }

        self.process_stopper(pids)
        return {
            "message": "小红书 MCP 已关闭。",
            "result": self.xhs_status(),
        }

    def publish_xhs(self, request: XhsPublishRequest) -> dict[str, Any]:
        result = publish_xhs_from_draft(
            draft=request.draft,
            title=request.title,
            images=request.images,
            tags=request.tags,
            visibility=request.visibility,
            approve=True,
            schedule_at=request.schedule_at,
            is_original=request.is_original,
            products=request.products,
            config=self.config,
            client=self.xhs_client_factory(),
        )
        return {
            "message": "小红书图文已提交发布。",
            "result": {
                "draft_path": str(result.draft_path),
                "title": result.payload.title,
                "images_count": len(result.payload.images),
                "tags": result.payload.tags,
                "visibility": result.payload.visibility,
                "publish_result": result.publish_result.text if result.publish_result else "",
            },
        }

    def _with_xhs_process_info(self, status: dict[str, Any]) -> dict[str, Any]:
        return {
            **status,
            **self._xhs_process_info(),
        }

    def _with_xhs_connection_info(self, status: dict[str, Any]) -> dict[str, Any]:
        connected = bool(status.get("connected"))
        return {
            **status,
            "mcp_running": connected,
            "can_stop": connected,
            "pids": [],
        }

    def _xhs_process_info(self) -> dict[str, Any]:
        try:
            mcp_exe = _configured_xhs_file(self.config, "mcp_exe")
            pids = self.process_finder(mcp_exe)
        except RuntimeError as exc:
            return {
                "mcp_running": False,
                "can_stop": False,
                "pids": [],
                "process_error": str(exc),
            }
        return {
            "mcp_running": bool(pids),
            "can_stop": bool(pids),
            "pids": pids,
        }


def create_app(
    config: dict[str, Any] | None = None,
    provider_factory: ProviderFactory | None = None,
    xhs_client_factory: XhsClientFactory | None = None,
    process_starter: ProcessStarter | None = None,
    process_finder: XhsProcessFinder | None = None,
    process_stopper: XhsProcessStopper | None = None,
) -> FastAPI:
    cfg = config or load_config()
    factory = provider_factory or (lambda: DashScopeProvider(cfg))
    service = ConsoleService(
        cfg,
        factory,
        xhs_client_factory,
        process_starter,
        process_finder,
        process_stopper,
    )
    app = FastAPI(title="修身炉本地控制台")
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.state.console_service = service

    @app.get("/", response_class=HTMLResponse)
    def index() -> HTMLResponse:
        return HTMLResponse(CONSOLE_HTML)

    @app.get("/xhs", response_class=HTMLResponse)
    def xhs_page() -> HTMLResponse:
        return HTMLResponse(XHS_HTML)

    @app.get("/task-tree", response_class=HTMLResponse)
    def task_tree_page() -> HTMLResponse:
        return HTMLResponse(TASK_TREE_HTML)

    @app.get("/task-tree/edit")
    def task_tree_editor_page() -> FileResponse:
        return FileResponse(STATIC_DIR / "task-tree-editor.html")

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

    @app.post("/api/user-notes")
    def api_user_notes(request: UserNotesRequest) -> dict[str, Any]:
        return _handle(lambda: service.save_user_notes(request))

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

    @app.get("/api/task-tree")
    def api_task_tree(title: str | None = None) -> dict[str, Any]:
        return _handle(lambda: service.task_tree_state(title))

    @app.post("/api/task-tree")
    def api_save_task_tree(request: TaskTreeRequest) -> dict[str, Any]:
        return _handle(lambda: service.save_task_tree(request))

    @app.get("/api/xhs/defaults")
    def api_xhs_defaults() -> dict[str, Any]:
        return _handle(service.xhs_defaults)

    @app.post("/api/xhs/draft/open")
    def api_xhs_open_draft(request: XhsOpenDraftRequest) -> dict[str, Any]:
        return _handle(lambda: service.open_xhs_draft(request))

    @app.post("/api/xhs/path-status")
    def api_xhs_path_status(request: XhsPathStatusRequest) -> dict[str, Any]:
        return _handle(lambda: service.xhs_path_status(request))

    @app.post("/api/xhs/cover/generate")
    def api_xhs_generate_cover(request: XhsCoverGenerateRequest) -> dict[str, Any]:
        return _handle(lambda: service.generate_xhs_cover(request))

    @app.post("/api/xhs/images/open")
    def api_xhs_open_images(request: XhsOpenImagesRequest) -> dict[str, Any]:
        return _handle(lambda: service.open_xhs_images(request))

    @app.get("/api/xhs/cover")
    def api_xhs_cover() -> FileResponse:
        path = resolve_project_path(cfg.get("paths", {}).get("post_image_dir", "data/post/images")) / "xiushenlu-xhs-cover.png"
        if not path.exists():
            raise HTTPException(status_code=404, detail="默认封面图不存在。")
        return FileResponse(path)

    @app.get("/api/xhs/status")
    def api_xhs_status() -> dict[str, Any]:
        return _handle(service.xhs_status)

    @app.post("/api/xhs/start")
    def api_xhs_start() -> dict[str, Any]:
        return _handle(service.start_xhs_mcp)

    @app.post("/api/xhs/stop")
    def api_xhs_stop() -> dict[str, Any]:
        return _handle(service.stop_xhs_mcp)

    @app.post("/api/xhs/publish")
    def api_xhs_publish(request: XhsPublishRequest) -> dict[str, Any]:
        return _handle(lambda: service.publish_xhs(request))

    return app


def _build_xhs_client(config: dict[str, Any]) -> XhsMcpClient:
    settings = config.get("xiaohongshu", {})
    return XhsMcpClient(
        url=settings.get("mcp_url", "http://localhost:18060/mcp"),
        timeout=float(settings.get("timeout", 30)),
    )


def _xhs_status_payload(client: XhsMcpClient, config: dict[str, Any] | None = None) -> dict[str, Any]:
    if not client.can_connect():
        return {
            "connected": False,
            "logged_in": False,
            "text": "",
            "error": "未连接",
        }

    return {
        "connected": True,
        "logged_in": False,
        "text": "MCP 已连接",
        "error": None,
    }


def _describe_publish_path(path_text: str) -> dict[str, Any]:
    value = path_text.strip()
    if not value:
        return {
            "path": "",
            "kind": "empty",
            "exists": False,
            "is_file": False,
            "message": "未填写",
        }
    if value.startswith(("http://", "https://")):
        return {
            "path": value,
            "kind": "url",
            "exists": None,
            "is_file": None,
            "message": "远程 URL",
        }

    path = Path(value).expanduser()
    if not path.is_absolute():
        path = resolve_project_path(value)
    try:
        resolved = path.resolve()
        exists = resolved.exists()
        is_file = resolved.is_file() if exists else False
    except (OSError, RuntimeError, ValueError) as exc:
        return {
            "path": value,
            "kind": "local",
            "exists": False,
            "is_file": False,
            "message": "路径无法检查",
        }

    if exists and is_file:
        message = "文件存在"
    elif exists:
        message = "路径存在，但不是文件"
    else:
        message = "文件不存在"
    return {
        "path": str(resolved),
        "kind": "local",
        "exists": exists,
        "is_file": is_file,
        "message": message,
    }


def _wait_for_xhs_connection(factory: XhsClientFactory, timeout_seconds: float = 12.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    last_status: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        last_status = _xhs_status_payload(factory())
        if last_status["connected"]:
            return last_status
        time.sleep(0.8)
    error = (last_status or {}).get("error") or "xiaohongshu-mcp 启动后仍无法连接。"
    raise RuntimeError(error)


def _configured_xhs_file(config: dict[str, Any], key: str) -> Path:
    value = config.get("xiaohongshu", {}).get(key)
    if not value:
        raise RuntimeError(f"缺少配置：xiaohongshu.{key}")
    path = resolve_project_path(value).resolve()
    if not path.is_file():
        raise RuntimeError(f"配置的文件不存在：xiaohongshu.{key}={path}")
    return path


def _configured_xhs_working_dir(config: dict[str, Any], executable: Path) -> Path:
    value = config.get("xiaohongshu", {}).get("working_dir")
    path = resolve_project_path(value).resolve() if value else executable.parent
    if not path.is_dir():
        raise RuntimeError(f"配置的工作目录不存在：{path}")
    return path


def _start_configured_process(executable: Path, working_dir: Path, hidden: bool) -> None:
    creationflags = 0
    if hidden and sys.platform.startswith("win"):
        creationflags = subprocess.CREATE_NO_WINDOW
    try:
        subprocess.Popen(
            [str(executable)],
            cwd=str(working_dir),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creationflags,
        )
    except OSError as exc:
        raise RuntimeError(f"无法启动 {executable.name}：{exc}") from exc


def _find_configured_process_ids(executable: Path) -> list[int]:
    if not sys.platform.startswith("win"):
        return []

    script = (
        "$targetName = [System.IO.Path]::GetFileNameWithoutExtension($env:XHS_MCP_EXE);"
        "try {"
        "  $items = @(Get-CimInstance Win32_Process -ErrorAction Stop | "
        "    Where-Object {"
        "      $name = if ($_.ExecutablePath) { [System.IO.Path]::GetFileNameWithoutExtension($_.ExecutablePath) } else { $_.Name };"
        "      $name -like 'xiaohongshu-mcp*' -or $name -eq $targetName"
        "    } | "
        "    Select-Object -ExpandProperty ProcessId);"
        "} catch {"
        "  $items = @(Get-Process | Where-Object {"
        "    try { $_.ProcessName -like 'xiaohongshu-mcp*' -or $_.ProcessName -eq $targetName } catch { $false }"
        "  } | Select-Object -ExpandProperty Id);"
        "}"
        "$items | ConvertTo-Json -Compress"
    )
    result = subprocess.run(
        ["powershell", "-NoProfile", "-Command", script],
        env={**os.environ, "XHS_MCP_EXE": str(executable)},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        timeout=10,
        check=False,
    )
    if result.returncode != 0:
        message = result.stderr.strip() or "进程查询失败。"
        raise RuntimeError(f"无法查询 xiaohongshu-mcp 进程：{message}")

    output = result.stdout.strip()
    if not output:
        return []
    try:
        parsed = json.loads(output)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"进程查询返回格式异常：{output}") from exc
    if isinstance(parsed, int):
        return [parsed]
    if isinstance(parsed, list):
        return [int(item) for item in parsed]
    return []


def _stop_process_ids(pids: list[int]) -> None:
    clean_pids = sorted({int(pid) for pid in pids if int(pid) > 0})
    if not clean_pids:
        return
    if not sys.platform.startswith("win"):
        raise RuntimeError("当前平台暂不支持从控制台关闭 xiaohongshu-mcp。")

    ids = ",".join(str(pid) for pid in clean_pids)
    script = (
        f"$ids = @({ids});"
        "Stop-Process -Id $ids -ErrorAction SilentlyContinue;"
        "$deadline = (Get-Date).AddSeconds(5);"
        "do {"
        "  $alive = @(Get-Process -Id $ids -ErrorAction SilentlyContinue);"
        "  if ($alive.Count -eq 0) { break }"
        "  Start-Sleep -Milliseconds 200;"
        "} while ((Get-Date) -lt $deadline);"
        "$alive = @(Get-Process -Id $ids -ErrorAction SilentlyContinue);"
        "if ($alive.Count -gt 0) { Stop-Process -Id $alive.Id -Force -ErrorAction SilentlyContinue };"
        "$remaining = @(Get-Process -Id $ids -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Id);"
        "$remaining | ConvertTo-Json -Compress"
    )
    result = subprocess.run(
        ["powershell", "-NoProfile", "-Command", script],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        timeout=10,
        check=False,
    )
    if result.returncode != 0:
        message = result.stderr.strip() or "进程关闭失败。"
        raise RuntimeError(f"无法关闭 xiaohongshu-mcp 进程：{message}")
    remaining = result.stdout.strip()
    if remaining:
        raise RuntimeError(f"xiaohongshu-mcp 进程未能关闭：{remaining}")


def _task_tree_file_payload(item: Any) -> dict[str, Any]:
    return {
        "title": item.title,
        "filename": item.filename,
        "path": str(item.path),
    }


def _task_tree_document_payload(document: Any) -> dict[str, Any]:
    return {
        "title": document.title,
        "filename": document.filename,
        "path": str(document.path),
        "text": document.text,
        "tree": document.tree,
    }


def _handle(operation: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    try:
        return operation()
    except OperationCancelled as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (ValueError, PlanUpdateParseError, NightlyReviewParseError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except OSError as exc:
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


def _today_xhs_draft_path(config: dict[str, Any], day: str | None = None) -> Path:
    today_text = day or date.today().isoformat()
    return (
        resolve_project_path(config.get("paths", {}).get("post_dir", "data/post/data"))
        / f"{today_text}.txt"
    ).resolve()


def _resolve_xhs_draft_path(config: dict[str, Any], path_text: str) -> Path:
    value = path_text.strip()
    if not value:
        raise ValueError("文本路径不能为空。")
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = resolve_project_path(value)
    resolved = path.resolve()
    base_dir = resolve_project_path(config.get("paths", {}).get("post_dir", "data/post/data")).resolve()
    if not _is_relative_to(resolved, base_dir):
        raise ValueError(f"草稿必须位于 data/post/data 目录内（当前配置：{base_dir}）：{resolved}")
    return resolved


def _read_xhs_draft_text(path_text: str, config: dict[str, Any]) -> tuple[Path, str]:
    resolved = _resolve_xhs_draft_path(config, path_text)
    if not resolved.exists():
        raise ValueError(f"草稿文件不存在：{resolved}")
    if not resolved.is_file():
        raise ValueError(f"草稿路径不是文件：{resolved}")
    return resolved, safe_read_text(resolved, config)


def _resolve_image_open_targets(image_values: list[str]) -> list[str]:
    clean_values = [value.strip() for value in image_values if value and value.strip()]
    if not clean_values:
        raise ValueError("请先填写图片路径。")

    targets: list[str] = []
    for value in clean_values:
        if value.startswith(("http://", "https://")):
            targets.append(value)
            continue
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = resolve_project_path(value)
        resolved = path.resolve()
        if not resolved.exists():
            raise ValueError(f"图片文件不存在：{resolved}")
        if not resolved.is_file():
            raise ValueError(f"图片路径不是文件：{resolved}")
        targets.append(str(resolved))

    return targets


def _is_relative_to(path: Path, parent: Path) -> bool:
    return path == parent or parent in path.parents


def _open_path_with_default_app(path: Path) -> None:
    _open_value_with_default_app(str(path))


def _open_value_with_default_app(value: str) -> None:
    try:
        if sys.platform.startswith("win"):
            os.startfile(value)  # type: ignore[attr-defined]
            return
        if sys.platform == "darwin":
            subprocess.Popen(["open", value], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return
        subprocess.Popen(["xdg-open", value], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as exc:
        raise RuntimeError(f"无法打开文件：{exc}") from exc


def _open_path_with_vscode(path: Path) -> None:
    command = _find_vscode_command()
    if command is None:
        raise RuntimeError("未找到 VS Code 命令行 `code`。请先在 VS Code 中安装 Shell Command，或确认 code 已加入 PATH。")
    try:
        subprocess.Popen([command, str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as exc:
        raise RuntimeError(f"无法用 VS Code 打开文件：{exc}") from exc


def _find_vscode_command() -> str | None:
    for candidate in ("code", "code.cmd"):
        command = shutil.which(candidate)
        if command:
            return command
    return None


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
    .header-actions {{
      flex: 0 0 auto;
      justify-content: flex-end;
    }}
    .header-actions input[type="date"] {{
      width: 132px;
      min-width: 132px;
    }}
    .menu-wrap {{
      position: relative;
    }}
    .menu-button {{
      display: inline-flex;
      align-items: center;
      justify-content: center;
      flex: 0 0 auto;
      width: 38px;
      min-width: 38px;
      height: 38px;
      border: 1px solid var(--line);
      background: #fffefa;
      color: var(--text);
      padding: 0;
    }}
    .menu-button:hover {{
      background: var(--surface-2);
    }}
    .menu-icon {{
      display: grid;
      gap: 4px;
      width: 16px;
    }}
    .menu-icon span {{
      display: block;
      height: 2px;
      border-radius: 999px;
      background: currentColor;
    }}
    .menu-list {{
      position: absolute;
      right: 0;
      top: calc(100% + 4px);
      z-index: 20;
      min-width: 168px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--surface);
      box-shadow: var(--shadow);
      padding: 6px;
    }}
    .menu-list a {{
      display: block;
      border-radius: 6px;
      color: var(--text);
      padding: 8px 10px;
      text-decoration: none;
      white-space: nowrap;
    }}
    .menu-list a:hover {{
      background: var(--surface-2);
    }}
    .reserved-action.is-placeholder {{
      visibility: hidden;
      pointer-events: none;
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
      <div class="row header-actions">
        <input id="dateInput" type="date" aria-label="日期">
        <button class="secondary" id="refreshBtn">刷新</button>
        <button class="warn reserved-action is-placeholder" id="stopBtn" aria-hidden="true" disabled>停止</button>
        <div class="menu-wrap">
          <button class="menu-button" id="menuBtn" type="button" aria-label="菜单" title="菜单" aria-haspopup="menu" aria-expanded="false">
            <span class="menu-icon" aria-hidden="true">
              <span></span>
              <span></span>
              <span></span>
            </span>
          </button>
          <div class="menu-list" id="mainMenu" hidden>
            <a href="/task-tree">长期任务树</a>
            <a href="/task-tree/edit">工作树编辑器</a>
            <a href="/xhs">发布小红书</a>
          </div>
        </div>
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
          <span class="panel-title">添加任务</span>
        </summary>
        <div class="collapsible-body">
          <label for="addInput">新增任务</label>
          <textarea id="addInput" spellcheck="false"></textarea>
          <button id="addBtn">添加任务</button>
        </div>
      </details>
      <details class="panel">
        <summary class="collapsible-summary">
          <span class="panel-title">生成计划</span>
        </summary>
        <div class="collapsible-body">
          <label for="tasksInput">今日待办</label>
          <textarea id="tasksInput" spellcheck="false"></textarea>
          <div class="row">
            <button id="planBtn">生成计划</button>
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
      <details class="panel">
        <summary class="collapsible-summary">
          <span class="panel-title">杂事</span>
        </summary>
        <div class="collapsible-body">
          <label for="miscInput">用户填写</label>
          <textarea id="miscInput" spellcheck="false"></textarea>
          <button class="secondary" id="saveMiscBtn">保存杂事</button>
          <div class="path" id="miscPath"></div>
        </div>
      </details>
      <details class="panel">
        <summary class="collapsible-summary">
          <span class="panel-title">长远计划</span>
        </summary>
        <div class="collapsible-body">
          <label for="longPlanInput">用户填写</label>
          <textarea id="longPlanInput" spellcheck="false"></textarea>
          <button class="secondary" id="saveLongPlanBtn">保存长远计划</button>
          <div class="path" id="longPlanPath"></div>
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
          button.classList.toggle("is-placeholder", !operationActive);
          button.setAttribute("aria-hidden", String(!operationActive));
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
      $("miscInput").value = data.user_notes?.misc?.text || "";
      $("miscPath").textContent = data.user_notes?.misc?.path || "";
      $("longPlanInput").value = data.user_notes?.long_plan?.text || "";
      $("longPlanPath").textContent = data.user_notes?.long_plan?.path || "";
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

    $("dateInput").addEventListener("change", () => {{
      const selectedDate = $("dateInput").value;
      $("reviewDateInput").value = selectedDate;
      loadState(selectedDate);
    }});
    $("refreshBtn").addEventListener("click", () => loadState());
    $("stopBtn").addEventListener("click", () => stopCurrentOperation());
    $("menuBtn").addEventListener("click", (event) => {{
      event.stopPropagation();
      const menu = $("mainMenu");
      const hidden = menu.hidden;
      menu.hidden = !hidden;
      $("menuBtn").setAttribute("aria-expanded", String(hidden));
    }});
    document.addEventListener("click", (event) => {{
      const menu = $("mainMenu");
      const button = $("menuBtn");
      if (!menu.hidden && !menu.contains(event.target) && event.target !== button) {{
        menu.hidden = true;
        button.setAttribute("aria-expanded", "false");
      }}
    }});
    $("sloganInput").addEventListener("input", saveSlogan);
    $("openTasksBtn").addEventListener("click", () => runAction("打开文件", () =>
      requestJson("/api/tasks/open", {{
        method: "POST",
      }})
    ));
    function saveUserNotes(label) {{
      return runAction(label, () =>
        requestJson("/api/user-notes", {{
          method: "POST",
          body: JSON.stringify({{
            misc: $("miscInput").value,
            long_plan: $("longPlanInput").value,
          }}),
        }})
      );
    }}
    $("saveMiscBtn").addEventListener("click", () => saveUserNotes("保存杂事"));
    $("saveLongPlanBtn").addEventListener("click", () => saveUserNotes("保存长远计划"));
    $("planBtn").addEventListener("click", () => runLongAction("生成计划", (signal) =>
      requestJson("/api/plan", {{
        method: "POST",
        signal,
        body: JSON.stringify({{ tasks: $("tasksInput").value }}),
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
    submitOnCtrlEnter("miscInput", "saveMiscBtn");
    submitOnCtrlEnter("longPlanInput", "saveLongPlanBtn");
    loadSlogan();
    loadState();
    loadOperation().catch(() => null);
  </script>
</body>
</html>
"""


TASK_TREE_SAMPLE_JSON = """{
  "version": 1,
  "title": "示例长期任务",
  "summary": "把一个长期目标拆成可继续推进的层级。",
  "nodes": [
    {
      "id": "phase-1",
      "title": "第一阶段：明确方向",
      "content": "先把目标边界、资料和验收标准定清楚。",
      "children": [
        {
          "id": "daily-review",
          "title": "每天记录 10 分钟推进情况",
          "content": "记录今天推进了什么、卡在哪里、下一步是什么。"
        }
      ]
    }
  ]
}"""


TASK_TREE_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>长期任务树 - 修身炉</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #f7f4ed;
      --surface: #fffdf8;
      --surface-2: #f1eee7;
      --text: #24211d;
      --muted: #6f6a60;
      --line: #d8d1c5;
      --accent: #2f6f5e;
      --accent-dark: #245548;
      --danger: #a33b32;
      --ok: #2f6f5e;
      --habit: #7b4d97;
      --phase: #8a5a22;
      --shadow: 0 16px 36px rgba(37, 32, 25, 0.08);
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font-family: "Segoe UI", "Microsoft YaHei", Arial, sans-serif;
      font-size: 15px;
      line-height: 1.5;
    }
    header {
      position: sticky;
      top: 0;
      z-index: 5;
      border-bottom: 1px solid var(--line);
      background: rgba(247, 244, 237, 0.94);
      backdrop-filter: blur(10px);
    }
    .bar {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      max-width: 1360px;
      margin: 0 auto;
      padding: 14px 20px;
    }
    h1 {
      margin: 0;
      font-size: 22px;
      font-weight: 720;
      letter-spacing: 0;
    }
    h2 {
      margin: 0 0 12px;
      font-size: 16px;
      font-weight: 700;
      letter-spacing: 0;
    }
    main {
      display: grid;
      grid-template-columns: minmax(340px, 480px) minmax(420px, 1fr);
      gap: 16px;
      max-width: 1360px;
      margin: 0 auto;
      padding: 16px 20px 28px;
    }
    section {
      background: var(--surface);
      border: 1px solid var(--line);
      border-radius: 8px;
      box-shadow: var(--shadow);
      padding: 14px;
    }
    label {
      display: block;
      margin: 0 0 6px;
      color: var(--muted);
      font-size: 13px;
    }
    input, textarea, select {
      width: 100%;
      border: 1px solid var(--line);
      border-radius: 7px;
      background: #fffefa;
      color: var(--text);
      font: inherit;
      padding: 9px 10px;
      outline: none;
    }
    textarea {
      min-height: 480px;
      resize: vertical;
      font-family: Consolas, "Cascadia Mono", "Microsoft YaHei UI", monospace;
      font-size: 13px;
      line-height: 1.55;
    }
    input:focus, textarea:focus, select:focus {
      border-color: var(--accent);
      box-shadow: 0 0 0 3px rgba(47, 111, 94, 0.14);
    }
    button {
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
    }
    button:hover { background: var(--accent-dark); }
    button.secondary {
      background: var(--surface-2);
      color: var(--text);
      border: 1px solid var(--line);
    }
    button.secondary:hover { background: #e7e1d7; }
    button:disabled {
      cursor: wait;
      opacity: 0.65;
    }
    .row {
      display: flex;
      align-items: center;
      gap: 8px;
      flex-wrap: wrap;
    }
    .stack {
      display: grid;
      gap: 14px;
      align-content: start;
    }
    .split {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 8px;
    }
    .meta {
      color: var(--muted);
      font-size: 13px;
      overflow-wrap: anywhere;
    }
    .status {
      min-height: 24px;
      color: var(--muted);
      font-size: 13px;
      white-space: pre-wrap;
    }
    .status.ok { color: var(--ok); }
    .status.error { color: var(--danger); }
    .tree-root {
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fffcf4;
      min-height: 560px;
      max-height: calc(100vh - 162px);
      overflow: auto;
      padding: 14px;
    }
    .root-title {
      margin: 0 0 6px;
      font-size: 20px;
      font-weight: 720;
    }
    .root-summary {
      margin: 0 0 14px;
      color: var(--muted);
    }
    .tree-list {
      list-style: none;
      margin: 0;
      padding-left: 0;
    }
    .tree-list .tree-list {
      border-left: 1px solid var(--line);
      margin-left: 13px;
      padding-left: 16px;
    }
    .tree-node {
      margin: 7px 0;
    }
    .tree-node.collapsed > .tree-list {
      display: none;
    }
    .node-row {
      display: grid;
      grid-template-columns: 18px minmax(0, 1fr);
      gap: 7px;
      align-items: start;
      border: 1px solid transparent;
      border-radius: 7px;
      padding: 7px 8px;
      cursor: default;
    }
    .tree-node.has-children > .node-row {
      cursor: pointer;
    }
    .node-row:hover {
      border-color: var(--line);
      background: #fffefa;
    }
    .node-row:focus-visible {
      outline: 3px solid rgba(47, 111, 94, 0.18);
      outline-offset: 2px;
    }
    .twisty {
      width: 8px;
      height: 8px;
      border-right: 2px solid var(--muted);
      border-bottom: 2px solid var(--muted);
      transform: rotate(45deg);
      margin-top: 7px;
    }
    .tree-node.collapsed > .node-row .twisty {
      transform: rotate(-45deg);
    }
    .tree-node:not(.has-children) .twisty {
      border-color: transparent;
    }
    .node-title {
      font-weight: 680;
      overflow-wrap: anywhere;
    }
    .node-content {
      margin-top: 3px;
      color: var(--muted);
      font-size: 13px;
      overflow-wrap: anywhere;
    }
    .empty-tree {
      color: var(--muted);
    }
    @media (max-width: 980px) {
      main { grid-template-columns: 1fr; }
      .tree-root { max-height: none; }
    }
    @media (max-width: 640px) {
      .bar { align-items: flex-start; flex-direction: column; }
      main { padding: 12px; }
      .split { grid-template-columns: 1fr; }
      button { width: 100%; }
    }
  </style>
</head>
<body>
  <header>
    <div class="bar">
      <div>
        <h1>长期任务树</h1>
        <div class="meta">Codex 拆分 JSON -> 本地树状图</div>
      </div>
      <div class="row">
        <a href="/"><button class="secondary" type="button">返回控制台</button></a>
        <a href="/task-tree/edit"><button class="secondary" type="button">工作树编辑器</button></a>
      </div>
    </div>
  </header>
  <main>
    <section class="stack">
      <h2>输入</h2>
      <div class="split">
        <div>
          <label for="treeTitleInput">保存标题</label>
          <input id="treeTitleInput" type="text" spellcheck="false" placeholder="标题会作为 JSON 文件名">
        </div>
        <div>
          <label for="treeSelect">已保存</label>
          <select id="treeSelect"></select>
        </div>
      </div>
      <div class="row">
        <button id="renderBtn" type="button">渲染</button>
        <button id="saveBtn" type="button">保存</button>
        <button class="secondary" id="reloadBtn" type="button">重新读取</button>
        <button class="secondary" id="expandBtn" type="button">全部展开</button>
        <button class="secondary" id="collapseBtn" type="button">全部收起</button>
      </div>
      <div class="status" id="statusText">准备中</div>
      <div class="meta" id="pathText"></div>
      <div>
        <label for="treeJsonInput">任务树 JSON</label>
        <textarea id="treeJsonInput" spellcheck="false"></textarea>
      </div>
    </section>
    <section>
      <h2>树状图</h2>
      <div class="tree-root" id="treeView">
        <div class="empty-tree">等待渲染。</div>
      </div>
    </section>
  </main>
  <script>
    const $ = (id) => document.getElementById(id);
    const state = {
      busy: false,
      sample: "",
    };

    function setBusy(value) {
      state.busy = value;
      for (const button of document.querySelectorAll("button")) {
        button.disabled = value;
      }
      $("treeSelect").disabled = value || $("treeSelect").options.length === 0;
    }

    function setStatus(text, kind = "") {
      const el = $("statusText");
      el.textContent = text || "";
      el.className = kind ? `status ${kind}` : "status";
    }

    async function requestJson(url, options = {}) {
      const response = await fetch(url, {
        headers: { "Content-Type": "application/json" },
        ...options,
      });
      const body = await response.json().catch(() => ({ detail: response.statusText }));
      if (!response.ok) {
        throw new Error(body.detail || response.statusText);
      }
      return body;
    }

    async function loadState(title = "") {
      setBusy(true);
      try {
        const query = title ? `?title=${encodeURIComponent(title)}` : "";
        const data = await requestJson(`/api/task-tree${query}`);
        renderState(data);
        setStatus(data.selected ? `已读取 ${data.selected.filename}` : "还没有保存的任务树。", "ok");
      } catch (error) {
        setStatus(error.message, "error");
      } finally {
        setBusy(false);
      }
    }

    function renderState(data) {
      state.sample = data.sample || "";
      const items = data.items || [];
      $("treeSelect").innerHTML = items.length
        ? items.map((item) => `<option value="${escapeHtml(item.title)}">${escapeHtml(item.filename)}</option>`).join("")
        : '<option value="">暂无</option>';
      $("treeSelect").disabled = items.length === 0;

      if (data.selected) {
        $("treeTitleInput").value = data.selected.title;
        $("treeJsonInput").value = data.selected.text;
        $("pathText").textContent = data.selected.path;
        $("treeSelect").value = data.selected.title;
        renderTree(data.selected.tree);
        return;
      }

      $("treeTitleInput").value = "";
      $("treeJsonInput").value = state.sample;
      $("pathText").textContent = data.directory || "";
      renderEmptyTree("粘贴 Codex 输出的 JSON 后点击渲染。");
    }

    function renderFromEditor() {
      try {
        const tree = parseTreeText($("treeJsonInput").value);
        if (!$("treeTitleInput").value.trim() && tree.title) {
          $("treeTitleInput").value = tree.title;
        }
        renderTree(tree);
        setStatus("已渲染，尚未保存。", "ok");
      } catch (error) {
        setStatus(error.message, "error");
      }
    }

    async function saveTree() {
      setBusy(true);
      setStatus("保存中...");
      try {
        const data = await requestJson("/api/task-tree", {
          method: "POST",
          body: JSON.stringify({
            title: $("treeTitleInput").value,
            text: $("treeJsonInput").value,
          }),
        });
        renderState(data.state);
        setStatus(data.message || "任务树已保存。", "ok");
      } catch (error) {
        setStatus(error.message, "error");
      } finally {
        setBusy(false);
      }
    }

    function parseTreeText(text) {
      const stripped = String(text || "").trim();
      const match = stripped.match(/^```(?:json)?\\s*([\\s\\S]*?)\\s*```$/i);
      const source = match ? match[1].trim() : stripped;
      if (!source) {
        throw new Error("任务树 JSON 不能为空。");
      }
      return JSON.parse(source);
    }

    function renderTree(tree) {
      const nodes = Array.isArray(tree.nodes) ? tree.nodes : [];
      const summary = tree.summary ? `<p class="root-summary">${escapeHtml(tree.summary)}</p>` : "";
      $("treeView").innerHTML = `
        <h3 class="root-title">${escapeHtml(tree.title || "未命名任务树")}</h3>
        ${summary}
        ${nodes.length ? `<ul class="tree-list">${nodes.map(renderNode).join("")}</ul>` : '<div class="empty-tree">没有节点。</div>'}
      `;
    }

    function renderNode(node) {
      const children = Array.isArray(node.children) ? node.children : [];
      const hasChildren = children.length > 0;
      return `
        <li class="tree-node ${hasChildren ? "has-children" : ""}">
          <div class="node-row" role="${hasChildren ? "button" : "group"}" tabindex="0" aria-expanded="${hasChildren ? "true" : "false"}">
            <span class="twisty" aria-hidden="true"></span>
            <div>
              <div class="node-title">${escapeHtml(node.title || "未命名节点")}</div>
              ${node.content ? `<div class="node-content">${escapeHtml(node.content)}</div>` : ""}
            </div>
          </div>
          ${hasChildren ? `<ul class="tree-list">${children.map(renderNode).join("")}</ul>` : ""}
        </li>
      `;
    }

    function renderEmptyTree(text) {
      $("treeView").innerHTML = `<div class="empty-tree">${escapeHtml(text)}</div>`;
    }

    function setAllCollapsed(collapsed) {
      for (const node of document.querySelectorAll(".tree-node.has-children")) {
        node.classList.toggle("collapsed", collapsed);
        const row = node.querySelector(":scope > .node-row");
        if (row) {
          row.setAttribute("aria-expanded", String(!collapsed));
        }
      }
    }

    function toggleNode(row) {
      const node = row.closest(".tree-node");
      if (!node || !node.classList.contains("has-children")) {
        return;
      }
      const collapsed = node.classList.toggle("collapsed");
      row.setAttribute("aria-expanded", String(!collapsed));
    }

    function escapeHtml(value) {
      return String(value)
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;")
        .replaceAll("'", "&#039;");
    }

    $("treeView").addEventListener("click", (event) => {
      const row = event.target.closest(".node-row");
      if (row) {
        toggleNode(row);
      }
    });
    $("treeView").addEventListener("keydown", (event) => {
      if (event.key !== "Enter" && event.key !== " ") {
        return;
      }
      const row = event.target.closest(".node-row");
      if (row) {
        event.preventDefault();
        toggleNode(row);
      }
    });
    $("treeSelect").addEventListener("change", () => loadState($("treeSelect").value));
    $("renderBtn").addEventListener("click", renderFromEditor);
    $("saveBtn").addEventListener("click", saveTree);
    $("reloadBtn").addEventListener("click", () => loadState($("treeTitleInput").value));
    $("expandBtn").addEventListener("click", () => setAllCollapsed(false));
    $("collapseBtn").addEventListener("click", () => setAllCollapsed(true));
    loadState();
  </script>
</body>
</html>
"""


XHS_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>发布小红书 - 修身炉</title>
  <style>
    :root {
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
      --ok: #2f6f5e;
      --shadow: 0 16px 36px rgba(37, 32, 25, 0.08);
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font-family: "Segoe UI", "Microsoft YaHei", Arial, sans-serif;
      font-size: 15px;
      line-height: 1.5;
    }
    header {
      position: sticky;
      top: 0;
      z-index: 5;
      border-bottom: 1px solid var(--line);
      background: rgba(247, 244, 237, 0.94);
      backdrop-filter: blur(10px);
    }
    .bar {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      max-width: 1180px;
      margin: 0 auto;
      padding: 14px 20px;
    }
    h1 {
      margin: 0;
      font-size: 22px;
      font-weight: 720;
      letter-spacing: 0;
    }
    h2 {
      margin: 0 0 12px;
      font-size: 16px;
      font-weight: 700;
      letter-spacing: 0;
    }
    main {
      display: grid;
      grid-template-columns: minmax(320px, 380px) minmax(420px, 1fr);
      gap: 16px;
      max-width: 1180px;
      margin: 0 auto;
      padding: 16px 20px 28px;
    }
    section {
      background: var(--surface);
      border: 1px solid var(--line);
      border-radius: 8px;
      box-shadow: var(--shadow);
      padding: 14px;
    }
    label {
      display: block;
      margin: 0 0 6px;
      color: var(--muted);
      font-size: 13px;
    }
    input, textarea, select {
      width: 100%;
      border: 1px solid var(--line);
      border-radius: 7px;
      background: #fffefa;
      color: var(--text);
      font: inherit;
      padding: 9px 10px;
      outline: none;
    }
    input:focus, textarea:focus, select:focus {
      border-color: var(--accent);
      box-shadow: 0 0 0 3px rgba(47, 111, 94, 0.14);
    }
    textarea {
      min-height: 108px;
      resize: vertical;
    }
    button {
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
    }
    button:hover { background: var(--accent-dark); }
    button.secondary {
      background: var(--surface-2);
      color: var(--text);
      border: 1px solid var(--line);
    }
    button.secondary:hover { background: #e7e1d7; }
    button.warn { background: var(--warn); }
    button:disabled {
      cursor: wait;
      opacity: 0.65;
    }
    .row {
      display: flex;
      align-items: center;
      gap: 8px;
      flex-wrap: wrap;
    }
    .stack {
      display: grid;
      gap: 14px;
      align-content: start;
    }
    .split {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 10px;
    }
    .status {
      min-height: 24px;
      color: var(--muted);
      font-size: 13px;
      white-space: pre-wrap;
      overflow-wrap: anywhere;
    }
    .status.ok { color: var(--ok); }
    .status.error { color: var(--danger); }
    .meta {
      color: var(--muted);
      font-size: 13px;
      overflow-wrap: anywhere;
    }
    .field {
      display: grid;
      gap: 6px;
    }
    .field-heading {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
      flex-wrap: wrap;
    }
    .field-heading label {
      margin: 0;
    }
    .field-heading button {
      min-height: 32px;
      padding: 5px 10px;
    }
    .inline-control {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      color: var(--text);
    }
    .inline-control input {
      width: auto;
      margin: 0;
    }
    .meta.ok { color: var(--ok); }
    .meta.error { color: var(--danger); }
    @media (max-width: 900px) {
      main { grid-template-columns: 1fr; }
    }
    @media (max-width: 640px) {
      .bar { align-items: flex-start; flex-direction: column; }
      main { padding: 12px; }
      .split { grid-template-columns: 1fr; }
      button { width: 100%; }
    }
  </style>
</head>
<body>
  <header>
    <div class="bar">
      <div>
        <h1>发布小红书</h1>
        <div class="meta">修身炉内容出口</div>
      </div>
      <div class="row">
        <a href="/"><button class="secondary" type="button">返回控制台</button></a>
      </div>
    </div>
  </header>
  <main>
    <div class="stack">
      <section>
        <h2>MCP</h2>
        <div class="status" id="xhsStatus">准备中</div>
        <div class="row">
          <button id="startMcpBtn" type="button" disabled>打开 MCP</button>
        </div>
      </section>
    </div>
    <section>
      <h2>发布内容</h2>
      <div class="stack">
        <div class="field">
          <div class="field-heading">
            <label for="draftInput">文本路径</label>
            <button class="secondary" id="openDraftBtn" type="button">打开草稿</button>
          </div>
          <input id="draftInput" type="text" spellcheck="false" required>
          <div class="meta" id="draftState"></div>
          <div class="status" id="draftOpenStatus"></div>
        </div>
        <div class="field">
          <div class="field-heading">
            <label for="imageInput">图片路径（每行一张）</label>
            <div class="row">
              <button class="secondary" id="generateCoverBtn" type="button">生成封面</button>
              <button class="secondary" id="openImagesBtn" type="button">打开图片</button>
            </div>
          </div>
          <textarea id="imageInput" spellcheck="false" required></textarea>
          <div class="meta" id="imageState"></div>
          <div class="status" id="coverStatus"></div>
        </div>
        <div class="split">
          <div class="field">
            <label for="titleInput">标题</label>
            <input id="titleInput" type="text" spellcheck="false" required>
          </div>
          <div class="field">
            <label for="visibilityInput">可见范围</label>
            <select id="visibilityInput"></select>
          </div>
        </div>
        <div class="split">
          <div class="field">
            <label for="tagsInput">标签（可选）</label>
            <input id="tagsInput" type="text" spellcheck="false">
          </div>
          <div class="field">
            <label for="scheduleInput">定时发布（可选）</label>
            <input id="scheduleInput" type="text" spellcheck="false">
          </div>
        </div>
        <label class="inline-control">
          <input id="originalInput" type="checkbox">
          原创标记（可选）
        </label>
        <div class="row">
          <button id="publishBtn" type="button" disabled>发布</button>
        </div>
        <div class="status" id="publishStatus"></div>
      </div>
    </section>
  </main>
  <script>
    const $ = (id) => document.getElementById(id);
    const state = {
      busy: false,
      statusLoading: true,
      mcpConnected: false,
      canStopMcp: false,
      pathTimer: null,
    };

    function setBusy(value) {
      state.busy = value;
      updateControls();
    }

    function setStatusLoading(value) {
      state.statusLoading = value;
      updateControls();
    }

    function updateControls() {
      const mcpButton = $("startMcpBtn");
      mcpButton.disabled = state.busy || state.statusLoading;
      mcpButton.textContent = state.canStopMcp ? "关闭 MCP" : "打开 MCP";
      $("openDraftBtn").disabled = state.busy;
      $("generateCoverBtn").disabled = state.busy;
      $("openImagesBtn").disabled = state.busy;
      $("publishBtn").disabled = state.busy || !state.mcpConnected;
    }

    function setStatus(id, text, kind = "") {
      const el = $(id);
      el.textContent = text || "";
      el.className = kind ? `status ${kind}` : "status";
    }

    async function requestJson(url, options = {}) {
      const response = await fetch(url, {
        headers: { "Content-Type": "application/json" },
        ...options,
      });
      const body = await response.json().catch(() => ({ detail: response.statusText }));
      if (!response.ok) {
        throw new Error(body.detail || response.statusText);
      }
      return body;
    }

    async function loadDefaults() {
      const data = await requestJson("/api/xhs/defaults");
      $("draftInput").value = data.draft_path;
      $("imageInput").value = data.image_path;
      $("titleInput").value = data.title;
      $("tagsInput").value = data.tags.join(" ");
      $("visibilityInput").innerHTML = data.visibility_options
        .map((item) => `<option value="${escapeHtml(item)}">${escapeHtml(item)}</option>`)
        .join("");
      $("visibilityInput").value = data.visibility;
      await updatePathStatus();
    }

    function schedulePathStatus() {
      if (state.pathTimer) {
        window.clearTimeout(state.pathTimer);
      }
      state.pathTimer = window.setTimeout(updatePathStatus, 250);
    }

    async function updatePathStatus() {
      try {
        const data = await requestJson("/api/xhs/path-status", {
          method: "POST",
          body: JSON.stringify({
            draft: $("draftInput").value,
            images: splitLines($("imageInput").value),
          }),
        });
        renderPathStatus(data);
      } catch (error) {
        $("draftState").textContent = error.message;
        $("draftState").className = "meta error";
      }
    }

    function renderPathStatus(data) {
      renderSinglePathStatus("draftState", data.draft);
      const images = data.images || [];
      const imageState = $("imageState");
      if (!images.length) {
        imageState.textContent = "未填写";
        imageState.className = "meta error";
        return;
      }
      imageState.textContent = images
        .map((item, index) => `${index + 1}. ${item.message}`)
        .join("\\n");
      imageState.className = images.every(isUsablePathStatus) ? "meta ok" : "meta error";
    }

    function renderSinglePathStatus(id, item) {
      const el = $(id);
      el.textContent = item.message;
      el.className = isUsablePathStatus(item) ? "meta ok" : "meta error";
    }

    function isUsablePathStatus(item) {
      return item.kind === "url" || (item.exists && item.is_file);
    }

    async function checkStatus() {
      setStatusLoading(true);
      try {
        const data = await requestJson("/api/xhs/status");
        renderMcpStatus(data);
        return data;
      } finally {
        setStatusLoading(false);
      }
    }

    function renderMcpStatus(data) {
      state.mcpConnected = Boolean(data.connected);
      state.canStopMcp = Boolean(data.can_stop);
      if (!data.connected) {
        setStatus("xhsStatus", "未连接");
      } else if (data.error) {
        setStatus("xhsStatus", data.error, "error");
      } else {
        setStatus("xhsStatus", data.text || "MCP 已连接", "ok");
      }
      updateControls();
    }

    async function toggleMcp() {
      if (state.canStopMcp) {
        await stopMcp();
      } else {
        await startMcp();
      }
    }

    async function startMcp() {
      setBusy(true);
      setStatus("xhsStatus", "打开 MCP 中...");
      try {
        const data = await requestJson("/api/xhs/start", { method: "POST" });
        renderMcpStatus(data.result);
      } catch (error) {
        setStatus("xhsStatus", error.message, "error");
      } finally {
        setBusy(false);
      }
    }

    async function stopMcp() {
      setBusy(true);
      setStatus("xhsStatus", "关闭 MCP 中...");
      try {
        const data = await requestJson("/api/xhs/stop", { method: "POST" });
        renderMcpStatus(data.result);
      } catch (error) {
        setStatus("xhsStatus", error.message, "error");
      } finally {
        setBusy(false);
      }
    }

    async function openDraft() {
      setBusy(true);
      setStatus("draftOpenStatus", "打开草稿中...");
      try {
        const data = await requestJson("/api/xhs/draft/open", {
          method: "POST",
          body: JSON.stringify({ draft: $("draftInput").value }),
        });
        if (data.result && data.result.draft_path) {
          $("draftInput").value = data.result.draft_path;
        }
        await updatePathStatus();
        setStatus("draftOpenStatus", data.message || "已打开草稿", "ok");
      } catch (error) {
        setStatus("draftOpenStatus", error.message, "error");
      } finally {
        setBusy(false);
      }
    }

    async function generateCover() {
      const draft = $("draftInput").value.trim();
      if (!draft) {
        setStatus("coverStatus", "请先填写文本路径。", "error");
        return;
      }
      setBusy(true);
      setStatus("coverStatus", "生成封面中...");
      try {
        const data = await requestJson("/api/xhs/cover/generate", {
          method: "POST",
          body: JSON.stringify({ draft }),
        });
        if (data.result && data.result.image_path) {
          $("imageInput").value = data.result.image_path;
        }
        await updatePathStatus();
        setStatus("coverStatus", data.message || "封面已生成", "ok");
      } catch (error) {
        setStatus("coverStatus", error.message, "error");
      } finally {
        setBusy(false);
      }
    }

    async function openImages() {
      const images = splitLines($("imageInput").value);
      if (!images.length) {
        setStatus("coverStatus", "请先填写图片路径。", "error");
        return;
      }
      setBusy(true);
      setStatus("coverStatus", "打开图片中...");
      try {
        const data = await requestJson("/api/xhs/images/open", {
          method: "POST",
          body: JSON.stringify({ images }),
        });
        setStatus("coverStatus", data.message || "已打开图片", "ok");
      } catch (error) {
        setStatus("coverStatus", error.message, "error");
      } finally {
        setBusy(false);
      }
    }

    function collectPublishRequest() {
      return {
        draft: $("draftInput").value,
        title: $("titleInput").value,
        images: splitLines($("imageInput").value),
        tags: splitTags($("tagsInput").value),
        visibility: $("visibilityInput").value,
        schedule_at: $("scheduleInput").value,
        is_original: $("originalInput").checked,
        products: [],
      };
    }

    async function publishXhs() {
      const request = collectPublishRequest();
      const missing = missingRequiredFields(request);
      if (missing.length) {
        setStatus("publishStatus", `请补全：${missing.join("、")}`, "error");
        return;
      }
      if (!window.confirm(`确认发布小红书？可见范围：${request.visibility}`)) {
        return;
      }
      setBusy(true);
      setStatus("publishStatus", "发布中...");
      try {
        const data = await requestJson("/api/xhs/publish", {
          method: "POST",
          body: JSON.stringify(request),
        });
        setStatus("publishStatus", data.message || "发布完成", "ok");
      } catch (error) {
        setStatus("publishStatus", error.message, "error");
      } finally {
        setBusy(false);
      }
    }

    function splitLines(text) {
      return String(text || "")
        .split(/\\r?\\n/)
        .map((item) => item.trim())
        .filter(Boolean);
    }

    function splitTags(text) {
      return String(text || "")
        .split(/[\\s,，]+/)
        .map((item) => item.trim())
        .filter(Boolean);
    }

    function missingRequiredFields(request) {
      const missing = [];
      if (!request.draft.trim()) {
        missing.push("文本路径");
      }
      if (!request.images.length) {
        missing.push("图片路径");
      }
      if (!request.title.trim()) {
        missing.push("标题");
      }
      return missing;
    }

    function escapeHtml(value) {
      return String(value)
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;")
        .replaceAll("'", "&#039;");
    }

    $("startMcpBtn").addEventListener("click", toggleMcp);
    $("openDraftBtn").addEventListener("click", openDraft);
    $("generateCoverBtn").addEventListener("click", generateCover);
    $("openImagesBtn").addEventListener("click", openImages);
    $("publishBtn").addEventListener("click", publishXhs);
    $("draftInput").addEventListener("input", schedulePathStatus);
    $("imageInput").addEventListener("input", schedulePathStatus);
    loadDefaults()
      .then(checkStatus)
      .catch((error) => {
        setStatus("xhsStatus", error.message, "error");
        setStatusLoading(false);
      });
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
