# 更新日志

## v0.4.1

### 修复
- _conf_schema.json 文件带 UTF-8 BOM：AstrBot 加载插件时用 json.loads 解析 schema，首行首列即抛
  JSONDecodeError，导致插件加载失败、网页端服务不启动（重载失败的表现即为此）。现以无 BOM 的 UTF-8 重写。
- 顺带清除 data/cmd_config.json 的同类 BOM，避免后续读写踩同一个坑。
- metadata.yaml 版本号 v0.4.0 → v0.4.1。

### 原因
v0.4.0 保存 schema 文件时带上了 BOM，重载即失败；代码本身无误，仅文件编码问题。

## v0.4.0

* 新增：聊天上下文桥（单向，聊天 → 朋友圈）。他发帖、评论、回复之前，会先取一眼你们最近的私聊，「刚在回家路上却说自己刚睡醒」这类前后矛盾会明显减少。
* 新增配置：chat_bridge_enabled、chat_bridge_max_messages、chat_bridge_max_chars、chat_bridge_stale_minutes、chat_bridge_session。
* 移除：旧的「朋友圈 → 聊天」注入（moment_bridge_enabled、moment_bridge_max_events、moment_bridge_expire_hours）及 core/bridge.py。旧方向每轮对话都要注入，成本高且并非必要；需要他知道朋友圈近况时，直接把截图发给他即可。
* 明确：NPC 始终不会读取你们的私聊内容；朋友圈的通知推送不受本次改动影响。
* 原因：朋友圈侧的生成此前完全看不到私聊，导致称呼、位置、时间等指代错位；改为只在他发言时取一次上下文，开销小且针对性更强。

## v0.3.0

### 新增
- 点赞：数据库新增 likes 表与增删查接口（storage/db.py），同一人对同一条动态只能赞一次（唯一索引保证）。
- core/like_engine.py（新文件）：他从你的动态点赞、NPC 陆续来赞的人员挑选与延迟排期，复用现有 NPC 名单与概率配置，不额外配置。
- 接口 POST /api/likes/toggle；/api/posts 返回时给每条动态带上点赞汇总（谁赞了、赞数、我赞没赞）。
- 网页端每条动态底部「赞」按钮，点亮后再点取消；下方显示「××、×× 觉得很赞」。点赞不推送聊天通知。
- 删除自己的评论：新增接口 POST /api/comments/delete，只允许删除 author='user' 的评论，他发的与 NPC 发的一律拒绝。
- 网页端自己发的评论右侧「删除」按钮，二次确认后消失，评论数同步 -1。
- 配置面板新增 4 项：like_enabled（点赞总开关）、ai_like_probability、npc_like_probability、npc_like_count_max。
- 单元测试：点赞去重、删除评论权限判断。

### 修改
- /api/config 增加返回 like_enabled；网页端据此决定是否渲染点赞按钮与「觉得很赞」名单，关掉开关不会留下点了没反应的按钮。
- metadata.yaml 版本号 v0.2.0 → v0.3.0。

### 修复
- 网页端表情数据不随动态列表返回，刷新后表情消失（数据其实一直在库里），现随列表一并返回。
- 删除 _trigger_likes 末尾一行残留的 `del recent`：该变量在函数内并未定义，会让点赞排期结束后抛出 NameError，
  导致网页端发帖接口返回失败（动态其实已经入库，点赞与 NPC 评论也已排期），并让 AI 自动发帖的收尾流程中断。

### 原因
用户反馈：发出的评论写错了想撤回；朋友圈只有评论显得冷清，希望有点赞。

## v0.2.0

### 新增
- NPC 评论体系（core/npc_engine.py）：你和他发的每条动态都会按概率随机来 1~3 个 NPC 评论；
  NPC 之间有接话概率，他也有概率回应 NPC 的评论。名单与人设来自配置面板，代码里不写死角色。
- 朋友圈 → 聊天上下文桥（core/bridge.py）：在 on_llm_request 里把朋友圈的新动态、新评论
  作为背景资料软注入本轮对话，同一批事件只注入一次，有条数上限与过期时间，可整体关闭。
- 配置面板新增：NPC 评论开关与名单、每条动态有人评论的概率、接话概率、他回应 NPC 的概率、
  朋友圈近况注入开关；高级区新增数量范围、延迟区间、同一 NPC 冷却、评论字数上限、注入条数与过期时长。
- 数据库新增 comments.author_name 列（自动迁移），用于记录 NPC 昵称。
- 数据库新增 get_activity_since / get_comment / count_npc_comments / has_ai_reply_to 接口。
- 单元测试 tests/test_npc_engine.py、tests/test_bridge.py（名单解析、挑人、模型输出解析、事件描述）。

### 修改
- 网页端帖子操作栏显示评论数（💬 评论 · 3），展开评论区时显示「共 N 条评论」，发完评论本地计数 +1。
- 网页端支持回复某条评论：每条评论右侧「回复」按钮，输入框上方显示「回复 @昵称」，可取消。
- 「回复」关系渲染改为通用逻辑：任何有 parent_comment_id 的评论都会显示「回复 @被回复者」。
- NPC 昵称在网页端使用区别色（灰蓝）。
- 延迟评论/回复从 asyncio.call_later 改为可取消的异步任务池，插件卸载时统一取消。
- /api/config 增加返回 NPC 名单（仅名字）。
- 移除 initialize 里的临时调试日志。

### 修复
- 网页端看不到评论数量。
- 网页端无法回复他人（含 NPC、他）的评论。
- 朋友圈里发生的事不会进入私聊上下文，聊天与朋友圈像两个世界。

### 原因
用户反馈：评论区只有两个人太冷清；网页端缺少评论数量与回复能力；
朋友圈与聊天互不相通，角色在私聊里不知道朋友圈发生了什么。

## v0.1.0
- 初始版本：自动发帖、AI 评论与回复、网页端时间线、表情、通知、密码登录等。
