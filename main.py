"""AstrBot Moment Plugin — 主入口

注册插件生命周期、自托管 HTTP 服务、AI 发帖/评论逻辑。
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from astrbot.api import logger
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.core.config.astrbot_config import AstrBotConfig

from .storage.db import MomentDatabase
from .core.material import MaterialCollector
from .core.post_engine import PostEngine
from .core.scheduler import Scheduler
from .core.standalone_server import MomentServer


@register(
    "astrbot_plugin_moment",
    "YuanYuan",
    "让 AI 和你都能发布动态、互相评论，像真实朋友一样分享日常生活。",
    "v0.1.0",
    "https://github.com/your-repo/astrbot_plugin_moment",
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

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def initialize(self) -> None:
        """初始化数据库、素材收集器、发帖引擎、调度器、HTTP 服务。"""
        try:
            # ===== 临时调试代码 =====
            import inspect
            persona_mgr = getattr(self.context, "persona_mgr", None)
            if persona_mgr:
                logger.info(f"[moment-debug] persona_mgr exists! Methods: {[m for m in dir(persona_mgr) if not m.startswith('_')]}")
                if hasattr(persona_mgr, "personas"):
                    personas = persona_mgr.personas
                    logger.info(f"[moment-debug] personas type: {type(personas)}")
                    if isinstance(personas, dict):
                        logger.info(f"[moment-debug] personas keys: {list(personas.keys())}")
                        # 尝试打印第一个人设的所有属性
                        if personas:
                            first_key = list(personas.keys())[0]
                            first_p = personas[first_key]
                            logger.info(f"[moment-debug] First persona type: {type(first_p)}")
                            if hasattr(first_p, '__dict__'):
                                logger.info(f"[moment-debug] Persona attrs: {list(first_p.__dict__.keys())}")
                            elif isinstance(first_p, dict):
                                logger.info(f"[moment-debug] Persona keys: {list(first_p.keys())}")
            else:
                logger.info("[moment-debug] NO persona_mgr found on context")
            # ========================

            # 数据库
            self.db = MomentDatabase(self.db_path)
            await self.db.connect()
            logger.info(f"[moment] 数据库就绪: {self.db_path}")

            # 素材收集器
            self.material = MaterialCollector(self.context, self.config)

            # 发帖引擎
            self.post_engine = PostEngine(self.context, self.config)

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

    # ------------------------------------------------------------------
    # AI 触发接口（供 server 调用）
    # ------------------------------------------------------------------

    def _trigger_ai_comment(self, post_id: int, content: str):
        """调度 AI 对用户帖子的延迟评论。"""
        if not self.config.get("ai_comment_enabled", True):
            return
        delay = int(self.config.get("ai_comment_delay_minutes", 3)) * 60
        asyncio.get_event_loop().call_later(
            delay,
            lambda: asyncio.create_task(self._do_ai_comment(post_id, content)),
        )

    def _trigger_ai_reply(self, post_id: int, post_content: str, comment: dict, parent_id=None):
        """调度 AI 对用户评论的延迟回复。"""
        if not self.config.get("ai_comment_enabled", True):
            return

        delay = int(self.config.get("ai_reply_delay_minutes", 2)) * 60

        async def _check_and_reply():
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

        asyncio.get_event_loop().call_later(
            delay,
            lambda: asyncio.create_task(_check_and_reply()),
        )

    # ------------------------------------------------------------------
    # AI 发帖核心逻辑
    # ------------------------------------------------------------------

    async def _do_ai_post(self):
        """执行一次 AI 发帖。"""
        if not self.db or not self.material or not self.post_engine:
            return

        # 1. 收集素材
        materials = await self.material.collect(db_instance=self.db)

        # 2. 生成动态
        result = await self.post_engine.generate_post(materials)
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

    # ------------------------------------------------------------------
    # 通知推送
    # ------------------------------------------------------------------

    async def _send_notification(self, message: str, post_id: int = None, ntype: str = "new_post"):
        """存储通知到数据库（仅用于网页通知面板，不推送到聊天）。"""
        # (旧版本用来控制是否推送到当前群聊，现已屏蔽消息推送逻辑)
        if self.db:
            await self.db.create_notification(ntype, message, ref_post_id=post_id)
