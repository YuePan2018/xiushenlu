# 修身炉技术说明

本文承接旧版 README 的技术细节。项目首页见 [README.md](README.md)，最新规划见 [docs/规划/2026-05-08_修身炉知识成长助手规划.md](docs/规划/2026-05-08_修身炉知识成长助手规划.md)。

## 定位

修身炉当前是一个本地优先的 Python 执行闭环，用固定 pipeline 帮助记录资料、生成计划、沉淀晚间表扬，并为后续知识库、post、工具、通知和自动化打基础。

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

日常控制台可以直接双击 `run_console.bat`。默认会监听 `0.0.0.0:8765`，自动识别电脑当前局域网 IPv4，并打开形如 `http://192.168.x.x:8765/` 的网页；同一 Wi-Fi 下的手机也访问这个地址。首次弹出 Windows 防火墙提示时，允许“专用网络”。

只允许本机访问时，使用：

```powershell
.\run_console.bat --local-only
```

本机-only 模式监听 `127.0.0.1:8765`，默认地址是 `http://127.0.0.1:8765/`。

运行窗口保持打开时，可以输入 `重启` 来重启控制台并重新打开网页；按 `Ctrl+C` 会停止控制台并关闭窗口。CLI 命令统一使用 `conda run --no-capture-output -n xiushenlu python app/main.py ...`。

## Demo 流程

```powershell
# 1. 写入今日待办并生成计划，会调用 LLM
conda run --no-capture-output -n xiushenlu python app/main.py plan --tasks "今天完成项目文档更新；整理面试讲解；晚上生成表扬"

# 2. 临时新增任务并局部更新今日计划，会调用 LLM
conda run --no-capture-output -n xiushenlu python app/main.py plan --add "补一版资料吸纳格式"

# 3. 追加过程记录，并让 LLM 返回受限补丁更新“任务管理”表的状态/用时列
conda run --no-capture-output -n xiushenlu python app/main.py log "整理了 README 和技术说明"

# 4. 查看今天的 daily，不调用 LLM
conda run --no-capture-output -n xiushenlu python app/main.py status

# 5. 生成晚间表扬，会调用 LLM；默认会滚动待办，当天 review 还会更新 token 统计
conda run --no-capture-output -n xiushenlu python app/main.py review

# 6. 手动重新统计今日和本月 token，不调用 LLM
conda run --no-capture-output -n xiushenlu python app/main.py cost
```

也可以启动本地控制台调试同一组能力：

```powershell
conda run --no-capture-output -n xiushenlu python app/main.py console
```

控制台目前支持查看 daily、查看今日待办、保存今日待办、打开待办文件、写入记录、生成计划、日内局部更新、生成表扬、停止当前 LLM 操作、手动 token 统计、小红书图文发布和工作树编辑。首页日期右侧的菜单可进入 `/xhs` 发布页和 `/task-tree` 工作树页。资料吸纳、自动化、通知、审批、工具和知识库区域只预留布局。

控制台里的“保存待办”和“生成计划”是两个独立动作：“保存待办”只写入 `data/user_inputs/today_tasks.md`，不调用 LLM；“生成计划”等价于 `python app/main.py plan`。

控制台里的“停止”是防误点的 v1 语义：它不会强行中断 DashScope SDK 正在进行的同步网络调用，但会标记当前操作为取消；如果 LLM 稍后返回，后端会在写入 daily、`today_tasks.md` 或事件日志前丢弃结果。同一时间后端只允许一个 LLM 操作运行。

工作树页不调用 LLM。`/task-tree` 使用本地 vendored SimpleMindMap 以 `organizationStructure` 布局加载工作树 JSON，支持根在上、叶在下的矩形节点、拖拽、缩放、节点内容编辑、编辑模式和子树展开/收起。“选择文件”只扫描 `paths.task_tree_dir` 根目录下的 `.json` 文件，不递归子目录；菜单以文件名 stem 展示，读取时使用完整文件名作为 key。选择文件会把标题设为文件名 stem，并把磁盘里的 JSON 原文加载到输入区；左侧 JSON 输入只作为导入/导出缓冲区，点击“渲染输入”才会导入画布，点击“同步当前树”才会把画布导出为 JSON。保存时始终以当前画布树为准；如果 JSON 输入有未渲染改动，页面会阻止保存，要求先渲染输入或同步当前树。保存时会校验 JSON，并按“保存标题”写入 `data/task_tree/<标题>.json`，标题会清洗为合法 Windows 文件名，保存后刷新文件列表并选中新文件。

