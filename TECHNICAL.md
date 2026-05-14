# 修身炉技术说明

本文承接旧版 README 的技术细节。项目首页见 [README.md](README.md)，最新规划见 [docs/规划/2026-05-08_修身炉知识成长助手规划.md](docs/规划/2026-05-08_修身炉知识成长助手规划.md)。

## 定位

修身炉当前是一个本地优先的 Python 执行闭环，用固定 pipeline 帮助记录资料、生成计划、沉淀复盘，并为后续知识库、post、工具、通知和自动化打基础。

当前主路径：

```text
app/main.py -> app.llm.dashscope_impl.DashScopeProvider -> dashscope.MultiModalConversation.call()
```

## 快速运行

在项目根目录运行：

```powershell
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

conda run --no-capture-output -n xiushenlu python app/main.py --help
conda run --no-capture-output -n xiushenlu python app/main.py status
```

日常可以直接双击 `run_main.bat`。无参数时它会启动本地控制台并打开网页；如果 8765 端口已有控制台在运行，则只打开现有页面。默认地址是 `http://127.0.0.1:8765`。

运行窗口保持打开时，可以输入 `重启` 来重启控制台并重新打开网页；按 `Ctrl+C` 会停止控制台并关闭窗口。传入参数时，例如 `run_main.bat status`，仍转发到 CLI。

## Demo 流程

```powershell
# 1. 写入今日待办并生成计划，会调用 LLM
conda run --no-capture-output -n xiushenlu python app/main.py plan --tasks "今天完成项目文档更新；整理面试讲解；晚上复盘"

# 2. 临时新增任务并局部更新今日计划，会调用 LLM
conda run --no-capture-output -n xiushenlu python app/main.py plan --add "补一版资料导入格式"

# 3. 追加过程记录，并让 LLM 返回受限补丁更新“时间安排”表的状态/备注列
conda run --no-capture-output -n xiushenlu python app/main.py log "整理了 README 和技术说明"

# 4. 查看今天的 daily，不调用 LLM
conda run --no-capture-output -n xiushenlu python app/main.py status

# 5. 生成晚间复盘，会调用 LLM；默认会滚动待办，当天 review 还会更新 token 统计
conda run --no-capture-output -n xiushenlu python app/main.py review

# 6. 手动重新统计今日和本月 token，不调用 LLM
conda run --no-capture-output -n xiushenlu python app/main.py cost
```

也可以启动本地控制台调试同一组能力：

```powershell
conda run --no-capture-output -n xiushenlu python app/main.py console
```

控制台目前支持查看 daily、查看今日待办、保存今日待办、打开待办文件、写入记录、生成计划、日内局部更新、生成复盘、停止当前 LLM 操作、手动 token 统计和小红书图文发布。首页日期右侧的“发布”下拉菜单进入 `/xhs` 发布页。自动化、通知、审批、工具和知识区域只预留布局。

控制台里的“保存待办”和“生成计划”是两个独立动作：“保存待办”只写入 `data/user_inputs/today_tasks.md`，不调用 LLM；“生成计划”等价于 `python app/main.py plan`。

控制台里的“停止”是防误点的 v1 语义：它不会强行中断 DashScope SDK 正在进行的同步网络调用，但会标记当前操作为取消；如果 LLM 稍后返回，后端会在写入 daily、`today_tasks.md` 或事件日志前丢弃结果。同一时间后端只允许一个 LLM 操作运行。

## 小红书发布

小红书图文发布不内嵌第三方代码。第三方 `xiaohongshu-mcp` 应 clone 到本项目同级目录，例如：

```text
C:\Users\Ua Pan\Desktop\project\
  xiushenlu\
  xiaohongshu-mcp\
```

登录和 cookies 由 `xiaohongshu-mcp` 自己管理；修身炉只通过 `config/app.yaml` 中的 `xiaohongshu.*` 配置启动 release exe 并调用本地 MCP 服务。默认 MCP 地址是 `http://localhost:18060/mcp`。

推荐操作顺序：

```powershell
# 1. 启动修身炉控制台。
cd C:\Users\Ua Pan\Desktop\project\xiushenlu
conda run --no-capture-output -n xiushenlu python app/main.py console

# 2. 在控制台首页日期右侧，打开“发布”下拉菜单，进入“发布小红书”。

# 3. 在 /xhs 页面点击“打开 MCP”。
#    页面会按配置启动 xiaohongshu.mcp_exe，并只检查 MCP URL 是否连通。
#    小红书登录态由 xiaohongshu-mcp cookies 管理；发布失败时按 MCP 返回错误处理。

# 4. 选择文本路径、图片路径、标题、标签、可见范围等，点击“发布”并确认。
```

`/xhs` 页面行为：

