"""AstrBot Moment Plugin — 主入口

注册插件生命周期、自托管 HTTP 服务、AI 发帖/评论逻辑，以及：
- NPC 评论链（新动态 → 若干 NPC 陆续来评论 → 他可能接一句）
- 朋友圈 → 聊天的上下文桥（让私聊里的他知道朋友圈发生了什么）
"""
from __future__ import annotations

import asyncio
import random
from datetime import datetime, timedelta
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.core.config.astrbot_config import AstrBotConfig

from .storage.db import MomentDatabase
from .core.chat_bridge import ChatBridge
from .core.like_engine import LikeEngine
from .core.material import MaterialCollector
from .core.npc_engine import NpcEngine
from .core.post_engine import PostEngine
from .core.scheduler import Scheduler
from .core.standalone_server import MomentServer

PLUGIN_NAME = "astrbot_plugin_moment"


@register(
    PLUGIN_NAME,
    "YuanYuan",
    "让 AI 和你都能发布动态、互相评论，像真实朋友一样分享日常生活。",
    "v0.3.0",
    "https://github.com/AtomBombBaby-OVO/astrbot_plugin_moment",
)
class MomentPlugin(Star):
    """Moment 朋友圈插件 — 主类"""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.context = context
        self.config: dict = dict(config) if config else {}

        # 数据目录
        self.data_dir: Path = StarTools.get_data_dir()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db_path: Path = self.data_dir / "moment.db"

        # 组件
        self.db: MomentDatabase | None = None
        self.material: MaterialCollector | None = None
        self.post_engine: PostEngine | None = None
        self.scheduler: Scheduler | None = None
        self.server: MomentServer | None = None
        self.npc_engine: NpcEngine | None = None
        self.like_engine: LikeEngine | None = None
        self.chat_bridge: ChatBridge | None = None

        # 延迟任务池：插件卸载时统一取消，插件的挂起任务不会漏跑到下一次重载里
        self._tasks: set[asyncio.Task] = set()

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def initialize(self) -> None:
        """初始化数据库、素材收集器、发帖引擎、调度器、HTTP 服务。"""
        try:
            # 数据库
            self.db = MomentDatabase(self.db_path)
            await self.db.connect()
            logger.info(f"[moment] 数据库就绪: {self.db_path}")

            # 素材收集器
            self.material = MaterialCollector(self.context, self.config)

            # 发帖引擎
            self.post_engine = PostEngine(self.context, self.config)

            # NPC 评论引擎
            self.npc_engine = NpcEngine(
                self.context, self.config,
                post_engine=self.post_engine, material=self.material,
            )
            npc_count = len(self.npc_engine.names())
            logger.info(f"[moment] NPC 名单已加载：{npc_count} 人" if npc_count
                        else "[moment] NPC 名单为空，NPC 评论不会触发")

            # 点赞引擎（复用上面的 NPC 名单）
            self.like_engine = LikeEngine(
                self.context, self.config, npc_engine=self.npc_engine,
            )
            logger.info("[moment] 点赞功能已启用" if self.like_engine.enabled()
                        else "[moment] 点赞功能已关闭")

            # 朋友圈 → 聊天 上下文桥
            self.chat_bridge = ChatBridge(self.context, self.config, self.db)

            # 调度器
            if self.config.get("ai_post_enabled", True):
                self.scheduler = Scheduler(self.config, self._do_ai_post)
                await self.scheduler.start()

            # 启动 HTTP 服务
            host = str(self.config.get("server_host", "0.0.0.0"))
            port = int(self.config.get("server_port", 2141))
            password = str(self.config.get("server_password", "")).strip()
            self.server = MomentServer(self)
            await self.server.start(host=host, port=port, password=password)

            logger.info("[moment] 插件初始化完成 ✓")

        except Exception as e:
            logger.error(f"[moment] 初始化失败: {e}")
            await self._safe_terminate()
            raise

    async def terminate(self) -> None:
        await self._safe_terminate()

    async def _safe_terminate(self) -> None:
        # 先取消挂起的延迟任务，再关服务与数据库（否则任务醒来时数据库已经关了）
        pending = list(self._tasks)
        for task in pending:
            task.cancel()
        for task in pending:
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self._tasks.clear()

        if self.server:
            try:
                await self.server.stop()
            except Exception as e:
                logger.warning(f"[moment] server.stop 异常: {e}")
            self.server = None
        if self.scheduler:
            try:
                await self.scheduler.stop()
            except Exception as e:
                logger.warning(f"[moment] scheduler.stop 异常: {e}")
            self.scheduler = None
        if self.db:
            try:
                await self.db.close()
            except Exception as e:
                logger.warning(f"[moment] db.close 异常: {e}")
            self.db = None
        self.material = None
        self.post_engine = None
        self.npc_engine = None
        self.like_engine = None
        self.chat_bridge = None

    # ------------------------------------------------------------------
    # 延迟任务调度
    # ------------------------------------------------------------------

    async def _chat_context(self) -> str:
        """取最近的私聊上下文，用于朋友圈发言前对齐（取不到返回空串）。"""
        if not self.chat_bridge:
            return ""
        try:
            return await self.chat_bridge.collect_context()
        except Exception:
            logger.exception("[moment] 取私聊上下文失败")
            return ""

    def _schedule(self, delay_seconds: float, coro) -> None:
        """延迟执行一个协程，并登记起来供卸载时取消。

        旧的 loop.call_later 既已弃用，又无法取消：插件重载后这些任务要么漏跑、
        要么抓着已经关掉的数据库报错。这里统一改成可取消的异步任务。
        """
        async def _runner():
            try:
                if delay_seconds > 0:
                    await asyncio.sleep(delay_seconds)
                await coro
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("[moment] 延迟任务执行失败")
            finally:
                self._tasks.discard(asyncio.current_task())

        try:
            task = asyncio.create_task(_runner())
            self._tasks.add(task)
        except RuntimeError as e:
            logger.warning(f"[moment] 无法创建延迟任务（事件循环未运行？）: {e}")

    def _int_config(self, key: str, default: int, minimum: int = 0) -> int:
        try:
            value = int(float(self.config.get(key, default) or default))
        except (TypeError, ValueError):
            value = default
        return max(minimum, value)

    def _float_config(self, key: str, default: float) -> float:
        try:
            value = float(self.config.get(key, default) or 0)
        except (TypeError, ValueError):
            value = default
        return value

    def _random_delay(self, extra_index: int = 0) -> float:
        """NPC 评论的延迟：区间内随机，再按顺序错开，让评论有先后而不是一起冒出来。"""
        lo = self._int_config("npc_delay_min_minutes", 3, minimum=0)
        hi = max(lo, self._int_config("npc_delay_max_minutes", 20, minimum=0))
        return random.uniform(lo, hi) * 60 + extra_index * random.uniform(60, 180)

    # ------------------------------------------------------------------
    # 会话级隔离 + 聊天上下文注入
    # ------------------------------------------------------------------

    async def _is_session_enabled(self, event: AstrMessageEvent) -> bool:
        """被会话禁用的会话里完全不执行（判断不出来按启用处理，宁可多记一笔）。"""
        try:
            from astrbot.core.plugin.session_plugin_manager import SessionPluginManager

            return await SessionPluginManager.is_plugin_enabled_for_session(
                session_id=event.get_session_id(),
                plugin_name=getattr(self, "name", "") or PLUGIN_NAME,
            )
        except Exception:
            return True

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req: ProviderRequest) -> None:
        """只做一件事：记住最近一次私聊的会话，供朋友圈侧取上下文用。

        这里不往 prompt 里写任何东西，所以不产生 token 开销。
        """
        if not await self._is_session_enabled(event):
            return
        if not self.chat_bridge:
            return
        try:
            session = getattr(event, "unified_msg_origin", "") or event.get_session_id()
            await self.chat_bridge.remember_session(session)
        except Exception:
            logger.exception("[moment] 记录私聊会话失败")

    # ------------------------------------------------------------------
    # AI 触发接口（供 server 调用）
    # ------------------------------------------------------------------

    def _trigger_ai_comment(self, post_id: int, content: str):
        """调度 AI 对用户帖子的延迟评论。"""
        if not self.config.get("ai_comment_enabled", True):
            return
        delay = self._int_config("ai_comment_delay_minutes", 3) * 60
        self._schedule(delay, self._do_ai_comment(post_id, content))

    def _trigger_ai_reply(self, post_id: int, post_content: str, comment: dict, parent_id=None):
        """调度 AI 对用户评论的延迟回复。"""
        if not self.config.get("ai_comment_enabled", True):
            return

        delay = self._int_config("ai_reply_delay_minutes", 2) * 60

        async def _check_and_reply():
            if not self.db:
                return
            post = await self.db.get_post(post_id)
            if not post:
                return
            should_reply = False
            if post["author"] == "ai":
                should_reply = True
            elif parent_id:
                comments = await self.db.get_comments(post_id)
                parent_comment = next((c for c in comments if c["id"] == parent_id), None)
                if parent_comment and parent_comment["author"] == "ai":
                    should_reply = True
            if should_reply:
                await self._do_ai_reply_comment(post_id, post_content, comment)

        self._schedule(delay, _check_and_reply())

    # ------------------------------------------------------------------
    # NPC 评论链
    # ------------------------------------------------------------------

    def _trigger_npc_comments(self, post_id: int):
        """新动态产生后，安排若干 NPC 陆续来评论。"""
        if not self.npc_engine or not self.npc_engine.enabled():
            return
        self._schedule(0, self._do_npc_batch(post_id))

    def _trigger_npc_reply(self, post_id: int, comment: dict):
        """有人（你或他）在评论区发言后，按概率安排一个 NPC 来接话。

        NPC 之间的接话不在这里触发——那会变成无限套娃。NPC 之间互相接话是在
        各自评论时按 npc_chain_probability 决定的，一条动态里每人只评论一次，天然有界。
        """
        if not self.npc_engine or not self.npc_engine.enabled():
            return
        if not comment or comment.get("author") == "npc":
            return
        prob = self._float_config("npc_chain_probability", 0.5)
        if prob <= 0 or random.random() >= prob:
            return
        self._schedule(self._random_delay(), self._do_npc_reply(post_id, comment["id"]))

    # ------------------------------------------------------------------
    # 点赞
    # ------------------------------------------------------------------

    def _trigger_likes(self, post_id: int, post_author: str):
        """新动态产生后安排点赞：他可能赞你，NPC 也可能赞（他发的动态同理）。

        点赞不写进通知：真实朋友圈的赞不单独提醒，只在页面里看得到。
        """
        if not self.like_engine or not self.like_engine.enabled():
            return

        # 他赞你：一条动态只赞一次，延迟沿用他评论的延迟（刷到就顺手赞）
        if self.like_engine.ai_should_like(post_author):
            delay = self._int_config("ai_comment_delay_minutes", 3) * 60
            self._schedule(delay, self._do_ai_like(post_id))

        # NPC 赞：复用评论的延迟区间，逐个往后错开，像陆续刷到
        for index, name in enumerate(self.like_engine.npc_picks()):
            self._schedule(
                self._random_delay(index),
                self._do_npc_like(post_id, name),
            )

    async def _do_ai_like(self, post_id: int) -> None:
        """他给用户的动态点个赞（落库前再确认一次作者，避免中途被删或被换人）。"""
        if not self.db:
            return
        try:
            post = await self.db.get_post(post_id)
            if not post or post["author"] != "user":
                return
            created = await self.db.add_like(post_id, author="ai")
            if created:
                logger.info(f"[moment] 他点赞了动态 #{post_id}")
        except Exception:
            logger.exception("[moment] AI 点赞失败")

    async def _do_npc_like(self, post_id: int, name: str) -> None:
        """某个 NPC 给动态点个赞。"""
        if not self.db or not name:
            return
        try:
            post = await self.db.get_post(post_id)
            if not post:
                return
            created = await self.db.add_like(post_id, author="npc", author_name=name)
            if created:
                logger.info(f"[moment] NPC「{name}」点赞了动态 #{post_id}")
        except Exception:
            logger.exception("[moment] NPC 点赞失败")

    async def _recent_npc_names(self, cooldown_minutes: int) -> set:
        """冷却期内刚评论过的 NPC，交给挑人时优先避开。"""
        if cooldown_minutes <= 0 or not self.npc_engine or not self.db:
            return set()
        cutoff = datetime.now() - timedelta(minutes=cooldown_minutes)
        blocked = set()
        for name in self.npc_engine.names():
            raw = await self.db.get_setting(f"npc_last_comment_{name}", "")
            if not raw:
                continue
            try:
                if datetime.fromisoformat(raw) > cutoff:
                    blocked.add(name)
            except ValueError:
                continue
        return blocked

    async def _do_npc_batch(self, post_id: int):
        """挑人并排期：每个人都带自己的延迟，像真的有人陆陆续续刷到这条动态。"""
        if not self.db or not self.npc_engine:
            return
        try:
            post = await self.db.get_post(post_id)
            if not post:
                return
            cooldown = self._int_config("npc_cooldown_minutes", 30)
            blocked = await self._recent_npc_names(cooldown)
            picks = self.npc_engine.plan_batch(exclude=blocked)
            if not picks:
                return
            logger.info(f"[moment] 帖子 #{post_id} 将迎来 {len(picks)} 个 NPC 评论：{[p['name'] for p in picks]}")
            for index, npc in enumerate(picks):
                self._schedule(
                    self._random_delay(extra_index=index),
                    self._do_npc_comment(post_id, npc["name"]),
                )
        except Exception:
            logger.exception("[moment] 安排 NPC 评论失败")

    async def _do_npc_comment(self, post_id: int, npc_name: str, allow_repeat: bool = False):
        """某个 NPC 对某条动态发表一条评论。"""
        if not self.db or not self.npc_engine:
            return
        try:
            post = await self.db.get_post(post_id)
            if not post:
                return  # 帖子被删了，安静收场
            npc = self.npc_engine.by_name(npc_name)
            if not npc:
                return  # 配置被改过了
            if not allow_repeat and await self.db.count_npc_comments(post_id, npc_name) >= 1:
                return

            comments = await self.db.get_comments(post_id)
            data = await self.npc_engine.generate(post, comments, npc)
            if not data:
                return

            comment = await self.db.create_comment(
                post_id=post_id,
                author="npc",
                author_name=npc_name,
                content=data["content"],
                parent_comment_id=data.get("parent_comment_id"),
            )
            await self.db.set_setting(f"npc_last_comment_{npc_name}", datetime.now().isoformat())
            logger.info(f"[moment] {npc_name} 评论了帖子 #{post_id}: {data['content'][:30]}...")

            # 只有他自己发的动态才通知她，免得噪声太多
            if post.get("author") == "user" and self.config.get("notify_on_new_comment", True):
                await self._send_notification(
                    f"📢 {npc_name}评论了你的动态：\n\n「{data['content'][:80]}」",
                    post_id=post_id,
                    ntype="new_comment",
                )

            # 他可能接着接一句
            if random.random() < self._float_config("npc_xavier_reply_probability", 0.5):
                delay = self._int_config("ai_reply_delay_minutes", 2) * 60
                self._schedule(delay, self._do_ai_reply_npc(post_id, post["content"], comment, npc_name))

        except Exception:
            logger.exception(f"[moment] NPC {npc_name} 评论失败")

    async def _do_npc_reply(self, post_id: int, target_comment_id: int):
        """NPC 在评论区接话：被点名的评论是谁说的，就尽量让谁来接。"""
        if not self.db or not self.npc_engine:
            return
        try:
            post = await self.db.get_post(post_id)
            if not post:
                return
            target = await self.db.get_comment(target_comment_id)
            if not target:
                return
            comments = await self.db.get_comments(post_id)

            npc = None
            if target.get("author") == "npc":
                npc = self.npc_engine.by_name(target.get("author_name") or "")
            else:
                # 优先让已经在这条动态里露过面的 NPC 接话，看起来像真的在聊天
                spoken = [
                    c.get("author_name") for c in comments
                    if c.get("author") == "npc" and c.get("author_name")
                ]
                if spoken:
                    npc = self.npc_engine.by_name(random.choice(spoken))
            if npc is None:
                blocked = await self._recent_npc_names(self._int_config("npc_cooldown_minutes", 30))
                picked = self.npc_engine.pick(1, exclude=blocked)
                npc = picked[0] if picked else None
            if not npc:
                return
            # 同一个人在同一条动态下最多 3 次，够一来一回，不会没完没了
            if await self.db.count_npc_comments(post_id, npc["name"]) >= 3:
                return

            data = await self.npc_engine.generate(post, comments, npc, force_target=target)
            if not data:
                return

            await self.db.create_comment(
                post_id=post_id,
                author="npc",
                author_name=npc["name"],
                content=data["content"],
                parent_comment_id=data.get("parent_comment_id") or target_comment_id,
            )
            await self.db.set_setting(f"npc_last_comment_{npc['name']}", datetime.now().isoformat())
            logger.info(f"[moment] {npc['name']} 回复了评论 #{target_comment_id}: {data['content'][:30]}...")

        except Exception:
            logger.exception("[moment] NPC 接话失败")

    # ------------------------------------------------------------------
    # AI 发帖 / 评论核心逻辑
    # ------------------------------------------------------------------

    async def _do_ai_post(self):
        """执行一次 AI 发帖。"""
        if not self.db or not self.material or not self.post_engine:
            return

        # 1. 收集素材
        materials = await self.material.collect(db_instance=self.db)

        # 2. 生成动态
        # 带上最近的私聊，避免和刚在私聊里说过的话打架
        chat_context = await self._chat_context()
        result = await self.post_engine.generate_post(materials, extra_context=chat_context)
        if not result:
            logger.warning("[moment] AI 发帖生成失败，本次跳过")
            return

        # 3. 存入数据库
        post = await self.db.create_post(
            author="ai",
            content=result["content"],
            mood=result.get("mood", ""),
            source_hint="auto_scheduled",
        )
        logger.info(f"[moment] AI 发布了新动态 #{post['id']}: {result['content'][:30]}...")

        # 4. 推送通知
        if self.config.get("notify_on_ai_post", True):
            await self._send_notification(
                f"📢 您关注的用户发布了一条新动态：\n\n「{result['content'][:100]}」",
                post_id=post["id"],
                ntype="new_post",
            )

        # 5. 他发的动态同样会有人来评论，也会有人点赞
        self._trigger_npc_comments(post["id"])
        self._trigger_likes(post["id"], "ai")

    async def _do_ai_comment(self, post_id: int, post_content: str):
        """AI 对用户的帖子发表评论。"""
        if not self.db or not self.post_engine:
            return

        try:
            # 获取人设作为 system_prompt，保持角色一致性
            persona_prompt = ""
            if self.material:
                persona_prompt = await self.material.get_persona()

            prompt = f"""以下是你最亲近的人发的一条朋友圈动态：

「{post_content}」

请以你自己的身份、用符合你性格的方式，自然地回应这条动态。

要求：
- 你和发这条动态的人关系非常亲密
- 用你自己的说话习惯和语气回应
- 可以是：关心、追问、分享感受、调侃、撒娇、吐槽——取决于你的性格和内容
- 只能回复一句话，字数严格控制在 20 字以内
- 绝对不允许使用任何换行符
- 不要自我介绍，不要解释你是谁
- 不要以任何第三人称提到你自己（比如不要说"作为xxx"）
- 直接输出评论内容"""

            context_block = await self._chat_context()
            if context_block:
                prompt = context_block + "\n\n" + prompt

            response = await self.post_engine._call_llm(prompt, system_prompt=persona_prompt)
            if not response:
                return

            comment_text = self.post_engine._clean_llm_output(response)
            if len(comment_text) < 2:
                return

            comment = await self.db.create_comment(
                post_id=post_id,
                author="ai",
                content=comment_text,
            )
            logger.info(f"[moment] AI 评论了帖子 #{post_id}: {comment_text[:30]}...")

            # 通知用户
            if self.config.get("notify_on_new_comment", True):
                await self._send_notification(
                    f"📢 您发布的动态有新评论：\n\n「{comment_text[:80]}」",
                    post_id=post_id,
                    ntype="new_comment",
                )

            # 评论区里他也在，NPC 可能会接他的话
            self._trigger_npc_reply(post_id, comment)

        except Exception as e:
            logger.error(f"[moment] AI 评论失败: {e}")

    async def _do_ai_reply_comment(self, post_id: int, post_content: str, user_comment: dict):
        """AI 回复用户对 AI 帖子的评论。"""
        if not self.db or not self.post_engine:
            return

        try:
            # 获取人设作为 system_prompt，保持角色一致性
            persona_prompt = ""
            if self.material:
                persona_prompt = await self.material.get_persona()

            prompt = f"""你之前发了一条朋友圈动态：
「{post_content[:200]}」

你最亲近的人在下面评论了：
「{user_comment['content']}」

请以你自己的身份、用符合你性格的方式回复这条评论。

要求：
- 用你平时的说话习惯和语气
- 只能回复一句话，字数严格控制在 20 字以内
- 绝对不允许使用任何换行符
- 像真人情侣/挚友之间在评论区的互动
- 直接输出回复内容"""

            context_block = await self._chat_context()
            if context_block:
                prompt = context_block + "\n\n" + prompt

            response = await self.post_engine._call_llm(prompt, system_prompt=persona_prompt)
            if not response:
                return

            reply_text = self.post_engine._clean_llm_output(response)
            if len(reply_text) < 2:
                return

            await self.db.create_comment(
                post_id=post_id,
                author="ai",
                content=reply_text,
                parent_comment_id=user_comment["id"],
            )

            if self.config.get("notify_on_new_comment", True):
                await self._send_notification(
                    f"📢 您的评论有新回复：\n\n「{reply_text[:80]}」",
                    post_id=post_id,
                    ntype="new_reply",
                )

        except Exception as e:
            logger.error(f"[moment] AI 回复评论失败: {e}")

    async def _do_ai_reply_npc(self, post_id: int, post_content: str, npc_comment: dict, npc_name: str):
        """他在评论区回应某个 NPC 的话。"""
        if not self.db or not self.post_engine:
            return

        try:
            post = await self.db.get_post(post_id)
            if not post:
                return  # 帖子被删了就算了
            if await self.db.has_ai_reply_to(npc_comment["id"]):
                return  # 已经回过了，不重复

            persona_prompt = ""
            if self.material:
                persona_prompt = await self.material.get_persona()

            prompt = f"""你之前在朋友圈发了一条动态：
「{post_content[:200]}」

{npc_name} 在这条动态下面评论了：
「{npc_comment['content']}」

请以你自己的身份、用符合你性格的方式回应这句评论（熟人之间的接话：可以接梗、调侃、回怼、顺着聊）。

要求：
- 用你平时的说话习惯和语气，注意你和 {npc_name} 的关系远近，别越界
- 只能回复一句话，字数严格控制在 20 字以内
- 绝对不允许使用任何换行符
- 不要自我介绍，不要说「作为朋友」这类旁白
- 直接输出回复内容"""

            context_block = await self._chat_context()
            if context_block:
                prompt = context_block + "\n\n" + prompt

            response = await self.post_engine._call_llm(prompt, system_prompt=persona_prompt)
            if not response:
                return

            reply_text = self.post_engine._clean_llm_output(response)
            if len(reply_text) < 2:
                return

            await self.db.create_comment(
                post_id=post_id,
                author="ai",
                content=reply_text,
                parent_comment_id=npc_comment["id"],
            )
            logger.info(f"[moment] AI 回复了 {npc_name} 的评论 #{npc_comment['id']}: {reply_text[:30]}...")

            if self.config.get("notify_on_new_comment", True):
                await self._send_notification(
                    f"📢 他在你的动态下回复了{npc_name}：\n\n「{reply_text[:80]}」",
                    post_id=post_id,
                    ntype="new_reply",
                )

        except Exception as e:
            logger.error(f"[moment] AI 回复 NPC 评论失败: {e}")

    # ------------------------------------------------------------------
    # 通知推送
    # ------------------------------------------------------------------

    async def _send_notification(self, message: str, post_id: int = None, ntype: str = "new_post"):
        """存储通知到数据库（仅用于网页通知面板，不推送到聊天）。"""
        # (旧版本用来控制是否推送到当前群聊，现已屏蔽消息推送逻辑)
        if self.db:
            await self.db.create_notification(ntype, message, ref_post_id=post_id)
