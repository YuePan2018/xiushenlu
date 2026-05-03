# AGENTS

本文件给后续参与本项目的 agent 或开发者使用。目标是让自动化协作者先理解项目边界，再动代码。

## 项目定位

修身炉是一个本地优先的个人执行管理助手。当前阶段不是完整自主 agent，而是一个可追踪的 CLI + 本地控制台执行闭环：

```text
长期目标 + 今日待办 -> 今日计划 -> 过程记录 -> 晚间复盘 -> token 统计
```

Phase 1 已完成，下一阶段主要方向是自动化与通知、本地控制台、安全审批。

## 当前命令

在项目根目录运行：

```powershell
conda activate xiushenlu
python app/main.py --help
```

日常调试控制台可直接双击 `run_main.bat`。无参数时它会启动 `python app/main.py console --host 127.0.0.1 --port 8765` 并打开网页；如果传入参数，例如 `run_main.bat status`，仍转发到 CLI。

| 命令 | 参数 | 是否调用 LLM | 作用 |
| --- | --- | --- | --- |
| `python app/main.py` | 无 | 是 | smoke test，测试 DashScope 连通性。 |
| `python app/main.py plan` | 无 | 是 | 读取长期目标和今日待办，生成今日计划。 |
| `python app/main.py plan --tasks "今天要做的事"` | `--tasks` 覆盖写入 `today_tasks.md` | 是 | 写入今日待办并生成今日计划。 |
| `python app/main.py plan --add "新增任务"` | `--add` 追加一条今日任务 | 是 | 局部更新今日计划；单测已通过，真实链路仍需验收。 |
| `python app/main.py log "过程记录"` | 记录文本 | 否 | 追加一条今日过程记录。 |
| `python app/main.py review` | 无 | 是 | 生成今天的晚间复盘。 |
| `python app/main.py review --date YYYY-MM-DD` | `--date` 指定历史日期 | 是 | 生成指定日期的复盘。 |
| `python app/main.py status` | 无 | 否 | 打印今天的 daily。 |
| `python app/main.py cost` | 无 | 否 | 本地汇总今日和本月 token，并写入 daily。 |
| `python app/main.py console` | `--host`、`--port`、`--reload` | 视操作而定 | 启动本地控制台，默认 `127.0.0.1:8765`；当前只控制已有 plan/log/review 和 daily/today_tasks 展示。 |

## Pipeline 指令

| Pipeline | 输入 | Prompt/输出约束 | 写入 | 事件 |
| --- | --- | --- | --- | --- |
| `daily_plan` | 日期、`goals.md`、`today_tasks.md` 或 `--tasks` | 输出今日待办原文、结合长期目标的简短建议、风险提醒、收尾检查项；可用表格；不用代码块；不以询问句结尾。 | daily 的 `计划` 区块 replace | `llm_call`、`plan_generated` |
| `plan_update` | 日期、`goals.md`、`today_tasks.md`、当天 daily、`--add` 新任务 | 只输出严格 JSON，包含 `updated_today_tasks`、`updated_daily_original`、`target_heading`、`new_task_advice`；只为新增任务生成建议，不重写整份计划。 | `today_tasks.md` replace；daily 的 `计划` 区块局部更新 | `llm_call`、`plan_updated` |
| `nightly_review` | 日期、当天 daily、当天事件日志 | 输出“完成了什么”“改进建议”“值得肯定的行为”；重点分析安排和工程经验；没有记录时明确说明；最后给一句基于事实的表扬。 | daily 的 `复盘` 区块 replace | `llm_call`、`review_generated` |
| `log` | 手动记录文本 | 不调用 LLM | daily 的 `记录` 区块 append | `user_log` |
| `cost` | 本地 `llm_call` 事件 | 不调用 LLM，不做费用估算 | daily 的 `记录` 区块 append | 不新增 cost 事件 |

## 代码边界

| 文件 | 职责 |
| --- | --- |
| `app/main.py` | CLI 命令入口，当前使用 `DashScopeProvider`。 |
| `app/console.py` | FastAPI 本地控制台，复用已有 daily、inbox、logger 和 plan/log/review pipeline；不展示 token 统计和事件日志。 |
| `app/pipelines/daily_plan.py` | 今日计划 pipeline。 |
| `app/pipelines/plan_update.py` | 日内计划局部更新 pipeline；已有单测，真实 LLM 链路待验收。 |
| `app/pipelines/nightly_review.py` | 晚间复盘 pipeline。 |
| `app/llm/provider.py` | LLM Provider 抽象和 usage 结构。 |
| `app/llm/dashscope_impl.py` | 当前主 LLM Provider，DashScope SDK 直调。 |
| `app/llm/qwen_agent_impl.py` | 历史/备选 `qwen_agent` 实现，当前 CLI 不使用。 |
| `app/llm/usage.py` | 将 Provider usage 写入 `llm_call` 事件。 |
| `app/daily.py` | daily Markdown 读写。 |
| `app/inbox.py` | today tasks 读写。 |
| `app/memory/goals.py` | 长期目标只读读取。 |
| `app/logger.py` | 按日 events JSON Lines 日志。 |
| `app/cost.py` | 本地 token 汇总。 |
| `app/safety.py` | 路径白名单和 protected file 检查。 |

