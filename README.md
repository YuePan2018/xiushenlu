# 修身炉

修身炉是一个面向个人认知与执行管理的本地助手项目。当前版本已完成 Phase 1 的最小闭环：项目骨架、配置加载、Qwen Agent LLM Provider、事件日志、计划、记录、复盘、状态查看、token 统计和基础路径安全。

第一阶段目标是先跑通最小闭环：

```text
记录 -> 计划 -> 复盘
```

当前还不是完整自主 agent，也不是自动化调度系统。它现在是一个可继续扩展的本地 Python CLI 执行闭环。

## 当前能做到什么

- 使用独立 conda 环境 `xiushenlu` 运行项目。
- 从 `config/app.yaml` 读取模型、超时、重试次数、数据目录等配置。
- 通过 `DASHSCOPE_API_KEY` 连接 DashScope。
- 使用 `qwen_agent.agents.Assistant` 调用 Qwen 模型。
- 提供一个薄的 `LLMProvider.chat(prompt) -> str` 抽象，后续每日计划、晚间复盘 pipeline 可以复用。
- 运行 `python app/main.py`，向模型发送一句测试 prompt，并打印模型回复。
- 使用 `EventLogger.append_event(type, summary, detail=None)` 追加写入本地事件日志。
- 已有数据目录约定说明和长期目标模板。
- 使用 `read_goals()` 只读读取 `data/memory/goals.md`。
- 运行 `python app/main.py plan`，读取长期目标和今日待办，生成当天计划并写入 `data/daily/YYYY-MM-DD.md`。
- 运行 `python app/main.py log "内容"`，向当天 daily 追加记录。
- 运行 `python app/main.py review`，根据当天 daily 和事件日志生成复盘。
- 运行 `python app/main.py status`，查看当天 daily 内容。
- 运行 `python app/main.py cost`，查看今日和本月 LLM token 消耗；模型价格未配置时只统计 token，不估算费用。
- 使用路径白名单保护运行时文件读写，并阻止普通流程改写 `data/memory/goals.md`。
- 已建立 Phase 1 需要的数据目录：
  - `data/daily/`
  - `data/inbox/`
  - `data/memory/`
  - `data/logs/`

## 当前还不能做什么

- 还没有定时调度。
- 还没有手机通知。
- 还没有 Web 控制台。

这些能力会在后续里程碑中逐步补齐。

## 环境

本项目使用 conda 环境：

```powershell
conda activate xiushenlu
```

当前环境是从已有的 `shenshen` 环境克隆出来的，用于避免在 base 环境或系统 Python 中安装项目依赖。

如果以后需要从配置文件重建环境，可以参考：

```powershell
conda env create -f environment.yml
```

依赖清单也保留在 `requirements.txt`，与 `llm/requirements.txt` 的原始 Qwen Agent 项目依赖保持一致。

## 配置

主配置文件是：

```text
config/app.yaml
```

当前默认模型：

```yaml
llm:
  provider: qwen_agent
  model: "qwen3-max-2026-01-23"
  timeout: 30
  retry_count: 2
  api_key_env: "DASHSCOPE_API_KEY"
```

需要确保环境变量 `DASHSCOPE_API_KEY` 可用。也可以在项目根目录放 `.env` 文件，由 `python-dotenv` 自动加载。

## 运行

在项目根目录执行：

```powershell
conda activate xiushenlu
python app/main.py
```

成功时会看到模型返回一句确认连通的回复。

生成今日计划：

```powershell
conda activate xiushenlu
python app/main.py plan
```

计划命令会读取：

- `data/memory/goals.md`：长期目标，只读输入。
- `data/inbox/today_tasks.md`：今日待办，可手动编辑。

输出会写入：

- `data/daily/YYYY-MM-DD.md`
- `data/logs/events.jsonl`

添加今日记录：

```powershell
python app/main.py log "今天完成了一个关键任务"
```

生成晚间复盘：

```powershell
python app/main.py review
```

查看今日状态：

```powershell
python app/main.py status
```

查看 token 消耗：

```powershell
python app/main.py cost
```

## 当前目录结构

```text
xiushenlu/
  app/
    main.py
    config.py
    llm/
      provider.py
      qwen_agent_impl.py
  config/
    app.yaml
  data/
    daily/
    inbox/
    memory/
    logs/
    state/
    quarantine/
  docs/
    规划/
    吸纳/
    执行/
  llm/
  environment.yml
  requirements.txt
  README.md
```

## 文档

项目文档已按用途整理：

- `docs/规划/`：目标、路线图、能力批次和实施边界。
- `docs/吸纳/`：外部产品、框架和方案的调研吸收。
- `docs/执行/`：按日期记录每天实际完成的事。

## 下一步

按照 `docs/规划/2026-04-16_修身炉规划.md`，Phase 1 的能力批次已经完成。下一步进入后续里程碑：

- 自动化与通知：定时运行计划/复盘，并通过 PushPlus / PushDeer 推送。
- 本地控制台：查看状态、日志、计划和复盘。
- 更完整的安全与审批：工具注册、审批队列、异常暂停。
