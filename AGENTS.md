# AGENTS

本文件给后续参与本项目的 agent 或开发者使用。目标是让自动化协作者先理解项目边界，再动代码。

## 项目定位

修身炉是一个本地优先的个人执行管理助手。当前阶段不是完整自主 agent，而是一个可追踪的 CLI 执行闭环：

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

可用命令：

- `python app/main.py plan`
- `python app/main.py plan --tasks "今天要做的事"`
- `python app/main.py log "过程记录"`
- `python app/main.py review`
- `python app/main.py status`
- `python app/main.py cost`

只有 `plan`、`review` 和默认 smoke test 会调用 LLM。`log`、`status`、`cost` 都是本地操作。

## 代码边界

- `app/main.py`：CLI 命令入口。
- `app/pipelines/daily_plan.py`：今日计划 pipeline。
- `app/pipelines/nightly_review.py`：晚间复盘 pipeline。
- `app/llm/provider.py`：LLM Provider 抽象。
- `app/llm/qwen_agent_impl.py`：Qwen Agent Provider 实现。
- `app/daily.py`：daily Markdown 读写。
- `app/inbox.py`：today tasks 读写。
- `app/logger.py`：按日 events JSON Lines 日志。
- `app/cost.py`：本地 token 汇总。
- `app/safety.py`：路径白名单和 protected file 检查。

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

当前正式事件类型只有：

- `llm_call`
- `plan_generated`
- `user_log`
- `review_generated`

不要重新加入 `today_tasks_updated` 或 `cost_reported`。

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

## 验证建议

不需要 LLM 的改动至少跑：

```powershell
python -m compileall app
python app/main.py --help
python app/main.py status
```

只有改到 `app/cost.py`、事件日志统计、token usage 记录或相关展示时，才需要额外运行：

```powershell
python app/main.py cost
```

涉及 `plan` 或 `review` 的改动，再考虑真实 LLM 验证。真实 LLM 验证前确认 `DASHSCOPE_API_KEY` 已配置。

## 设计取舍

当前阶段的原则是：

```text
代码控制流程，LLM 负责生成文本。
```

因此不要轻易引入自主 agent loop。只有在工具注册、审批、预算和暂停机制更完善后，再把自主性扩大。