工作树编辑模式打开后，节点右侧 `+` 会调用 SimpleMindMap 的 `INSERT_NODE` 新增同级节点，节点下侧 `+` 会调用 `INSERT_CHILD_NODE` 新增子节点；根节点不显示同级新增入口。右侧“节点属性”面板通过 `SET_NODE_TEXT` 修改节点标题，通过 `SET_NODE_DATA` 修改 `_xiushenlu.content`，删除节点调用 `REMOVE_NODE` 删除整支子树；撤销和重做分别调用 `BACK` 与 `FORWARD`。页面保存前会把 SimpleMindMap 当前数据转回项目工作树 JSON。

工作树 JSON 根对象需要包含 `title` 和 `nodes`，可选 `summary`。节点包含 `title`，可选 `id`、`content` 和 `children`；`children` 是子节点数组，可省略或为空。旧 JSON 中的 `note` 会在保存时迁移为 `content`，`kind`、`cadence`、`status`、`tags` 等旧节点标签字段会被丢弃。

## 桌面宠物

Windows 桌面宠物是一个轻量 Tkinter/Pillow 小窗口，独立于本地控制台运行。它不会读取浏览器、聊天软件、密钥、Codex 日志或 daily 内容，只读取配置和 `data/desktop_pet` 下的宠物素材与位置状态。

默认宠物素材来自 OpenPets 的 fox 包。首次启动或检查时，如果 `data/desktop_pet/pets/fox` 缺少素材，会从 `desktop_pet.default_asset_url` 下载 ZIP，校验 `pet.json` 和 spritesheet 路径后解压到 `data/desktop_pet`。素材和状态属于本地运行数据，不提交到 Git。

运行方式：

```powershell
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

conda run --no-capture-output -n xiushenlu python app/main.py pet --check
conda run --no-capture-output -n xiushenlu python app/main.py pet
```

也可以双击 `run_pet.bat`。常用参数：

- `--pet fox`：指定宠物目录名，默认读取 `desktop_pet.default_pet`。
- `--scale 0.5`：调整显示尺寸，默认读取 `desktop_pet.default_scale`。
- `--asset-url URL`：素材缺失时使用指定 ZIP 下载地址。
- `--no-download`：素材缺失时直接报错，不联网下载。
- `--check`：只检查素材和 spritesheet，不打开窗口。

交互行为：

- 鼠标靠近时，宠物会朝鼠标方向移动或转身。
- 左键点击会触发短暂 poke/wave 动画。
- 左键拖拽可以移动宠物，松手后记录位置。
- 右键菜单可暂停、重新加载素材或退出。

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
- 文本路径默认 `data/post/data/YYYY-MM-DD.txt`；文本路径旁的“打开草稿”会按当前输入框路径创建缺失草稿，并用 VS Code 打开。草稿路径必须位于 `data/post/data`。
- 图片路径默认 `data/post/images/xiushenlu-xhs-cover.png`，支持多行，每行一个本地绝对路径或 HTTP/HTTPS URL。
- 点击“生成封面”会读取文本路径对应的整篇草稿，调用 DashScope 图片模型生成 1 张 PNG，下载保存到 `data/post/images`，并用本地图片路径替换图片路径输入框。
- 点击“打开图片”会按图片路径逐项调用系统默认应用打开；本地文件必须存在，HTTP/HTTPS 图片会交给系统默认浏览器或处理器。
- 标题为必填；标签、定时发布和原创标记为可选；可见范围默认 `公开可见`。
- 发布按钮会真实调用 `publish_content`，前端确认框通过后后端固定按 `approve=true` 执行；发布前不再预检查登录状态，cookies 失效或未登录时由 MCP 发布接口返回错误。

命令行仍可作为备用入口：