- MCP 状态接口只做 MCP URL 连通性检查并读取本地用户名缓存，不主动调用 `check_login_status`。
- 如果 MCP URL 能连上，按钮显示“关闭 MCP”；平时状态加载不扫描 Windows 进程。点击“关闭 MCP”时才扫描并关闭本机所有 `xiaohongshu-mcp*` 进程，包括不是当前控制台启动的进程。
- 用户名缓存到 `data/state/xhs_account.json`；常规 MCP 状态同步只读缓存，不调用 `/api/v1/user/me`，登录完成后才单独刷新一次用户名缓存。
- 文本路径默认 `post/data/YYYY-MM-DD.txt`，文件不存在时只提示，不自动创建。
- 图片路径默认 `post/images/xiushenlu-xhs-cover.png`，支持多行，每行一个本地绝对路径或 HTTP/HTTPS URL。
- 标题为必填；标签、定时发布和原创标记为可选；可见范围默认 `公开可见`。
- 发布按钮会真实调用 `publish_content`，前端确认框通过后后端固定按 `approve=true` 执行；发布前不再预检查登录状态，cookies 失效或未登录时由 MCP 发布接口返回错误。

命令行仍可作为备用入口：

```powershell
# 检查修身炉能否连上 MCP；不会确认小红书实时登录状态。
conda run --no-capture-output -n xiushenlu python app/main.py xhs status

# 先 dry-run：读取草稿、校验参数、写 post_publish_requested，不真实发布。
conda run --no-capture-output -n xiushenlu python app/main.py xhs publish --draft post/data/2026-05-12.txt --title "修身炉进展" --image "C:\path\to\image.png" --tag 修身炉

# 确认内容、图片和可见范围后，再追加 --approve 实发。
conda run --no-capture-output -n xiushenlu python app/main.py xhs publish --draft post/data/2026-05-12.txt --title "修身炉进展" --image "C:\path\to\image.png" --tag 修身炉 --approve
```

参数说明：

- `--draft`：必须指向 `post/data` 里的正文草稿。
- `--title`：小红书标题，最多约 20 个中文字或英文单词。
- `--image`：至少 1 张图片；支持本地绝对路径或 HTTP/HTTPS URL，本地路径更稳定。
- `--tag`：话题标签，可重复传入，`#修身炉` 和 `修身炉` 都会归一化为 `修身炉`。
- `--visibility`：默认 `仅自己可见`，可显式传 `公开可见` 或 `仅互关好友可见`。
- `--approve`：真正调用 `publish_content` 的确认开关；不带时只做 dry-run。

发布前不再调用 `check_login_status`，会直接调用 `publish_content`。如果 cookies 失效或 MCP 未登录，修身炉会记录 `post_failed` 并展示 MCP 返回的错误，不会自动重试；发布请求会记录 `post_publish_requested`，发布成功记录 `post_published`。

## 命令

| 命令 | 是否调用 LLM | 作用 |
| --- | --- | --- |
| `python app/main.py` | 是 | smoke test，测试 DashScope 连通性。 |
| `python app/main.py plan` | 是 | 根据长期目标和 `today_tasks.md` 生成今日计划。 |
| `python app/main.py plan --tasks "..."` | 是 | 先覆盖写入今日待办，再生成计划。 |
| `python app/main.py plan --add "..."` | 是 | 追加一条今日待办，并把新增任务并入 daily 的待办原文和时间安排表。 |
| `python app/main.py log "..."` | 是 | 先追加一条过程记录，再让 LLM 返回补丁，由代码只更新“时间安排”表的 `状态` 和 `备注` 两列；状态支持空、`○`、`✓`、`×`，其中 `×` 表示删除、取消或不再追踪；失败时保留记录。 |
| `python app/main.py review` | 是 | 根据今天 daily 生成复盘，滚动明日待办，并更新 token 统计；状态为 `×` 的任务不会滚动到明天。 |
| `python app/main.py review --date YYYY-MM-DD` | 是 | 对指定日期生成复盘，并默认把未完成项滚动到当前 `today_tasks.md`。历史日期不会把本次 token 统计写回历史 daily。 |
| `python app/main.py review --date YYYY-MM-DD --no-rollover` | 是 | 只对指定日期生成复盘，不滚动当前待办。 |
| `python app/main.py status` | 否 | 打印今天的 daily。 |
| `python app/main.py cost` | 否 | 汇总今日和本月 token，并覆盖 daily 的 token 统计区块。 |
| `python app/main.py xhs status` | 否 | 通过本地 `xiaohongshu-mcp` 检查 MCP 连通性和本地缓存状态。 |
| `python app/main.py xhs publish ...` | 否 | 从 `post/data` 草稿发布小红书图文；不带 `--approve` 只记录请求。 |
| `python app/main.py console` | 视操作而定 | 启动本地控制台，复用已有 pipeline 和本地读写能力。 |

`plan --add` 要求模型返回严格 JSON，并逐字保留新增任务；程序会更新 `today_tasks.md`、替换 daily 里已有的“今日待办”原文，并把新增任务作为一行追加到时间安排表，`状态` 和 `备注` 两列保持为空。解析失败时不会写入 `today_tasks.md` 或 daily。

