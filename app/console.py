from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any, Callable

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app.config import load_config, resolve_project_path
from app.daily import append_record, daily_path, read_daily
from app.inbox import read_today_tasks, today_tasks_path, write_today_tasks
from app.llm.dashscope_impl import DashScopeProvider
from app.llm.provider import LLMProvider
from app.logger import EventLogger
from app.pipelines.daily_plan import generate_daily_plan
from app.pipelines.nightly_review import NightlyReviewParseError, generate_nightly_review
from app.pipelines.plan_update import PlanUpdateParseError, generate_plan_update


ProviderFactory = Callable[[], LLMProvider]
STATIC_DIR = Path(__file__).resolve().parent / "static"


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

    class Config:
        extra = "forbid"


class ConsoleService:
    def __init__(self, config: dict[str, Any], provider_factory: ProviderFactory) -> None:
        self.config = config
        self.provider_factory = provider_factory

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

    def generate_plan(self, request: PlanRequest) -> dict[str, Any]:
        add = request.add.strip() if request.add is not None else None
        if request.add is not None and not add:
            raise ValueError("新增任务不能为空。")

        provider = self.provider_factory()
        event_logger = EventLogger(config=self.config)
        if add is not None:
            result = generate_plan_update(provider, add, config=self.config, logger=event_logger)
            return {
                "message": "计划已局部更新。",
                "result": {
                    "date": result.date,
                    "daily_path": str(result.daily_path),
                    "today_tasks_path": str(result.today_tasks_path),
                    "target_heading": result.target_heading,
                    "new_task_advice": result.new_task_advice,
                },
                "state": self.snapshot(result.date),
            }

        result = generate_daily_plan(provider, config=self.config, logger=event_logger)
        return {
            "message": "计划已生成。",
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
        path = append_record(content, self.config)
        EventLogger(config=self.config).append_event(
            "user_log",
            "添加今日记录",
            {
                "date": path.stem,
                "daily_path": str(path),
                "content": content,
            },
        )
        return {
            "message": "记录已写入。",
            "result": {"daily_path": str(path)},
            "state": self.snapshot(path.stem),
        }

    def generate_review(self, request: ReviewRequest) -> dict[str, Any]:
        target_date = _parse_date(request.date) if request.date else None
        provider = self.provider_factory()
        result = generate_nightly_review(
            provider,
            config=self.config,
            target_date=target_date,
            logger=EventLogger(config=self.config),
        )
        return {
            "message": "复盘已生成。",
            "result": {
                "date": result.date,
                "daily_path": str(result.path),
                "review": result.review,
            },
            "state": self.snapshot(result.date),
        }


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

    @app.post("/api/tasks")
    def api_tasks(request: TasksRequest) -> dict[str, Any]:
        return _handle(lambda: service.save_tasks(request))

    @app.post("/api/plan")
    def api_plan(request: PlanRequest) -> dict[str, Any]:
        return _handle(lambda: service.generate_plan(request))

    @app.post("/api/log")
    def api_log(request: LogRequest) -> dict[str, Any]:
        return _handle(lambda: service.add_log(request))

    @app.post("/api/review")
    def api_review(request: ReviewRequest) -> dict[str, Any]:
        return _handle(lambda: service.generate_review(request))

    return app


def _handle(operation: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    try:
        return operation()
    except (ValueError, PlanUpdateParseError, NightlyReviewParseError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


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
    section {{
      background: var(--surface);
      border: 1px solid var(--line);
      border-radius: 8px;
      box-shadow: var(--shadow);
      padding: 14px;
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
      <div class="top-status">
        <h2>运行状态</h2>
        <div class="status" id="statusText">准备中</div>
      </div>
      <div class="row">
        <input id="dateInput" type="date" aria-label="日期">
        <button class="secondary" id="refreshBtn">刷新</button>
      </div>
    </div>
  </header>
  <main>
    <div class="stack">
      <section>
        <h2>过程记录</h2>
        <label for="logInput">记录内容</label>
        <textarea id="logInput" spellcheck="false"></textarea>
        <button id="logBtn">写入记录</button>
      </section>
      <section>
        <h2>今日待办</h2>
        <label for="tasksInput">today_tasks.md</label>
        <textarea id="tasksInput" spellcheck="false"></textarea>
        <div class="row">
          <button class="secondary" id="saveTasksBtn">保存待办</button>
          <button id="planBtn">生成计划</button>
          <button class="secondary" id="reloadTasksBtn">读取待办</button>
        </div>
        <div class="path" id="tasksPath"></div>
      </section>
      <section>
        <h2>日内更新</h2>
        <label for="addInput">新增任务</label>
        <textarea id="addInput" spellcheck="false"></textarea>
        <button id="addBtn">局部更新计划</button>
      </section>
      <section>
        <h2>复盘</h2>
        <div class="split">
          <div>
            <label for="reviewDateInput">复盘日期</label>
            <input id="reviewDateInput" type="date">
          </div>
          <div>
            <label>&nbsp;</label>
            <button id="reviewBtn">生成复盘</button>
          </div>
        </div>
      </section>
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
    const state = {{ current: null, busy: false }};
    const $ = (id) => document.getElementById(id);

    if (window.marked) {{
      marked.setOptions({{
        gfm: true,
        breaks: false,
      }});
    }}

    function setBusy(value) {{
      state.busy = value;
      for (const button of document.querySelectorAll("button")) {{
        button.disabled = value;
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
        throw new Error(body.detail || response.statusText);
      }}
      return body;
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

    function submitOnCtrlEnter(inputId, buttonId) {{
      $(inputId).addEventListener("keydown", (event) => {{
        if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) {{
          event.preventDefault();
          if (!state.busy) {{
            $(buttonId).click();
          }}
        }}
      }});
    }}

    $("refreshBtn").addEventListener("click", () => loadState());
    $("reloadTasksBtn").addEventListener("click", () => loadState());
    $("saveTasksBtn").addEventListener("click", () => runAction("保存待办", () =>
      requestJson("/api/tasks", {{
        method: "POST",
        body: JSON.stringify({{ tasks: $("tasksInput").value }}),
      }})
    ));
    $("planBtn").addEventListener("click", () => runAction("生成计划", () =>
      requestJson("/api/plan", {{
        method: "POST",
        body: JSON.stringify({{}}),
      }})
    ));
    $("addBtn").addEventListener("click", () => runAction("局部更新", async () => {{
      const data = await requestJson("/api/plan", {{
        method: "POST",
        body: JSON.stringify({{ add: $("addInput").value }}),
      }});
      $("addInput").value = "";
      return data;
    }}));
    $("logBtn").addEventListener("click", () => runAction("写入记录", async () => {{
      const data = await requestJson("/api/log", {{
        method: "POST",
        body: JSON.stringify({{ content: $("logInput").value }}),
      }});
      $("logInput").value = "";
      return data;
    }}));
    $("reviewBtn").addEventListener("click", () => runAction("生成复盘", () =>
      requestJson("/api/review", {{
        method: "POST",
        body: JSON.stringify({{ date: $("reviewDateInput").value }}),
      }})
    ));
    submitOnCtrlEnter("logInput", "logBtn");
    submitOnCtrlEnter("addInput", "addBtn");
    loadState();
  </script>
</body>
</html>
"""


app = create_app()