```powershell
# 检查修身炉能否连上 MCP；不会确认小红书实时登录状态。
conda run --no-capture-output -n xiushenlu python app/main.py xhs status

# 先 dry-run：读取草稿、校验参数、写 post_publish_requested，不真实发布。
conda run --no-capture-output -n xiushenlu python app/main.py xhs publish --draft data/post/data/2026-05-12.txt --title "修身炉进展" --image "C:\path\to\image.png" --tag 修身炉

# 确认内容、图片和可见范围后，再追加 --approve 实发。
conda run --no-capture-output -n xiushenlu python app/main.py xhs publish --draft data/post/data/2026-05-12.txt --title "修身炉进展" --image "C:\path\to\image.png" --tag 修身炉 --approve
```

参数说明：

- `--draft`：必须指向 `data/post/data` 里的正文草稿。
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
| `python app/main.py plan --add "..."` | 是 | 追加一条今日待办，并把新增任务并入 daily 的待办原文和任务管理表。 |
| `python app/main.py log "..."` | 是 | 先追加一条过程记录，再让 LLM 返回补丁，由代码更新“任务管理”表的 `状态` 和 `用时` 两列；状态支持空、`○`、`✓`、`×`，其中 `×` 表示删除、取消或不再追踪；失败时保留记录。 |
| `python app/main.py review` | 是 | 根据今天 daily 在末尾生成一段表扬，滚动明日待办，并更新 token 统计；状态为 `×` 的任务不会滚动到明天。 |
| `python app/main.py review --date YYYY-MM-DD` | 是 | 对指定日期生成一段表扬，并默认把未完成项滚动到当前 `today_tasks.md`。历史日期不会把本次 token 统计写回历史 daily。 |
| `python app/main.py review --date YYYY-MM-DD --no-rollover` | 是 | 只对指定日期生成一段表扬，不滚动当前待办。 |
| `python app/main.py status` | 否 | 打印今天的 daily。 |
| `python app/main.py cost` | 否 | 汇总今日和本月 token，并覆盖 daily 的 token 统计区块。 |
| `python app/main.py xhs status` | 否 | 通过本地 `xiaohongshu-mcp` 检查 MCP 连通性和本地缓存状态。 |
| `python app/main.py xhs publish ...` | 否 | 从 `data/post/data` 草稿发布小红书图文；不带 `--approve` 只记录请求。 |
| `python app/main.py console` | 视操作而定 | 启动本地控制台，复用已有 pipeline 和本地读写能力。 |
| `python app/main.py pet` | 否 | 启动轻量桌面宠物；首次运行会按配置下载默认素材。 |
| `python app/main.py pet --check` | 否 | 检查桌宠素材与 spritesheet，不打开窗口。 |

`plan` 生成任务管理表时，`优先级` 和 `预计` 由 LLM 判断，但 `任务` 列会由程序尽量匹配并回填今日待办里的任务正文，避免把待办扩写成计划说明。任务管理固定拆成 `【目标】`、`【日常】`、`【xiushenlu维护】` 三个表；`【日常】` 来自今日待办的同名分组，项目修 bug、排障、维护和优化进入 `【xiushenlu维护】`，其余进入 `【目标】`。`【目标】` 和 `【日常】` 使用 `| 任务 | 优先级 | 预计 | 状态 | 用时 |`，`【xiushenlu维护】` 使用 `| 任务 | 优先级 | 状态 |`，不写 `预计` 和 `用时`。`plan --add` 要求模型返回严格 JSON，并逐字保留新增任务；程序会更新 `today_tasks.md`、替换 daily 里已有的“今日待办”原文，并把新增任务正文追加到对应任务管理表。解析失败时不会写入 `today_tasks.md` 或 daily。

