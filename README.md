# 修身炉

修身炉是一个本地优先的知识成长助手。

目标是和我一起消化知识、积累资料、运用知识，并把这些过程变成可追踪、可复用、可继续升级的个人知识系统。

```text
资料进入 -> 消化整理 -> 知识沉淀 -> 运用反馈 -> 再次升级
```

当前版本已经具备基础 daily 闭环：今日计划、过程记录、日内更新、晚间复盘、待办滚动、本地控制台和 token 统计。后续所有 pipeline、post、工具、通知、自动化和审批能力，都会围绕“积累资料并用回知识”这条主线展开。

小红书图文发布采用轻量外部适配：第三方 `xiaohongshu-mcp` 独立放在本项目同级目录运行，修身炉只读取 `post/data/` 草稿、做发布前校验和审批门禁，并通过本地 MCP 地址调用发布工具。

## Demo 流程

日常先双击 `run_main.bat` 打开本地控制台，然后按一天的节奏使用：

1. 写今日待办：把今天要做的事放进输入槽，作为当天资料和行动的起点。
2. 生成计划：助手结合长期目标，生成统一格式的今日待办和带优先级的“任务管理”表。
3. 记录过程：执行中随手写下进展、想法、卡点和临时材料；系统会在保留原始记录后，让 LLM 判断“任务管理”表中已有任务的状态和备注是否需要更新。状态支持空、`○`、`✓`、`×`，其中 `×` 表示删除、取消或不再追踪，晚间复盘不会把它滚动到第二天。
4. 日内调整：临时新增任务时，局部更新今日待办和“任务管理”表，不重写整份 daily；今日待办统一写成 `【分组】` 加编号列表。
5. 查看 daily：随时回看今天的计划、记录、复盘和 token 统计。
6. 晚间复盘：根据当天任务快照和过程记录生成复盘，只从计划内未完成任务和显式明日计划滚动待办；过程记录只作为证据和分析来源，不派生新任务。
7. 统计消耗：汇总本地 LLM token 使用情况，避免后台调用失控。

小红书发布分两步：先在同级目录启动第三方 `xiaohongshu-mcp`，再回到修身炉调用发布命令。

```powershell
# 终端 1：启动第三方 MCP。第一次使用前先人工登录。
cd C:\Users\Ua Pan\Desktop\project\xiaohongshu-mcp
go run cmd/login/main.go
go run . -headless=false

# 终端 2：回到修身炉检查登录状态。
cd C:\Users\Ua Pan\Desktop\project\xiushenlu
conda run --no-capture-output -n xiushenlu python app/main.py xhs status

# 先 dry-run：只记录发布请求，不真实发布。
conda run --no-capture-output -n xiushenlu python app/main.py xhs publish --draft post/data/YYYY-MM-DD.txt --title "标题" --image "C:\path\to\image.png" --tag 修身炉

# 确认后实发：追加 --approve。
conda run --no-capture-output -n xiushenlu python app/main.py xhs publish --draft post/data/YYYY-MM-DD.txt --title "标题" --image "C:\path\to\image.png" --tag 修身炉 --approve
```

`post/data/YYYY-MM-DD.txt` 是正文草稿；标题用 `--title` 传入，图片用 `--image` 传入本地绝对路径或 HTTP/HTTPS URL。默认可见范围是 `仅自己可见`。

## Milestone

### 1. 日常资料记录

先让资料稳定进入系统。现有 daily、计划、过程记录、复盘、post 草稿、链接、文本、文件摘要和 agent 协作记录都属于这一阶段。

目标不是自动读取一切，而是让用户明确提供的资料能被稳定记录、回看和追溯。

### 2. 知识库建立

把资料整理为主题、来源、时间、核心观点、行动项、项目启发和复用经验。

`docs/吸纳/` 承接外部资料消化，`docs/执行/` 承接项目过程和复盘，`docs/规划/` 承接目标、路线和能力边界。资料量不足时先保持 Markdown 和清晰索引，不急着引入复杂向量库。

### 3. 自动维护和升级知识

让助手基于已有资料生成周复盘、主题索引、知识缺口、过期提醒、workflow 草稿、skill 草稿和工具改进建议。

所有权威记忆、规划修改、对外发布和代码修改都先生成草稿或待审批动作，不自动越权执行。

## 技术细节

日常运行命令、控制台说明、配置、模块职责、数据规则和验证建议见 [TECHNICAL.md](TECHNICAL.md)。

最新规划见 [docs/规划/2026-05-08_修身炉知识成长助手规划.md](docs/规划/2026-05-08_修身炉知识成长助手规划.md)。
