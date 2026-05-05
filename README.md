# 修身炉

修身炉是一个面向个人认知与执行管理的本地助手项目。当前版本已完成 Phase 1 的最小闭环：配置加载、DashScope 直调 LLM Provider、事件日志、计划、记录、复盘、状态查看、token 统计和基础路径安全。当前新增了本地控制台雏形，用于在网页里调试已有 CLI 能力。

它不是一个完整自主 agent，也不是后台调度系统。当前定位是一个可追踪、可审计、可逐步扩展的本地 Python 执行闭环：

```text
长期目标 + 今日待办 -> 今日计划 -> 过程记录 -> 晚间复盘 -> 明日待办滚动 -> token 统计
```

## 项目亮点

| 亮点 | 当前实现 |
| --- | --- |
| LLM Provider 抽象 | 业务 pipeline 只依赖 `LLMProvider.chat(prompt)`，当前主实现是 `DashScopeProvider`。 |
| 固定 pipeline 优先 | 由代码控制流程，LLM 只负责生成计划/复盘文本，不让模型自行决定工具调用。 |
| 双层数据 | Markdown daily 给用户阅读，按日 JSON Lines events 给机器统计、审计和复盘。 |
| 安全边界 | 用路径白名单限制文件读写，并保护长期目标文件不被普通流程改写。 |
| 数据隐私 | `data/` 下实际个人数据和运行数据默认不提交，只提交模板、占位文件和说明文档。 |
| 成本意识 | 每次 LLM 调用记录 token usage，`cost` 命令本地统计并写入 daily。 |

## Demo 流程

在项目根目录运行：

```powershell
conda activate xiushenlu

# 1. 写入今日待办并生成计划，会调用 LLM
python app/main.py plan --tasks "今天完成项目文档更新；整理面试讲解；晚上复盘"

# 2. 可选：临时新增任务并局部更新今日计划，会调用 LLM
python app/main.py plan --add "补一版 plan_update 单测"

# 3. 过程中追加记录，不调用 LLM
python app/main.py log "完成 README 和 AGENTS 的文档修正"
python app/main.py log "补充 pipeline 参数表和模块职责表"

# 4. 查看今天的 daily，不调用 LLM
python app/main.py status

# 5. 根据当天 daily 生成复盘，会调用 LLM；当天 review 会把未完成任务和明日计划滚入 today_tasks.md，并追加 token 统计
python app/main.py review

# 6. 手动重新统计今日和本月 token，不调用 LLM
python app/main.py cost
```

也可以启动本地控制台调试同一组能力：

```powershell
conda run --no-capture-output -n xiushenlu python app/main.py console
```

更日常的方式是直接双击 `run_main.bat`，它会启动控制台并打开网页；如果 8765 端口已有控制台在运行，则只打开现有页面。默认地址是 `http://127.0.0.1:8765`。启动窗口保持打开时，可以输入 `重启` 来重启控制台并重新打开网页；按 `Ctrl+C` 会停止控制台并关闭窗口。控制台目前只封装常用执行能力：查看 daily、查看今日待办、保存今日待办、写入记录、生成计划、日内局部更新、生成复盘；生成今天复盘时会复用 CLI 的待办滚动逻辑，并自动追加 token 统计。自动化、通知、审批、工具和知识区域只预留布局。token 统计和事件日志仍由 CLI 与本地文件保留，但不在控制台展示。

控制台里的“保存待办”和“生成计划”是两个独立动作：“保存待办”只写入 `data/user_inputs/today_tasks.md`，不调用 LLM；“生成计划”等价于 `python app/main.py plan`，只读取已保存的 `today_tasks.md`。

这组命令会产生三类本地文件：

| 文件 | 用途 |
| --- | --- |
| `data/user_inputs/today_tasks.md` | 今日待办输入。`plan --tasks "..."` 会覆盖写入它；当天 `review` 成功后也会用未完成任务和明日计划覆盖它。 |
| `data/user_inputs/明日计划.md` | 明日计划暂存。当天 `review` 成功滚动后会清空它；历史 `review --date` 不会改动它。 |
| `data/user_records/YYYY-MM-DD.md` | 人类可读 daily，包含计划、记录、复盘和 token 统计。 |
| `data/system_logs/YYYY-MM-DD.jsonl` | 机器可读事件流，记录 `llm_call`、`plan_generated`、`plan_updated`、`user_log`、`review_generated`。 |

