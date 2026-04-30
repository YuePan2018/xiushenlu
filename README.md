# 修身炉

修身炉是一个面向个人认知与执行管理的本地助手项目。当前版本已完成 Phase 1 的最小闭环：配置加载、DashScope 直调 LLM Provider、事件日志、计划、记录、复盘、状态查看、token 统计和基础路径安全。

它不是一个完整自主 agent，也不是后台调度系统。当前定位是一个可追踪、可审计、可逐步扩展的本地 Python CLI 执行闭环：

```text
长期目标 + 今日待办 -> 今日计划 -> 过程记录 -> 晚间复盘 -> token 统计
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

# 2. 过程中追加记录，不调用 LLM
python app/main.py log "完成 README 和 AGENTS 的文档修正"
python app/main.py log "补充 pipeline 参数表和模块职责表"

# 3. 查看今天的 daily，不调用 LLM
python app/main.py status

# 4. 根据当天 daily 生成复盘，会调用 LLM
python app/main.py review

# 5. 统计今日和本月 token，不调用 LLM
python app/main.py cost
```

这组命令会产生两类本地文件：

| 文件 | 用途 |
| --- | --- |
| `data/user_inputs/today_tasks.md` | 今日待办输入。`plan --tasks "..."` 会覆盖写入它。 |
| `data/user_records/YYYY-MM-DD.md` | 人类可读 daily，包含计划、记录、复盘和 token 统计。 |
| `data/system_logs/YYYY-MM-DD.jsonl` | 机器可读事件流，记录 `llm_call`、`plan_generated`、`user_log`、`review_generated`。 |

## 命令与参数

| 命令 | 参数 | 是否调用 LLM | 作用 | 主要读写 |
| --- | --- | --- | --- | --- |
| `python app/main.py` | 无 | 是 | smoke test，发送一句连通性 prompt | 只输出到控制台 |
| `python app/main.py plan` | 无 | 是 | 根据长期目标和 `today_tasks.md` 生成今日计划 | 读 `goals.md` / `today_tasks.md`，写 daily 和 events |
| `python app/main.py plan --tasks "..."` | `--tasks`：今日待办文本 | 是 | 先覆盖写入今日待办，再生成计划 | 写 `today_tasks.md`，写 daily 和 events |
| `python app/main.py log "..."` | 位置参数：记录内容，可多词 | 否 | 追加一条过程记录 | 写 daily 的 `记录` 区块，写 `user_log` 事件 |
| `python app/main.py review` | 无 | 是 | 根据今天的 daily 生成晚间复盘 | 读 daily，写 daily 和 events |
| `python app/main.py review --date YYYY-MM-DD` | `--date`：历史日期 | 是 | 对指定日期生成复盘 | 读指定日期 daily/events，写指定日期 daily/events |
| `python app/main.py status` | 无 | 否 | 打印今天的 daily | 读 daily |
| `python app/main.py cost` | 无 | 否 | 汇总今日和本月 token，并追加到 daily | 读 events，写 daily 的 `记录` 区块 |


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
| `app/llm/provider.py` | 定义 `LLMProvider.chat()` 抽象和 `LLMCallUsage` 结构。 | 标准库 |
| `app/llm/dashscope_impl.py` | 当前主 LLM 实现；读取 `DASHSCOPE_API_KEY`；调用 DashScope；记录 usage。 | `dashscope`、`python-dotenv`、`provider` |
| `app/llm/qwen_agent_impl.py` | 历史/备选 `qwen_agent` 实现，当前 CLI 不走这条路径。 | `qwen_agent`、`dashscope`、`provider` |
| `app/llm/usage.py` | 把 Provider 的 `last_usage` 写成 `llm_call` 事件。 | `logger`、`provider` |
| `app/pipelines/daily_plan.py` | 今日计划 pipeline：读 goals/tasks，构造 prompt，调用 LLM，写 daily 和事件。 | `config`、`daily`、`inbox`、`goals`、`provider`、`usage`、`logger` |
| `app/pipelines/nightly_review.py` | 晚间复盘 pipeline：读 daily/events，构造 prompt，调用 LLM，写 daily 和事件。 | `config`、`daily`、`provider`、`usage`、`logger` |
| `app/daily.py` | daily Markdown 路径、读取、区块替换、记录追加。 | `config`、`safety` |
| `app/inbox.py` | `today_tasks.md` 读取和写入。 | `config`、`safety` |
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
| `*.example.md` | `data/memory/goals.md` |
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

## 下一步

Phase 1 已完成。下一阶段进入 Milestone 2：

| 方向 | 目标 |
| --- | --- |
| 自动化与通知 | 定时运行计划/复盘，并通过 PushPlus / PushDeer 推送。 |
| 本地控制台 | 查看状态、日志、计划和复盘。 |
| 安全与审批 | 工具注册、审批队列、异常暂停和预算控制。 |