`log` 的用时列是从 daily 记录派生出来的结果，不作为权威累计变量保存。每次写入记录后，程序会把当天“记录”小节重新交给 LLM 判断每个任务相关的记录 ID，再由代码重新计算总用时并覆盖可更新表格；`【xiushenlu维护】` 表只更新 `状态`，不会计算或写入用时。如果本次记录明显是新的修身炉 / xiushenlu 修 bug、修复、优化或维护任务，LLM 可以通过 `maintenance_additions` 请求新增一行，代码只把它追加到维护表，并要求证据来自记录原文。如果相关记录中出现明确写出的时长，例如“20分钟”“用时40m”“耗时1.5h”，只使用最后一次明确时长，不累加，也不再计算首尾时间差；如果没有明确时长，只有最后一条相关记录内容包含 `&` 时，才使用最后一条相关记录的 `HH:MM:SS` 减去第一条相关记录的 `HH:MM:SS`，否则用时留空。

任务管理表的备注功能已删除，目标/日常表头只接受 `| 任务 | 优先级 | 预计 | 状态 | 用时 |`；维护表头只接受 `| 任务 | 优先级 | 状态 |`。历史 daily 如果仍是旧 `备注` 表头，后续 `log` 不会自动更新任务表；需要人工或单独迁移为 `用时` 表头后再继续使用自动更新。

`review` 的事实来源是 daily 里固化的 `今日待办原文` 和 `记录`，不是当前 `today_tasks.md`。默认会滚动生成新的 `today_tasks.md`；需要只补历史表扬时显式加 `--no-rollover`。`明日计划.md` 只用于生成新的 `today_tasks.md`，不用于表扬内容。daily 末尾只写一段基于当天工作、自然真诚且不超过 100 字的表扬；如果已有旧 `## 复盘` 区块，本次成功生成后会移除旧复盘。解析失败时不会写表扬、不会覆盖 `today_tasks.md`，也不会清空 `明日计划.md`。

## 配置

主配置文件是 `config/app.yaml`。

| 配置段 | 含义 |
| --- | --- |
| `llm.provider` | 当前为 `dashscope`。 |
| `llm.model` | 当前默认 `qwen3.6-plus`。 |
| `llm.api_key_env` | 默认 `DASHSCOPE_API_KEY`，也可通过项目根目录 `.env` 加载。 |
| `assistant.system_prompt` | Provider 发送给模型的 system prompt。 |
| `paths.*` | daily、inbox、memory、logs、state、quarantine 等目录。 |
| `paths.task_tree_dir` | 工作树 JSON 保存目录，默认 `data/task_tree`。 |
| `paths.post_dir` | 小红书正文草稿目录，默认 `data/post/data`。 |
| `paths.post_image_dir` | 小红书默认图片目录，默认 `data/post/images`。 |
| `xiaohongshu.mcp_url` | 本地 `xiaohongshu-mcp` MCP 地址，默认 `http://localhost:18060/mcp`。 |
| `xiaohongshu.cover_model` | 小红书封面生成模型，默认 `qwen-image-2.0`。 |
| `xiaohongshu.mcp_exe` | 第三方 MCP release 主程序路径；控制台“打开/关闭 MCP”使用它。 |
| `xiaohongshu.login_exe` | 第三方登录工具路径；需要重新登录时可手动使用它。 |
| `xiaohongshu.working_dir` | 运行第三方 exe 的工作目录，通常是同级 `xiaohongshu-mcp`。 |
| `desktop_pet.asset_dir` | 桌宠素材和状态目录，默认 `data/desktop_pet`。 |
| `desktop_pet.default_pet` | 默认宠物目录名，当前为 `fox`。 |
| `desktop_pet.default_asset_url` | 默认宠物 ZIP 下载地址。 |
| `desktop_pet.default_scale` | 桌宠显示缩放比例。 |
| `desktop_pet.move_speed` | 鼠标吸引移动速度。 |
| `desktop_pet.attraction_radius` | 鼠标吸引半径，单位为像素。 |
| `safety.allowed_dirs` | 允许读写的数据目录白名单。 |
| `safety.protected_files` | 受保护文件，当前包含 `data/memory/goals.md`。 |

`app/llm/qwen_agent_impl.py` 仍保留为历史/备选实现，但 CLI 当前不使用它。

## 模块职责