## 命令与参数

| 命令 | 参数 | 是否调用 LLM | 作用 | 主要读写 |
| --- | --- | --- | --- | --- |
| `python app/main.py` | 无 | 是 | smoke test，发送一句连通性 prompt | 只输出到控制台 |
| `python app/main.py plan` | 无 | 是 | 根据长期目标和 `today_tasks.md` 生成今日计划 | 读 `goals.md` / `today_tasks.md`，写 daily 和 events |
| `python app/main.py plan --tasks "..."` | `--tasks`：今日待办文本 | 是 | 先覆盖写入今日待办，再生成计划 | 写 `today_tasks.md`，写 daily 和 events |
| `python app/main.py plan --add "..."` | `--add`：新增今日任务 | 是 | 追加一条今日待办，并局部更新当天计划 | 写 `today_tasks.md`，写 daily 和 `plan_updated` 事件 |
| `python app/main.py log "..."` | 位置参数：记录内容，可多词 | 否 | 追加一条过程记录 | 写 daily 的 `记录` 区块，写 `user_log` 事件 |
| `python app/main.py review` | 无 | 是 | 根据今天的 daily 生成晚间复盘，滚动明日待办，并追加 token 统计 | 读 daily/events、`today_tasks.md`、`明日计划.md`；写 daily/events、`today_tasks.md`，清空 `明日计划.md` |
| `python app/main.py review --date YYYY-MM-DD` | `--date`：历史日期 | 是 | 对指定日期生成复盘，不滚动当前待办 | 读指定日期 daily/events，写指定日期 daily/events |
| `python app/main.py status` | 无 | 否 | 打印今天的 daily | 读 daily |
| `python app/main.py cost` | 无 | 否 | 手动汇总今日和本月 token，并追加到 daily | 读 events，写 daily 的 `记录` 区块 |
| `python app/main.py console` | `--host`、`--port`、`--reload` | 视操作而定 | 启动本地控制台，复用已有 pipeline 和本地读写能力 | 通过 API 间接读写 daily 和 today_tasks |

`plan --add` 是日内计划更新入口，目前本地单测已覆盖解析、写入和失败保护。它要求模型返回严格 JSON，并且必须逐字保留新增任务、只生成不超过 200 字的新任务建议；如果解析失败或内容不符合约束，流程会停止写入 `today_tasks.md` 和 daily。进入自动化前，还需要完成一次真实 DashScope 链路验收。

当天 `review` 也是受控写入入口：模型必须返回严格 JSON，包含复盘正文和新的完整 `today_tasks.md`。解析失败时不会写入复盘、不会覆盖 `today_tasks.md`，也不会清空 `明日计划.md`，不会追加 token 统计。只有复盘日期等于今天时才触发这一步；历史日期复盘只更新对应 daily。当天复盘成功后会自动把本次复盘 LLM 调用计入今日/本月 token 统计并追加到 daily。


## 配置

主配置文件是 `config/app.yaml`。

| 配置段 | 当前值/含义 |
| --- | --- |
| `llm.provider` | `dashscope`，表示当前主路径是 DashScope SDK 直调。 |
| `llm.model` | 当前默认 `qwen3.5-plus`。 |
| `llm.api_key_env` | 默认 `DASHSCOPE_API_KEY`，也可通过项目根目录 `.env` 加载。 |
| `assistant.system_prompt` | Provider 发送给模型的 system prompt。 |
| `paths.*` | daily、inbox、memory、logs、state、quarantine 等目录。 |
| `safety.allowed_dirs` | 允许读写的数据目录白名单。 |
| `safety.protected_files` | 受保护文件，当前包含 `data/memory/goals.md`。 |

当前代码主路径：

```text
app/main.py -> app.llm.dashscope_impl.DashScopeProvider -> dashscope.MultiModalConversation.call()
```

`app/llm/qwen_agent_impl.py` 仍保留为历史/备选实现，但 CLI 当前不使用它。

## app 模块职责