当前主 LLM 路径：

```text
app/main.py -> DashScopeProvider -> dashscope.MultiModalConversation.call()
```

## 数据规则

`data/` 下实际个人数据和运行数据默认不提交到 Git。

可以提交：

- `data/README.md`
- `.gitkeep`
- `*.example.md`

不应提交：

- `data/user_records/*.md`
- `data/user_inputs/today_tasks.md`
- `data/memory/goals.md`
- `data/system_logs/*.jsonl`
- `data/state/*`
- `data/quarantine/*`

`data/memory/goals.md` 是用户维护的长期目标权威来源。普通 pipeline 只能读取它，不能自动改写它。

`data/user_inputs/today_tasks.md` 是当天输入，可以由用户手写，也可以通过 `python app/main.py plan --tasks "..."` 更新。

当前文档化事件类型：

- `llm_call`
- `plan_generated`
- `plan_updated`
- `user_log`
- `review_generated`

不要重新加入 `today_tasks_updated`、`cost_reported` 或其他临时事件，除非先同步更新统计和文档。`plan_updated` 只对应 `plan --add` 的日内计划局部更新；在真实链路验收前，不要依赖它扩展统计口径。

## 安全规则

- 文件读写应优先使用 `app.safety.safe_read_text`、`safe_write_text`、`safe_append_text` 或已封装好的模块。
- 不要绕过 `app/safety.py` 直接写 `data/` 下的运行文件。
- 不要让 LLM 输出直接变成 shell 命令。
- 不要自动读取浏览器、聊天软件、密钥、cookie 或项目外敏感目录。
- 不要自动删除文件；后续需要清理时优先移动到 `data/quarantine/`。

## 文档规则

- `docs/规划/`：目标、路线图、能力批次和实施边界。
- `docs/吸纳/`：外部产品、框架和方案的调研吸收。
- `docs/执行/`：按日期记录每天实际完成的事、复盘和面试讲解材料。

面试向总览在：

```text
docs/执行/2026-04-19.md
```

更新功能时，优先同步：

- `README.md`
- `AGENTS.md`
- `docs/执行/YYYY-MM-DD.md`
- `docs/规划/2026-04-16_修身炉规划.md`

## 自动化运行注意

- 后续 agent 或自动化运行 Python/验证命令时，默认使用 `conda run -n xiushenlu python ...`，不要直接使用裸 `python ...`。
- 不要假设 `conda activate xiushenlu` 会跨工具调用或跨 shell 生效；如果使用 `conda activate`，必须和实际 Python 命令放在同一条 PowerShell 调用中。
- Windows 下 `conda run` 执行 `--help`、`status` 等中文输出命令时，如果遇到 GBK/Unicode 输出错误，优先设置 UTF-8 环境变量并加 `--no-capture-output`，例如：

```powershell
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
conda run --no-capture-output -n xiushenlu python app/main.py --help
```

- 如果 `rg` 在当前环境里被拒绝执行，改用 PowerShell 原生命令检索，例如 `Get-ChildItem` 和 `Select-String`。

## 验证建议

不需要 LLM 的改动至少跑：

```powershell
conda run -n xiushenlu python -m compileall app
conda run --no-capture-output -n xiushenlu python app/main.py --help
conda run --no-capture-output -n xiushenlu python app/main.py status
```

只有改到 `app/cost.py`、事件日志统计、token usage 记录或相关展示时，才需要额外运行：

```powershell
conda run --no-capture-output -n xiushenlu python app/main.py cost
```

涉及 `plan` 或 `review` 的改动，再考虑真实 LLM 验证。真实 LLM 验证前确认 `DASHSCOPE_API_KEY` 已配置。

涉及 `plan --add` 或 `app/pipelines/plan_update.py` 时，优先跑：

```powershell
conda run -n xiushenlu python -m unittest tests.test_plan_update
```

如果单测超时或失败，先不要把 `plan --add` 视为已验收能力。即使单测通过，进入自动化前仍要做一次真实 LLM 链路验收。

## 设计取舍

当前阶段的原则是：

```text
代码控制流程，LLM 负责生成文本。
```

因此不要轻易引入自主 agent loop。只有在工具注册、审批、预算和暂停机制更完善后，再把自主性扩大。