| 文件 | 职责 |
| --- | --- |
| `app/main.py` | CLI 命令入口；加载配置；组装 Provider；调用 pipeline 或本地读写函数。 |
| `app/console.py` | FastAPI 本地控制台；展示 daily 和 today_tasks；触发已有 plan/log/review 能力，并提供工作树页面路由。 |
| `app/pipelines/daily_plan.py` | 今日计划 pipeline；LLM 生成任务管理表，程序回填 `任务` 列并拆分为目标、日常和 xiushenlu 维护三类。 |
| `app/pipelines/log_schedule_update.py` | 写入记录后的任务管理表更新 pipeline；LLM 返回状态和计时线索，代码重算并写入可更新表的 `状态` 和 `用时` 两列。 |
| `app/pipelines/plan_update.py` | 日内计划局部更新 pipeline；更新待办原文并把新增任务追加到对应任务管理表，解析失败时停止写入。 |
| `app/pipelines/nightly_review.py` | 晚间表扬 pipeline；当天生成成功后滚动待办并清空 `明日计划.md`。 |
| `app/posting/` | 小红书图文发布与封面生成适配：读取 `data/post/data` 草稿、校验参数、调用本地 MCP、调用 DashScope 图片模型并记录事件。 |
| `app/desktop_pet/` | 桌宠素材下载校验、spritesheet 切片、位置状态和 Tkinter 透明窗口。 |
| `app/task_tree.py` | 工作树 JSON 校验、标题文件名清洗和 `data/task_tree` 安全读写。 |
| `app/daily.py` | daily Markdown 路径、读取、区块替换和记录追加。 |
| `app/inbox.py` | `today_tasks.md` 和 `明日计划.md` 的读写封装。 |
| `app/logger.py` | 按日 JSON Lines 事件追加和读取。 |
| `app/cost.py` | 汇总本地 `llm_call` 事件统计 token，并汇总 `image_generation_usage` 事件统计文生图图片数。 |
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
- `data/desktop_pet/*`
- `data/task_tree/*.json`
- `data/quarantine/*`

主要运行文件：

- `data/user_inputs/today_tasks.md`：当前待办输入槽。
- `data/user_inputs/明日计划.md`：明日计划暂存；当天 `review` 成功滚动后清空。
- `data/task_tree/<标题>.json`：工作树页面保存的结构化拆分结果。
- `data/user_records/YYYY-MM-DD.md`：人类可读 daily。
- `data/system_logs/YYYY-MM-DD.jsonl`：机器可读事件流。

## 验证建议

### 长驻服务验证

验证网页、MCP、本地 API 或需要 dev server 的功能前，先检查现场，不要直接启动或重启服务：

```powershell
netstat -ano | Select-String -Pattern ":8765"
Get-Process -Name python,conda,node -ErrorAction SilentlyContinue | Select-Object Id,ProcessName,StartTime,Path
```

如果目标端口已有监听，先向用户说明端口、PID 和可能来源，并询问是复用、关闭重启还是换端口。未得到明确同意前，不关闭、不重启、不抢占端口。

优先使用会自动退出的验证方式：

- `conda run -n xiushenlu python -m unittest ...`
- FastAPI `TestClient`
- 静态 HTML / JS 字符串检查
- 对已存在服务的只读 API 请求

确实必须临时启动 `uvicorn`、dev server、watch 或 MCP 时，先说明端口，记录 PID，验证结束后清理。不要用普通同步 shell 调用直接运行长驻命令，也不要依赖 `timeout_ms` 防卡住；一次启动卡住后，停止同类重试，先向用户汇报。

如果测试、验证或检查命令卡住、被中断，或留下残留 `conda`/`python` 进程，先按 `AGENTS.md` 记录失败并清理本次残留进程，再分析最近改动里的循环、阻塞或进程等待点。修复明确原因后，如需重新验证，使用受控子进程方式，例如直接调用 xiushenlu 环境 Python，并用 `$p.WaitForExit(<毫秒>)` 判断是否结束；不要用 `Wait-Process` 的返回值判断完成状态，也不要继续换同类 `conda run + unittest` 命令试探。

不需要 LLM 的文档或本地逻辑改动至少跑：

```powershell
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

conda run -n xiushenlu python -m compileall app
conda run -n xiushenlu python -m unittest tests.test_desktop_pet
conda run --no-capture-output -n xiushenlu python app/main.py pet --check
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