| 文件 | coding 职责 | 主要依赖 |
| --- | --- | --- |
| `app/main.py` | CLI 命令入口；解析参数；加载配置；组装 Provider；调用 pipeline 或本地读写函数。 | `config`、`DashScopeProvider`、`daily`、`inbox`、`logger`、`cost`、`pipelines` |
| `app/config.py` | 读取 YAML 配置；把相对路径解析到项目根目录。 | `yaml`、`pathlib` |
| `app/console.py` | FastAPI 本地控制台；展示 daily 和 today_tasks，支持保存待办，并触发已有 plan/log/review 能力。 | `fastapi`、`daily`、`inbox`、`logger`、`pipelines` |
| `app/llm/provider.py` | 定义 `LLMProvider.chat()` 抽象和 `LLMCallUsage` 结构。 | 标准库 |
| `app/llm/dashscope_impl.py` | 当前主 LLM 实现；读取 `DASHSCOPE_API_KEY`；调用 DashScope；记录 usage。 | `dashscope`、`python-dotenv`、`provider` |
| `app/llm/qwen_agent_impl.py` | 历史/备选 `qwen_agent` 实现，当前 CLI 不走这条路径。 | `qwen_agent`、`dashscope`、`provider` |
| `app/llm/usage.py` | 把 Provider 的 `last_usage` 写成 `llm_call` 事件。 | `logger`、`provider` |
| `app/pipelines/daily_plan.py` | 今日计划 pipeline：读 goals/tasks，构造 prompt，调用 LLM，写 daily 和事件。 | `config`、`daily`、`inbox`、`goals`、`provider`、`usage`、`logger` |
| `app/pipelines/plan_update.py` | 日内计划更新 pipeline：读 goals/tasks/daily，追加新增任务，局部更新 daily 计划并写事件。 | `config`、`daily`、`inbox`、`goals`、`provider`、`usage`、`logger`、`safety` |
| `app/pipelines/nightly_review.py` | 晚间复盘 pipeline：读 daily/events，构造 prompt，调用 LLM，写 daily 和事件；当天复盘还会滚动 `today_tasks.md` 并清空 `明日计划.md`。 | `config`、`daily`、`inbox`、`provider`、`usage`、`logger` |
| `app/daily.py` | daily Markdown 路径、读取、区块替换、记录追加。 | `config`、`safety` |
| `app/inbox.py` | `today_tasks.md` 和 `明日计划.md` 的路径、读取、写入/清空封装。 | `config`、`safety` |
| `app/memory/goals.py` | 长期目标只读读取。 | `config`、`safety` |
| `app/logger.py` | 按日 JSON Lines 事件追加和读取。 | `config`、`safety` |
| `app/cost.py` | 汇总本地 `llm_call` 事件，统计今日和本月 token。 | `logger` |
| `app/safety.py` | 路径白名单、protected file 检查、安全读写封装。 | `config`、`pathlib` |

## 数据规则

`data/` 下实际个人数据和运行数据默认不提交到 Git。

| 可以提交 | 不应提交 |
| --- | --- |
| `data/README.md` | `data/user_records/*.md` |
| `.gitkeep` | `data/user_inputs/today_tasks.md` |
| `*.example.md` | `data/user_inputs/明日计划.md` |
|  | `data/memory/goals.md` |
|  | `data/system_logs/*.jsonl` |
|  | `data/state/*` |
|  | `data/quarantine/*` |

## 验证建议

不需要 LLM 的文档或本地逻辑改动至少跑：

```powershell
python -m compileall app
python app/main.py --help
python app/main.py status
```

只有改到 `app/cost.py`、事件日志统计、token usage 记录或相关展示时，才额外运行：

```powershell
python app/main.py cost
```

涉及 `plan` 或 `review` 的改动，再考虑真实 LLM 验证。真实 LLM 验证前确认 `DASHSCOPE_API_KEY` 已配置。

涉及 `plan --add` 或 `app/pipelines/plan_update.py` 时，优先跑：

```powershell
python -m unittest tests.test_plan_update
```

2026-05-02 已在 `xiushenlu` conda 环境下验证通过；真实 LLM 端到端链路仍需单独验收。

## 下一步

Phase 1 已完成。下一阶段先把本地控制台用起来，再通过控制台验收 `plan --add` 和自动化：

| 方向 | 目标 |
| --- | --- |
| 本地控制台 | 先控制已有内容，作为调试和验收入口。 |
| 自动化与通知 | 定时运行计划/复盘，并通过 PushPlus / PushDeer 推送。 |
| 安全与审批 | 工具注册、审批队列、异常暂停和预算控制。 |

`plan --add` 的真实链路验收可以通过控制台完成，避免先写自动化调度器再排查 UI、状态和写入问题。