`review` 的事实来源是 daily 里固化的 `今日待办原文` 和 `记录`，不是当前 `today_tasks.md`。默认会滚动生成新的 `today_tasks.md`；需要只补历史复盘时显式加 `--no-rollover`。`明日计划.md` 只用于生成新的 `today_tasks.md`，不用于复盘正文。解析失败时不会写复盘、不会覆盖 `today_tasks.md`，也不会清空 `明日计划.md`。

## 配置

主配置文件是 `config/app.yaml`。

| 配置段 | 含义 |
| --- | --- |
| `llm.provider` | 当前为 `dashscope`。 |
| `llm.model` | 当前默认 `qwen3.6-plus`。 |
| `llm.api_key_env` | 默认 `DASHSCOPE_API_KEY`，也可通过项目根目录 `.env` 加载。 |
| `assistant.system_prompt` | Provider 发送给模型的 system prompt。 |
| `paths.*` | daily、inbox、memory、logs、state、quarantine 等目录。 |
| `paths.post_dir` | 小红书正文草稿目录，默认 `post/data`。 |
| `paths.post_image_dir` | 小红书默认图片目录，默认 `post/images`。 |
| `xiaohongshu.mcp_url` | 本地 `xiaohongshu-mcp` MCP 地址，默认 `http://localhost:18060/mcp`。 |
| `xiaohongshu.mcp_exe` | 第三方 MCP release 主程序路径；控制台“打开/关闭 MCP”使用它。 |
| `xiaohongshu.login_exe` | 第三方登录工具路径；需要重新登录时可手动使用它。 |
| `xiaohongshu.working_dir` | 运行第三方 exe 的工作目录，通常是同级 `xiaohongshu-mcp`。 |
| `safety.allowed_dirs` | 允许读写的数据目录白名单。 |
| `safety.protected_files` | 受保护文件，当前包含 `data/memory/goals.md`。 |

`app/llm/qwen_agent_impl.py` 仍保留为历史/备选实现，但 CLI 当前不使用它。

## 模块职责

| 文件 | 职责 |
| --- | --- |
| `app/main.py` | CLI 命令入口；加载配置；组装 Provider；调用 pipeline 或本地读写函数。 |
| `app/console.py` | FastAPI 本地控制台；展示 daily 和 today_tasks；触发已有 plan/log/review 能力。 |
| `app/pipelines/daily_plan.py` | 今日计划 pipeline；LLM 生成五列表格形式的时间安排。 |
| `app/pipelines/log_schedule_update.py` | 写入记录后的时间安排表更新 pipeline；LLM 只返回补丁，代码只写 `状态` 和 `备注` 两列。 |
| `app/pipelines/plan_update.py` | 日内计划局部更新 pipeline；更新待办原文并给时间安排表追加一行，解析失败时停止写入。 |
| `app/pipelines/nightly_review.py` | 晚间复盘 pipeline；当天复盘成功后滚动待办并清空 `明日计划.md`。 |
| `app/posting/` | 小红书图文发布轻量适配：读取 `post/data` 草稿、校验参数、调用本地 MCP 并记录事件。 |
| `app/daily.py` | daily Markdown 路径、读取、区块替换和记录追加。 |
| `app/inbox.py` | `today_tasks.md` 和 `明日计划.md` 的读写封装。 |
| `app/logger.py` | 按日 JSON Lines 事件追加和读取。 |
| `app/cost.py` | 汇总本地 `llm_call` 事件，统计 token。 |
| `app/safety.py` | 路径白名单、protected file 检查和安全读写封装。 |

## 数据规则

`data/` 下实际个人数据和运行数据默认不提交到 Git。

可以提交：

- `data/README.md`
- `.gitkeep`
- `*.example.md`

不应提交：

- `data/user_records/*.md`
- `data/user_inputs/today_tasks.md`
- `data/user_inputs/明日计划.md`
- `data/memory/goals.md`
- `data/system_logs/*.jsonl`
- `data/state/*`
- `data/quarantine/*`

主要运行文件：

- `data/user_inputs/today_tasks.md`：当前待办输入槽。
- `data/user_inputs/明日计划.md`：明日计划暂存；当天 `review` 成功滚动后清空。
- `data/user_records/YYYY-MM-DD.md`：人类可读 daily。
- `data/system_logs/YYYY-MM-DD.jsonl`：机器可读事件流。

## 验证建议

不需要 LLM 的文档或本地逻辑改动至少跑：

```powershell
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

conda run -n xiushenlu python -m compileall app
conda run --no-capture-output -n xiushenlu python app/main.py --help
conda run --no-capture-output -n xiushenlu python app/main.py status
```

只有改到 `app/cost.py`、事件日志统计、token usage 记录或相关展示时，才额外运行：

```powershell
conda run --no-capture-output -n xiushenlu python app/main.py cost
```

涉及 `plan --add` 或 `app/pipelines/plan_update.py` 时，优先跑：

```powershell
conda run -n xiushenlu python -m unittest tests.test_plan_update
```

涉及 `plan` 或 `review` 的改动，再考虑真实 LLM 验证。真实 LLM 验证前确认 `DASHSCOPE_API_KEY` 已配置。
