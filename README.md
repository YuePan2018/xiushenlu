# 修身炉

修身炉是一个面向个人认知与执行管理的本地助手项目。当前版本处于 Phase 1 / 批次 A：已经完成项目骨架、配置加载、Qwen Agent LLM Provider 接入，以及一次真实 LLM 连通验收。

第一阶段目标是先跑通最小闭环：

```text
记录 -> 计划 -> 复盘
```

当前还不是完整 agent，也不是自动化调度系统。它现在更接近一个可继续扩展的本地 Python CLI 骨架。

## 当前能做到什么

- 使用独立 conda 环境 `xiushenlu` 运行项目。
- 从 `config/app.yaml` 读取模型、超时、重试次数、数据目录等配置。
- 通过 `DASHSCOPE_API_KEY` 连接 DashScope。
- 使用 `qwen_agent.agents.Assistant` 调用 Qwen 模型。
- 提供一个薄的 `LLMProvider.chat(prompt) -> str` 抽象，后续每日计划、晚间复盘 pipeline 可以复用。
- 运行 `python app/main.py`，向模型发送一句测试 prompt，并打印模型回复。
- 使用 `EventLogger.append_event(type, summary, detail=None)` 追加写入本地事件日志。
- 已有数据目录约定说明和长期目标模板。
- 已建立 Phase 1 需要的数据目录：
  - `data/daily/`
  - `data/inbox/`
  - `data/memory/`
  - `data/logs/`

## 当前还不能做什么

- 还不能生成每日计划文件。
- 还不能生成晚间复盘。
- 还没有 `plan`、`review`、`log`、`status` 等 CLI 命令。
- 还没有定时调度。
- 还没有手机通知。
- 还没有 Web 控制台。

这些能力会在 Phase 1 后续能力批次中逐步补齐。

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

按照 `docs/规划/2026-04-16_修身炉规划.md`，下一步是批次 C：

- 新增 `app/pipelines/daily_plan.py`。
- 读取 `data/memory/goals.md` 和 `data/inbox/today_tasks.md`。
- 调用 LLM 生成计划。
- 写入 `data/daily/YYYY-MM-DD.md`。
- 记录计划生成事件。
